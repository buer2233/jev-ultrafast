"""自然语言用例的 pytest 入口。

一条 YAML 用例 → 一个 pytest 用例 → 一个 Allure story。
用例标题来自运行时读到的 YAML，因此必须用 allure.dynamic.*（导入期还没有这些数据）。
"""

import allure
import pytest

from jev_ultrafast.framework import reporting, run_case


@pytest.mark.nl_case
def test_natural_language_case(nl_case, nl_setup):  # noqa: ARG001  (nl_setup 靠依赖关系生效)
    """执行一条自然语言用例。

    第二个参数 `nl_setup` 不做别的，只靠**依赖关系**保证它先跑：
    它按用例声明的 `setup:` 走接口把前置数据备好（见 conftest.py）。
    前置没备好时它要么 skip、要么直接报错，**不会**让用例带着坏前置去跑。

    重新发起时用 pytest-rerunfailures 做【用例级】重跑：
        uv run pytest tests/test_nl_cases.py --reruns 1
    刻意不做步骤级重试——浏览器变更操作重试可能重复提交（见 AGENTS.md）。
    """
    if nl_case.get("_skip"):
        pytest.skip(nl_case["_skip"])
    allure.dynamic.epic(nl_case.get("epic", "E9-UI自动化"))
    allure.dynamic.feature(nl_case.get("feature", "未分类"))
    allure.dynamic.story(nl_case["name"])
    allure.dynamic.title(nl_case["name"])
    allure.dynamic.description(nl_case["goal"])
    if nl_case.get("severity"):
        allure.dynamic.severity(nl_case["severity"])
    allure.dynamic.tag(*nl_case.get("tags", []))
    allure.dynamic.parameter("用例来源", nl_case.get("__source__", ""))
    allure.dynamic.parameter("登录角色", nl_case.get("login", "none"))
    allure.dynamic.parameter("断言合成", nl_case.get("expect_mode", "and"))

    reporting.attach_environment(nl_case)
    run_case(nl_case)