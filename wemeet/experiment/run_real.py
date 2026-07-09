"""실환경(Docker 클러스터) 3대 스케줄러 비교 실험 오케스트레이터.

각 스케줄러 모드(static/dynamic/q_learning)를 순차로:
  ① 이전 결과 CSV·GCS 상태 리셋 → ② babyray-net 네트워크 보장 →
  ③ docker-compose up (head 가 예산 소진 시 자동 종료 → --exit-code-from head 로 전체 down) →
  ④ down 정리
전 모드 종료 후 벤치마크 시각화/분석 리포트를 생성한다.

    docker network create babyray-net          # 최초 1회(오케스트레이터가 없으면 생성 시도)
    python -m wemeet.experiment.run_real --budget 10 --chart

실제 실행에는 호스트에 Docker(+compose)가 필요하다. `--dry-run` 으로 실행 없이 각 단계의
커맨드·환경변수·리셋 경로만 출력해 사전 점검할 수 있다.
"""

import argparse
import os
import shutil
import subprocess
import sys

# 프로젝트 루트 디렉토리를 path에 추가하여 wemeet 패키지 임포트 지원
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from wemeet.config import paths

DEFAULT_MODES = ["static", "dynamic", "q_learning"]
NETWORK_NAME = "babyray-net"


def _detect_compose():
    """사용 가능한 docker compose 실행기를 감지한다.

    Returns:
        list[str] | None: `["docker", "compose"]` 또는 `["docker-compose"]`. 둘 다 없으면 None.
    """
    if shutil.which("docker"):
        try:
            r = subprocess.run(["docker", "compose", "version"],
                               capture_output=True, timeout=15)
            if r.returncode == 0:
                return ["docker", "compose"]
        except Exception:
            pass
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return None


def _run(cmd, env=None, dry=False, timeout=None, check=False, cwd=paths.PROJECT_ROOT):
    """서브프로세스 실행(또는 dry-run 시 출력만)."""
    printable = " ".join(cmd)
    if dry:
        print(f"    [dry-run] $ {printable}")
        return 0
    print(f"    $ {printable}")
    try:
        r = subprocess.run(cmd, env=env, timeout=timeout, cwd=cwd)
        if check and r.returncode != 0:
            print(f"    ! 종료 코드 {r.returncode}")
        return r.returncode
    except subprocess.TimeoutExpired:
        print(f"    ! 타임아웃({timeout}s) 초과 — 강제 정리로 넘어갑니다.")
        return 124


def _reset_mode_artifacts(mode, dry=False):
    """해당 모드의 벤치마크 CSV 와 GCS 상태 파일을 삭제(append 오염·상태 잔재 제거)."""
    for target in (paths.benchmark_csv(mode), paths.gcs_state_path()):
        if dry:
            print(f"    [dry-run] rm -f {target}")
            continue
        try:
            if os.path.exists(target):
                os.remove(target)
                print(f"    리셋: {target}")
        except OSError as e:
            print(f"    리셋 경고({target}): {e}")


def _mode_env(mode, budget, keep_training):
    """모드별 서브프로세스 환경변수(compose 인터폴레이션 입력)를 구성한다."""
    env = dict(os.environ)
    env["SCHEDULER_MODE"] = mode
    env["EXPERIMENT_AUTOEXIT"] = "1"  # 예산 소진 시 head 자동 종료 → --exit-code-from 으로 전체 down
    # q_learning 은 기본 추론 모드(탐험 노이즈 배제). --keep-training 시에만 온라인 학습.
    env["Q_LEARNING_TRAINING_MODE"] = "true" if (mode == "q_learning" and keep_training) else "false"
    if budget is not None:
        env["INITIAL_VIRTUAL_BUDGET"] = str(budget)
    return env


