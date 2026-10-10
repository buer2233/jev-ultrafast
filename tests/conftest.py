"""自然语言用例的 pytest 接入。

职责：
  · 在【收集期之前】起一个只监听回环的 fixture 服务，供 {{ fixture_url }} 解析；
  · 把 cases/ 下的 YAML 收集成参数化用例；
  · 把 E9 环境地址注入用例，供 {{ base_url }} 解析。
"""

import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import allure
import allure_commons
import pytest
from allure_commons import model2
from allure_commons.utils import uuid4
from allure_pytest.listener import AllureListener

from jev_ultrafast.framework import e9_api, e9_config, e9_setup, e9_workflow
from jev_ultrafast.framework.loader import load_cases
from jev_ultrafast.framework.runner import resolve_report_options

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_HTML = REPO_ROOT / "jev_ultrafast" / "static" / "fixture.html"

_server = None


class _FixtureHandler(BaseHTTPRequestHandler):
    """只服务 fixture.html；只监听 127.0.0.1，不对外暴露。"""

    def do_GET(self):
        payload = FIXTURE_HTML.read_text(encoding="utf-8").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


def pytest_configure(config):
    """收集期就要能解析 {{ fixture_url }}，所以服务在这里起，而不是用 session fixture。"""
    global _server
    if os.environ.get("JEV_FIXTURE_URL") or _server is not None:
        return
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    _server = ThreadingHTTPServer(("127.0.0.1", port), _FixtureHandler)
    threading.Thread(target=_server.serve_forever, daemon=True).start()
    os.environ["JEV_FIXTURE_URL"] = f"http://127.0.0.1:{port}"


def pytest_unconfigure(config):  # noqa: ARG001  (pluggy 要求参数名与 hookspec 一致)
    global _server
    if _server is not None:
        _server.shutdown()
        _server = None


def pytest_addoption(parser):
    parser.addoption("--cases-dir", default=None, help="自然语言用例目录（默认 cases/）")
    parser.addoption(
        "--case", action="append", default=None, metavar="ID",
        help="只执行指定 id 的用例，可重复。用于本地调试，不改变用例内容。",
    )
    parser.addoption(
        "--nl", action="store_true", default=False,
        help="执行自然语言用例（会调用付费模型 API，并接管一个 Chrome 标签页）。",
    )
    # 报告层三个开关。三个都**必须是 default=None**：None 才表示"没显式传"，
    # 据此才能实现"未传用配置默认、传了用传值"（实施方案 §5.3）。
    parser.addoption(
        "--video-record", default=None, choices=["0", "1", "-1"], metavar="MODE",
        help="录屏档位：0 不记录 / 1 记录全部 / -1 仅保留失败用例的录屏（默认取配置）。",
    )
    parser.addoption(
        "--wait-stable", dest="wait_stable", action="store_true", default=None,
        help="启用页面稳定等待（默认取配置）。",
    )
    parser.addoption(
        "--no-wait-stable", dest="wait_stable", action="store_false",
        help="关闭页面稳定等待。⚠️ 可能推高【付费】决策请求数——它省下的只是免费的页面观察，"
             "却可能让「渲染中途决策 → 页面过期 → 重决策」变多。定位是调试/诊断，不是回归省钱。",
    )
    parser.addoption(
        "--wait-step-ms", dest="wait_step_ms", default=None, metavar="MS",
        help="等待耗时超过该毫秒数才独立成报告步骤（默认取配置）。",
    )


