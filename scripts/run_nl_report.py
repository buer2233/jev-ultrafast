"""跑一次自然语言用例，并把 Allure 报告生成到带时间戳的独立目录。

**为什么需要它**：`allure generate -o report/allure-report --clean` 会把上一次的报告整个
抹掉，于是"上次跑出来什么样"再也看不到，回归对比只能靠人记。本脚本照 api-test-E9
（同作者的 E9 接口自动化框架）的做法，把每次报告放进
`report/allure-report/<YYYYMMDD_HHMMSS>/`，旧报告一律保留。

用法（**必须带 `--env-file .env`**，模型 Key 在那里）：

    uv run --env-file .env python scripts/run_nl_report.py --case e9-wf-designer-create
    uv run --env-file .env python scripts/run_nl_report.py --case 甲 --case 乙
    uv run --env-file .env python scripts/run_nl_report.py          # 跑全部 NL 用例

除本脚本自己的开关外，参数原样透传给 pytest。退出码与 pytest 一致——用例失败脚本也失败，
但**报告照常生成**：失败那一次恰恰是最该看报告的。

逻辑的可测部分在 `jev_ultrafast/framework/reporting.py`（目录选取、命令构造、结果清理），
本文件只负责串联与执行，和 `install_report_plugin.py` 之于 `report_plugin/install.py` 同一套路。
"""

import subprocess
import sys
from pathlib import Path

# 本机 Python stdout 是 GBK（cp936）：脚本里的 `⚠️` 不在 GBK 字符集里，输出被重定向
# 到文件/管道时会把 print 打成 UnicodeEncodeError（2026-10-10 在 serve_report.py 上
# 实测同类崩溃）。编码不动（控制台里中文照常显示），只把编不出来的字符替换成 `?`。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from jev_ultrafast.framework import reporting  # noqa: E402
from jev_ultrafast.framework.report_plugin.install import install, resolve_tool  # noqa: E402


def build_pytest_command(passthrough):
    """构造 pytest 命令参数列表。

    默认补 `--reruns 1`：模型连接失败是已知的瞬时故障（AGENTS.md 里记着那条实测），
    不加的话一次网络抖动就会让整条用例白跑。调用方自己传了 `--reruns` 就以它为准。
    """
    command = [
        sys.executable, "-m", "pytest", "tests/test_nl_cases.py", "--nl",
        f"--alluredir={reporting.ALLURE_RESULTS_DIR}",
    ]
    if not any(arg == "--reruns" or arg.startswith("--reruns=") for arg in passthrough):
        command += ["--reruns", "1"]
    return command + list(passthrough)


def main(argv=None):
    passthrough = list(sys.argv[1:] if argv is None else argv)

    results_dir = reporting.ALLURE_RESULTS_DIR
    results_dir.mkdir(parents=True, exist_ok=True)

    # 先清上次的结果：allure-pytest 自己也只在会话开始清一次，而我们要保证
    # "本次结果 + 本次报告"一一对应，不把上一次的用例混进来。
    leftovers = reporting.clean_results_dir(results_dir)
    if leftovers:
        print(f"⚠️ 有 {len(leftovers)} 个旧结果没删掉（不影响本次执行）：{leftovers[:5]}")

    pytest_command = build_pytest_command(passthrough)
    print("执行：", " ".join(str(part) for part in pytest_command))
    pytest_code = subprocess.run(pytest_command, cwd=REPO_ROOT).returncode

    # 无论用例过没过都生成报告。
    report_dir = reporting.timestamped_report_dir()
    generate_command = reporting.allure_generate_command(results_dir, report_dir)
    # Windows 上 `allure` 解析出来是 allure.BAT，而 subprocess 不认没有扩展名的 .bat
    # （CreateProcess WinError 2，症状酷似"allure 没装"）。必须先过 resolve_tool。
    generate_command[0] = resolve_tool(generate_command[0])
    print("\n生成报告：", " ".join(str(part) for part in generate_command))
    if subprocess.run(generate_command, cwd=REPO_ROOT).returncode != 0:
        print("❌ allure generate 失败，本次报告未生成。")
        return pytest_code or 1

    # 插件必须在 generate 【之后】装：它改的是生成结果里的 index.html。
    # 装不上不该把一次已经跑完的执行判成失败——报告本身是好的，只是没有时间轴，
    # 而"报告能打开、只是少了插件功能"远好于"什么都没有"。
    try:
        install(report_dir)
    except Exception as error:  # noqa: BLE001
        print(f"⚠️ step-video 插件未装上（报告本身可用）：{type(error).__name__}: {error}")

    print(
        f"\n报告目录：{report_dir}\n"
        "⚠️ 不要用 file:// 直接打开 index.html —— Allure 是前端路由应用，"
        "点 Categories 之类会再去拉 data/*.json，file:// 下会被浏览器拦掉。\n"
        f'请执行：allure open "{report_dir}"\n'
        "要把报告发给别的电脑看（allure open 只绑 127.0.0.1，别人连不上）：\n"
        "  uv run python scripts/serve_report.py        # 一个地址永远指向最新一份报告"
    )
    return pytest_code


if __name__ == "__main__":
    raise SystemExit(main())