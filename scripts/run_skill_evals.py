"""跑遍 .claude/skills 下所有 skill 的 evals，汇总成一份结果。

每个 skill 的 evals 是自带的 `.claude/skills/<skill>/evals/run.py`——它们各自
最清楚该断言什么，所以这里只做编排，不做断言。

用法：
    uv run python scripts/run_skill_evals.py              # 免费档（含需要 Chrome 的）
    uv run python scripts/run_skill_evals.py --paid       # 再加上要调付费 API 的
    uv run python scripts/run_skill_evals.py --list       # 只列出各 skill 的用例
    uv run python scripts/run_skill_evals.py --skill run-jev
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = ROOT / ".claude" / "skills"

# 每个 skill 接受哪些开关。写死在这里而不是去问 runner，是为了让"支持什么"
# 一眼可见——万一某个 runner 加了新开关忘了登记，下面的一致性检查会报出来。
FLAGS = {
    "run-jev": {"--no-browser"},
    "nl-case-author": set(),
    "nl-case-run": {"--paid"},
    # 全离线、免费，所以不接受任何开关（只认编排层无条件追加的 --list）
    "e9-graph-query": set(),
}


def discover():
    found = {}
    for runner in sorted(SKILLS_DIR.glob("*/evals/run.py")):
        found[runner.parent.parent.name] = runner
    return found


def main():
    parser = argparse.ArgumentParser(description="跑遍所有 skill 的 evals")
    parser.add_argument("--list", action="store_true", help="只列出，不执行")
    parser.add_argument("--paid", action="store_true", help="连同会调付费 API 的用例")
    parser.add_argument("--no-browser", action="store_true", help="跳过需要 Chrome 的用例")
    parser.add_argument("--skill", action="append", metavar="NAME", help="只跑指定 skill")
    args = parser.parse_args()

    runners = discover()
    unregistered = set(runners) - set(FLAGS)
    if unregistered:
        print(f"这些 skill 有 evals 但没在 FLAGS 里登记开关：{sorted(unregistered)}")
        return 1

    selected = {
        name: path for name, path in runners.items()
        if not args.skill or name in set(args.skill)
    }
    if not selected:
        print("没有找到任何 evals")
        return 1

    results = {}
    for name, runner in selected.items():
        invoked = []
        if args.paid and "--paid" in FLAGS[name]:
            invoked.append("--paid")
        if args.no_browser and "--no-browser" in FLAGS[name]:
            invoked.append("--no-browser")
        if args.list:
            invoked.append("--list")

        print(f"{'=' * 72}\n## {name}\n")
        result = subprocess.run(
            [sys.executable, str(runner), *invoked],
            cwd=ROOT, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        results[name] = result.returncode
        print()

    failed = [name for name, code in results.items() if code != 0]
    print("=" * 72)
    print(f"{len(results) - len(failed)}/{len(results)} 个 skill 的 evals 全绿"
          + (f"；未通过：{failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())