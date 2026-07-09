"""Worker 노드 gRPC 서비서 (wemeet/transport/worker_service.py).

Head 로부터 작업 할당(AssignTask)·상태 조회(GetTaskStatus)·자원 조정(ResizeResources)
요청을 처리한다. 부팅(하트비트 루프·serve)은 [wemeet.transport.worker] 가 담당.
"""

import threading
from wemeet.transport.proto import babyray_pb2, babyray_pb2_grpc
from wemeet.workload.runner import PyTorchTaskRunner

class BabyRayWorkerServicer(babyray_pb2_grpc.BabyRayServiceServicer):
    """
    Baby Ray Worker Node의 gRPC 서비스 처리를 전담하는 서비서 클래스입니다.
    Head로부터의 작업 할당 및 상태 확인 요청에 대응합니다.
    """
    def __init__(self, worker_type):
        """
        BabyRayWorkerServicer 인스턴스를 초기화합니다.

        Args:
            worker_type (str): 워커 노드 유형 ("on_demand" / "spot_a").
        """
        self.worker_type = worker_type # worker의 종류
        self.current_task_id = None #작업 ID를 저장
        self.runner = None # 실제 수행할 객체 - PyTorchTaskRunner.py에 있는 클래스
        self.lock = threading.Lock() # 여러 스레드가 동시에 변수를 건드리지 못하게 막는 race condition 방지

    def AssignTask(self, request, context):
        """
        새로운 AI 연산 작업을 할당받아 백그라운드 스레드에서 비동기로 실행합니다.

        Args:
            request (TaskAssignment): 할당받을 작업 정보가 담긴 요청 메시지.
            context (grpc.ServicerContext): gRPC 서비스 컨텍스트.

        Returns:
            TaskResult: 작업 할당 결과 메시지 (RUNNING 또는 FAILED).
        """
        with self.lock: # 안에 있는 critical section에 mutual exclusion 보장
            # 1. 중복 검사: 이미 작업 ID가 있고, 그 작업이 'RUNNING' 상태라면
            if self.current_task_id is not None and self.runner.status == "RUNNING":
                print(f"[Worker gRPC] 작업 거절: {request.task_id} (이유: 다른 작업 실행 중)")
                # TaskResult Return
                return babyray_pb2.TaskResult(
                    task_id=request.task_id,
                    status="FAILED",
                    execution_time=0.0,
                    message="Another task is already running on this worker."
                )
            
            # 2. 신규 작업 생성: 새로운 작업 ID를 저장합니다.
            self.current_task_id = request.task_id
            # # 전달받은 옵션으로 딥러닝 구동기(Runner) 객체를 생성합니다. -> 함수에서 정의 받은 옵션으로 만들기
            self.runner = PyTorchTaskRunner(
                task_id=request.task_id,
                model_type=request.model_type,
                epochs=request.epochs,
                worker_type=self.worker_type,
                dataset_path=request.dataset_path
            )
            
            # 3. 백그라운드 실행: 새로운 스레드를 만들어 runner.run 함수를 백그라운드에서 실행시킵니다.
            # daemon=True 설정: 메인 프로그램(worker.py)이 종료되면 이 스레드도 자동으로 함께 종료됩니다.
            threading.Thread(target=self.runner.run, daemon=True).start()
            
            # 정상적으로 작업이 생성되었을 때 TaskResult Return
            print(f"[Worker gRPC] 작업 접수 승인: {request.task_id}")
            return babyray_pb2.TaskResult(
                task_id=request.task_id,
                status="RUNNING",
                execution_time=0.0,
                message="Task assigned successfully, executing in background."
            )

    # 작업 상태 조회
    def GetTaskStatus(self, request, context):
        """
        현재 수행 중인 AI 연산 작업의 상태 및 진행 로그를 반환합니다.

        Args:
            request (TaskStatusRequest): 확인할 작업 ID 정보.
            context (grpc.ServicerContext): gRPC 서비스 컨텍스트.

        Returns:
            TaskStatusResponse: 작업 진행 상태, 진행률 및 학습 로그 문자열.
        """
        with self.lock: #mutual exclusion 보장
            # 현재 실행 중인 작업이 없거나, 작업 ID가 요청과 다르면
            if self.runner is None or self.runner.task_id != request.task_id:
                return babyray_pb2.TaskStatusResponse(
                    status="NOT_FOUND",
                    progress=0.0,
                    logs="No such task found on this worker."
                )
            #제대로 된 요청이라면 현재 작업의 상태, 진행률, 로그를 모아서 반환
            return babyray_pb2.TaskStatusResponse(
                status=self.runner.status,
                progress=self.runner.progress,
                logs="\n".join(self.runner.logs)
            )
            
    # request - 클라이언트가 보낸 자원 크기
    def ResizeResources(self, request, context):
        """
        워커 노드의 cGroup 자원 격리 한도를 동적으로 조정합니다 (현재 스펙 정의용).

        Args:
            request (ResizeRequest): 변경할 CPU 코어 수 및 메모리 용량.
            context (grpc.ServicerContext): gRPC 서비스 컨텍스트.

        Returns:
            ResizeResponse: 조정 성공 여부 메시지.
        """
        print(f"[Worker gRPC] 자원 크기 조절 요청 수신: CPU={request.cpu_cores} Cores, Mem={request.memory_bytes} Bytes")
        # response - 헤드에게 보내는 답변
        return babyray_pb2.ResizeResponse(
            success=True,
            message=f"Configured worker cGroups: CPU={request.cpu_cores}, Mem={request.memory_bytes}"
        )


# --- 2. 하트비트 송신 클라이언트 루프 (Head로 전송) ---

