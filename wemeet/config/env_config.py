"""WE-MEET: 환경/확률 설정 단일 로더 (common/env_config.py)

common/cost_model.yaml (요금·GPU배율·회수기본확률) 과 common/sim_env.yaml (그 외 모든
확률/환경 변수)을 한 번 읽어 캐싱하는 싱글턴 로더. 프로젝트 전역이 이 모듈만 참조하면
수치의 유일 진실이 성립한다.

[안전성] 두 YAML 이 없거나 일부 키가 비어도, 내장 DEFAULTS(현 하드코딩 값과 비트 동일)로
  딥머지 폴백하므로 오프라인/컨테이너 어디서든 동일 값을 반환한다.
"""

import os
import copy

try:
    import yaml
except Exception:  # pragma: no cover - yaml 부재 시에도 DEFAULTS 로 동작
    yaml = None

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
COST_MODEL_PATH = os.path.join(_THIS_DIR, "cost_model.yaml")
SIM_ENV_PATH = os.path.join(_THIS_DIR, "sim_env.yaml")

# ------------------------------------------------------------------------------
# 내장 폴백 (sim_env.yaml / cost_model.yaml 과 비트 동일해야 함 — 값 무변경 불변식)
# ------------------------------------------------------------------------------
_COST_MODEL_DEFAULT = {
    "nodes": {
        # memory_limit_mb: 실제 docker cgroup 상한(spurious OOM-kill 방지 위해 torch 런타임 수용 크기로 상향). cost_model.yaml 과 비트 동일.
        "on_demand": {"cpu_limit": 2.0, "memory_limit_mb": 4096, "cost_per_hour": 7.10,
                      "gpu_scale_factor": 1.0, "preemption_probability": 0.0},
        "spot_a": {"cpu_limit": 1.0, "memory_limit_mb": 2560, "cost_per_hour": 2.20,
                   "gpu_scale_factor": 0.6, "preemption_probability": 0.50},
        "spot_b": {"cpu_limit": 0.5, "memory_limit_mb": 1536, "cost_per_hour": 0.90,
                   "gpu_scale_factor": 0.5, "preemption_probability": 0.10},
    }
}

_SIM_ENV_DEFAULT = {
    "failure": {
        "oom": {  # [2026-07-09 완화] 확률 OOM 과도 → 완화. sim_env.yaml 과 비트 동일. ⚠️ 물리 변경 → q_table 재학습 필요.
            "base_prob": {
                "LSTM": {"on_demand": 0.01, "spot_a": 0.08, "spot_b": 0.40},
                "RNN":  {"on_demand": 0.00, "spot_a": 0.01, "spot_b": 0.02},
                "CNN":  {"on_demand": 0.00, "spot_a": 0.00, "spot_b": 0.01},
            },
            "colocation_step": 0.10,
            "prob_cap": 0.60,
            "legacy_lstm_prob": 0.04,
        },
        "eviction": {"idle_factor": 0.25, "poll_sec": 10.0},
        "danger_phase": {"cycle_sec": 30.0, "window_sec": 10.0},
        "out_of_capacity_prob": 0.30,
    },
    "workload": {
        "model_types": ["CNN", "RNN", "LSTM"],
        "epochs": {"min": 12, "max": 20},
        "timeout": {"min": 5, "max": 12},
        "burst_prob": 0.04, "burst_min": 5, "burst_max": 8,
        "normal_prob": 0.18,
        "queue_cap": 25, "tasks_cap": 100,
        "pretrain": {"burst_prob": 0.067, "normal_prob": 0.30},
    },
    "budget": {"initial_virtual_budget": 1.5, "sim_range": [0.5, 3.0]},
    "scaling": {"max_spot_scale_floor": 5, "boot_sec": 8.0, "max_task_attempts": 3},
    "exec_time": {"floor": 1.5, "per_epoch": {"CNN": 0.06, "RNN": 0.03, "LSTM": 0.04}, "merge_sec": 1.0},
    "reward": {"success": 20.0, "cost_weight": 1000.0, "delay_penalty_weight": 5.0,
               "eviction_penalty": 25.0, "oom_penalty": 20.0, "makespan_weight": 0.5,
               "cosched_bonus": 0.15, "cosched_penalty": 0.20},
    "qlearning": {"alpha": 0.1, "gamma": 0.9, "epsilon": 1.0, "epsilon_min": 0.05, "decay_rate": 0.995},
    "state_buckets": {"q_light_max": 3, "q_med_max": 7, "sla_tight_sec": 10.0, "sla_med_sec": 30.0,
                      "budget_critical": 0.7, "budget_low": 3.0, "cost_level_threshold": 9.0},
    "scheduler_policy": {
        "model_node_preference": {
            "LSTM": ["on_demand", "spot_a"],
            "CNN": ["on_demand", "spot_a", "spot_b"],
            "RNN": ["spot_b", "on_demand", "spot_a"],
            "MERGE": ["on_demand", "spot_a", "spot_b"],
        },
        "model_forbidden": {"LSTM": ["spot_b"]},
        "scale_out_burst_qlen": 8,
        "scale_out_normal_qlen": 2,
        "scale_in_sec_dynamic": 15.0,
        "scale_in_sec_sim": 3.0,
    },
    "scenarios": {
        "normal": {"desc": "평시 부하 (sim_env 기본값)"},
        "burst_heavy": {"desc": "폭주 트래픽 (버스트 빈발)", "burst_prob": 0.10, "normal_prob": 0.25},
        "low_budget": {"desc": "저예산 (조기 파산 압박)", "initial_virtual_budget": 0.8},
        "high_eviction": {"desc": "고회수 (Spot 변동성 심화)", "eviction_mult": 1.5},
    },
}

