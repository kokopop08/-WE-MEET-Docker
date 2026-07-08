import os
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
        df = pd.read_csv(csv_path)
        if df.empty:
            return None
        # 데이터 정제
        df['cost'] = pd.to_numeric(df['cost'], errors='coerce').fillna(0.0)
        df['delay'] = pd.to_numeric(df['delay'], errors='coerce').fillna(0.0)
        df['execution_time'] = pd.to_numeric(df['execution_time'], errors='coerce').fillna(0.0)
        
        # 메트릭 취합
        total_tasks = len(df)
        success_tasks = len(df[df['status'] == 'SUCCESS'])
        success_rate = (success_tasks / total_tasks) * 100 if total_tasks > 0 else 0
        
        sla_compliant = len(df[df['delay'] <= 0.0])
        sla_rate = (sla_compliant / total_tasks) * 100 if total_tasks > 0 else 0
        
        avg_delay = df['delay'].mean()
        total_cost = df['cost'].sum()
        avg_exec_time = df['execution_time'].mean()
        
        print(f"[{mode_name}] 분석 완료 -> 태스크수: {total_tasks}, 성공률: {success_rate:.1f}%, SLA준수율: {sla_rate:.1f}%, 총비용: ${total_cost:.5f}, 평균지연: {avg_delay:.2f}초")
        
        return {
            "mode": mode_name,
            "total_tasks": total_tasks,
            "success_rate": success_rate,
            "sla_rate": sla_rate,
            "avg_delay": avg_delay,
            "total_cost": total_cost,
            "avg_exec_time": avg_exec_time,
            "df": df
        }
    except Exception as e:
        print(f"[에러] {mode_name} 파일 파싱 실패: {e}")
        return None

def main():
    data_dir = "data"
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
    
    # 그래프 시각화 영역 생성 (2x2 Grid)
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle("스케줄러 알고리즘별 성능 비교 벤치마크 (Static vs Dynamic vs Q-Learning)", fontsize=16, fontweight='bold')
    
    sns.set_theme(style="whitegrid")
    plt.rcParams['font.family'] = 'Malgun Gothic'
    
    # 1. 평균 지연 시간 비교 (지연이 작을수록 좋음)
    sns.barplot(x="mode", y="avg_delay", data=summary_df, ax=axes[0, 0], palette="coolwarm")
    axes[0, 0].set_title("1. 평균 SLA 지연 시간 (초) [↓이 좋음]", fontsize=12, fontweight='bold')
    axes[0, 0].set_ylabel("평균 지연 시간 (초)")
    axes[0, 0].set_xlabel("스케줄러 모드")
    for p in axes[0, 0].patches:
        axes[0, 0].annotate(f"{p.get_height():.2f}s", (p.get_x() + p.get_width() / 2., p.get_height()),
                            ha='center', va='center', xytext=(0, 5), textcoords='offset points')

    # 2. 누적 인프라 비용 비교 (비용이 작을수록 좋음)
    sns.barplot(x="mode", y="total_cost", data=summary_df, ax=axes[0, 1], palette="viridis")
    axes[0, 1].set_title("2. 누적 가상 서버 사용 비용 ($) [↓이 좋음]", fontsize=12, fontweight='bold')
    axes[0, 1].set_ylabel("총 비용 (USD)")
    axes[0, 1].set_xlabel("스케줄러 모드")
    for p in axes[0, 1].patches:
        axes[0, 1].annotate(f"${p.get_height():.5f}", (p.get_x() + p.get_width() / 2., p.get_height()),
                            ha='center', va='center', xytext=(0, 5), textcoords='offset points')

    # 3. SLA 데드라인 준수율 비교 (높을수록 좋음)
    sns.barplot(x="mode", y="sla_rate", data=summary_df, ax=axes[1, 0], palette="crest")
    axes[1, 0].set_title("3. SLA 데드라인 준수율 (%) [↑이 좋음]", fontsize=12, fontweight='bold')
    axes[1, 0].set_ylabel("준수율 (%)")
    axes[1, 0].set_xlabel("스케줄러 모드")
    axes[1, 0].set_ylim(0, 110)
    for p in axes[1, 0].patches:
        axes[1, 0].annotate(f"{p.get_height():.1f}%", (p.get_x() + p.get_width() / 2., p.get_height()),
                            ha='center', va='center', xytext=(0, 5), textcoords='offset points')

    # 4. 평균 실행 시간 비교
    sns.barplot(x="mode", y="avg_exec_time", data=summary_df, ax=axes[1, 1], palette="flare")
    axes[1, 1].set_title("4. 태스크당 평균 실행 연산 시간 (초)", fontsize=12, fontweight='bold')
    axes[1, 1].set_ylabel("평균 연산 시간 (초)")
    axes[1, 1].set_xlabel("스케줄러 모드")
    for p in axes[1, 1].patches:
        axes[1, 1].annotate(f"{p.get_height():.2f}s", (p.get_x() + p.get_width() / 2., p.get_height()),
                            ha='center', va='center', xytext=(0, 5), textcoords='offset points')

    plt.tight_layout(rect=[0, 0, 1, 0.95])
    
    # 결과 이미지 저장
    output_img = "data/benchmark_comparison_chart.png"
    plt.savefig(output_img, dpi=150)
    print(f"\n[성공] 벤치마크 비교 시각화 그래프가 {output_img}에 성공적으로 저장되었습니다!")
    plt.close()

if __name__ == "__main__":
    main()
