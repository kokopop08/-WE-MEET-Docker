# WE-MEET 실환경(Docker) 실험 지원 패치 노트

- **작성일**: 2026-07-09
- **브랜치**: `refactor/feature-packages`
- **범위**: 실환경 3대 스케줄러 비교 실험을 원커맨드로 자동화 (런타임 설정 env화 + 예산 소진 자동 종료 + 오케스트레이터)
- **한 줄 요약**: 스케줄러 모드를 코드 수정 없이 환경변수로 고르고, 예산 소진 시 head가 자동 종료되며, `python -m wemeet.experiment.run_real` 한 줄로 static→dynamic→q_learning 실측·시각화까지 수행한다.

## 배경/어려움
- 스케줄러 모드가 코드 상수(`gcs_state.SCHEDULER_MODE`)라 모드별 실험마다 파일 편집 필요.
- head가 예산 소진(스케줄러 자연 종료) 후에도 상주(`while True: sleep`)해 무인 실험 불가.
- `log_benchmark_metric`이 CSV에 append → 재실행 시 데이터 혼입.
- (기존 장점) 스케줄러는 예산 소진 시 `daemon.py`에서 break → 세 모드가 동일 지점에서 멈춰 공정 비교 성립.

## 사용자 결정
① 원커맨드 오케스트레이터(3모드 순차 + 자동 시각화) ② 예산 소진 시 head 자동 종료(env 게이팅).

## 변경 내역
1. **`gcs_state.py` 런타임 설정 env화**: `SCHEDULER_MODE`(env 우선, 기본 dynamic), `Q_LEARNING_TRAINING_MODE`(기본 True 유지), `INITIAL_VIRTUAL_BUDGET`(env float 오버라이드), 신규 `EXPERIMENT_AUTOEXIT`·`SCHEDULER_COMPLETED` 플래그. `_env_truthy` 헬퍼.
2. **자동 종료(이식성 안전)**: `daemon.py`가 예산 소진 break 직전 `SCHEDULER_COMPLETED=True` 설정 → `head.py serve()` 메인 루프를 1초 폴링으로 바꿔 `EXPERIMENT_AUTOEXIT` 시 기존 `handle_shutdown()`(gRPC 정지·.pt 정리·컨테이너 청소·exit)로 graceful 종료. `os.kill(SIGTERM)` 대신 플래그 폴링(Windows/컨테이너 이식성 + 기존 경로 재사용).
3. **`docker-compose.yml`**: head env 에 `SCHEDULER_MODE`/`Q_LEARNING_TRAINING_MODE`/`EXPERIMENT_AUTOEXIT`/`INITIAL_VIRTUAL_BUDGET` 인터폴레이션 추가.
4. **신규 `wemeet/experiment/run_real.py`**: 모드별 [CSV·gcs_state 리셋 → network 보장 → `compose up --abort-on-container-exit --exit-code-from head`(head 자동 종료로 전체 down) → `down`] 반복 후 분석/차트. `docker compose`/`docker-compose` 자동 감지, `--modes/--budget/--timeout/--no-build/--keep-training/--chart/--dry-run`.
5. **README**: "실환경 실험" 절 + 제어 env 문서화.

## 검증
- **오프라인**: env 파싱(mode/training/budget/autoexit/기본값) assert 통과, `--dry-run` 3모드 커맨드·env·리셋 경로 정확 출력, daemon/head/run_real byte-compile, 기존 offline benchmark 회귀 정상.
- **사용자 머신(docker 필요, 미검증)**: `docker network create babyray-net` 후 `python -m wemeet.experiment.run_real --budget 10 --chart`.

## 주의
- 자동 종료는 `EXPERIMENT_AUTOEXIT=1`일 때만(평상시 배포는 상주).
- q_learning 실환경 비교는 기본 추론 모드(사전학습 q_table.json 사용, 탐험 노이즈 배제); 온라인 학습은 `--keep-training`.
- 초기 예산 $1.5는 실 워커 상시 과금으로 매우 빨리 소진 → `--budget` 상향 권장.
