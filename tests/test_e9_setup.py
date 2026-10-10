"""Contract tests for the named-precondition layer (`e9_setup` + `e9_workflow` + loader hook).

Why these exist: the whole "one UI case = one login identity" design rests on a
**collection-time / run-time handshake** that nothing else guards. The request
name is computed during collection (loader), the precondition is built during
execution (fixture), and the two never talk at run time. If the formula drifts,
the symptom is "the case cannot find the request it just created" — which looks
like a product bug and costs an hour to trace back. So the formula is pinned here.

Everything is offline: the API layer is exercised through a stubbed `_call`, so
no E9 environment and no credentials are needed.
"""

import re
import textwrap
from pathlib import Path

import pytest

from jev_ultrafast.framework import e9_setup, e9_workflow
from jev_ultrafast.framework.loader import CaseError, load_cases

CASES_YAML = """
version: 1
cases:
  - id: t-one
    name: 一号
    url: "{{ base_url }}/a"
    setup: wf-approval2
    goal: 找到「{{ wf_request_name }}」
    expect:
      - type: text_not_contains
        value: "{{ wf_request_name }}"
      - type: element_count
        label_contains: 流程
        min: 2
  - id: t-two
    name: 二号
    url: "{{ base_url }}/b"
    goal: 找到「{{ wf_request_name }}」
    expect:
      - type: url_contains
        value: /b
"""


def _load(tmp_path, body=CASES_YAML):
    (tmp_path / "cases.yaml").write_text(textwrap.dedent(body), encoding="utf-8")
    return load_cases(tmp_path)


def test_request_name_is_unique_per_case(tmp_path):
    """Same file, same collection -> each case gets **its own** instance name.

    Not cosmetic: two cases sharing a name create two identically-titled request
    instances, and "open the one I just made" then resolves to whichever the
    list happens to return first.
    """
    cases = _load(tmp_path)
    names = [case["_wf_request_name"] for case in cases]
    assert len(set(names)) == len(names), f"实例名不唯一：{names}"
    for case, name in zip(cases, names, strict=True):
        assert case["id"] in name


def test_request_name_prefix_is_ascii_only():
    """实例名前缀**不许出现中文字**——它与系统自动标题必须一眼分得开。

    实测（2026-10-09，TC03）：接口建的实例名原来叫「UI自动化流程提交_…」，
    而 E9 给 UI 自建实例自动生成的标题叫「UI自动化流程提交-AutoTest001-2026-10-09」，
    **前七个字一模一样**。同一条待办列表里躺着两条这样的流程，goal 要模型"逐字对上"，
    模型连着两轮都挑走了那条自动标题——进去签了别人的流程。
    在 goal 里加"要带下划线的那条、别要连字符那条"也没救回来。

    所以前缀改成纯 ASCII，让两者**零重叠**。这条测试防的是"有人觉得中文更友好又改回去"。
    """
    prefix = e9_setup.REQUEST_NAME_PREFIX
    assert prefix.isascii(), f"实例名前缀不能含中文（会与自动标题混淆）：{prefix!r}"
    # 顺带确认它确实不等于那条自动标题的前缀
    assert not e9_setup.WORKFLOW_NAME.startswith(prefix)


def test_loader_and_setup_share_one_formula(tmp_path):
    """The loader and the precondition layer must derive the same name.

    They cannot exchange the value at run time (goal is substituted during
    collection), so both call `e9_setup.request_name_for`. This test is what
    makes "call the same function" a requirement rather than a convention.
    """
    case = _load(tmp_path)[0]
    run_id = case["_wf_request_name"].removeprefix(f"{e9_setup.REQUEST_NAME_PREFIX}_")
    run_id = run_id.rsplit("_", 1)[0]
    assert e9_setup.request_name_for(run_id, case["id"]) == case["_wf_request_name"]


def test_variables_substitute_inside_expect(tmp_path):
    """`expect` strings get substituted too — a draft-only detail that is easy to lose.

    The assertion "the list does not contain the request I just made" is only
    meaningful if the name inside it is the same one the fixture created.
    """
    case = _load(tmp_path)[0]
    assert "{{" not in case["expect"][0]["value"]
    assert case["expect"][0]["value"] == case["_wf_request_name"]


