"""把生成的 Allure 报告共享给局域网里的其它电脑：找报告、找本机地址、起静态站点。

**为什么需要这一层**（2026-10-10 实测，用户问"能不能换成别人也能访问的 IP"）：

- 报告是**纯静态站点**（index.html + data/*.json + 已装的 step-video 插件）；
- 但两种常见的打开方式都**只绑回环地址**：`allure open` 与 IDE 内置服务器
  （IDEA 的 63342 端口）都只监听 127.0.0.1——别的电脑连不上；
- `file://` 直接开 index.html 同样不行：Allure 是前端路由应用，点 Categories
  之类会再拉 data/*.json，file:// 下会被浏览器拦掉。

所以这里只做几件可测的小事，`scripts/serve_report.py` 负责串联与打印：

- `resolve_report_dir()`：把"没给 / 给了目录名 / 给了完整路径"统一成一份报告目录；
- `lan_ipv4_addresses()`：本机可分享的 IPv4，**默认出口网卡排最前**；
- `serve()`：把目录绑到 `0.0.0.0:<port>` 的静态 HTTP 服务（多线程、禁缓存），
  `follow_latest=True` 时服务的是**报告根目录**、`/` 每次请求都跳到**此刻最新**的一份
  （同事只记一个地址，以后新跑的报告刷新就能看到；旧的仍可按 `/时间戳/index.html` 打开）。

⚠️ 报告里有内网地址与页面截图，只把它开在**该看的人**所在的网络里。
"""

import functools
import http.server
import socket
from pathlib import Path

from . import reporting


def resolve_report_dir(value=None, root=None):
    """把「报告目录名 / 完整路径 / 什么都不给」解析成一个存在的报告目录。

    - 什么都不给：取 `report/allure-report/` 下**名字最大**的一份（目录名是
      `YYYYMMDD_HHMMSS`，字典序即时间序；`timestamped_report_dir()` 的 `_001`
      后缀同样排在同秒的前一份之后）；
    - 只认**含 index.html 的目录**：报告根目录下也可能有别的目录，
      不是报告的不该被当成报告；
    - 找不到时把话说清楚，并提示先跑 `scripts/run_nl_report.py`。

    Args:
        value: 报告目录名（相对 `report/allure-report/`）、完整路径，或 None。
        root: 报告根目录（测试用）；缺省 `report/allure-report`。

    Returns:
        Path: 存在的报告目录。

    Raises:
        FileNotFoundError: 指名要的目录不存在，或根目录下还没有任何报告。
    """
    base = Path(root or reporting.ALLURE_REPORT_ROOT)
    if value:
        named = Path(value)
        if named.is_dir():
            return named
        candidate = base / value
        if candidate.is_dir():
            return candidate
        raise FileNotFoundError(f"找不到报告目录：{value}（{base} 下也没有这个名字）")
    candidates = []
    if base.is_dir():
        candidates = sorted(
            (entry for entry in base.iterdir() if entry.is_dir() and (entry / "index.html").is_file()),
            key=lambda entry: entry.name,
        )
    if not candidates:
        raise FileNotFoundError(f"{base} 下还没有生成的报告，先跑 scripts/run_nl_report.py")
    return candidates[-1]


def report_root():
    """报告根目录（`report/allure-report`）——跟随最新模式服务的正是它。"""
    return Path(reporting.ALLURE_REPORT_ROOT)


def _usable_ipv4(address):
    """回环（127.*）与 link-local（169.254.*，APIPA）对别人没有意义，滤掉。

    虚拟网卡（Hyper-V / WSL / VPN）的地址**不做猜测性识别**：这类网卡各式各样，
    猜错不如原样列出、把"默认出口网卡"标出来——选哪个能通，本机主人比脚本清楚。
    """
    return bool(address) and not address.startswith("127.") and not address.startswith("169.254.")


