"""断言层的离线契约：`detail` 必须是一句【真话】。

实测（2026-09-29）：一条全绿用例的断言汇总里写着"页面文本不应出现 '流转设定'，但出现了"
——通过时照搬了失败模板。核对报告的人（包括 AI）据此把通过误读成没通过，白跑一轮复核；
而断言汇总正是失败用例的第一手证据，它说的每句话都得能当真。

这两个 checker 是重灾区：`text_not_contains` 的"但出现了"、`element_absent` 的"但找到了"，
都是**只在失败时成立**的断言。其余 checker 写的是"期望 X，实际 Y"这类中性陈述，
通过失败都成立，故不动。
"""

from jev_ultrafast.framework import assertions


def _page(*, text="", url="https://example.test/", actions=()):
    return {"text": text, "url": url, "actions": list(actions)}


def test_text_not_contains_tells_the_truth_when_passing():
    result = assertions.check_text_not_contains(_page(text="流程名称列表"), {"value": "流转设定"}, None)
    assert result["ok"] is True
    assert "但出现了" not in result["detail"], "通过时不能说'但出现了'——报告会反着说话"
    assert "未出现" in result["detail"]


def test_text_not_contains_still_reports_the_failure():
    result = assertions.check_text_not_contains(_page(text="流转设定"), {"value": "流转设定"}, None)
    assert result["ok"] is False
    assert "但出现了" in result["detail"]


def test_element_count_role_filter_counts_only_that_role():
    """同一标题出现在两个表面时，按 role 只数列表行。

    实测（2026-10-10，E9 待办/已办）：列表里那行是 `link`，右下角「有流程到达」
    通知浮层里那条是别的角色；浮层不可关、不自动消失。不带 role 过滤会把通知一起数进去，
    于是"这条流程已离开我的待办"永远红。role 过滤不是放宽，是把话说准。
    """
    page = _page(actions=[
        {"id": "e1", "label": "jev-wf_1_tc03", "role": "button"},   # 通知浮层
    ])
    assert assertions.check_element_count(
        page, {"label_contains": "jev-wf_1_tc03", "role": "link", "equals": 0}, None)["ok"] is True
    assert assertions.check_element_count(
        page, {"label_contains": "jev-wf_1_tc03", "equals": 0}, None)["ok"] is False

    listed = _page(actions=[{"id": "e1", "label": "jev-wf_1_tc03", "role": "link"}])
    assert assertions.check_element_count(
        listed, {"label_contains": "jev-wf_1_tc03", "role": "link", "equals": 0}, None)["ok"] is False


def test_element_absent_tells_the_truth_when_passing():
    result = assertions.check_element_absent(_page(), {"label": "提交"}, None)
    assert result["ok"] is True
    assert "但找到了" not in result["detail"]


def test_element_absent_still_reports_the_failure():
    page = _page(actions=[{"id": "e1", "label": "提交", "role": "button"}])
    result = assertions.check_element_absent(page, {"label": "提交"}, None)
    assert result["ok"] is False
    assert "但找到了" in result["detail"]


def test_passing_details_never_contain_a_failure_only_phrase():
    """通例式检查：把当前所有 checker 的措辞里"只在失败时成立"的词集中在这里。

    新增 checker 时若写了这类措辞，会被这条测试提醒去分支。
    """
    failure_only = ("但出现了", "但找到了", "但存在")
    passing = [
        assertions.check_url_contains(_page(url="https://a.test/x"), {"value": "/x"}, None),
        assertions.check_url_equals(_page(url="https://a.test/x"), {"value": "https://a.test/x"}, None),
        assertions.check_text_contains(_page(text="abc"), {"value": "b"}, None),
        assertions.check_text_not_contains(_page(text="abc"), {"value": "z"}, None),
        assertions.check_element_exists(
            _page(actions=[{"id": "e1", "label": "提交", "role": "button"}]), {"label": "提交"}, None
        ),
        assertions.check_element_absent(_page(), {"label": "提交"}, None),
        assertions.check_element_value(
            _page(actions=[{"id": "e1", "label": "标题", "value": "v"}]),
            {"label": "标题", "equals": "v"}, None,
        ),
        assertions.check_element_count(
            _page(actions=[{"id": "e1", "label": "行A"}, {"id": "e2", "label": "行B"}]),
            {"label_contains": "行", "equals": 2}, None,
        ),
        assertions.check_element_enabled(
            _page(actions=[{"id": "e1", "label": "提交", "role": "button"}]), {"label": "提交"}, None
        ),
    ]
    for result in passing:
        assert result["ok"] is True, result
        for phrase in failure_only:
            assert phrase not in result["detail"], f"通过的断言却说：{result['detail']}"