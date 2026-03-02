#!/usr/bin/env python3
"""测试运行脚本"""

import argparse
import subprocess
import sys


def run_tests(args):
    """运行测试"""
    pytest_args = ["pytest", "-v"]

    # 添加标记过滤器
    if args.mark:
        pytest_args.extend(["-m", args.mark])

    # 添加覆盖参数
    if not args.no_cov:
        pytest_args.extend(["--cov=core", "--cov=plugins", "--cov-report=term-missing"])

    # 添加并行执行
    if args.parallel:
        pytest_args.extend(["-n", str(args.workers)])

    # 添加文件/目录
    pytest_args.extend(args.targets)

    print(f"Running: {' '.join(pytest_args)}")
    result = subprocess.run(pytest_args)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description="运行XiaoQing测试套件")
    parser.add_argument("targets", nargs="*", default=["tests"])
    parser.add_argument("-m", "--mark", help="按标记过滤 (unit, integration, plugin, core, slow)")
    parser.add_argument("--no-cov", action="store_true", help="禁用覆盖率")
    parser.add_argument("--parallel", action="store_true", help="并行执行")
    parser.add_argument("-n", "--workers", type=int, default=4, help="并行worker数")

    args = parser.parse_args()
    sys.exit(run_tests(args))


if __name__ == "__main__":
    main()
