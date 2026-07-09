import os
import sys

# 1) script_dir(wemeet/observability/)가 sys.path에 있으므로, standard library 'logging' 모듈 섀도잉 방지를 위해 제거
_script_dir = os.path.dirname(os.path.abspath(__file__))
while _script_dir in sys.path:
    sys.path.remove(_script_dir)

# 2) 프로젝트 루트 디렉토리를 path 최상위에 추가하여 wemeet 패키지 임포트 지원
_project_root = os.path.abspath(os.path.join(_script_dir, '../..'))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

# 한글 폰트 설정 (한글 깨짐 방지 - 시스템 폰트 로드)
plt.rcParams['font.family'] = 'Malgun Gothic' # Windows 기본 맑은 고딕
plt.rcParams['axes.unicode_minus'] = False

def load_and_summarize(csv_path, mode_name):
    if not os.path.exists(csv_path):
        print(f"[경고] {csv_path} 파일이 존재하지 않아 {mode_name} 벤치마크 데이터를 건너뜁니다.")
        return None
    try:
        # 헤더가 없는 CSV 파일이므로 컬럼명을 수동 지정하여 로드
        columns = [
            "timestamp", "mode", "task_id", "model_type", "status", 
            "execution_time", "cost", "delay", 
            "od_count", "spot_a_count", "spot_b_count", "budget"
        ]
        df = pd.read_csv(csv_path, header=None, names=columns)
        if df.empty:
            return None
        # 데이터 정제
        df['cost'] = pd.to_numeric(df['cost'], errors='coerce').fillna(0.0)
        df['delay'] = pd.to_numeric(df['delay'], errors='coerce').fillna(0.0)
        df['execution_time'] = pd.to_numeric(df['execution_time'], errors='coerce').fillna(0.0)
        
        # 날짜 정제 및 깨진 행 제거 (스레드 간 동시 쓰기 누수로 발생한 깨진 행 필터링)
        df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
        df = df.dropna(subset=['mode', 'task_id']) # 깨진 행 제거
        df = df[df['timestamp'].notnull()]
        df = df[(df['timestamp'].dt.year >= 2020) & (df['timestamp'].dt.year <= 2030)]
        
        if df.empty:
            return None

        # 메트릭 취합
        total_tasks = len(df)
        success_df = df[df['status'] == 'SUCCESS']
        success_tasks = len(success_df)
        success_rate = (success_tasks / total_tasks) * 100 if total_tasks > 0 else 0
        failure_rate = 100.0 - success_rate
        
        sla_compliant = len(df[df['delay'] <= 0.0])
        sla_rate = (sla_compliant / total_tasks) * 100 if total_tasks > 0 else 0
        
        missed_sla_df = df[df['delay'] > 0.0]
        avg_missed_delay = missed_sla_df['delay'].mean() if len(missed_sla_df) > 0 else 0.0
        
        total_cost = df['cost'].sum()
        avg_exec_time = success_df['execution_time'].mean() if success_tasks > 0 else 0
        
        # 추가 지표 계산 (처리량 및 건당 비용)
        duration_seconds = (df['timestamp'].max() - df['timestamp'].min()).total_seconds() if not df['timestamp'].isnull().all() else 0
        throughput = (success_tasks / duration_seconds) if duration_seconds > 0 else 0
        cost_per_task = (total_cost / success_tasks) if success_tasks > 0 else 0
        
        print(f"[{mode_name}] 분석 완료 -> 처리량: {throughput:.3f} tasks/s, 평균연산: {avg_exec_time:.2f}초, 실패율: {failure_rate:.1f}%, 건당비용: ${cost_per_task:.4f}, SLA: {sla_rate:.1f}%")
        
        return {
            "mode": mode_name,
            "total_tasks": total_tasks,
            "success_rate": success_rate,
            "failure_rate": failure_rate,
            "sla_rate": sla_rate,
            "avg_missed_delay": avg_missed_delay,
            "throughput": throughput,
            "total_cost": total_cost,
            "cost_per_task": cost_per_task,
            "avg_exec_time": avg_exec_time,
            "df": df
        }
    except Exception as e:
        print(f"[에러] {mode_name} 파일 파싱 실패: {e}")
        return None

