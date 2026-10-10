"""`framework/report_share.py` 的离线单测：目录解析、地址排序、静态站点本身。

全部只碰回环地址（127.0.0.1）：测试若按默认值绑 0.0.0.0，每次 `uv run pytest`
都会弹 Windows 防火墙询问框——那种打扰会让"离线自检"这条纪律没人愿意执行，
所以"默认绑所有网卡"这条契约用签名断言钉住，真实绑定行为在回环上验。
"""

import http.client
import inspect
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from jev_ultrafast.framework import report_share

ROOT = Path(__file__).resolve().parents[1]


def _make_report(root, name, *, with_index=True):
    directory = root / name
    directory.mkdir(parents=True)
    if with_index:
        (directory / "index.html").write_text(f"<html>报告 {name}</html>", encoding="utf-8")
    return directory


@pytest.fixture
def running_server():
    """工厂 fixture：起一个**只绑回环**的服务器（避开防火墙弹窗），用完逐个关掉。"""
    servers = []

    def start(directory, *, follow_latest=False):
        server = report_share.serve(directory, host="127.0.0.1", port=0, follow_latest=follow_latest)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def local_opener():
    """禁用系统/环境代理的 opener：本机有 Clash（7890），走代理会把贴回环的请求变成 502
    （本仓库踩过这个坑，见记忆里的 loopback proxy trap）。这里测的是"服务器答不答"。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ------------------------- 报告目录解析 -------------------------


def test_resolve_report_dir_defaults_to_latest(tmp_path):
    _make_report(tmp_path, "20260101_000000")
    _make_report(tmp_path, "20260102_000000")
    # 同秒退让出来的 `_001` 后缀（timestamped_report_dir 的产物）要排在更后
    latest = _make_report(tmp_path, "20260102_000000_001")

    assert report_share.resolve_report_dir(root=tmp_path) == latest


def test_resolve_report_dir_skips_dirs_without_index(tmp_path):
    report = _make_report(tmp_path, "20260101_000000")
    _make_report(tmp_path, "20260103_000000", with_index=False)  # 名字更大但不是报告

    assert report_share.resolve_report_dir(root=tmp_path) == report


def test_resolve_report_dir_accepts_name_or_full_path(tmp_path):
    target = _make_report(tmp_path, "20260101_000000")

    assert report_share.resolve_report_dir("20260101_000000", root=tmp_path) == target
    assert report_share.resolve_report_dir(str(target), root=tmp_path) == target


def test_resolve_report_dir_explains_what_is_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="20200101_000000"):
        report_share.resolve_report_dir("20200101_000000", root=tmp_path)
    with pytest.raises(FileNotFoundError, match="run_nl_report"):
        report_share.resolve_report_dir(root=tmp_path)


# ------------------------- 局域网地址发现 -------------------------


def test_lan_ipv4_addresses_orders_primary_first_and_filters_junk():
    """默认出口网卡排最前；回环、link-local、重复项都不进列表。"""
    addresses = report_share.lan_ipv4_addresses(
        gather=lambda: ["192.168.1.5", "127.0.0.1", "169.254.9.9", "10.1.2.3", "192.168.1.5"],
        primary_probe=lambda: "10.1.2.3",
    )

    assert addresses == ["10.1.2.3", "192.168.1.5"]


def test_lan_ipv4_addresses_keeps_going_when_probe_fails():
    """探测不出默认出口（隔离网段）时，其余网卡的地址照常给。"""
    addresses = report_share.lan_ipv4_addresses(gather=lambda: ["192.168.1.5"], primary_probe=lambda: "")
    assert addresses == ["192.168.1.5"]

    # 探测到回环说明没有真网卡，同样滤掉
    assert report_share.lan_ipv4_addresses(gather=lambda: [], primary_probe=lambda: "127.0.0.1") == []


def test_lan_ipv4_addresses_never_returns_loopback_or_link_local():
    """真调用一次真实网卡（不断言具体值——开发机网卡不可预期，只验过滤规则）。"""
    for address in report_share.lan_ipv4_addresses():
        assert not address.startswith("127.")
        assert not address.startswith("169.254.")


# ------------------------- 静态站点本身 -------------------------


def test_serve_defaults_bind_all_interfaces():
    """默认绑 0.0.0.0 是"别的电脑能连"的核心契约（见模块 docstring 为何不真绑）。"""
    assert inspect.signature(report_share.serve).parameters["host"].default == "0.0.0.0"


def test_serve_serves_the_report_dir_over_http(tmp_path, running_server, local_opener):
    base = running_server(_make_report(tmp_path, "20260101_000000"))

    with local_opener.open(f"{base}/", timeout=10) as response:  # 根 = 报告根，直接得到 index.html
        body = response.read().decode("utf-8")
        assert response.status == 200
        assert "20260101_000000" in body
        assert response.headers["Cache-Control"] == "no-store"

    with pytest.raises(urllib.error.HTTPError) as missing:
        local_opener.open(f"{base}/data/missing.json", timeout=10)
    assert missing.value.code == 404


def test_follow_latest_redirects_root_to_the_newest_report(tmp_path, running_server, local_opener):
    older = _make_report(tmp_path, "20260101_000000")
    newer = _make_report(tmp_path, "20260102_000000")
    base = running_server(tmp_path, follow_latest=True)

    with local_opener.open(f"{base}/index.html", timeout=10) as response:  # urllib 默认跟随 302
        assert response.status == 200
        assert f"报告 {newer.name}" in response.read().decode("utf-8")
        assert response.url == f"{base}/{newer.name}/index.html"

    # 旧报告仍可按时间戳直接打开：昨天发给同事的链接不作废
    with local_opener.open(f"{base}/{older.name}/index.html", timeout=10) as response:
        assert f"报告 {older.name}" in response.read().decode("utf-8")


def test_follow_latest_picks_up_reports_created_while_running(tmp_path, running_server, local_opener):
    """这条是"跟随最新"的核心契约：服务一直开着，新跑一轮报告后，
    同一个地址**刷新即是最新**——不需要重启服务、不需要换链接。"""
    _make_report(tmp_path, "20260101_000000")
    base = running_server(tmp_path, follow_latest=True)
    with local_opener.open(f"{base}/index.html", timeout=10) as response:
        assert "20260101_000000" in response.read().decode("utf-8")

    _make_report(tmp_path, "20260102_000000")  # 服务开着的时候跑出了一份新报告
    with local_opener.open(f"{base}/index.html", timeout=10) as response:
        assert "20260102_000000" in response.read().decode("utf-8")


def test_follow_latest_redirect_is_well_formed_for_clients_that_read_bodies(tmp_path, running_server):
    """302 必须带 `Content-Length: 0`：HTTP/1.1 下"没有长度、也不关连接"的响应会让
    curl 这类**真要读正文**的客户端一直等（2026-10-10 实测整分钟无响应）。
    urllib 跟随跳转时不读正文，所以这条契约只能直接按 HTTP 对话来钉。"""
    report = _make_report(tmp_path, "20260101_000000")
    base = running_server(tmp_path, follow_latest=True)
    host, port = base.removeprefix("http://").split(":")

    connection = http.client.HTTPConnection(host, int(port), timeout=10)
    try:
        connection.request("GET", "/index.html")
        response = connection.getresponse()
        assert response.status == 302
        assert response.getheader("Location") == f"/{report.name}/index.html"
        assert response.getheader("Content-Length") == "0"
        # 跳转不许被缓存：缓存住的"最新"就是旧的（而且要恰好一条，别发重复头）
        assert response.getheader("Cache-Control") == "no-store"
        assert response.read() == b""  # 立刻读到空正文，而不是"等连接自己关"
    finally:
        connection.close()


def test_follow_latest_404s_when_there_is_nothing_to_serve(tmp_path, running_server, local_opener):
    base = running_server(tmp_path, follow_latest=True)

    with pytest.raises(urllib.error.HTTPError) as missing:
        local_opener.open(f"{base}/index.html", timeout=10)
    assert missing.value.code == 404


# ------------------------- 控制台编码（本机 cp936 的坑） -------------------------


def test_script_survives_a_gbk_console():
    """本机 Python stdout 是 GBK（cp936）：`⚠` 这类字符不在 GBK 字符集里，
    会把 print 直接打成 UnicodeEncodeError——本脚本第一版实操崩过一次。
    这里用不存在的报告名走报错分支（不起服务），在 PYTHONIOENCODING=gbk 下
    必须把话说完、以退出码 1 结束，而不是半路抛异常。"""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "serve_report.py"), "20200101_000000"],
        capture_output=True, text=True, encoding="gbk", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "gbk"}, cwd=ROOT, timeout=60,
    )
    assert result.returncode == 1, result.stderr
    assert "UnicodeEncodeError" not in result.stderr
    assert "找不到报告目录" in result.stdout