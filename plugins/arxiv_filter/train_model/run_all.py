"""
一键运行全部步骤，构建/更新训练数据集并训练模型

用法:
  python run_all.py              # 运行 Step 1-3（构建数据集）
  python run_all.py --train      # 运行 Step 1-4（构建数据集 + 训练）
  python run_all.py --step 2     # 只运行某一步
  python run_all.py --step 4     # 只运行训练

步骤说明:
  Step 1: 从笔记中提取正样本 arXiv ID 和日期范围
  Step 2: 通过 arXiv API 获取日期范围内所有 astro-ph 论文（首次运行耗时较长）
  Step 3: 合并、标记正负样本，输出最终训练数据集
  Step 4: 训练 ModernBERT 分类模型（需要 GPU）
"""

import subprocess
import sys
import argparse
import time


STEPS = [
    ("step1_extract_positive_ids.py", "提取正样本 ID 和日期范围"),
    ("step2_fetch_all_astro_ph.py",   "获取所有 astro-ph 论文"),
    ("step3_build_dataset.py",        "构建最终训练数据集"),
    ("arxiv_class.py",             "训练 ModernBERT 分类模型"),
]


def run_step(script: str, description: str) -> bool:
    print(f"\n{'='*60}")
    print(f"  {description}")
    print(f"  运行: {script}")
    print(f"{'='*60}\n")

    start = time.time()
    result = subprocess.run([sys.executable, script], cwd=".")
    elapsed = time.time() - start

    if result.returncode == 0:
        print(f"\n  {description} 完成 ({elapsed:.1f}s)")
        return True
    else:
        print(f"\n  {description} 失败 (退出码: {result.returncode})")
        return False


def main():
    parser = argparse.ArgumentParser(description="构建/更新 arxiv 训练数据集")
    parser.add_argument("--step", type=int, choices=[1, 2, 3, 4],
                        help="只运行指定步骤 (1/2/3/4)")
    parser.add_argument("--train", action="store_true",
                        help="包含 Step 4 训练（默认只运行 Step 1-3）")
    args = parser.parse_args()

    if args.step:
        idx = args.step - 1
        script, desc = STEPS[idx]
        success = run_step(script, f"Step {args.step}: {desc}")
        sys.exit(0 if success else 1)

    # 确定运行范围: 默认 Step 1-3, --train 则 Step 1-4
    steps_to_run = STEPS if args.train else STEPS[:3]

    print("=" * 60)
    print(f"构建训练数据集 - 运行 Step 1-{len(steps_to_run)}")
    print("=" * 60)

    total_start = time.time()
    for i, (script, desc) in enumerate(steps_to_run):
        success = run_step(script, f"Step {i+1}: {desc}")
        if not success:
            print(f"\n  Step {i+1} 失败，停止执行后续步骤")
            sys.exit(1)

    total_elapsed = time.time() - total_start
    print(f"\n{'='*60}")
    print(f"  全部步骤完成! 总耗时: {total_elapsed:.1f}s ({total_elapsed/60:.1f}min)")
    print(f"{'='*60}")

    if not args.train:
        print("\n数据集: arxiv_papers_with_abstract.csv")
        print("训练模型: python run_all.py --train  或  python arxiv_class_v3.py")


if __name__ == '__main__':
    main()