# ------------------------------------------------------------------------------
# 로드 / 딥머지 / 캐시
# ------------------------------------------------------------------------------
_cache = {"cost_model": None, "sim_env": None}


def _deep_merge(base, override):
    """override 의 값으로 base 를 재귀 병합한 새 dict 반환(리스트는 통째로 대체)."""
    out = copy.deepcopy(base)
    if not isinstance(override, dict):
        return out
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _load_yaml(path):
    if yaml is None or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[env_config 경고] {os.path.basename(path)} 로드 실패 → 폴백 사용: {e}")
        return {}


def _cost_model():
    if _cache["cost_model"] is None:
        _cache["cost_model"] = _deep_merge(_COST_MODEL_DEFAULT, _load_yaml(COST_MODEL_PATH))
    return _cache["cost_model"]


def _sim_env():
    if _cache["sim_env"] is None:
        _cache["sim_env"] = _deep_merge(_SIM_ENV_DEFAULT, _load_yaml(SIM_ENV_PATH))
    return _cache["sim_env"]


def reload():
    """캐시를 비워 다음 접근 시 YAML 을 다시 읽게 한다(테스트/핫리로드용)."""
    _cache["cost_model"] = None
    _cache["sim_env"] = None


# ------------------------------------------------------------------------------
# 접근자 (호출부는 반환 dict 을 그대로 소비)
# ------------------------------------------------------------------------------
def nodes():
    """cost_model.yaml 의 nodes 딕셔너리(cost_per_hour/gpu_scale_factor/preemption_probability 포함)."""
    return _cost_model().get("nodes", {})


def cost_per_hour():
    """{node_type: cost_per_hour} 매핑."""
    return {n: c.get("cost_per_hour", 0.0) for n, c in nodes().items()}


def gpu_scale():
    """{node_type: gpu_scale_factor} 매핑."""
    return {n: c.get("gpu_scale_factor", 1.0) for n, c in nodes().items()}


def preemption_probs():
    """{spot_a, spot_b: preemption_probability} — 회수 기본확률 유일 진실(cost_model.yaml)."""
    return {n: c.get("preemption_probability", 0.0)
            for n, c in nodes().items() if n in ("spot_a", "spot_b")}


def failure():
    return _sim_env()["failure"]


def workload():
    return _sim_env()["workload"]


def budget():
    return _sim_env()["budget"]


def scaling():
    return _sim_env()["scaling"]


def exec_time():
    return _sim_env()["exec_time"]


def reward():
    return _sim_env()["reward"]


def qlearning():
    return _sim_env()["qlearning"]


def state_buckets():
    return _sim_env()["state_buckets"]


def scheduler_policy():
    return _sim_env()["scheduler_policy"]


def scenarios():
    """측정 시나리오 프리셋 딕셔너리 {name: {overrides...}}."""
    return _sim_env().get("scenarios", {})