def pytest_collection_modifyitems(config, items):
    """收集后的两件事：逐用例重跑设置、以及默认跳过 NL 用例。

    默认跳过：项目契约是「测试离线、不调付费 API」（见 README 与 AGENTS.md），
    所以 `uv run pytest` 必须保持离线；NL 用例要通过 --nl 显式请求才会跑。
    """
    run_nl = bool(config.getoption("--nl")) or os.environ.get("JEV_NL_RUN") == "1"
    skip = pytest.mark.skip(reason="自然语言用例默认跳过（会调用付费 API）；用 --nl 运行")
    for item in items:
        if "nl_case" not in item.keywords:
            continue
        # 逐用例覆盖重跑次数。负向对照设计上就该失败，重跑只是白跑一遍。
        case = getattr(getattr(item, "callspec", None), "params", {}).get("nl_case")
        if not isinstance(case, dict):
            # 参数化列表为空时 pytest 会塞一个 NotSet 占位对象进来
            # （例如 cases/ 为空）。它不是用例，直接 .get 会炸成 INTERNALERROR，
            # 把"没有匹配的用例"报成 pytest 内部错误。
            continue
        if case.get("reruns") is not None:
            item.add_marker(pytest.mark.flaky(reruns=int(case["reruns"])))
        if not run_nl:
            item.add_marker(skip)


def pytest_generate_tests(metafunc):
    """把 YAML 用例收集成参数化用例。

    用 pytest_generate_tests 而不是 @pytest.mark.parametrize：
    用例是运行时从 YAML 读的，数量与内容都不在代码里，装饰器要静态列表。
    ids 固定用 case["id"]，这样 Allure 历史趋势与 -k 选择都稳定。
    """
    if "nl_case" not in metafunc.fixturenames:
        return

    cases = load_cases(metafunc.config.getoption("--cases-dir"))
    selected = metafunc.config.getoption("--case")
    if selected:
        wanted = list(dict.fromkeys(selected))
        available = {c["id"] for c in cases}
        unknown = [case_id for case_id in wanted if case_id not in available]
        # 写错的 id 必须【响亮地】失败。以前这里是静默过滤成空列表，
        # pytest 会塞一个 NotSet 占位、随后在 modifyitems 里炸成 INTERNALERROR，
        # 报错完全看不出是 id 写错了。
        if unknown:
            raise pytest.UsageError(
                f"--case 指定的用例不存在：{unknown}；当前可用：{sorted(available) or '（没有加载到任何用例）'}"
            )
        cases = [c for c in cases if c["id"] in set(wanted)]

    base_url = e9_config.base_url()
    for case in cases:
        case["_base_url"] = base_url
        # 报告层三个开关在这里解析，而不是在 runner 里：只有这里拿得到 pytest config
        # （`metafunc.config`），而 pytest 参数要到 pytest_configure 之后才可用。
        # 逐用例解析一次，与 `_base_url` 同一套路。
        # 顺带一个好处：值写错（如档位非法）在【收集期】就报错，不会跑到一半才炸。
        case["_report_options"] = resolve_report_options(metafunc.config, case)

    metafunc.parametrize("nl_case", cases, ids=[c["id"] for c in cases])


@pytest.fixture(scope="session")
def e9_base_url():
    return e9_config.base_url()


@pytest.fixture(scope="class")
def eb_mode(e9_base_url):
    """class 级前置：在 E9 上建一个建模模块（EB 表单），本类用例跑完即回收。

    **为什么是 class 级**：建这条数据要一次管理员接口登录 + 一次写接口，成本远高于
    单个用例本身；而同一类的用例共用同一个表单是合理的——它只是个可关联的壳。
    回收放在 finally，保证用例失败也不留垃圾数据。

    **为什么名字是常量而不是返回值**：用例 goal 里的 `{{ eb_mode_name }}` 在
    **收集期**就被替换掉了，而这个 fixture 要到**执行期**才跑，两者不可能通信。
    所以双方都引用 `e9_api.EB_MODE_NAME` 同一个常量（loader 已把它注册成变量）。

    前置数据走接口而不走 UI 是刻意的：让 agent 去点后台把这条数据建出来，既会吃掉
    用例的步数预算，又会让"前置没建好"和"被测功能有问题"混成同一个失败原因。
    """
    if not e9_base_url:
        pytest.skip("未配置 E9 环境（E9_BASE_URL / config.json），跳过需要前置数据的用例")

    session = e9_api.admin_session(e9_base_url)
    modeid = e9_api.create_mode(session, e9_base_url, modename=e9_api.EB_MODE_NAME)
    try:
        yield modeid
    finally:
        e9_api.delete_mode(session, e9_base_url, modeid)


