# WE-MEET 기능별 패키지 재배치 패치 노트

- **작성일**: 2026-07-09
- **브랜치**: `refactor/feature-packages`
- **범위**: 위치 기반(head/worker/common) → 기능 기반 우산 패키지 `wemeet/` 전면 재배치 · 혼재 모듈 분리 · 경로/설정 결합 정리 · docstring/pdoc 문서화 기반 마련
- **한 줄 요약**: `최상위 폴더명=import 루트=python -m 커맨드` 3중 결합을 우산 패키지 `wemeet/` 로 정리하고, 기능 경계가 흐린 모듈을 분리했으며, 동작·수치는 보존했다.

---

## 1. 배경/어려움

파일이 늘며 위치 기반 구조가 수정 위험을 키웠다. 조사에서 이동 시 함께 깨지는 결합을 전수 확인:
`docker-compose`/`cluster_manager`의 `python -m` 문자열, `compile_proto` 패치 문자열, `../head/q_learning`
볼륨, `sys.path.append` 7곳(깊이 상이), CWD 상대 `data/` 10곳, `dashboard.log_event` 전역 남용,
`gpu_simulator`(실행기+시뮬레이터)·`head.py`(서비서+부팅)·`task_executor`(스케줄링+학습) 혼재.

## 2. 결정 (사용자 승인)

① 우산 패키지 `wemeet/` (설치형 패키지화, sys.path 훅 제거) ② 혼재 모듈까지 분리(정석)
③ Google 스타일 docstring + pdoc. **불변식: 코드 이동·분할만, 동작/수치 보존.** `data/`·`q_table.json` 미이동.

## 3. 변경 내역

### 3-1. 레이아웃 (`wemeet/` 우산 패키지)
`config/`(env_config·settings·**paths(신규)**·yaml) · `transport/`(proto·head·head_service·worker·worker_service)
· `cluster/`(manager·resource_guard·gcs_state) · `scheduling/`(static·dynamic·qlearning_step·daemon·executor)
· `learning/`(agent·pretrain·reward_policy·state_features) · `simulation/`(failure_simulator·fast_sim·**benchmark(신규)**)
· `workload/`(runner·**dummy_load(신규)**·models) · `observability/`(dashboard·**logging(신규)**·reporting_*·web).

### 3-2. 경로/import 정리
- 전 내부 import를 매핑표대로 치환(22파일). `sys.path.append` 7곳 제거. `compile_proto` 출력경로·패치문자열을 `wemeet.transport.proto` 로. worker 의 bare `import gpu_simulator`(지연 포함) → `wemeet.workload.runner`. fast_sim 문자열 import → `wemeet.observability.reporting_visualize`.
- **신규 `config/paths.py`**: `__file__` 기준 `PROJECT_ROOT`/`DATA_DIR` 단일 해석. `gcs_state.STATE_FILE`↔`head.py` 백업경로 불일치 제거, benchmark/online-training CSV·체크포인트 glob을 여기로 통일. **CWD·파일깊이 무관 동일 절대경로**(검증 완료).
- **버그 수정**: `pretrain`·`executor` 가 이동 전 `../../common/cost_model.yaml` 을 가리켜 조용히 0.710 폴백으로 떨어지던 것을 `env_config.COST_MODEL_PATH` 로 교정(값 보존).

### 3-3. 기능 경계 분리
- **`observability/logging.py` 추출**: `log_event`/`event_logs`/`event_lock` 소유. 스케줄러·클러스터가 dashboard 대신 logging 에 의존(역결합 제거). dashboard 는 재-export로 하위호환.
- **dashboard**: 미사용 레거시 `DASHBOARD_HTML`(~850줄) 제거, `web/` 정적 서빙만.
- **`cluster/manager.py`**: 수동 yaml 로드·하드코딩 `{spot_a:0.5,...}` 제거 → `env_config.nodes()/preemption_probs()`.
- **`simulation`**: fast_sim(FastSimulator 엔진) / benchmark(반복·집계·리포트·`-m` 진입점) 분리.
- **`workload`**: runner(실행기) / dummy_load(더미 부하) 분리. `oom_simulated` 는 worker 와 프로세스 공유이므로 runner 잔류.
- **`transport`**: head/head_service, worker/worker_service (서비서 vs 부팅) 분리.

### 3-4. 배포/빌드
`docker-compose` 커맨드 2줄 + 볼륨(`../wemeet/learning`), `cluster/manager` 컨테이너 spawn 문자열,
`Dockerfile.head` CMD → 모두 `wemeet.transport.*`. `pyproject.toml`: setuptools packages.find(`wemeet*`)·
package-data(yaml/web/proto)·optional deps(worker/viz/docs/docker)·scripts·requires-python 3.10. README 갱신.

### 3-5. 문서화
`docs/DOCSTRING_STYLE.md`(Google 규약), `docs/build.(sh|ps1)`(pdoc). 배너 주석(`# ===`) → 모듈 docstring 12개 변환(pdoc 인식용).

## 4. 검증 (오프라인)
- 값 보존 회귀(요금·회수확률·danger·버킷) 통과. Q-테이블 416상태 정상 로드(재학습 불필요).
- 전 모듈 offline import / grpc·torch 모듈 byte-compile 통과. 잔존 old-import 0.
- `paths` CWD 독립 동일경로 확인. pretrain 오프라인 sim 스모크·`-m wemeet.simulation.benchmark` 리포트 생성 OK.

## 5. 남은 일 (follow-up)
- **`scheduling/executor.py` → executor/mapreduce 분리 미실시**: 800줄 gRPC 결합 허브 + map-reduce 로직이 dispatch 와 깊게 얽혀 오프라인 검증 불가·고위험. 별도 진행 권장.
- **docstring 전면 Google화**: 모듈 docstring 12개 변환 완료. 함수/클래스 단위 전면화는 `DOCSTRING_STYLE.md` 기준 점진 진행.
- **docker 실검증**: 이 환경(torch/docker/grpc 부재) 미실행 → 사용자 환경에서 `docker-compose up --build`(워커 spawn `-m wemeet.transport.worker` 포함) 확인 필요.
- **pdoc 실행**: `pip install -e ".[worker,viz,docs,docker]"` 후 `docs/build`. (pdoc 은 각 모듈을 import 하므로 grpc/torch/pandas 설치 환경 필요.)