def _render_reports(chart, dry=False):
    """전 모드 종료 후 분석 텍스트 + (옵션) 비교 차트를 생성한다."""
    if dry:
        print("  [dry-run] reporting_analyze.main() / reporting_visualize.main()")
        return
    try:
        from wemeet.observability import reporting_analyze
        reporting_analyze.main()
    except Exception as e:
        print(f"  [분석 경고] reporting_analyze 실패(선택 기능): {e}")
    if chart:
        try:
            from wemeet.observability import reporting_visualize
            reporting_visualize.main()
        except Exception as e:
            print(f"  [차트 경고] reporting_visualize 실패(pandas/matplotlib 필요): {e}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="WE-MEET 실환경 3대 스케줄러 비교 실험")
    parser.add_argument("--modes", default=",".join(DEFAULT_MODES),
                        help="실행할 스케줄러 모드(쉼표 구분). 기본: static,dynamic,q_learning")
    parser.add_argument("--budget", type=float, default=None,
                        help="초기 가상 예산($) 오버라이드. 실환경은 상시 과금이라 상향 권장(예: 10)")
    parser.add_argument("--compose-file", default="docker/docker-compose.yml",
                        help="docker-compose 파일 경로")
    parser.add_argument("--no-build", action="store_true", help="--build 생략(이미 빌드된 이미지 사용)")
    parser.add_argument("--timeout", type=int, default=1800,
                        help="모드별 최대 실행 시간(초). 초과 시 강제 down. 기본 1800")
    parser.add_argument("--keep-training", action="store_true",
                        help="q_learning 을 온라인 학습 모드로 실행(기본은 추론 모드)")
    parser.add_argument("--chart", action="store_true", help="종료 후 비교 차트(PNG) 생성")
    parser.add_argument("--dry-run", action="store_true", help="실제 실행 없이 커맨드/환경만 출력")
    args = parser.parse_args(argv)

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    compose = _detect_compose()
    if compose is None and not args.dry_run:
        print("[오류] docker / docker-compose 를 찾을 수 없습니다. Docker 설치 후 다시 실행하세요.")
        return 2
    if compose is None:
        compose = ["docker-compose"]  # dry-run 표시용

    cf = args.compose_file
    print(f"=== WE-MEET 실환경 실험 === 모드={modes} · 예산={args.budget or '기본(sim_env)'} · "
          f"compose={' '.join(compose)}{' · DRY-RUN' if args.dry_run else ''}")

    # 0) 네트워크 보장 (이미 있으면 무시)
    print("[0] babyray-net 네트워크 보장")
    _run(["docker", "network", "create", NETWORK_NAME], dry=args.dry_run)

    for i, mode in enumerate(modes, 1):
        print(f"\n[{i}/{len(modes)}] 모드 '{mode}' 실험 시작")
        _reset_mode_artifacts(mode, dry=args.dry_run)
        env = _mode_env(mode, args.budget, args.keep_training)
        print(f"    env: SCHEDULER_MODE={env['SCHEDULER_MODE']} "
              f"EXPERIMENT_AUTOEXIT=1 Q_LEARNING_TRAINING_MODE={env['Q_LEARNING_TRAINING_MODE']} "
              f"INITIAL_VIRTUAL_BUDGET={env.get('INITIAL_VIRTUAL_BUDGET', '(기본)')}")

        # [수정] 스팟 워커가 에빅션(SIGKILL, exit 137)으로 종료되는 것은 이 실험의 정상 동작이다.
        # 과거 `--abort-on-container-exit`(및 이를 암시하는 `--exit-code-from head`)는 '아무 컨테이너나'
        # 종료되면 전체 스택을 내려서, 스팟 워커 한 대의 에빅션마다 head 까지 강제 종료됐다.
        # → detached(-d) 로 띄운 뒤 head 컨테이너의 종료만 기다린다(head 는 예산 소진 시 EXPERIMENT_AUTOEXIT 로 자체 종료).
        up = compose + ["-f", cf, "up", "-d"]
        if not args.no_build:
            up.insert(up.index("up") + 1, "--build")
        rc = _run(up, env=env, dry=args.dry_run, timeout=args.timeout)

        if args.dry_run:
            print("    [dry-run] (백그라운드) docker compose logs -f  +  head 종료 대기: docker wait babyray-head")
        elif rc == 0:
            # 실시간 로그 스트리밍(백그라운드) — head 가 끝날 때까지 콘솔에 그대로 흘려보낸다.
            logs_proc = None
            try:
                logs_proc = subprocess.Popen(compose + ["-f", cf, "logs", "-f"], cwd=paths.PROJECT_ROOT)
            except Exception as e:
                print(f"    (로그 스트리밍 생략: {e})")
            # head 컨테이너 종료까지만 대기. 스팟 워커의 exit 137 은 무시된다.
            print("    head 컨테이너 종료 대기: docker wait babyray-head  (예산 소진 시 자동 종료)")
            _run(["docker", "wait", "babyray-head"], dry=False, timeout=args.timeout)
            if logs_proc is not None:
                try:
                    logs_proc.terminate()
                    logs_proc.wait(timeout=5)
                except Exception:
                    pass
        else:
            print(f"    ! compose up -d 실패(종료코드 {rc}) — 정리로 넘어갑니다.")

        # 항상 정리(타임아웃/오류 포함)
        _run(compose + ["-f", cf, "down"], env=env, dry=args.dry_run)
        print(f"    -> 결과 CSV: {paths.benchmark_csv(mode)}")

    print("\n[리포트] 벤치마크 분석/시각화")
    _render_reports(args.chart, dry=args.dry_run)
    print("\n=== 실험 완료. data/benchmark_results_*.csv 및 리포트를 확인하세요. ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