def _local_ipv4_candidates():
    """本机所有 IPv4（原样收集，不判可用性；多网卡机器会有好几个）。解析失败就空手回来。"""
    try:
        return [info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        return []


def _default_route_ipv4():
    """默认出口网卡上的地址。

    UDP connect **不发任何包**，只是让系统按路由表挑一张网卡；没有默认路由
    （纯隔离网段）时返回空串。这个地址通常就是"同事能 ping 通的那个"。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 53))
            return probe.getsockname()[0]
    except OSError:
        return ""


def lan_ipv4_addresses(*, gather=None, primary_probe=None):
    """可分享的局域网 IPv4 列表：默认出口网卡排最前，其余按发现顺序去重跟进。

    `gather` / `primary_probe` 只为单测注入（开发机上有一堆虚拟网卡，
    真值不可预期，而"排序与过滤规则"才是要钉住的契约）。
    """
    gather = gather or _local_ipv4_candidates
    primary_probe = primary_probe or _default_route_ipv4
    ordered = []
    for address in [primary_probe(), *gather()]:
        if _usable_ipv4(address) and address not in ordered:
            ordered.append(address)
    return ordered


class _ReportHandler(http.server.SimpleHTTPRequestHandler):
    """报告静态站点。只改两处行为，MIME 判断与目录索引逻辑一字未动。"""

    # HTTP/1.1 + keep-alive：报告首页一次要拉几十个 data/*.json，
    # HTTP/1.0 每拉一个都重开一条连接，同事那边会明显慢。
    protocol_version = "HTTP/1.1"

    def end_headers(self):
        # 报告目录名带时间戳、同一份内容不会再变；禁缓存是为了"换一份报告刷新就见到"，
        # 也免得中间有什么代理把旧报告缓存住。这行只动响应头，不影响任何读取逻辑。
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class _ReportsRootHandler(_ReportHandler):
    """报告根目录视图：`/` 跳到**此刻最新**的一份，`/<时间戳>/…` 照常静态服务。

    关键差别是"谁来找最新"：钉住模式在**启动时**解析一次；这里在**每次请求**时解析，
    所以服务一直开着、报告一直新出，分享出去的那一个地址永远指向最新的那轮结果。
    旧报告仍可按 `/<时间戳>/index.html` 直接打开——旧分享链接不作废。
    """

    def do_GET(self):  # noqa: N802 —— BaseHTTPRequestHandler 的接口名就是驼峰
        if self.path in ("/", "/index.html"):
            try:
                latest = resolve_report_dir(root=self.directory)
            except FileNotFoundError:
                # ⚠️ 状态行（第二个参数）只能是 latin-1，写中文会在服务线程里抛
                # UnicodeEncodeError（2026-10-10 被单测抓出来）；中文放第三个参数，
                # 那是 HTML 正文，按 UTF-8 编码，安全。
                self.send_error(
                    404, "No reports yet",
                    "report/allure-report 下还没有任何报告——先跑 scripts/run_nl_report.py",
                )
                return
            self.send_response(302)
            self.send_header("Location", f"/{latest.name}/index.html")
            # 跳转必须不缓存（一两秒的旧缓存都会让"最新"卡住）——这条由 _ReportHandler
            # 的 end_headers 统一加，这里不用再发一遍（发两遍会在响应头里出现两条）。
            # ⚠️ 必须显式给 0 长度：HTTP/1.1 下"没有 Content-Length 又不关连接"的空响应，
            # 会让 curl 这类**真要读正文**的客户端一直等到超时（2026-10-10 实测：整分钟无响应；
            # urllib 跟随跳转时不读正文，所以单测差点漏掉它——见 test_..._well_formed_...）。
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().do_GET()


def serve(directory, *, host="0.0.0.0", port=8899, follow_latest=False):
    """把目录作为静态站点绑到 host:port，返回**尚未 serve_forever** 的服务器对象。

    - `follow_latest=False`：把 `directory` 本身当报告根，`/` 就是这份报告；
    - `follow_latest=True`：`directory` 必须是**报告根目录**（`report_root()`），
      `/` 每次请求重定向到当时最新的那一份（见 `_ReportsRootHandler`）。

    多线程（ThreadingHTTPServer）：浏览器会并发拉几十个 data/*.json，单线程会排队等。
    绑 `0.0.0.0`（而不是 127.0.0.1）正是为让别的电脑能连——它同时暴露在**所有网卡**上，
    所以防火墙放行是使用前要确认的一步（放行命令见 `scripts/serve_report.py --help`）。
    """
    handler_class = _ReportsRootHandler if follow_latest else _ReportHandler
    handler = functools.partial(handler_class, directory=str(directory))
    return http.server.ThreadingHTTPServer((host, port), handler)