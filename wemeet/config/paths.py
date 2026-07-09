"""프로젝트 경로 단일 해석기 (wemeet/config/paths.py).

그동안 런타임 산출물 경로가 두 방식으로 흩어져 있었다: (1) CWD 상대 ``"data/..."``
(실행 위치가 루트가 아니면 깨짐), (2) ``__file__`` 기준 ``"../data/..."``/``"../../data/..."``
(파일 깊이에 따라 ``..`` 개수가 제각각). 같은 파일(``gcs_state.json``)을 두 방식이 서로 다르게
가리키는 불일치도 있었다.

이 모듈은 ``__file__`` 기준으로 ``PROJECT_ROOT`` 를 한 번 계산하고, 모든 데이터 경로를
그로부터 파생시켜 **작업 디렉토리(CWD)와 무관하게 항상 동일한 절대경로**를 보장한다.

Layout 가정:
    <PROJECT_ROOT>/
        wemeet/config/paths.py   <- 이 파일
        data/                    <- 런타임 상태(이동하지 않음; docker 는 /app/data 로 마운트)

docker 컨테이너에서는 WORKDIR ``/app`` 아래 ``wemeet/`` 와 ``data/`` 가 나란히 놓이므로
``PROJECT_ROOT`` == ``/app`` 이 되어 ``/app/data`` 로 정확히 해석된다.
"""

import os
import glob as _glob

# 이 파일: <ROOT>/wemeet/config/paths.py → dirname 3번이면 <ROOT>
PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)

#: 런타임 산출물(체크포인트·GCS 상태·벤치마크 CSV 등) 루트 디렉토리.
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

#: 설정 YAML 이 위치한 디렉토리(env_config 가 참조).
CONFIG_DIR = os.path.join(PROJECT_ROOT, "wemeet", "config")


def ensure_data_dir():
    """``DATA_DIR`` 이 없으면 생성하고 그 경로를 반환한다."""
    os.makedirs(DATA_DIR, exist_ok=True)
    return DATA_DIR


def data_path(*parts):
    """``DATA_DIR`` 하위의 임의 경로를 절대경로로 조합해 반환한다.

    Args:
        *parts: ``DATA_DIR`` 아래에 이어붙일 경로 조각들.

    Returns:
        str: 조합된 절대경로.
    """
    return os.path.join(DATA_DIR, *parts)


# --- 이름 있는 산출물 경로 헬퍼 --------------------------------------------
def gcs_state_path():
    """GCS 전역 상태 스냅샷 파일 경로 (``data/gcs_state.json``)."""
    return data_path("gcs_state.json")


def q_table_path():
    """Q-Learning Q-Table 파일 경로 (``data/q_table.json``)."""
    return data_path("q_table.json")


def q_table_metadata_path():
    """Q-Table 메타데이터(epsilon 등) 파일 경로 (``data/q_table_metadata.json``)."""
    return data_path("q_table_metadata.json")


def dataset_path(model_type):
    """모델 유형별 가상 데이터셋 파일 경로 (``data/<model>_dataset.pt``)."""
    return data_path(f"{model_type.lower()}_dataset.pt")


def checkpoint_path(task_id, epoch):
    """에포크별 체크포인트 파일 경로 (``data/checkpoint_<task>_epoch_<n>.pt``)."""
    return data_path(f"checkpoint_{task_id}_epoch_{epoch}.pt")


def final_model_path(task_id):
    """최종 학습 산출 모델 파일 경로 (``data/final_<task>.pt``)."""
    return data_path(f"final_{task_id}.pt")


def online_training_csv():
    """온라인 학습 이력 CSV 경로 (``data/online_training_history.csv``)."""
    return data_path("online_training_history.csv")


def benchmark_csv(mode):
    """스케줄러 모드별 벤치마크 결과 CSV 경로 (``data/benchmark_results_<mode>.csv``)."""
    return data_path(f"benchmark_results_{mode}.csv")


def benchmark_report(scenario):
    """시나리오별 벤치마크 요약 리포트 경로 (``data/benchmark_report_<scenario>.md``)."""
    return data_path(f"benchmark_report_{scenario}.md")


def benchmark_chart():
    """벤치마크 비교 차트 이미지 경로 (``data/benchmark_comparison_chart.png``)."""
    return data_path("benchmark_comparison_chart.png")


def glob_data(pattern):
    """``DATA_DIR`` 기준 glob 패턴에 매칭되는 절대경로 리스트를 반환한다.

    Args:
        pattern: ``DATA_DIR`` 에 대한 상대 glob 패턴(예: ``"checkpoint_*.pt"``).

    Returns:
        list[str]: 매칭된 파일들의 절대경로 리스트.
    """
    return _glob.glob(data_path(pattern))