def test_non_string_assertion_fields_are_left_alone(tmp_path):
    """Substitution must not coerce numbers — `min: 2` has to stay an int.

    `_substitute_deep` walks dicts and lists; a sloppy implementation would
    stringify every leaf and turn `min` into `"2"`, which the checker then
    compares against a count.
    """
    case = _load(tmp_path)[0]
    assert case["expect"][1]["min"] == 2
    assert isinstance(case["expect"][1]["min"], int)


def test_path_name_is_a_separate_variable():
    """The workflow TEMPLATE name and the request INSTANCE title are two names.

    Clicking a flow in "新建流程" takes the **template** name (a Chinese label the
    environment was built with). The **instance title** only exists in todo/done
    lists, and only when something wrote it there — the API does that when a
    fixture creates the request, but a case that builds its own instance in the UI
    has to type it into the 标题 field itself.

    Mixing them up is invisible until run time, and then it reads as "the flow
    isn't in the list" instead of "you searched for the wrong name".
    """
    from jev_ultrafast.framework.loader import _default_variables

    variables = _default_variables()
    assert variables["wf_path_name"], "wf_path_name 没被注入"
    instance = e9_setup.request_name_for(variables["run_id"], "t-one")
    # 只断言"是两个不同的值"。**不要**额外断言 path_name 不是 instance 的子串——
    # 实例名前缀恰好含「流程提交」这四个字，那是个巧合，不是不变量。
    assert variables["wf_path_name"] != instance


def test_ui_built_cases_reference_the_template_name():
    """In `workflow_submit.yaml`, a `setup: none` case must name the TEMPLATE.

    Those cases build their own request through the UI, so nothing has written an
    instance title into E9 yet — a goal telling the agent to click
    `{{ wf_request_name }}` in the 新建流程 list asks it to find a name that cannot
    be there. Pinned because it is exactly the mistake this layer invites.

    **必须扫 YAML 原文，不能扫 `load_cases()` 的返回值**：加载期已经把
    `{{ wf_path_name }}` 替换成真实名字了，拿替换后的 goal 去找那个变量名，
    当然找不到——这条断言会**永远失败**。（同一个坑 `nl-case-author/evals/README.md`
    记过两次，这里是第三次。）

    Scoped to that one file on purpose: the demo cases run against a public site,
    and the older E9 cases hardcode their own flow name — neither follows this
    convention, and flagging them would be noise, not signal.
    """
    raw = (Path(__file__).resolve().parents[1] / "cases" / "e9" / "workflow_submit.yaml").read_text(
        encoding="utf-8"
    )
    # 按 `- id:` 切成一条条的原文块，逐块判。
    blocks = re.split(r"\n  - id: ", raw)[1:]
    offenders = []
    for block in blocks:
        case_id = block.split("\n", 1)[0].strip()
        # `setup:` 声明在 goal 之前；没写就是默认的 none。
        # 取值后**允许跟行尾注释**（`setup: wf-approval2   # 前置：…`），
        # 所以不能拿 `$` 收尾——那样会把带前置的用例误判成 none（踩过一次：
        # 五条带前置的用例全被当成 none，然后因为没写 wf_path_name 而误报）。
        head = block[: block.find("goal:")]
        match = re.search(r"^\s*setup:\s*([^\s#]+)", head, re.MULTILINE)
        if (match.group(1) if match else "none") != "none":
            continue
        # ⚠️ 判据是 **`{{ wf_workflow_name }}`**（路径的全名），不是 `{{ wf_path_name }}`。
        # 后者只是给接口侧子串检索用的短关键字（"流程提交"），而"新建流程"那个列表
        # 里有几百条流程——goal 写"名字**含**某几个字"等于请模型凑合。
        # 实测（2026-10-09，TC01）：模型因此点成了另一条路径（workflowId=8025≠12522），
        # 整条用例作废。所以这里要求的是**能逐字对上**的那个变量。
        if "{{ wf_workflow_name }}" not in block:
            offenders.append(case_id)
    assert not offenders, (
        "setup: none 的用例要在「新建流程」列表里点名那条路径，用 {{ wf_workflow_name }}"
        "（全名，逐字匹配），不要用 {{ wf_path_name }}（子串关键字）：" + "；".join(offenders)
    )


