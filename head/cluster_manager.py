import os
import psutil # 호스트의 물리 메모리 사용량을 얻기 위해 사용
import subprocess #docker sdk를 import하기 위해 
import docker #docker SDK
import threading
import time
import random

# 공유 상태 및 대시보드 모듈 임포트
import head.state as state # state = 시스템의 전역 변수를 가지고 있음
import head.dashboard.server as dashboard

def get_gpu_free_memory():
    """
    [Global Host Resource Manager]
    nvidia-smi 명령어를 호출하여 호스트 GPU의 가용 VRAM 용량(MiB)을 획득합니다.

    Returns:
        int: 가용 GPU VRAM 용량 (MiB 단위, GPU 드라이버 미인식 시 -1 반환).
    """
    try:
        # nvidia-smi --query-gpu=memory.free --format=csv,nounits,noheader
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,nounits,noheader"],
            capture_output=True, text=True, check=True
        )
        free_vram = int(result.stdout.strip())
        return free_vram
    # nvidia-smi 명령어를 사용해 GPU의 남은 메모리를 가져옴
    except Exception:
        # GPU 드라이버 미인식 시 모니터링 불가 상태로 간주 (-1 반환)
        return -1

# SCALE-IN을 하는 데 핵심 -> 재시작시 죽지 않은 컨테이너 제거
def cleanup_zombie_containers():
    """
    [Docker SDK Startup Clean]
    Head 노드 기동 시, 호스트에 남겨진 이전 라이프사이클의 
    동적 Spot 워커 컨테이너(babyray-worker-2-*, babyray-worker-3-*)들을 일괄 정리하여 자원을 회수합니다.
    """
    # Docker Client가 연결이 안되어 있다면 실행하지 않음
    if state.DOCKER_CLIENT is None:
        dashboard.log_event("[Docker SDK] 도커 데몬 미연결로 잔존 컨테이너 정리 작업을 스킵합니다.")
        return
        
    dashboard.log_event("[Docker SDK] 기존 잔존 동적 컨테이너 청소 작업을 시작합니다...")
    try:
        # Docker SDK에서 list()로 모든 컨테이너 목록을 리스트 객체
        containers = state.DOCKER_CLIENT.containers.list(all=True)
        targets = []
        for container in containers:
            c_name = container.name
            # worker-1: on-demand / worker-2: spot-a / worker-3: spot-b
            if c_name.startswith("babyray-worker-2-") or c_name.startswith("babyray-worker-3-"):
                targets.append(container)
                
        if not targets:
            dashboard.log_event("[Docker SDK] 정리할 잔존 컨테이너가 없습니다.")
            return
            
        import threading
        threads = []
        
        def remove_container(c):
            try:
                dashboard.log_event(f"[Docker SDK] 잔존 컨테이너 강제 제거 시작: {c.name}")
                c.remove(force=True)
                dashboard.log_event(f"[Docker SDK] 잔존 컨테이너 강제 제거 성공: {c.name}")
            except Exception as e:
                dashboard.log_event(f"[Docker SDK 에러] 컨테이너 {c.name} 제거 실패: {e}")
                
        for container in targets:
            t = threading.Thread(target=remove_container, args=(container,))
            t.start()
            threads.append(t)
            
        # 모든 정리 작업이 4초 내에 끝나지 않으면 메인 스레드 진행 (타임아웃 방지)
        for t in threads:
            t.join(timeout=4.0)
            
        dashboard.log_event(f"[Docker SDK] 총 {len(targets)}개의 잔존 컨테이너에 대해 강제 정리 명령을 병렬 전송했습니다.")
        
    except Exception as e:
        dashboard.log_event(f"[Docker SDK 에러] 잔존 컨테이너 조회 중 에러 발생: {e}")


