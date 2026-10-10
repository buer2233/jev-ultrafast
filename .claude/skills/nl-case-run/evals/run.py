"""nl-case-run 的 evals：真跑 pytest 收集与报告链路，验证 skill 说的每件事。

分三档：
  free    不调模型、不碰浏览器，可以直接进 CI
  allure  要 allure CLI（本机 2.13.8），验证【报告不是空的】——这是最容易静默坏掉的一环
  paid    真的执行自然语言用例：会调付费 API，E9 那条还会产生真实数据。要 --paid 才跑

用法：
    uv run python .claude/skills/nl-case-run/evals/run.py             # free + allure
    uv run python .claude/skills/nl-case-run/evals/run.py --paid      # 再加付费的两条
    uv run python .claude/skills/nl-case-run/evals/run.py --list
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRATCH = ROOT / "artifacts" / "skill-evals"
ALLURE_CLI = shutil.which("allure")

# 子进程输出可能是 cp936；父进程按 UTF-8 解码会炸在读取线程里并返回空串。
RUN_ENV = {"PYTHONIOENCODING": "utf-8"}


def pytest(*args, timeout=600):
    """跑 pytest，返回 (退出码, 合并输出, 解析出的计数)。

    失败时把【完整输出】写进 artifacts/skill-evals/：只把输出的尾巴塞进报错
    信息里会把 traceback 截断，低频偶发就再也查不清了（踩过一次）。
    """
    # 必须带上 --env-file .env：付费用例要 TYPESAFE_API_KEY / TEXT_MODEL_API_KEY。
    # 只把 os.environ 传下去是不够的——eval 自己是用 `uv run python` 起的，
    # 环境里没有那两个密钥，于是 NL 用例会以 KeyError: 'TYPESAFE_API_KEY' 死掉。
    # 更坏的是：负向对照本来就期待失败，这种"环境没配好"的失败会被误判成
    # "框架如实报错"——eval 会替一个根本没跑起来的用例背书。
    # 路径也刻意用【相对形式】：把绝对 Windows 路径交给别的程序，反斜杠会被当转义
# 吃掉，`D:\AI\...\.env` 变成 `D:AIE9...env`，报错却是"找不到环境文件"。
    # 相对路径配合 cwd=ROOT，等价且不会踩这个坑。
    command = ["uv", "run"]
    if (ROOT / ".env").is_file():
        command += ["--env-file", ".env"]
    result = subprocess.run(
        [*command, "pytest", *args, "--tb=long"], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
        env={**os.environ, **RUN_ENV},
    )
    output = (result.stdout or "") + (result.stderr or "")
    counts = {key: int(value) for value, key in re.findall(
        r"(\d+) (passed|failed|skipped|error|errors|rerun)", output,
    )}
    if result.returncode != 0 and counts.get("failed"):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        log = SCRATCH / ("pytest-failure-" + re.sub(r"\W+", "-", " ".join(args))[:60] + ".log")
        log.write_text(output, encoding="utf-8")
    return result.returncode, output, counts


def collected_ids(*args):
    """--collect-only 收集到的用例 id（取参数化方括号里的部分）。"""
    code, output, _ = pytest("tests/test_nl_cases.py", "--collect-only", "-q", *args, timeout=180)
    if code != 0 and "collected" not in output and "no tests ran" not in output:
        return None, output
    ids = [
        match.group(1)
        for line in output.splitlines() if "::" in line
        if (match := re.search(r"\[(.*)\]\s*$", line))
    ]
    return ids, output


# ------------------------------------------------------------------ free

def ev_collects_e9_dir_only():
    """--cases-dir 是【替换】默认目录，不是追加。

    判据是**演示用例不许漏进来**——那才是「追加而不是替换」这个 bug 的信号。
    初版把它写成 `ids != ["e9-workflow-add-bym"]`（硬编码的单条清单），
    结果是 cases/e9/ 一多出用例（workflow_design.yaml 加了 3 条）就永久误报：
    它守的其实是「我当时数过的那个条数」，而不是「替换语义」。
    断言要和它声称守的东西对齐，否则红了也说不清是缺陷还是又加了一条用例。
    """
    ids, _ = collected_ids("--cases-dir", "cases/e9")
    if ids is None:
        return False, "收集失败"
    if not ids:
        return False, "cases/e9 下一条用例都没收集到"
    leaked = [case_id for case_id in ids if case_id.startswith("demo-")]
    if leaked:
        return False, f"--cases-dir 没有替换默认目录，演示用例漏了进来：{leaked}"
    declared = len(re.findall(r"^\s*-\s+id:", _read_e9_yaml(), re.MULTILINE))
    if len(ids) != declared:
        return False, f"cases/e9 的 YAML 里声明了 {declared} 条，实际收集到 {len(ids)} 条：{ids}"
    return True, f"--cases-dir cases/e9 只收集该目录的 {len(ids)} 条，无演示用例漏入"


def _read_e9_yaml():
    """把 cases/e9 下所有 YAML 拼起来，用于数 `- id:` 声明。"""
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "cases" / "e9").glob("*.yaml"))
    )


def ev_case_filter_narrows_to_one():
    ids, _ = collected_ids("--case", "demo-weaver-site-tour")
    if ids != ["demo-weaver-site-tour"]:
        return False, f"期望 1 条演示用例，实际 {ids}"
    return True, "--case 精确选中 1 条"


def ev_unknown_case_id_fails_loudly():
    """写错 id 必须【响亮地】失败，且报错要让人看出是 id 写错了。

    这条 eval 抓到过一个真缺陷：以前是静默过滤成空列表，pytest 塞一个 NotSet 占位，
    随后在 collection_modifyitems 里炸成 INTERNALERROR——报错里完全看不出
    是 --case 写错了。现在改成 UsageError，并在这里守住它不再退化。
    """
    code, output, _ = pytest(
        "tests/test_nl_cases.py", "--collect-only", "-q", "--case", "no-such-case", timeout=180,
    )
    # 认 pytest 的真实标记 "INTERNALERROR>"（带尖括号）。不能只搜 "INTERNALERROR"：
    # conftest.py 的注释里就有这个词，而 pytest 的 traceback 会把源码打出来——
    # 那样等于在 grep 自己的注释，永久误报（踩过一次）。
    if "INTERNALERROR>" in output:
        return False, "又炸成 INTERNALERROR 了——报错看不出是 id 写错"
    if code == 0:
        return False, "未知 id 竟然退出码 0（静默通过）"
    if "no-such-case" not in output:
        return False, f"报错里没提到写错的 id：{output.strip()[-150:]!r}"
    if "不存在" not in output:
        return False, f"报错没说是 id 不存在：{output.strip()[-150:]!r}"
    return True, "未知 id -> 非零退出 + 明确指出该 id 不存在"


def ev_nl_cases_skipped_by_default():
    """项目契约：默认离线。不带 --nl 时 NL 用例必须全部跳过。"""
    _, _, counts = pytest("tests/test_nl_cases.py", "-q", timeout=180)
    if counts.get("skipped", 0) < 3:
        return False, f"期望至少 3 条跳过，实际 {counts}"
    if counts.get("passed", 0) or counts.get("failed", 0):
        return False, f"默认离线不该执行 NL 用例，实际 {counts}"
    return True, f"不带 --nl：{counts.get('skipped')} skipped，没有跑到任何用例"


def ev_offline_suite_stays_green():
    """skill 承诺的 `uv run pytest` 基线。"""
    _, _, counts = pytest("-q", timeout=300)
    if counts.get("failed") or counts.get("error") or counts.get("errors"):
        return False, f"离线套件有失败：{counts}"
    if counts.get("passed", 0) < 30 or counts.get("skipped", 0) < 3:
        return False, f"结果与基线不符：{counts}"
    return True, f"{counts.get('passed')} passed, {counts.get('skipped')} skipped"


# ------------------------------------------------------------------ allure

# 一个最小的、带 allure.step 的用例。用它是为了【免费】验证步骤导出链路：
# framework/runner.py 里的 allure.step 只在付费的 NL 用例里才会跑到，
# 但"步骤有没有被导出"这件事跟付不付费无关，用一个合成用例就能守住。
STEP_PROBE = '''\
import allure


@allure.story("eval 探针")
def test_steps_survive_export():
    with allure.step("第一步 · 决策"):
        allure.attach("证据", "附件", allure.attachment_type.TEXT)
    with allure.step("第二步 · 断言"):
        assert True
'''


def ev_allure_report_is_not_empty():
    """报告静默为空是这块最容易坏的地方。

    allure-pytest ≥2.14 会写 titlePath，2.13.8 的 CLI 认不出来就丢掉全部结果，
    却照样打印 "Report successfully generated"。所以必须断言【报告里真有用例、
    真有步骤】，光看命令退出码和那句成功提示会被骗过去。
    """
    if not ALLURE_CLI:
        return False, "找不到 allure CLI"
    results = SCRATCH / "allure-results"
    report = SCRATCH / "allure-report"
    for path in (results, report):
        shutil.rmtree(path, ignore_errors=True)
    probe = SCRATCH / "test_allure_probe.py"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(STEP_PROBE, encoding="utf-8")

    _, output, counts = pytest(str(probe), f"--alluredir={results}", "-q", timeout=300)
    if counts.get("passed", 0) == 0:
        return False, f"探针用例没跑过：{output.strip()[-150:]!r}"
    generated = subprocess.run(
        [ALLURE_CLI, "generate", str(results), "-o", str(report), "--clean"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    summary_file = report / "widgets" / "summary.json"
    if generated.returncode != 0 or not summary_file.is_file():
        return False, f"生成报告失败：{(generated.stderr or generated.stdout).strip()[-150:]!r}"
    summary = json.loads(summary_file.read_text(encoding="utf-8"))
    total = (summary.get("statistic") or {}).get("total") or 0
    if not total:
        return False, "报告生成了但用例总数为 0（典型的 titlePath 不兼容）"
    # 步骤数要单独查导出的用例文件：summary 里没有这一项
    steps = 0
    for path in (report / "data" / "test-cases").glob("*.json"):
        case = json.loads(path.read_text(encoding="utf-8"))
        steps = max(steps, len((case.get("testStage") or {}).get("steps") or []))
    if not steps:
        return False, f"报告里有 {total} 条用例，但没有任何步骤——allure.step 没被导出"
    return True, f"报告含 {total} 条用例、{steps} 个步骤，步骤与附件都导出了"


# ------------------------------------------------------------------ paid

def ev_demo_case_passes():
    """公开网站上的演示用例。免费站点，不产生业务数据，但要调付费模型。"""
    _, output, counts = pytest(
        "tests/test_nl_cases.py", "--nl", "--case", "demo-weaver-site-tour", "-q", timeout=900,
    )
    if counts.get("passed"):
        return True, "演示用例通过"
    return False, f"演示用例未通过：{output.strip()[-200:]!r}"


def ev_negative_control_fails():
    """负向对照必须【因断言而失败】。它通过才说明框架在幻觉成功。

    光看"失败了"不够：环境没配好（缺 API Key）、页面打不开、浏览器掉线，
    都会让这条用例失败——那样 eval 会替一个根本没跑起来的用例背书。
    必须确认失败来自 runner 抛出的 AssertionError。
    """
    _, output, counts = pytest(
        "tests/test_nl_cases.py", "--nl", "--case", "demo-negative-missing-content", "-q", timeout=600,
    )
    if not counts.get("failed"):
        return False, f"负向对照竟然通过了——断言没有判别力：{output.strip()[-200:]!r}"
    if "AssertionError" not in output or "runner.py" not in output:
        return False, (
            "失败了，但不是断言失败——用例没真正跑到断言那一步，"
            f"这条 eval 不能据此背书：{output.strip()[-200:]!r}"
        )
    return True, "负向对照因断言而失败（框架没有幻觉成功）"


CASES = [
    ("collects-e9-dir-only", "--cases-dir 只收集该目录的用例", "free", ev_collects_e9_dir_only),
    ("case-filter-narrows-to-one", "--case 精确选中一条用例", "free", ev_case_filter_narrows_to_one),
    (
        "unknown-case-id-fails-loudly", "写错 id 时清晰报错，而不是 INTERNALERROR",
        "free", ev_unknown_case_id_fails_loudly,
    ),
    ("nl-cases-skipped-by-default", "不带 --nl 时 NL 用例全部跳过（离线契约）", "free", ev_nl_cases_skipped_by_default),
    ("offline-suite-stays-green", "uv run pytest 基线不破", "free", ev_offline_suite_stays_green),
    (
        "allure-report-is-not-empty", "Allure 报告有用例且有步骤（防静默空报告）",
        "allure", ev_allure_report_is_not_empty,
    ),
    ("demo-case-passes", "演示用例真的通过（付费）", "paid", ev_demo_case_passes),
    ("negative-control-fails", "负向对照如实失败（付费）", "paid", ev_negative_control_fails),
]


def main():
    parser = argparse.ArgumentParser(description="nl-case-run 的 evals")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--paid", action="store_true", help="连同会调付费 API 的用例一起跑")
    parser.add_argument("--only", action="append", metavar="ID")
    args = parser.parse_args()

    if args.list:
        for case_id, what, cost, _ in CASES:
            print(f"  {case_id:<34} [{cost:^6}] {what}")
        return 0

    selected = [c for c in CASES if not args.only or c[0] in set(args.only)]
    skipped = [c for c in selected if c[2] == "paid" and not args.paid]
    selected = [c for c in selected if c not in skipped]

    failures = []
    for case_id, what, cost, check in selected:
        try:
            ok, detail = check()
        except Exception as error:  # noqa: BLE001
            ok, detail = False, f"{type(error).__name__}: {error}"
        print(f"  {'PASS' if ok else 'FAIL'}  {case_id:<34} {detail}")
        if not ok:
            failures.append(case_id)

    print(f"\n{len(selected) - len(failures)}/{len(selected)} 通过")
    if skipped:
        print(f"未跑（需 --paid）：{[c[0] for c in skipped]}")
    if failures:
        print(f"失败：{failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())