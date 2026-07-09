"""벤치마크 하네스 (wemeet/simulation/benchmark.py).

`fast_sim.FastSimulator` 엔진을 N회(시드 변동) 반복 실행하여 통계(평균±표준편차)와
p50/p90·OOM/회수 분해 지표를 산출하고, 시나리오별 콘솔 비교표 + 마크다운 리포트를
생성한다. Q-Learning 은 학습 1회 후 평가 N회 구조.

    python -m wemeet.simulation.benchmark --runs 10 --scenario normal,burst_heavy --chart
"""

import os
from wemeet.simulation.fast_sim import FastSimulator, DATA_DIR, _WL, _ET  # noqa: F401
from wemeet.learning.agent import QLearningAgent
from wemeet.config import env_config as _ec

import json

def safe_save_q_table(agent):
    """Windows의 파일 잠금 및 Permission Error(WinError 32)를 우회하기 위한 직접 쓰기 방식의 안전 저장소 메서드."""
    try:
        existing_table = {}
        if os.path.exists(agent.q_table_path) and os.path.getsize(agent.q_table_path) > 0:
            try:
                with open(agent.q_table_path, 'r', encoding='utf-8') as f:
                    existing_table = json.load(f)
            except Exception:
                existing_table = {}
        
        for state_str, actions_dict in agent.q_table.items():
            if state_str not in existing_table:
                existing_table[state_str] = {}
            for act, q_val in actions_dict.items():
                existing_table[state_str][str(act)] = q_val
        
        # 임시 파일(.tmp) 생성 및 교체 대신 직접 파일 쓰기 (Permission Error 우회)
        with open(agent.q_table_path, 'w', encoding='utf-8') as f:
            json.dump(existing_table, f, indent=4)
            
        metadata_path = agent.q_table_path.replace(".json", "_metadata.json")
        with open(metadata_path, 'w', encoding='utf-8') as f:
            json.dump({"epsilon": agent.epsilon}, f, indent=4)
            
        print(f"   -> [성공] Q-Table 저장 완료: {agent.q_table_path}")
    except Exception as e:
        print(f"   -> [경고] Q-Table 저장 중 오류 발생 (직접 쓰기 우회 시도): {e}")

# ==============================================================================
# 지표 산출 · 통계 집계 · 리포트 (발표용 측정 도구 강화)
# ==============================================================================
import statistics
import math

MODE_LABELS = {"static": "Static", "dynamic": "Dynamic", "q_learning": "Q-Learning"}