def test_ui_built_cases_claim_the_auto_title_not_the_injected_one():
    """`setup: none` 的用例**不能**用 `{{ wf_request_name }}` 认领自己那条实例。

    这是上一版用例的直接错误。`wf_request_name` 是**我们自己造的名字**，
    由接口在 `setup:` 里写进实例标题——而 `setup: none` 的用例是 **UI 自己建**的实例，
    那台环境的「标题」字段是**只读**的（证据见 `e9_setup.system_request_name`），
    我们填的名字**从来没进去过**。所以拿它断言，页面里永远找不到。

    认领要用 `{{ wf_system_request_name }}`：系统按 `<路径名>-<登录名>-<日期>`
    自动生成的那个标题。

    ⚠️ 必须扫 **YAML 原文**：`load_cases()` 已经把变量替换成真实名字了，
    拿替换后的 expect 去找变量名当然找不到——那条断言会永远失败。
    （同一个坑 nl-case-author/evals/README.md 记过两次，这是第三次。）
    """
    raw = (Path(__file__).resolve().parents[1] / "cases" / "e9" / "workflow_submit.yaml").read_text(
        encoding="utf-8"
    )
    offenders = []
    for block in re.split(r"\n  - id: ", raw)[1:]:
        case_id = block.split("\n", 1)[0].strip()
        head = block[: block.find("goal:")]
        match = re.search(r"^\s*setup:\s*([^\s#]+)", head, re.MULTILINE)
        if (match.group(1) if match else "none") != "none":
            continue   # 走接口建实例的用例用 wf_request_name 是对的
        # 只看 expect 段：goal 里提不提是措辞问题，断言里用错才是真错。
        expect = block[block.find("expect:"):] if "expect:" in block else ""
        if "{{ wf_request_name }}" in expect:
            offenders.append(case_id)
    assert not offenders, (
        "setup: none 的用例用的是 UI 自建的实例，标题是系统自动生成的，"
        "断言里要用 {{ wf_system_request_name }}：" + "；".join(offenders)
    )


def test_system_request_name_follows_the_measured_formula():
    """自动标题的公式是**实测**出来的，不是猜的——所以把它钉住。

    实测（2026-10-09，三条流程给出一致形状）：
        UI自动化流程提交-AutoTest001-2026-10-09
        自由流程2-AutoTest001-2026-10-09
        BYM专用测试-AutoTest001-2026-10-09
    公式一旦漂移（比如路径改过名），`setup: none` 那两条的断言会静默失去意义
    （找不到 ≠ 不存在，是**假失败**；换成别的措辞还可能变成假通过），
    所以这里连分隔符一起断言。
    """
    name = e9_setup.system_request_name("AutoTest001", today="2026-10-09")
    assert name == f"{e9_setup.WORKFLOW_NAME}-AutoTest001-2026-10-09"


def test_system_request_name_is_not_the_injected_one():
    """两个名字必须**长得不一样**，否则"用错了"在报告里看不出来。

    它们都含同一个前缀（路径名），很容易互相冒充；分不出差别时，
    用例断言失效只会表现为"列表里找不到"，看着像环境问题。
    """
    injected = e9_setup.request_name_for("1009150724", "e9-wf-submit-tc01")
    system = e9_setup.system_request_name("AutoTest001", today="2026-10-09")
    assert injected != system
    # 分隔符不同：注入的用下划线，系统生成的用连字符。这是**可观察**的差别，
    # 人扫一眼待办列表就能认出哪条是接口建的、哪条是 UI 建的。
    assert "_" in injected and "-" in system


def test_misspelled_setup_field_is_rejected(tmp_path):
    """A typo'd key must fail loudly at load time, not silently do nothing."""
    body = CASES_YAML.replace("setup: wf-approval2", "setap: wf-approval2")
    with pytest.raises(CaseError, match="未知字段"):
        _load(tmp_path, body)