def main():
    # 스크립트 파일 위치 기준으로 data 디렉토리의 절대 경로 획득하여 CWD 영향 방지
    script_dir = os.path.dirname(os.path.abspath(__file__))
    from wemeet.config import paths
    data_dir = paths.DATA_DIR
    modes = {
        "static": os.path.join(data_dir, "benchmark_results_static.csv"),
        "dynamic": os.path.join(data_dir, "benchmark_results_dynamic.csv"),
        "q_learning": os.path.join(data_dir, "benchmark_results_q_learning.csv")
    }
    
    results = []
    for mode_name, path in modes.items():
        summary = load_and_summarize(path, mode_name.upper())
        if summary:
            results.append(summary)
            
    if not results:
        print("[에러] 비교할 수 있는 벤치마크 CSV 파일이 data/ 폴더에 존재하지 않습니다.")
        return

    # 데이터프레임 변환
    summary_df = pd.DataFrame([{k: v for k, v in r.items() if k != 'df'} for r in results])
    
    # 그래프 시각화 영역 생성 (2x3 Grid)
    fig, axes = plt.subplots(2, 3, figsize=(18, 10))
    fig.suptitle("스케줄러 알고리즘별 종합 성능 지표 벤치마크", fontsize=18, fontweight='bold')
    
    sns.set_theme(style="whitegrid")
    plt.rcParams['font.family'] = 'Malgun Gothic'
    
    # 1. 시간당 처리량 비교
    sns.barplot(x="mode", y="throughput", data=summary_df, ax=axes[0, 0], palette="coolwarm")
    axes[0, 0].set_title("1. 초당 처리량 (Throughput) [↑ 빠를수록 좋음]", fontsize=12, fontweight='bold')
    axes[0, 0].set_ylabel("Tasks / 초")
    axes[0, 0].set_xlabel("")
    for p in axes[0, 0].patches:
        axes[0, 0].annotate(f"{p.get_height():.3f}", (p.get_x() + p.get_width() / 2., p.get_height()),
                            ha='center', va='center', xytext=(0, 5), textcoords='offset points')

    # 2. 태스크당 평균 연산 시간
    sns.barplot(x="mode", y="avg_exec_time", data=summary_df, ax=axes[0, 1], palette="flare")
    axes[0, 1].set_title("2. 평균 연산 소요 시간 (초) [↓ 짧을수록 좋음]", fontsize=12, fontweight='bold')
    axes[0, 1].set_ylabel("소요 시간 (초)")
    axes[0, 1].set_xlabel("")
    for p in axes[0, 1].patches:
        axes[0, 1].annotate(f"{p.get_height():.2f}s", (p.get_x() + p.get_width() / 2., p.get_height()),
                            ha='center', va='center', xytext=(0, 5), textcoords='offset points')

    # 3. SLA 데드라인 준수율
    sns.barplot(x="mode", y="sla_rate", data=summary_df, ax=axes[0, 2], palette="crest")
    axes[0, 2].set_title("3. SLA 데드라인 준수율 (%) [↑ 높을수록 좋음]", fontsize=12, fontweight='bold')
    axes[0, 2].set_ylabel("준수율 (%)")
    axes[0, 2].set_xlabel("")
    axes[0, 2].set_ylim(0, 110)
    for p in axes[0, 2].patches:
        axes[0, 2].annotate(f"{p.get_height():.1f}%", (p.get_x() + p.get_width() / 2., p.get_height()),
                            ha='center', va='center', xytext=(0, 5), textcoords='offset points')

    # 4. 실패율 (장애/회수율)
    sns.barplot(x="mode", y="failure_rate", data=summary_df, ax=axes[1, 0], palette="rocket")
    axes[1, 0].set_title("4. 노드 회수/장애 실패율 (%) [낮을수록 안정적]", fontsize=12, fontweight='bold')
    axes[1, 0].set_ylabel("실패율 (%)")
    axes[1, 0].set_xlabel("스케줄러 모드")
    for p in axes[1, 0].patches:
        axes[1, 0].annotate(f"{p.get_height():.1f}%", (p.get_x() + p.get_width() / 2., p.get_height()),
                            ha='center', va='center', xytext=(0, 5), textcoords='offset points')

    # 5. 평균 초과 지연 시간 (SLA 실패한 태스크들만)
    sns.barplot(x="mode", y="avg_missed_delay", data=summary_df, ax=axes[1, 1], palette="magma")
    axes[1, 1].set_title("5. SLA 초과시 평균 페널티 지연 (초) [↓ 짧을수록 좋음]", fontsize=12, fontweight='bold')
    axes[1, 1].set_ylabel("초과 지연 시간 (초)")
    axes[1, 1].set_xlabel("스케줄러 모드")
    for p in axes[1, 1].patches:
        axes[1, 1].annotate(f"{p.get_height():.2f}s", (p.get_x() + p.get_width() / 2., p.get_height()),
                            ha='center', va='center', xytext=(0, 5), textcoords='offset points')

    # 6. 태스크당 처리 비용
    sns.barplot(x="mode", y="cost_per_task", data=summary_df, ax=axes[1, 2], palette="viridis")
    axes[1, 2].set_title("6. 1건당 평균 처리 비용 ($) [↓ 저렴할수록 좋음]", fontsize=12, fontweight='bold')
    axes[1, 2].set_ylabel("태스크당 비용 (USD)")
    axes[1, 2].set_xlabel("스케줄러 모드")
    for p in axes[1, 2].patches:
        axes[1, 2].annotate(f"${p.get_height():.5f}", (p.get_x() + p.get_width() / 2., p.get_height()),
                            ha='center', va='center', xytext=(0, 5), textcoords='offset points')

    plt.tight_layout(rect=[0, 0, 1, 0.95])
    
    # 결과 이미지 저장 (CWD 독립적인 절대 경로 활용)
    output_img = os.path.join(data_dir, "benchmark_comparison_chart.png")
    plt.savefig(output_img, dpi=150)
    print(f"\n[성공] 벤치마크 비교 시각화 그래프가 {output_img}에 성공적으로 저장되었습니다!")
    plt.close()

if __name__ == "__main__":
    main()