# Scale - out 하기 전에 자원이 충분한지 확인하는 함수 (Safety Guard)
def is_host_resource_sufficient():
    """
    [Global Host Resource Manager]
    호스트 시스템의 실시간 물리 메모리 사용률(%)의 임계 상한선(85%)을 검증하여 과부하 방지 안전 여부를 판정합니다.

    Returns:
        bool: 호스트 물리 메모리 사용률이 85.0% 이하인 경우 True, 초과한 경우 False.
    """
    if os.environ.get("BYPASS_RESOURCE_GUARD", "0") == "1":
        return True

    # [Safety Guard 임계값 85.0% 상한선 선정 이유]
    # RAM 전체 리소스 32GB 기준 85%를 소모할 시 가용 램 여유는 4.8GB가 됩니다.
    # 사용자의 4GB 이상 안전 여유 공간 상한선 제약을 준수하고 버벅임 및 VM 다운을 방지하기 위해 85%로 고정했습니다.
    try:
        mem = psutil.virtual_memory()
        usage_percent = mem.percent
        
        # WSL2 환경 검사 보정 (WSL2에서 메모리 한계를 잡은 경우 free 결과 보조 참고)
        if os.name != 'nt':
            try:
                result = subprocess.run(["free", "-b"], capture_output=True, text=True, timeout=2)
                lines = result.stdout.strip().splitlines()
                for line in lines:
                    if line.startswith("Mem:"):
                        parts = line.split()
                        if len(parts) >= 7:
                            total = int(parts[1])
                            available = int(parts[6])
                            wsl_usage = ((total - available) / total) * 100.0
                            usage_percent = max(usage_percent, wsl_usage)
            except Exception:
                pass

        if usage_percent > 85.0:
            print(f"[Global Resource Guard] 호스트 물리 메모리 사용률 상한선 초과 경고: {usage_percent:.1f}% > 85.0% (Safety Guard)")
            return False
        return True
    except Exception as e:
        print(f"[Global Resource Guard] 자원 점검 중 예외 발생: {e}")
        return True