def test_case_declared_skip_carries_its_reason_into_the_report(tmp_path):
    """`skip:` 必须把**理由原文**带进报告，而不是变成一个光秃秃的 SKIP。

    这条来自 2026-10-09 的真事：TC06 的改写前提（"不填必填项提交会被拦"）在目标环境上
    不成立——「密级」有默认值，不填直接提交**会成功**。与其把它改成别的负向凑绿
    （那等于偷偷换掉它在测什么），不如让它 SKIP 并把理由摆在报告里。
    所以"理由必须传下去"是这条功能的**全部意义**，钉住它。
    """
    body = CASES_YAML.replace(
        "    setup: wf-approval2",
        "    setup: wf-approval2\n    skip: 环境上必填项有默认值，这条前提不成立",
    )
    case = _load(tmp_path, body)[0]
    assert "必填项有默认值" in case["_skip"], case.get("_skip")


def test_a_case_without_skip_still_runs(tmp_path):
    """反向：没有 `skip:` 的用例**不能**被顺带跳过。

    `skip` 是布尔陷阱的高发地——`if case.get("skip")` 对空串/None 都该是假，
    而写成 `if "skip" in case` 会把每条用例都跳过，症状是"全绿"却什么都没跑。
    """
    case = _load(tmp_path)[0]
    assert not case.get("_skip"), f"没用例声明 skip，却被跳过了：{case.get('_skip')!r}"


def test_none_setup_touches_nothing():
    """`setup: none` must not reach for the network or need a base URL."""
    assert e9_setup.run("none", "", "") == {}


def test_unknown_setup_name_names_the_alternatives():
    """A bad `setup:` value should say what the valid ones are."""
    with pytest.raises(e9_setup.SetupError, match="未知的前置名"):
        e9_setup.run("wf-nope", "http://example.invalid", "x")


def test_registry_covers_every_name_used_by_cases():
    """Every `setup:` value shipped in `cases/` resolves to a registered recipe.

    This is the guard that keeps a case from being written against a
    precondition nobody implemented — it would only surface as a confusing
    failure when someone finally ran it.
    """
    used = {case.get("setup") for case in load_cases() if case.get("setup")}
    unknown = used - set(e9_setup.SETUPS)
    assert not unknown, f"用例声明了没实现的前置：{sorted(unknown)}"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"code": "SUCCESS"}, True),
        ({"code": {"name": "SUCCESS"}}, True),
        ({"code": {"statusCode": 1}}, True),
        ({"code": "PARAM_ERROR"}, False),
        ({"code": None}, False),
        ({}, False),
        ("not-a-dict", False),
    ],
)
def test_success_detection_accepts_every_observed_shape(payload, expected):
    """The PA interfaces do not agree on the shape of `code`; all observed forms count.

    Measured on the sibling project: some endpoints answer `code: "SUCCESS"`,
    others `code: {statusCode: 1}` or `code: {name: "SUCCESS"}`. Accepting only
    one shape turns a working call into a false failure.
    """
    assert e9_workflow._is_success(payload) is expected


def _fake_form(fields):
    """一个假的 `getCreateWorkflowRequestInfo` 响应。"""
    return {"data": {"workflowMainTableInfo": {"requestRecords": [{"workflowRequestTableFields": fields}]}}}


def test_main_data_skips_reserved_and_attachment_fields(monkeypatch):
    """`requestname`/`requestlevel`/`messageType` 与附件字段都不能进 `mainData`.

    Measured on the real environment (2026-09-30): including the attachment field
    makes the create fail with `field_fj: 字段未设置附件上传目录` — most templates
    have no upload directory configured, so it can never succeed. The reserved
    names are rejected as bad parameters outright.

    Convention copied from the sibling project's *passing* implementation
    (`test_workflow_r349363.py`), not guessed.
    """
    monkeypatch.setattr(
        e9_workflow,
        "_call",
        lambda *a, **k: _fake_form(
            [
                {"fieldName": "requestname", "fieldHtmlType": "1", "isMand": True},
                {"fieldName": "requestlevel", "fieldHtmlType": "1", "isMand": True},
                {"fieldName": "messageType", "fieldHtmlType": "1", "isMand": True},
                {"fieldName": "fj", "fieldHtmlType": "6", "isMand": True},
                {"fieldName": "标题", "fieldHtmlType": "1", "isMand": True},
            ]
        ),
    )
    main_data = e9_workflow.build_main_data(object(), "http://example.invalid", 1)
    names = [item["fieldName"] for item in main_data]
    assert names == ["标题"], f"应只剩普通字段，实际 {names}"