@pytest.fixture
def nl_setup(nl_case, e9_base_url):
    """按用例声明的 `setup:` 准备前置数据（**走接口**，见 framework/e9_setup.py）。

    为什么前置走接口而不是让 agent 在 UI 里建：源功能用例的前置常常是
    「人员01 建 → 两人会签 → 审批 → 归档」这样的跨人多节点链条，
    让 agent 去点会把用例的步数预算耗在与被测目标无关的准备上，
    而且"前置没建好"和"被测功能有问题"会混成同一个失败原因。

    **三种结局要分清**（这正是把它做成 fixture 而不是写在用例里的原因）：
      · 环境没搭这条流程路径 → `skip`，并说清缺什么（是环境没准备好）；
      · 前置准备过程本身出错 → **报错**，用例不会跑（是环境/配置有问题，不是被测功能有问题）；
      · 前置备好 → 把产出附进 Allure，用例照常跑。
    """
    name = nl_case.get("setup") or "none"
    if name == "none":
        yield {}
        return

    if not e9_base_url:
        pytest.skip("未配置 E9 环境（E9_BASE_URL / config.json），跳过需要前置数据的用例")

    request_name = nl_case.get("_wf_request_name") or ""
    try:
        facts = e9_setup.run(name, e9_base_url, request_name)
    except e9_workflow.WorkflowPathMissing as error:
        pytest.skip(f"前置 {name} 无法准备（环境缺流程路径）：{error}")

    # 前置产出挂进报告：用例失败时，第一件要判断的是"前置到底建出来没有"。
    allure.attach(
        f"前置：{name}\n流程实例名：{request_name}\n产出：{facts}",
        "前置数据",
        allure.attachment_type.TEXT,
    )
    yield facts


# --------------------------------- 共用测试基建 ---------------------------------

@pytest.fixture
def real_reporter():
    """挂一个真的 `AllureListener` 进插件管理器，让"真契约"测试**离线**也能跑。

    为什么这样能代表真实情况：`allure.step().__enter__` 走的是
    `plugin_manager.hook.start_step(...)`，而带 `@allure_commons.hookimpl` 的
    是 **listener 而不是 reporter**（`allure_pytest/listener.py:46-54`）——
    reporter 只是被 listener 显式调用的普通类。要让钩子真的响就必须注册 listener，
    而被测的那段代码与真实会话**完全一致**。

    `config=None`：`start_step` / `stop_step` 都不碰 `self.config`（它只被那几个
    `pytest_*` 钩子用），所以传 None 是安全的；那几个 pytest 钩子在 allure 自己的
    插件管理器里没有对应 hookspec，不会被调用。

    **还要自己造一个"父容器"**：`AllureReporter.start_step` 只在
    `_last_executable()` 找得到当前测试时才把步骤放进 `_items`，找不到就丢进
    `_orphan_items`（`reporter.py:72-78`），那时 `get_item` 返回 None。
    真实会话里容器由 listener 的 `pytest_runtest_protocol` 建，
    离线跑没有 pytest 侧的那一半，所以要 `schedule_test` 一个 `TestResult` 补上。

    这样安排的好处很实在：**"步骤参数与时间真的落到了对象上"可以离线断言**，
    不必花一次付费运行去验。
    """
    listener = AllureListener(config=None)
    allure_commons.plugin_manager.register(listener)
    reporter = listener.allure_logger

    container = str(uuid4())
    # 用 model2.TestResult 而不是 from ... import TestResult：后者会让 pytest 以为
    # 这是个待收集的测试类（名字以 Test 开头），报 PytestCollectionWarning。
    reporter.schedule_test(container, model2.TestResult(name="契约测试容器", uuid=container))
    yield reporter
    reporter.close_test(container)
    allure_commons.plugin_manager.unregister(listener)