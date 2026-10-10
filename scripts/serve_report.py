"""把生成的 Allure 报告用 HTTP 共享给同一网络里的其它电脑（对方用 IP 打开）。

**为什么要有它**：`allure open` 和 IDE 内置服务器（IDEA 的 63342 端口）都**只绑
127.0.0.1**，别的电脑连不上；`file://` 直接开 index.html 又会被浏览器拦掉
（Allure 是前端路由应用，点 Categories 之类会再拉 data/*.json）。
报告本来就是纯静态站点，只差一个绑到 `0.0.0.0` 的 HTTP 服务——本脚本补的就是这一步。

用法（Ctrl+C 停止；地址会打印出来）：

    uv run python scripts/serve_report.py                    # 跟随最新（推荐）：一个地址永远指向最新一份
    uv run python scripts/serve_report.py 20261010_150431    # 钉住某一份（目录名或完整路径）
    uv run python scripts/serve_report.py --port 9000

两种模式的区别：**跟随最新**服务的是报告根目录，`/` 每次请求都跳到当时最新的一份——
同事存一个地址，以后新跑的报告他们刷新就能看到（旧的仍可按 `/时间戳/index.html` 打开）；
**钉住某一份**适合"这次的结果固定发给别人看"，启动那一刻解析一次、之后不再变。

⚠️ 同事打不开时先看防火墙：Windows 防火墙默认拦入站，本机网络若归在「公用」配置下
   尤其如此。看到询问弹窗就勾上「公用网络」再允许；没有弹窗的话，实现者（管理员）
   可按 `--help` 末尾的命令放行端口。这一条在脚本输出里也会再提醒一次。
⚠️ 报告里有内网地址与页面截图，只把它开在该看的人所在的网络里。

可测的部分在 `jev_ultrafast/framework/report_share.py`（目录解析 / 地址发现 / 静态服务），
本文件只负责串联与打印，和 `run_nl_report.py` 之于 `framework/reporting.py` 同一套路。
"""

import argparse
import sys
from pathlib import Path

# 这台机器（中文 Windows）的 Python stdout 是 GBK（cp936，实测）：`⚠`/`❌` 之类
# 不在 GBK 字符集里的字符会把 print 打成 UnicodeEncodeError——本脚本第一版就这么
# 崩过一次。所以①消息里用 GBK 认得的字符（★＝、注意：）；②再给两个流兜底一层，
# 路径里真带出来生僻字符也只是替换成 `?`，不至于崩。编码**不动**，控制台里中文照常显示。
# line_buffering：输出被重定向到文件/管道时默认是块缓冲，横幅会卡在缓冲里不出现
# （实测只看到请求日志、看不到地址）——服务类脚本的"我在哪、地址是什么"必须立刻可见。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace", line_buffering=True)

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from jev_ultrafast.framework import report_share  # noqa: E402

FIREWALL_NOTE = (
    "注意：同事打不开时先查防火墙——它默认拦入站（网络配置为「公用」时尤其如此）。\n"
    "      看到询问弹窗就勾「公用网络」再允许；没有弹窗就按 --help 末尾的命令放行（要管理员）。"
)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="把生成的 Allure 报告共享给局域网里的其它电脑（对方用 IP 打开）。",
        epilog=(
            "防火墙放行（管理员 PowerShell；端口换成 --port 的值）：\n"
            '  New-NetFirewallRule -DisplayName "jev-allure-report" -Direction Inbound '
            "-Action Allow -Protocol TCP -LocalPort 8899 -Profile Any\n"
        ),
    )
    parser.add_argument(
        "report", nargs="?",
        help="报告目录名（report/allure-report 下）或完整路径；给了＝钉住这一份，不给＝跟随最新",
    )
    parser.add_argument("--port", type=int, default=8899, help="监听端口（缺省 8899）")
    parser.add_argument("--host", default="0.0.0.0", help="绑定地址（缺省 0.0.0.0＝所有网卡）")
    args = parser.parse_args(argv)

    latest_name = ""  # 只有"跟随最新"模式会用到
    if args.report:
        follow_latest = False
        try:
            directory = report_share.resolve_report_dir(args.report)
        except FileNotFoundError as error:
            print(f"错误：{error}")
            return 1
    else:
        follow_latest = True
        directory = report_share.report_root()
        try:
            # 先确认"至少有一份报告"再启动：带着一个注定 404 的服务站开门没有意义
            latest_name = report_share.resolve_report_dir().name
        except FileNotFoundError as error:
            print(f"错误：{error}")
            return 1

    server = report_share.serve(directory, host=args.host, port=args.port, follow_latest=follow_latest)
    port = server.server_address[1]
    if follow_latest:
        print(f"报告根目录：{directory}")
        print(f"当前最新：{latest_name}（/ 每次请求都会跳到当时最新的一份）")
    else:
        print(f"报告目录：{directory}")
    print(f"本机打开：http://127.0.0.1:{port}/index.html")

    if args.host in ("0.0.0.0", "::"):
        addresses = report_share.lan_ipv4_addresses()
        if addresses:
            print("局域网地址（★＝默认出口网卡，优先把这个发给同事）：")
            for index, address in enumerate(addresses):
                mark = "★" if index == 0 else " "
                print(f"  {mark} http://{address}:{port}/index.html")
        else:
            print("注意：没发现可分享的局域网地址（本机没有非回环 IPv4？）")
        if follow_latest:
            print("（同事存一个地址就行：以后新跑的报告，他们刷新即是最新；旧的按 /时间戳/index.html 打开）")
        print(FIREWALL_NOTE)
    else:
        print(f"注意：绑在 {args.host}：只有本机能连（要分享给别的电脑就别传 --host）")

    print("Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())