# spot_a,b 등의 worker를 하나 늘리는 scale - out
def scale_out_worker(node_type):
    """
    [Docker SDK Container Run API]
    Docker SDK를 사용하여 node_type에 기반한 신규 Spot 워커 노드를 동적으로 가동합니다.
    - cGroup 격리 제한(cpus, memory limit), 네트워크 자동 매핑 및 볼륨 마운트가 이식됩니다.

    Args:
        node_type (str): 가동할 노드 종류 ("spot_a" 등).

    Returns:
        bool: 컨테이너 생성 및 기동 성공 시 True, 실패 혹은 자원 가드 작동 시 False.
    """
    if state.DOCKER_CLIENT is None:
        print("[Docker SDK] 도커 데몬과 연결되어 있지 않아 동적 스케일아웃을 스킵합니다.")
        return False

    # 0. 공급 부족(OutOfCapacity / Provisioning 거절) 30% 확률 모사
    # 스팟 인스턴스 공급 부족 장애(OutOfCapacity) 현상을 소프트웨어적으로 시뮬레이션 (spot_a 및 spot_b 적용)
    if node_type in ["spot_a", "spot_b"] and random.random() < 0.3:
        dashboard.log_event(f"[Docker SDK] OutOfCapacity 감지: Spot-{node_type[-1].upper()} 자원 공급 부족으로 인해 노드 증설이 거절되었습니다.")
        return False

    try:
        spec = {
            "spot_a": {
                "nano_cpus": 1000000000, # 1.0 Core
                "mem_limit": "1024m",
                "port": 50060
            },
            "spot_b": {
                "nano_cpus": 500000000,  # 0.5 Core
                "mem_limit": "512m",
                "port": 50070            # Spot-B 전용 시작 포트 대역
            }
        }

        if node_type not in spec:
            print(f"[Docker SDK] 알 수 없는 노드 유형: {node_type}")
            return False

        node_spec = spec[node_type]

        # 1. 호스트 자원 가드 -> 아까 만들었던 함수 safett guard
        if not is_host_resource_sufficient():
            print("[Docker SDK] 호스트 물리 메모리 부족으로 스케일아웃 기동을 안전하게 거부합니다.")
            return False

        free_vram = get_gpu_free_memory()
        if free_vram != -1 and free_vram < 500:
            print(f"[Global Resource Guard] 가용 GPU VRAM 부족 ({free_vram} MiB < 500 MiB). 스케일아웃을 보류합니다.")
            return False

        # 2. 네트워크 자동 감지 - gRPC 통신을 위해서 head/worker가 같은 네트워크에 묶여야 함
        network_name = "babyray-net"
        try:
            head_container = state.DOCKER_CLIENT.containers.get("babyray-head")
            networks = head_container.attrs.get("NetworkSettings", {}).get("Networks", {})
            if networks:
                network_name = list(networks.keys())[0]
                print(f"[Docker SDK] 자동 감지된 클러스터 네트워크: {network_name}")
        except Exception as e:
            print(f"[Docker SDK] 네트워크 자동 감지 실패, 기본값 '{network_name}' 사용: {e}")

        # 3. 중복 포트 회피
        with state.registry_lock:
            existing_ports = [info["port"] for info in state.worker_registry.values()]

        candidate_port = node_spec["port"]
        while candidate_port in existing_ports:
            candidate_port += 1

        # 4. 고유 ID 및 컨테이너명 생성 (Index Recycling 메커니즘 적용)
        # - 정상 흐름: 스케일 인 시 컨테이너가 삭제되면 빈자리(Gap) 번호를 1번부터 찾아 재활용합니다.
        # - 예외 방어: GCS와 Docker 호스트 실존 컨테이너명을 교차 확인하여 이름 중복 충돌을 방지합니다.
        base_id = "worker-2" if node_type == "spot_a" else "worker-3"
        
        with state.registry_lock:
            # GCS 레지스트리에 등록된 순차 인덱스 추출
            existing_indices = []
            for wid in state.worker_registry.keys():
                name_part = wid.split("@")[0] if "@" in wid else wid
                if name_part.startswith(base_id + "-"):
                    try:
                        idx = int(name_part.split("-")[-1])
                        existing_indices.append(idx)
                    except ValueError:
                        pass
        
        # Docker 호스트에 실제 존재하는(종료됐지만 미삭제 포함) 컨테이너 인덱스도 추출
        try:
            all_containers = state.DOCKER_CLIENT.containers.list(all=True)
            for container in all_containers:
                c_name = container.name  # e.g. "babyray-worker-2-1"
                prefix = f"babyray-{base_id}-"
                if c_name.startswith(prefix):
                    try:
                        idx = int(c_name[len(prefix):])
                        if idx not in existing_indices:
                            existing_indices.append(idx)
                    except ValueError:
                        pass
        except Exception as e:
            print(f"[Docker SDK 경고] 기존 컨테이너 인덱스 조회 실패: {e}")

        # 1부터 시작하여 비어있는 가장 작은 번호(인덱스) 탐색 (Index Recycling)
        candidate_index = 1
        while candidate_index in existing_indices:
            candidate_index += 1

        worker_id = f"{base_id}-{candidate_index}"
        container_name = f"babyray-{worker_id}"

        # 5. 실행 커맨드
        cmd = [
            "python", "-m", "worker.worker",
            "--id", worker_id,
            "--type", node_type,
            "--port", str(candidate_port),
            "--head-host", "babyray-head",
            "--head-port", "50051"
        ]

        # GPU 요청 객체 빌드
        device_requests = []
        try:
            device_requests = [
                docker.types.DeviceRequest(count=-1, capabilities=[['gpu']])
            ]
        except Exception:
            device_requests = []

        # 6. 컨테이너 동적 생성 및 실행 (분리된 Worker 이미지 사용)
        state.DOCKER_CLIENT.containers.run(
            image="babyray-worker-image:latest",
            name=container_name,
            command=cmd,
            detach=True,
            network=network_name,
            nano_cpus=node_spec["nano_cpus"],
            mem_limit=node_spec["mem_limit"],
            device_requests=device_requests,
            environment={
                "NODE_TYPE": node_type,
                "HEAD_HOST": "babyray-head",
                "HEAD_PORT": "50051",
                "PYTHONUNBUFFERED": "1"
            },
            volumes={
                "babyray-data": {"bind": "/app/data", "mode": "rw"}
            }
        )

        dashboard.log_event(f"[Docker SDK] 신규 Spot 컨테이너 가동 완료: ID='{worker_id}' | Name='{container_name}' | Port={candidate_port}")
        return True

    except Exception as e:
        dashboard.log_event(f"[Docker SDK 에러] 동적 스케일아웃 실행 실패: {e}")
        return False