def test_main_data_falls_back_to_all_fields_when_nothing_is_mandatory(monkeypatch):
    """No `isMand` anywhere -> use **all** fields rather than an empty list.

    `isMand` is not trustworthy: on workflow 345 the API reports zero mandatory
    fields while the form still refuses to save until 「密级」 is set. Filtering by
    `isMand` alone therefore yields `mainData: []`, and the create is rejected with
    `新建流程主表数据不允许为空` — a failure whose message points nowhere near the
    real cause.
    """
    monkeypatch.setattr(
        e9_workflow,
        "_call",
        lambda *a, **k: _fake_form(
            [
                {"fieldName": "a", "fieldHtmlType": "1"},
                {"fieldName": "b", "fieldHtmlType": "2"},
            ]
        ),
    )
    main_data = e9_workflow.build_main_data(object(), "http://example.invalid", 1)
    assert [item["fieldName"] for item in main_data] == ["a", "b"]


def test_main_data_uses_a_legal_option_for_select_fields(monkeypatch):
    """Select fields need a value from their own option list, not a made-up string.

    A free-text placeholder on a dropdown field is the kind of thing the server
    accepts sometimes and rejects other times; using `selectvalues[0]` is what the
    proven implementation does.
    """
    monkeypatch.setattr(
        e9_workflow,
        "_call",
        lambda *a, **k: _fake_form(
            [{"fieldName": "密级", "fieldHtmlType": "2", "selectvalues": ["公开资源2", "内部"]}]
        ),
    )
    main_data = e9_workflow.build_main_data(object(), "http://example.invalid", 1)
    assert main_data[0]["fieldValue"] == "公开资源2"


def test_main_data_env_override_wins(monkeypatch):
    """`E9_WF_MAINDATA` short-circuits the whole derivation (escape hatch)."""
    monkeypatch.setenv("E9_WF_MAINDATA", '[{"fieldName":"x","fieldValue":"y"}]')

    def explode(*_a, **_k):
        raise AssertionError("有显式 mainData 时不该再去查表单定义")

    monkeypatch.setattr(e9_workflow, "_call", explode)
    assert e9_workflow.build_main_data(object(), "http://example.invalid", 1) == [
        {"fieldName": "x", "fieldValue": "y"}
    ]


def test_missing_workflow_path_is_a_distinct_error(monkeypatch):
    """`WorkflowPathMissing` is what lets the fixture SKIP instead of FAIL.

    "The environment has no such workflow path" and "the code is broken" need
    different handling, so they must not be the same exception.
    """
    monkeypatch.setattr(
        e9_workflow,
        "_call",
        lambda *a, **k: {"data": [{"workflowName": "别的流程", "workflowId": 7}]},
    )
    with pytest.raises(e9_workflow.WorkflowPathMissing, match="没有名字含"):
        e9_workflow.find_workflow_id(object(), "http://example.invalid", "流程提交")


def test_workflow_lookup_matches_by_keyword(monkeypatch):
    """A name hit returns the numeric id, not the raw value."""
    monkeypatch.setattr(
        e9_workflow,
        "_call",
        lambda *a, **k: {"data": [{"workflowName": "流程提交-自动化", "workflowId": "78"}]},
    )
    found = e9_workflow.find_workflow_id(object(), "http://example.invalid", "流程提交")
    assert found == 78
    assert isinstance(found, int)


def test_submit_sends_workflow_id_alongside_request_id(monkeypatch):
    """`submitRequest` must carry `workflowId` — omitting it is a documented trap.

    Some e-cology builds reject the call with `{code: PARAM_ERROR,
    errMsg: {errorParam_requestid: 0}}`: the message blames `requestid`, but the
    field actually missing is `workflowId`. Version that do not require it
    tolerate the extra one, so it is always sent.
    """
    captured = {}

    def fake_call(session, base_url, path, **kwargs):
        captured["path"] = path
        captured["payload"] = kwargs.get("json_body")
        return {"code": "SUCCESS"}

    monkeypatch.setattr(e9_workflow, "_call", fake_call)
    e9_workflow.submit_request(object(), "http://example.invalid", 11373, 78)

    assert captured["path"] == e9_workflow.SUBMIT_PATH
    assert captured["payload"]["requestId"] == 11373
    assert captured["payload"]["workflowId"] == 78
    assert captured["payload"]["otherParams"]["src"] == "submit"