def _percentile(values, pct):
    """정렬 기반 백분위수(선형 보간). numpy 의존 없이 동작."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return float(s[lo])
    return float(s[lo] + (s[hi] - s[lo]) * (k - lo))


def compute_metrics(sim):
    """단일 런의 로그(12컬럼)로부터 발표용 지표 dict 을 산출한다."""
    logs = sim.logs
    total = len(logs)
    if total == 0:
        return None

    succ = [r for r in logs if r[4] == "SUCCESS"]
    fail = [r for r in logs if r[4] != "SUCCESS"]
    # 실패 분해: exec_time==0 → OOM(즉사), exec_time>0 → 회수(진행 중 낭비). analyze_details 규칙.
    oom = [r for r in fail if float(r[5]) == 0.0]
    evict = [r for r in fail if float(r[5]) > 0.0]

    exec_times = [float(r[5]) for r in succ]
    delays = [float(r[7]) for r in logs]
    missed = [d for d in delays if d > 0.0]
    total_cost = sum(float(r[6]) for r in logs)
    duration = sim.current_time if sim.current_time > 0 else 1.0

    # 모델별 성공률
    per_model = {}
    for m in _WL["model_types"]:
        m_rows = [r for r in logs if r[3] == m]
        m_succ = [r for r in m_rows if r[4] == "SUCCESS"]
        per_model[m] = (len(m_succ) / len(m_rows) * 100.0) if m_rows else 0.0

    # 평균 활성 노드 수(노드 믹스 프록시) — 로그 시점 카운트 컬럼 평균
    avg_od = statistics.mean(float(r[8]) for r in logs)
    avg_sa = statistics.mean(float(r[9]) for r in logs)
    avg_sb = statistics.mean(float(r[10]) for r in logs)

    return {
        "total": total,
        "success_rate": len(succ) / total * 100.0,
        "failure_rate": len(fail) / total * 100.0,
        "sla_rate": sum(1 for d in delays if d <= 0.0) / total * 100.0,
        "oom_count": len(oom),
        "evict_count": len(evict),
        "throughput": len(succ) / duration,
        "total_cost": total_cost,
        "cost_per_task": (total_cost / len(succ)) if succ else 0.0,
        "exec_p50": _percentile(exec_times, 50),
        "exec_p90": _percentile(exec_times, 90),
        "delay_p90": _percentile(delays, 90),
        "avg_missed_delay": (statistics.mean(missed) if missed else 0.0),
        "budget_survival": 1.0 if sim.virtual_budget > 0.0 else 0.0,
        "final_budget": sim.virtual_budget,
        "per_model": per_model,
        "avg_nodes": {"on_demand": avg_od, "spot_a": avg_sa, "spot_b": avg_sb},
    }


# 표에 노출할 스칼라 지표(라벨·단위·개선방향)
_SCALAR_METRICS = [
    ("success_rate", "성공률", "%", "up"),
    ("sla_rate", "SLA 준수율", "%", "up"),
    ("throughput", "처리량", "tasks/s", "up"),
    ("cost_per_task", "건당 비용", "$", "down"),
    ("exec_p50", "연산 p50", "s", "down"),
    ("exec_p90", "연산 p90", "s", "down"),
    ("delay_p90", "지연 p90", "s", "down"),
    ("oom_count", "OOM 실패", "건", "down"),
    ("evict_count", "회수 실패", "건", "down"),
    ("budget_survival", "예산 생존율", "", "up"),
]


def aggregate(runs):
    """여러 런의 지표 리스트 → {metric: (mean, std, ci95)} + per_model 평균."""
    agg = {}
    for key, _, _, _ in _SCALAR_METRICS:
        vals = [r[key] for r in runs]
        mean = statistics.mean(vals)
        std = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        ci95 = (1.96 * std / math.sqrt(len(vals))) if len(vals) > 1 else 0.0
        agg[key] = (mean, std, ci95)
    # per_model 평균
    pm = {}
    for m in _WL["model_types"]:
        pm[m] = statistics.mean(r["per_model"].get(m, 0.0) for r in runs)
    agg["per_model"] = pm
    return agg


def train_qlearning_agent(episodes, base_seed):
    """Q-Learning 에이전트를 사전학습→미세조정하고 반환(디스크 저장 포함). 평가는 이 에이전트로 N회."""
    print(f"\n[Q-LEARNING] 사전 학습 시작 ({episodes} 에피소드)...")
    agent = QLearningAgent()
    original_save = agent.save_q_table
    agent.save_q_table = lambda: None   # 학습 루프 중 디스크 락 회피
    original_decay = agent.decay_rate
    agent.decay_rate = 1.0              # 에피소드 단위 선형 감쇄 사용

    decay_span = max(1.0, episodes * 0.8)
    for ep in range(1, episodes + 1):
        agent.epsilon = max(0.05, 1.0 - (ep / decay_span))
        sim = FastSimulator("q_learning", q_learning_training=True, seed=base_seed + ep)
        sim.q_agent = agent
        sim.run_to_end(write_csv=False)
        if ep % max(1, episodes // 5) == 0:
            print(f"   -> 진행률 {ep}/{episodes} (Epsilon {agent.epsilon:.3f}, 상태수 {len(agent.q_table)})")

    print("   -> 미세 조정 및 Q-Table 저장...")
    agent.epsilon = 0.05
    sim = FastSimulator("q_learning", q_learning_training=True, seed=base_seed + episodes + 1)
    sim.q_agent = agent
    sim.run_to_end(write_csv=False)
    safe_save_q_table(agent)
    agent.save_q_table = original_save
    agent.decay_rate = original_decay
    return agent


def run_mode(mode, runs, base_seed, scenario, agent=None):
    """한 모드를 runs 회(시드 변동) 반복 실행 → 런별 지표 리스트. 첫 런은 대표 CSV 로 저장."""
    metrics = []
    for i in range(runs):
        sim = FastSimulator(mode, q_learning_training=False, seed=base_seed + i, scenario=scenario)
        if mode == "q_learning":
            sim.q_agent = agent
        sim.run_to_end(write_csv=(i == 0))  # 대표 1회분만 CSV(시각화 호환)
        m = compute_metrics(sim)
        if m:
            metrics.append(m)
    return metrics


def _fmt(mean, std, unit):
    if unit == "$":
        return f"${mean:.4f} ±{std:.4f}"
    if unit == "%":
        return f"{mean:.1f}% ±{std:.1f}"
    if unit == "s":
        return f"{mean:.2f}s ±{std:.2f}"
    if unit == "건":
        return f"{mean:.1f} ±{std:.1f}"
    return f"{mean:.2f} ±{std:.2f}"


def print_console_table(scenario_name, agg_by_mode, runs):
    modes = list(agg_by_mode.keys())
    print(f"\n{'='*78}\n  시나리오 [{scenario_name}]  ({runs}회 반복 · 평균±표준편차)\n{'='*78}")
    header = f"  {'지표':<14}" + "".join(f"{MODE_LABELS.get(m, m):>21}" for m in modes)
    print(header)
    print("  " + "-" * (14 + 21 * len(modes)))
    for key, label, unit, _ in _SCALAR_METRICS:
        row = f"  {label:<14}"
        for m in modes:
            mean, std, _ = agg_by_mode[m][key]
            row += f"{_fmt(mean, std, unit):>21}"
        print(row)


def _conclusion(agg_by_mode):
    """지표 기반 핵심 결론 문장 자동 생성."""
    def best(key, direction):
        items = [(m, agg_by_mode[m][key][0]) for m in agg_by_mode]
        return (max if direction == "up" else min)(items, key=lambda x: x[1])
    b_sla = best("sla_rate", "up")
    b_cost = best("cost_per_task", "down")
    b_succ = best("success_rate", "up")
    return (f"SLA 준수율 1위: **{MODE_LABELS.get(b_sla[0])}** ({b_sla[1]:.1f}%) · "
            f"건당비용 최저: **{MODE_LABELS.get(b_cost[0])}** (${b_cost[1]:.4f}) · "
            f"성공률 1위: **{MODE_LABELS.get(b_succ[0])}** ({b_succ[1]:.1f}%)")


def write_markdown_report(scenario_name, scenario_desc, agg_by_mode, runs):
    modes = list(agg_by_mode.keys())
    lines = []
    lines.append(f"# WE-MEET 벤치마크 리포트 — 시나리오 `{scenario_name}`")
    lines.append("")
    lines.append(f"- 설명: {scenario_desc}")
    lines.append(f"- 반복 횟수: {runs}회(시드 변동) · 표기: 평균 ± 표준편차")
    lines.append(f"- 스케줄러: {', '.join(MODE_LABELS.get(m, m) for m in modes)}")
    lines.append("")
    lines.append(f"> **핵심 결론:** {_conclusion(agg_by_mode)}")
    lines.append("")
    # 비교 표
    lines.append("| 지표 | " + " | ".join(MODE_LABELS.get(m, m) for m in modes) + " |")
    lines.append("|---|" + "---|" * len(modes))
    for key, label, unit, direction in _SCALAR_METRICS:
        arrow = "↑" if direction == "up" else "↓"
        cells = []
        for m in modes:
            mean, std, _ = agg_by_mode[m][key]
            cells.append(_fmt(mean, std, unit))
        lines.append(f"| {label} ({arrow}) | " + " | ".join(cells) + " |")
    # 모델별 성공률
    lines.append("")
    lines.append("### 모델별 성공률 (%)")
    lines.append("| 모델 | " + " | ".join(MODE_LABELS.get(m, m) for m in modes) + " |")
    lines.append("|---|" + "---|" * len(modes))
    for model in _WL["model_types"]:
        cells = [f"{agg_by_mode[m]['per_model'][model]:.1f}" for m in modes]
        lines.append(f"| {model} | " + " | ".join(cells) + " |")
    lines.append("")

    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"benchmark_report_{scenario_name}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def maybe_render_chart():
    """visualize_benchmarks.py 를 호출해 대표 CSV 로 2x3 차트 생성(옵션)."""
    try:
        import importlib
        viz = importlib.import_module("wemeet.observability.reporting_visualize")
        viz.main()
    except Exception as e:
        print(f"[차트 경고] 시각화 생성 실패(선택 기능): {e}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="WE-MEET 초고속 벤치마크 측정 도구")
    parser.add_argument("--runs", type=int, default=10, help="모드별 반복 횟수(시드 변동, 통계 신뢰성)")
    parser.add_argument("--scenario", type=str, default="normal",
                        help="시나리오 프리셋(쉼표로 복수 지정). 예: normal,burst_heavy")
    parser.add_argument("--seed", type=int, default=42, help="반복 시드 베이스")
    parser.add_argument("--episodes", type=int, default=2000, help="Q-Learning 사전학습 에피소드 수")
    parser.add_argument("--chart", action="store_true", help="종료 후 visualize_benchmarks 차트 생성")
    parser.add_argument("--modes", type=str, default="static,dynamic,q_learning",
                        help="측정할 스케줄러(쉼표 구분)")
    args = parser.parse_args()

    all_scenarios = _ec.scenarios()
    scen_names = [s.strip() for s in args.scenario.split(",") if s.strip()]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]

    print("=== WE-MEET 초고속 경량 벤치마크 시뮬레이터 ===")
    print(f"모드={modes} · 반복={args.runs}회 · 시나리오={scen_names}")

    # Q-Learning 은 시나리오·시드와 무관하게 학습은 1회만(비용 절감). 평가만 반복.
    agent = None
    if "q_learning" in modes:
        agent = train_qlearning_agent(args.episodes, args.seed)

    for scen_name in scen_names:
        scenario = dict(all_scenarios.get(scen_name, {}))
        scen_desc = scenario.pop("desc", scen_name)
        agg_by_mode = {}
        for mode in modes:
            print(f"\n[{MODE_LABELS.get(mode, mode)}] 시나리오 '{scen_name}' {args.runs}회 실행...")
            runs_metrics = run_mode(mode, args.runs, args.seed, scenario, agent=agent)
            if runs_metrics:
                agg_by_mode[mode] = aggregate(runs_metrics)
        if not agg_by_mode:
            print(f"[경고] 시나리오 '{scen_name}' 결과 없음.")
            continue
        print_console_table(scen_name, agg_by_mode, args.runs)
        report_path = write_markdown_report(scen_name, scen_desc, agg_by_mode, args.runs)
        print(f"\n  -> 요약 리포트 저장: {report_path}")

    if args.chart:
        print("\n[차트] visualize_benchmarks 렌더링...")
        maybe_render_chart()

    print("\n=== 측정 완료. data/benchmark_report_*.md 를 확인하세요. ===")

if __name__ == "__main__":
    main()
