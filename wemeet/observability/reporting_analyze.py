import os
import pandas as pd

def analyze_file(csv_path, mode_name):
    if not os.path.exists(csv_path):
        print(f"{mode_name}: CSV file not found.")
        return
    
    columns = [
        "timestamp", "mode", "task_id", "model_type", "status", 
        "execution_time", "cost", "delay", 
        "od_count", "spot_a_count", "spot_b_count", "budget"
    ]
    df = pd.read_csv(csv_path, header=None, names=columns)
    
    # 깨진 행 제거
    df['timestamp_parsed'] = pd.to_datetime(df['timestamp'], errors='coerce')
    df = df.dropna(subset=['mode', 'task_id'])
    df = df[df['timestamp_parsed'].notnull()]
    df = df[(df['timestamp_parsed'].dt.year >= 2020) & (df['timestamp_parsed'].dt.year <= 2030)]
    df = df.drop(columns=['timestamp_parsed'])

    total = len(df)
    success = df[df['status'] == 'SUCCESS']
    failed = df[df['status'] == 'FAILED']
    
    success_count = len(success)
    failed_count = len(failed)
    
    oom_count = len(failed[failed['execution_time'] == 0.0])
    eviction_count = len(failed[failed['execution_time'] > 0.0])
    
    print(f"=== {mode_name} ===")
    print(f"Total tasks: {total}")
    print(f"Success tasks: {success_count} ({success_count/total*100:.1f}%)")
    print(f"Failed tasks: {failed_count} ({failed_count/total*100:.1f}%)")
    print(f"  - OOM failures (exec_time == 0): {oom_count} ({oom_count/total*100:.1f}%)")
    print(f"  - Eviction failures (exec_time > 0): {eviction_count} ({eviction_count/total*100:.1f}%)")
    
    # Analyze by model type
    print("Failures by Model Type:")
    for mtype in df['model_type'].unique():
        m_df = df[df['model_type'] == mtype]
        m_total = len(m_df)
        m_failed = len(m_df[m_df['status'] == 'FAILED'])
        m_oom = len(m_df[(m_df['status'] == 'FAILED') & (m_df['execution_time'] == 0.0)])
        m_evict = len(m_df[(m_df['status'] == 'FAILED') & (m_df['execution_time'] > 0.0)])
        print(f"  {mtype}: Total={m_total}, Failed={m_failed} (OOM={m_oom}, Evict={m_evict})")

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    from wemeet.config import paths
    data_dir = paths.DATA_DIR
    
    analyze_file(os.path.join(data_dir, "benchmark_results_static.csv"), "STATIC")
    analyze_file(os.path.join(data_dir, "benchmark_results_dynamic.csv"), "DYNAMIC")

if __name__ == '__main__':
    main()