# FIFO, LIFO가 아닌 메모리 사용량이 높은 순으로 정렬해 끄도록 설계 -> 생존성을 극대화
def scale_in_specific_worker(node_type):
    """
    [Docker SDK Container Stop/Remove API]
    GCS worker_registry에서 지정된 node_type 중 IDLE 상태인 워커를 선별하여 안전하게 종료 및 삭제합니다.
    - OOM 이상 징후(메모리 사용률 90% 이상)가 감지된 노드가 있을 경우 우선적으로 회수 후보로 삼습니다.

    Args:
        node_type (str): 감축 회수할 대상 워커 종류 ("spot_a" 등).

    Returns:
        bool: 회수 성공 시 True, IDLE 워커 부재 혹은 예외 발생 시 False.
    """
    if state.DOCKER_CLIENT is None:
        print("[Docker SDK] 도커 데몬 미연결로 스케일인을 스킵합니다.")
        return False

    try:
        target_worker_id = None
        with state.registry_lock:
            # IDLE 상태인 해당 타입의 워커들을 필터링
            idle_workers = [
                (wid, info) for wid, info in state.worker_registry.items()
                if info["node_type"] == node_type and info["status"] == "IDLE"
            ]
            if idle_workers:
                # 메모리 사용률이 높은 순(오버로드 상태)으로 정렬하여 1순위로 회수
                # 메모리(mem) 소모율이 가장 높은 녀석이 가장 오도록 정렬
                idle_workers.sort(key=lambda x: x[1].get("mem", 0.0), reverse=True)
                target_worker_id = idle_workers[0][0]

        if target_worker_id is None:
            dashboard.log_event(f"[Docker SDK] 감축 경고: 회수 가능한 IDLE 상태의 {node_type} 워커가 존재하지 않습니다.")
            return False

        # worker-id format: worker-2-1 -> container name: babyray-worker-2-1
        container_ref = f"babyray-{target_worker_id}"

        # 1. Docker SDK를 통한 컨테이너 중지 및 제거
        try:
            container = state.DOCKER_CLIENT.containers.get(container_ref)
            # 도커 SDK로 타겟 컨테이너 핸들러를 가져와 5초 유예 정지 -> 제거
            dashboard.log_event(f"[Docker SDK] IDLE 컨테이너 회수 시작: {target_worker_id} (컨테이너 ID: {container_ref})")
            container.stop(timeout=5)
            container.remove()
            dashboard.log_event(f"[Docker SDK] IDLE 컨테이너 회수 성공: {target_worker_id}")
        except Exception as e:
            dashboard.log_event(f"[Docker SDK 경고] 컨테이너 직접 조작 실패 ({e}), GCS 레지스트리만 소거 처리 진행.")

        # 2. GCS 레지스트리에서 제거 - Docker 컨테이너 제거 후 dict(인메모리 캐시)에서 제거
        with state.registry_lock:
            if target_worker_id in state.worker_registry:
                del state.worker_registry[target_worker_id]

        return True

    except Exception as e:
        dashboard.log_event(f"[Docker SDK 에러] 스케일인 수행 실패: {e}")
        return False