# ── 「流转到哪了」的判据：待办归属，不是节点名 ──────────────────────────────
#
# 这一组守的是 2026-10-09 实测到的一个陷阱：`getWorkflowRequest` 的
# `currentNodeName`（以及 `getRequestStatus` 的 `currentNodeId`）**会报错**——
# 两条 `status` 完全相同的流程，一条报「审批2」、另一条报「创建1」，
# 而后者那条的待办确实已经在审批2 的两个会签人手里。
#
# 前置配方原来拿节点名当判据，于是**配置全对也全线失败**，报出
# 「应到「审批2」实际停在「创建1」」这种把人引向路径配置的错误方向。


def test_request_ids_normalises_both_response_shapes(monkeypatch):
    """E9 有接口回裸数组、有接口回 `{data: [...]}`；id 也有 requestId/requestid 两种拼法。"""
    monkeypatch.setattr(
        e9_workflow, "_call",
        lambda *a, **k: [{"requestId": 11}, {"requestid": "22"}, {"requestId": None}],
    )
    assert e9_workflow.request_ids_of(object(), "http://x", "u") == {"11", "22"}

    monkeypatch.setattr(e9_workflow, "_call", lambda *a, **k: {"data": [{"requestid": 33}]})
    assert e9_workflow.request_ids_of(object(), "http://x", "u") == {"33"}


def test_in_done_passes_the_handled_flag(monkeypatch):
    seen = {}

    def fake_call(_s, _b, path, *, form=None, params=None, timeout=30):
        seen["path"], seen["form"] = path, form
        return []

    monkeypatch.setattr(e9_workflow, "_call", fake_call)
    e9_workflow.in_done(object(), "http://x", "u", 7)
    assert seen["path"] == e9_workflow.DONE_PATH
    assert seen["form"]["isHandled"] == "true"


def test_create_and_submit_judges_by_todo_not_by_node_name(monkeypatch):
    """**核心回归线。** 节点名报错也要能通过——判据必须是"待办里有没有它"。"""
    monkeypatch.setattr(e9_setup, "POLL_INTERVAL_S", 0)  # 离线测试不等真实时钟
    monkeypatch.setattr(
        e9_workflow, "wait_for_node",
        lambda *a, **k: "创建1",  # 故意报一个错的节点名
    )
    monkeypatch.setattr(e9_workflow, "create_request", lambda *a, **k: 4242)
    monkeypatch.setattr(e9_workflow, "in_todo", lambda _s, _b, login, rid: login == "AutoTest002")

    ctx = e9_setup._Context("http://x", "R")
    ctx.workflow_id = lambda: 78  # 跳过列表查找
    ctx.session = lambda role: object()
    monkeypatch.setattr(e9_setup.e9_config, "load_account",
                        lambda role: {"user_name": {"employee2": "AutoTest002",
                                                    "employee3": "AutoTest003"}[role]})
    # 只有 employee2 有待办 → 会签第二人缺失，必须报错而不是静默通过
    with pytest.raises(e9_setup.SetupError) as err:
        ctx.create_and_submit()
    assert "employee3" in str(err.value)
    # 报错要把人往「密级」这个真实方向上引，而不是只说"节点不对"
    assert "密级" in str(err.value)


def test_await_todo_rejects_a_role_string_instead_of_a_tuple(monkeypatch):
    """`await_todo` 只收**角色序列**；传字符串会按字符迭代，必须当场炸掉。

    这一条来自 2026-10-09 的真事：`wf-draft` 里写成 `ctx.await_todo(ROLE_REQUESTER, …)`
    （少了一对括号），于是它拿着 'e'、'm'、'p'… 去查账号，报的是
    **"config.json 中缺少角色 'e' 的有效凭据"**——报错指向配置，
    根因却在少了那一对括号，完全指不回来。

    这里钉住的是**症状的形状**：角色名不合法时，报错里要出现那个单字符，
    而不是静默地"找不到就当没有"。
    """
    monkeypatch.setattr(e9_setup, "POLL_INTERVAL_S", 0)

    def fake_load(role):
        if len(str(role)) < 2:
            raise RuntimeError(f"config.json 中缺少角色 {role!r} 的有效凭据")
        return {"user_name": role}

    monkeypatch.setattr(e9_setup.e9_config, "load_account", fake_load)
    monkeypatch.setattr(e9_workflow, "in_todo", lambda *a, **k: False)

    ctx = e9_setup._Context("http://x", "R")
    ctx.session = lambda role: object()
    with pytest.raises(RuntimeError, match="角色 'e'"):
        ctx.await_todo("employee1", 1)


