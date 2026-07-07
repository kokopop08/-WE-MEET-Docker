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
from head.resource_guard import get_gpu_free_memory, is_host_resource_sufficient
from common.failure_simulator import FailureSimulator # 장애 시뮬레이션 판단 로직 중앙화 모듈

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
            # 끝에 대시(-)가 없는 고정 이름 형태("babyray-worker-2")도 매칭되도록 접두사 조건 보완
            if c_name.startswith("babyray-worker-2") or c_name.startswith("babyray-worker-3"):
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
            
        # 모든 정리 작업이 완전히 끝나도록 병렬 조인 타임아웃을 넉넉히(10초) 설정
        import time
        start_time = time.time()
        for t in threads:
            elapsed = time.time() - start_time
            remaining = max(0.1, 10.0 - elapsed)
            t.join(timeout=remaining)
            
        dashboard.log_event(f"[Docker SDK] 총 {len(targets)}개의 잔존 컨테이너에 대해 강제 정리 명령을 병렬 전송했습니다.")
        
    except Exception as e:
        dashboard.log_event(f"[Docker SDK 에러] 잔존 컨테이너 조회 중 에러 발생: {e}")


def _load_node_config(node_type):
    """cost_model.yaml에서 노드 스펙 및 GPU 스케일 팩터를 단 한 번만 로드합니다."""
    # Default fallback values
    cpu_limit = 1.0 if node_type == "spot_a" else 0.5
    mem_limit_mb = 1024 if node_type == "spot_a" else 512
    gpu_scale = 0.6 if node_type == "spot_a" else 0.3
    
    cost_model_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../common/cost_model.yaml'))
    if os.path.exists(cost_model_path):
        try:
            import yaml
            with open(cost_model_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
                nodes = config.get("nodes", {})
                if node_type in nodes:
                    spec = nodes[node_type]
                    cpu_limit = spec.get("cpu_limit", cpu_limit)
                    mem_limit_mb = spec.get("memory_limit_mb", mem_limit_mb)
                    gpu_scale = spec.get("gpu_scale_factor", gpu_scale)
        except Exception as e:
            print(f"[Docker SDK] cost_model.yaml 로드 오류: {e}")
            
    return cpu_limit, mem_limit_mb, gpu_scale

def _find_available_port(base_port):
    """GCS 레지스트리를 검사하여 포트 충돌이 없는 가용 포트를 반환합니다."""
    with state.registry_lock:
        existing_ports = [info["port"] for info in state.worker_registry.values()]
        
    candidate_port = base_port
    while candidate_port in existing_ports:
        candidate_port += 1
    return candidate_port

def _find_next_worker_index(base_id):
    """GCS 레지스트리와 Docker 호스트의 실존 컨테이너를 스캔하여 빈 인덱스를 재활용(Recycling)합니다."""
    existing_indices = []
    
    # 1. GCS 레지스트리 스캔
    with state.registry_lock:
        for wid in state.worker_registry.keys():
            name_part = wid.split("@")[0] if "@" in wid else wid
            if name_part.startswith(base_id + "-"):
                try:
                    idx = int(name_part.split("-")[-1])
                    existing_indices.append(idx)
                except ValueError:
                    pass
                    
    # 2. Docker 호스트 스캔 (GCS 미등록 상태 컨테이너 방어)
    try:
        all_containers = state.DOCKER_CLIENT.containers.list(all=True)
        for container in all_containers:
            c_name = container.name
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
        
    candidate_index = 1
    while candidate_index in existing_indices:
        candidate_index += 1
    return candidate_index

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
    if FailureSimulator.check_out_of_capacity(node_type):
        dashboard.log_event(f"[Docker SDK] OutOfCapacity 감지: Spot-{node_type[-1].upper()} 자원 공급 부족으로 인해 노드 증설이 거절되었습니다.")
        return False

    try:
        # 1. 설정 및 자원 규격 단 1회 로드
        cpu_limit, mem_limit_mb, gpu_scale = _load_node_config(node_type)

        # 2. 호스트 자원 가드 검사
        if not is_host_resource_sufficient():
            print("[Docker SDK] 호스트 물리 메모리 부족으로 스케일아웃 기동을 안전하게 거부합니다.")
            return False

        free_vram = get_gpu_free_memory()
        if free_vram != -1 and free_vram < 500:
            print(f"[Global Resource Guard] 가용 GPU VRAM 부족 ({free_vram} MiB < 500 MiB). 스케일아웃을 보류합니다.")
            return False

        # 3. 네트워크 자동 감지
        network_name = "babyray-net"
        try:
            head_container = state.DOCKER_CLIENT.containers.get("babyray-head")
            networks = head_container.attrs.get("NetworkSettings", {}).get("Networks", {})
            if networks:
                network_name = list(networks.keys())[0]
                print(f"[Docker SDK] 자동 감지된 클러스터 네트워크: {network_name}")
        except Exception as e:
            print(f"[Docker SDK] 네트워크 자동 감지 실패, 기본값 '{network_name}' 사용: {e}")

        # 4. 중복 포트 회피 및 인덱스 재활용 (Index Recycling)
        base_port = 50060 if node_type == "spot_a" else 50070
        candidate_port = _find_available_port(base_port)
        
        base_id = "worker-2" if node_type == "spot_a" else "worker-3"
        candidate_index = _find_next_worker_index(base_id)

        worker_id = f"{base_id}-{candidate_index}"
        container_name = f"babyray-{worker_id}"

        # 5. 실행 커맨드 구성
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
            pass

        mps_percentage = str(int(gpu_scale * 100))

        # 6. 컨테이너 동적 실행
        state.DOCKER_CLIENT.containers.run(
            image="babyray-worker-image:latest",
            name=container_name,
            command=cmd,
            detach=True,
            network=network_name,
            cpu_period=100000,
            cpu_quota=int(cpu_limit * 100000),
            mem_limit=f"{int(mem_limit_mb)}m",
            device_requests=device_requests,
            environment={
                "NODE_TYPE": node_type,
                "HEAD_HOST": "babyray-head",
                "HEAD_PORT": "50051",
                "PYTHONUNBUFFERED": "1",
                "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": mps_percentage
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
                
            # cost_model.yaml에서 preemption_probability 동적 로드
            preemption_probs = {"spot_a": 0.5, "spot_b": 0.1}
            cost_model_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../common/cost_model.yaml'))
            if os.path.exists(cost_model_path):
                try:
                    import yaml
                    with open(cost_model_path, 'r', encoding='utf-8') as f:
                        config = yaml.safe_load(f)
                        nodes = config.get("nodes", {})
                        for k, v in nodes.items():
                            if "preemption_probability" in v:
                                preemption_probs[k] = v["preemption_probability"]
                except Exception:
                    pass

            p_spot = 1 if (time.time() % 30.0) < 10.0 else 0
            
            spot_workers = []
            with state.registry_lock:
                for wid, info in state.worker_registry.items():
                    if info["node_type"] in ["spot_a", "spot_b"]:
                        spot_workers.append((wid, info["node_type"]))
            
            if not spot_workers:
                continue
                
            for wid, n_type in spot_workers:
                eviction_prob = FailureSimulator.EVICTION_BASE_PROB.get(n_type, 0.3) if p_spot == 1 else (FailureSimulator.EVICTION_BASE_PROB.get(n_type, 0.3) * FailureSimulator.EVICTION_IDLE_FACTOR)
                if FailureSimulator.check_eviction(n_type, p_spot, preemption_probs):
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

def resize_worker_resources(worker_id, cpu_limit, mem_limit_mb):
    """
    호스트 Docker SDK를 사용하여 실행 중인 워커 컨테이너의 cGroup 리소스 한도를 실시간으로 업데이트합니다.
    """
    if state.DOCKER_CLIENT is None:
        return False, "Docker client unavailable"
    
    container_name = f"babyray-{worker_id}"
    if worker_id == "worker-1":
        container_name = "babyray-on-demand"
    elif worker_id == "worker-3":
        container_name = "babyray-worker-3"
        
    try:
        container = state.DOCKER_CLIENT.containers.get(container_name)
        mem_limit_bytes = int(mem_limit_mb * 1024 * 1024)
        cpu_period = 100000
        cpu_quota = int(cpu_limit * 100000)
        
        # Docker SDK container update 호출
        container.update(
            cpu_period=cpu_period,
            cpu_quota=cpu_quota,
            mem_limit=mem_limit_bytes,
            memswap_limit=mem_limit_bytes
        )
        print(f"[Docker SDK] cGroup 자원 크기 업데이트 성공 -> {container_name} | CPU: {cpu_limit} Cores (quota: {cpu_quota}), Mem: {mem_limit_mb} MB")
        return True, "Success"
    except Exception as e:
        print(f"[Docker SDK 에러] cGroup 자원 업데이트 실패 -> {container_name}: {e}")
        return False, str(e)