def get_container_metrics(container_name):
    """
    [Docker SDK Resource Monitor API]
    Docker SDK 객체를 통해 해당 워커 컨테이너의 실시간 메모리/CPU 사용률 메트릭을 도출합니다.

    Args:
        container_name (str): Docker 컨테이너명.

    Returns:
        tuple: (cpu_percent (float), mem_percent (float)) 형식의 튜플 (실패 시 0.0, 0.0 반환).
    """
    if state.DOCKER_CLIENT is None:
        return 0.0, 0.0
        
    try:
        container = state.DOCKER_CLIENT.containers.get(container_name)
        # 호출한 딱 그 한 순간의 도커 시스템 메트릭 원시(Raw) 정보 수집
        stats = container.stats(stream=False)
        
        # CPU 계산
        # 도커 컨테이너의 CPU 퍼센티지를 구하는 리눅스 표준 공식
        cpu_delta = stats['cpu_stats']['cpu_usage']['total_usage'] - stats['precpu_stats']['cpu_usage']['total_usage']
        system_delta = stats['cpu_stats']['system_cpu_usage'] - stats['precpu_stats']['system_cpu_usage']
        
        # cGroup CPU cores 수 계산 - 멀티코어 환경을 반영하기 위해 활성화된 CPU 코어 수
        online_cpus = stats['cpu_stats'].get('online_cpus', 1)
        
        cpu_percent = 0.0
        if system_delta > 0.0 and cpu_delta > 0.0:
            cpu_percent = (cpu_delta / system_delta) * 100.0 * online_cpus

        # Memory 계산
        mem_usage = stats['memory_stats'].get('usage', 0.0)
        mem_limit = stats['memory_stats'].get('limit', 1.0)
        mem_percent = (mem_usage / mem_limit) * 100.0
        
        return round(cpu_percent, 1), round(mem_percent, 1)

    except Exception:
        return 0.0, 0.0

def start_spot_eviction_loop():
    """
    [Spot Eviction Daemon Thread]
    백그라운드에서 실시간 요금제 위험도 P_spot 수준에 맞춰 주기적으로 
    기동 중인 Spot 워커 컨테이너를 강제 정지 및 제거하여 Eviction 장애를 유도합니다.
    """
    def eviction_loop():
        dashboard.log_event("=== [Eviction Daemon] 실시간 스팟 강제 회수 모니터링 데몬 기동 ===")
        while True:
            time.sleep(10.0)  # 10초 주기로 회수 여부 심사
            
            # 아키텍처 개선: 메모리 가드 초과로 증설이 막힌 경우, 기존 노드 보존을 위해 강제 회수 동결(Freeze)
            if not is_host_resource_sufficient():
                dashboard.log_event("[Eviction Daemon] 호스트 메모리 부족 감지 -> 기존 Spot 워커 보호를 위해 강제 회수를 일시 중단(Freeze)합니다.")
                continue
                
            p_spot = 1 if (time.time() % 30.0) < 10.0 else 0
            eviction_prob = 0.15 if p_spot == 1 else 0.05
            
            spot_workers = []
            with state.registry_lock:
                for wid, info in state.worker_registry.items():
                    if info["node_type"] in ["spot_a", "spot_b"]:
                        spot_workers.append(wid)
            
            if not spot_workers:
                continue
                
            for wid in spot_workers:
                if random.random() < eviction_prob:
                    container_ref = f"babyray-{wid}"
                    dashboard.log_event(f"[Eviction Daemon] !!! 스팟 강제 회수(Eviction) 발생 !!! -> 대상: {wid} (확률: {eviction_prob*100:.1f}%)")
                    
                    # 1. GCS 레지스트리에서 즉시 격리 삭제
                    with state.registry_lock:
                        if wid in state.worker_registry:
                            del state.worker_registry[wid]
                            
                    # 2. 물리 컨테이너 강제 소거 (stop & remove)
                    try:
                        if state.DOCKER_CLIENT is not None:
                            container = state.DOCKER_CLIENT.containers.get(container_ref)
                            container.stop(timeout=1)
                            container.remove(force=True)
                            dashboard.log_event(f"[Eviction Daemon] 컨테이너 강제 회수 완료: {container_ref}")
                    except Exception as e:
                        dashboard.log_event(f"[Eviction Daemon 오류] 컨테이너 소거 중 실패: {e}")

    threading.Thread(target=eviction_loop, daemon=True).start()