def test_await_todo_passes_when_every_role_has_it(monkeypatch):
    monkeypatch.setattr(e9_workflow, "in_todo", lambda *a, **k: True)
    monkeypatch.setattr(e9_setup.e9_config, "load_account", lambda role: {"user_name": role})
    ctx = e9_setup._Context("http://x", "R")
    ctx.session = lambda role: object()
    ctx.await_todo(("employee2", "employee3"), 1)  # 不抛即通过


# ── 角色映射：不要让用例引用一个"环境里根本没有"的账号 ──────────────────────
#
# 这一组来自 2026-10-09 的真事：源用例里有「人员01~06」六个人，而目标环境只建了
# 五个账号。TC05/TC08 的 `login: employee6` 于是会一路"正常收集、正常执行"，
# 直到**登录那一步才失败**——报错是"登录失败"，看不出缺的是环境而不是用例。
# 而 e9_setup 里那几个 ROLE_* 一旦指向不存在的角色，前置配方也会栽在同一个地方。

CASES_DIR = Path(__file__).resolve().parents[1] / "cases"
EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config.example.json"


def _roles_in_example_config():
    import json

    return set(json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8")))


def _logins_used_by_cases():
    """扫 **YAML 原文**取 login / setup。

    ⚠️ 不要扫 `load_cases()` 的产出——加载期会把 `{{ 变量 }}` 替换掉，
    拿替换后的内容做规则检查是本仓库踩过两次的坑（见 nl-case-author 的 evals README）。
    """
    import re as _re

    logins = set()
    for path in sorted(CASES_DIR.rglob("*.yaml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            found = _re.match(r"\s*login:\s*(\S+)", line)
            if found:
                logins.add(found.group(1))
    return logins


def test_every_login_role_exists_in_the_example_config():
    """用例写到的角色，模板配置里必须有个位置——否则换环境时没人知道要配什么。"""
    declared = _roles_in_example_config()
    used = _logins_used_by_cases()
    missing = sorted(used - declared - {"none"})
    assert not missing, f"用例引用了 config.example.json 里没有的角色：{missing}"


def test_setup_roles_exist_in_the_example_config():
    """`ROLE_*` 是**用例之外**的引用点：前置配方自己登录，不经过用例的 login 字段。

    2026-10-09 的教训就是这个——`ROLE_ARCHIVER = "employee6"` 指向了不存在的账号，
    而它不在任何用例的 `login:` 里，所以上面那条测试看不见它。
    """
    declared = _roles_in_example_config()
    roles = {
        "ROLE_REQUESTER": e9_setup.ROLE_REQUESTER,
        "ROLE_ARCHIVER": e9_setup.ROLE_ARCHIVER,
        **{f"ROLE_COUNTERSIGN[{i}]": r for i, r in enumerate(e9_setup.ROLE_COUNTERSIGN)},
        **{f"ROLE_APPROVER[{i}]": r for i, r in enumerate(e9_setup.ROLE_APPROVER)},
    }
    missing = {name: role for name, role in roles.items() if role not in declared}
    assert not missing, f"e9_setup 的角色指向了模板里没有的账号：{missing}"


def test_archiver_reuses_an_existing_role_rather_than_a_sixth_account():
    """归档那一角**刻意**由链条末端账号兼——环境只有 5 个账号。

    这条不是在锁死"必须兼"，而是钉住"**别悄悄改回一个不存在的第 6 个账号**"。
    真要补出第 6 个账号，改 ROLE_ARCHIVER、config.example.json、TC05/TC08 三处，
    那时这条测试会提醒你一起改。
    """
    assert e9_setup.ROLE_ARCHIVER in {
        *e9_setup.ROLE_COUNTERSIGN, *e9_setup.ROLE_APPROVER, e9_setup.ROLE_REQUESTER
    }, "ROLE_ARCHIVER 必须指向一个链条上已有的角色（本环境没有第 6 个账号）"
