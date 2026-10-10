"""Offline contracts for a dynamic operation/target policy. No paid APIs."""

import json
import time
from copy import deepcopy
from unittest.mock import Mock

import httpx
import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast import model
from jev_ultrafast.browser import StalePage, browser_operation, fingerprint


def page():
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search",
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Open Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


def choice(ids, selected):
    return {"choice": selected, "confidence": 1.0, "probabilities": {i: float(i == selected) for i in ids}}


def decision(action="e1"):
    return {
        "choice": action,
        "operation": "TYPE_TEXT",
        "target": "1",
        "confidence": 1.0,
        "probabilities": {action: 1.0},
        "latency_ms": 10,
        "usage": {},
    }


class StubResponse:
    """足够像 httpx.Response 的最小替身：只用到 status_code / is_error / json()。"""

    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.is_error = status_code >= 400
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def test_step_budget_is_the_case_s_own_not_the_library_default():
    """用例的 `max_steps` 必须真的生效，而不是被库里的默认值截断。

    实测（2026-09-25）：用例写 `max_steps: 90`，框架层也按 90 解析了，但库里的那道墙
    读的是模块常量 `questions.MAX_STEPS`（60），于是**第 60 步抛 ValueError** 把用例打成
    broken，报的还是"demo budget"——和用例里那个数字对不上，排查时看不出是同一件事。
    这一条钉住：预算是**实例属性**，调用方能覆盖；库自己的默认值不变（demo.py 仍按 60）。
    """
    from jev_ultrafast.questions import MAX_STEPS as library_default

    agent = loop.Agent.__new__(loop.Agent)
    agent.max_steps = 90
    assert agent.max_steps != library_default
    # 签名默认值仍是库那个常量：演示/单测不传时行为一字不改
    # （全是关键字参数，所以没有位置默认值，__defaults__ 就是 None）
    assert loop.Agent.__init__.__defaults__ is None
    assert loop.Agent.__init__.__kwdefaults__["max_steps"] is library_default
    # 非法值响亮报错，不静默退化成某个默认预算
    for bad in (0, -1, None):
        with pytest.raises(ValueError, match="max_steps"):
            loop.Agent.__new__(loop.Agent) and loop.Agent(
                "about:blank", "noop", max_steps=bad)


def test_transport_failure_is_retried_like_a_429(monkeypatch):
    """连接失败与 429 同类：都发生在任何浏览器动作之前，所以都该重试。

    实测（2026-09）：演示用例的首次尝试就死在连接失败上，靠 pytest 的用例级
    重跑才通过——那次失败本可以在这一层自愈。
    """
    attempts = []

    def post(*_args, **_kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("connection refused")
        return StubResponse(200, {"model": "test"})

    monkeypatch.setattr(model, "CLIENT", Mock(post=post))
    monkeypatch.setattr(model.time, "sleep", lambda _seconds: None)
    assert model.post_json("https://example.test", "key", {}) == {"model": "test"}
    assert len(attempts) == 2


def test_transport_retries_are_recorded_so_a_slow_decision_explains_itself(monkeypatch):
    """传输层的重试要留痕——否则"这次为什么花了 47 秒"只能靠猜。

    实测（2026-09-25）：两条几十秒的决策，`重发次数`（决策层重发）都是 1、
    请求体还是全表最小的，报告里只有一个异常大的耗时数字。真相是客户端超时 25s，
    超时后 post_json 静默重发了一次（25 + 0.5 + ≈22 ≈ 47.5s）。
    这一条钉住：每次 HTTP 尝试的结果都进 trace，并出现在决策结果里。
    """
    calls = []

    def post(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("timed out")
        return StubResponse(200, {"model": "test", "answers": {}})

    monkeypatch.setattr(model, "CLIENT", Mock(post=post))
    monkeypatch.setattr(model.time, "sleep", lambda _seconds: None)
    trace = []
    assert model.post_json("https://example.test", "key", {}, trace=trace)["model"] == "test"
    assert [entry["结果"] for entry in trace] == ["传输层失败：ReadTimeout", "成功"]


def test_transport_failure_is_bounded_and_still_reported(monkeypatch):
    """重试有上限，且最终如实抛出——持续故障不能被重试掩盖成别的错误。"""
    attempts = []

    def post(*_args, **_kwargs):
        attempts.append(1)
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(model, "CLIENT", Mock(post=post))
    monkeypatch.setattr(model.time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="Model connection failed"):
        model.post_json("https://example.test", "key", {})
    assert len(attempts) == 3


@pytest.mark.parametrize("mutation", ["unknown", "nan", "missing", "negative", "non_max", "confidence"])
def test_invalid_choice_is_rejected(mutation):
    a = choice(["a", "b"], "a")
    if mutation == "unknown":
        a["choice"] = "invented"
    elif mutation == "nan":
        a["probabilities"]["a"] = float("nan")
    elif mutation == "missing":
        del a["probabilities"]["b"]
    elif mutation == "negative":
        a["probabilities"]["b"] = -1
    elif mutation == "non_max":
        a["choice"] = "b"
    else:
        a["confidence"] = 5
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.validate_choice(a, {"a", "b"})


def test_one_index_per_node_with_operation_specific_targets():
    elements, targets, controls = model.action_space(page()["actions"])
    assert len(elements) == 2
    assert elements[0]["operations"] == ["TYPE_TEXT", "CLICK"]
    assert targets["TYPE_TEXT"]["1"]["id"] == "e1"
    assert targets["CLICK"]["1"]["id"] == "e2"
    assert targets["CLICK"]["2"]["id"] == "e3"
    assert "WAIT" in controls


def test_all_heads_are_one_request_and_only_matching_head_executes(monkeypatch):
    calls = []

    def post(_url, _key, body, **_extra):
        calls.append(body)
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "TYPE_TEXT"),
                "type_text_target": choice(["1"], "1"),
                "click_target": {"choice": "invented"},
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(page(), "Find a book", [])
    assert len(calls) == 1
    assert d["operation"] == "TYPE_TEXT" and d["target"] == "1" and d["choice"] == "e1"
    assert set(calls[0]["questions"]) == {"operation", "click_target", "type_text_target"}


def test_click_cannot_consume_a_text_target(monkeypatch):
    def post(_url, _key, body, **_extra):
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "type_text_target": choice(["1"], "1"),
                "click_target": choice(["1", "2", "999"], "999"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.choose(page(), "Find a book", [])


def test_unusable_response_is_retried_because_nothing_was_executed(monkeypatch):
    """响应不合法时重发【决策】。重发的是只读请求，不是浏览器变更操作。

    实测（2026-09）TypeSafe 偶尔返回自相矛盾的响应：choice 选了 DONE(0.37)，
    而 CLICK 的概率更高(0.38)，validate_choice 拒收，整条用例就此死掉——尽管那时
    浏览器一动没动。5 次运行里撞到 1 次。
    """
    calls = []

    def post(_url, _key, body, **_extra):
        calls.append(body)
        if len(calls) == 1:
            # 第一次：choice 指向一个不存在的操作，validate_choice 会拒收
            return {"model": "test", "answers": {"operation": {"choice": "invented"}}}
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "click_target": choice(["1", "2"], "2"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(page(), "Press Go", [])
    assert len(calls) == 2
    assert d["choice"] == "e3" and d["operation"] == "CLICK"
    # 重发必须在结果里留痕，否则偶发的服务端不一致会被静默自愈掩盖
    assert d["decision_attempts"] == 2


def test_retry_is_bounded_and_persistent_failure_stays_visible(monkeypatch):
    """持续不合法就如实抛出，不被重试掩盖成静默成功；重发次数有上限。"""
    calls = []

    def post(_url, _key, body, **_extra):
        calls.append(body)
        return {"model": "test", "answers": {"operation": {"choice": "invented"}}}

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.choose(page(), "Press Go", [])
    assert len(calls) == model.DECISION_ATTEMPTS


def test_target_head_receives_control_state_and_full_next_step_rules(monkeypatch):
    p = page()
    p["actions"].insert(0, {
        "id": "toggle", "kind": "click", "label": "Free cancellation", "node": 30,
        "role": "checkbox", "checked": "true", "selected": False,
    })

    def post(_url, _key, body, **_extra):
        questions = body["questions"]
        target = questions["click_target"]
        assert target["criteria"]["1"]["checked"] == "true"
        assert target["criteria"]["1"]["selected"] is False
        assert questions["operation"]["instructions"]["rules"] in target["instructions"]["rules"]
        return {
            "model": "test",
            "answers": {
                "operation": choice(questions["operation"]["criteria"], "CLICK"),
                "click_target": choice(target["criteria"], "3"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(p, "Search with free cancellation", [])
    assert d["choice"] == "e3"


def test_quoted_task_text_still_uses_the_llm(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(return_value={"choices": [{"message": {"content": '{"text":"Zurich"}'}}]})
    monkeypatch.setattr(model, "post_json", post)
    context = model.field_context('Fly from "Zurich" to London', page()["actions"][0], page(), [])
    assert model.field_text(context)[0] == "Zurich"
    assert post.call_count == 1
    sent = json.loads(post.call_args.args[2]["messages"][1]["content"])
    assert sent["goal"] == 'Fly from "Zurich" to London'


def test_missing_text_credential_stops_before_guessing(monkeypatch):
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TEXT_MODEL_API_KEY"):
        model.field_text({"goal": 'Enter "Zurich"'})


@pytest.fixture
def runner():
    a = loop.Agent.__new__(loop.Agent)
    a.screenshots = False
    a.pending_text = None
    # 手工搭的 Agent 绕过了 __init__，预算得自己给——它是实例属性而不是模块常量
    # （用例的 max_steps 要能覆盖库的默认值，见 test_step_budget_is_the_case_s_own...）
    from jev_ultrafast.questions import MAX_STEPS
    a.max_steps = MAX_STEPS
    p = page()
    a.state = {
        "browser": Mock(fresh=Mock(return_value=True), observe=Mock(return_value=p)),
        "page": p,
        "decision": decision(),
        "goal": "Find a book",
        "history": [],
        "decisions": [],
        "status": "predicted",
        "started_at": time.perf_counter(),
        "record": False,
        "text_calls": [],
    }
    return a


def test_stale_decision_is_consumed_before_any_mutation(runner):
    runner.state["browser"].fresh.return_value = False
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_not_called()
    assert runner.state["decision"] is None


def test_generated_text_reused_only_for_identical_retry_context(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 1
    assert runner.state["browser"].act.call_count == 2  # The first call rejects before any browser input.
    assert runner.pending_text is None


def test_changed_field_context_does_not_reuse_generated_text(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["page"]["text"] = "Different page context"
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 2


def test_discarded_attempt_is_recorded_and_does_not_count_as_progress(runner, monkeypatch):
    """被拒的尝试要留痕，但【不能】进 history。

    留痕是给模型看的：少了它，模型看到的"最近动作"里只有成功，于是每轮都在同一个局面上
    从零推理、每轮重选同一个被拒的目标（实测空转 87 步）。
    不进 history 是给预算与判定看的：history 同时是步数预算和"最近三次无变化即 blocked"
    的输入，混进去会让空转自己吃满预算、并污染那个判定。
    """
    monkeypatch.setattr(loop, "field_text", Mock(return_value=("book", {"model": "t", "latency_ms": 1})))
    runner.state["browser"].act.side_effect = StalePage("Target changed or is covered. Observe again.")
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})

    assert len(runner.state["discards"]) == 1
    recorded = runner.state["discards"][0]
    # reason 原样带上：模型得知道是"被遮挡"还是"页面已变"，两者对策不同。
    assert recorded["reason"] == "Target changed or is covered. Observe again."
    assert recorded["kind"] in {"fill", "click"}
    assert runner.state["history"] == []


def test_no_op_action_is_recorded_as_a_discard(runner, monkeypatch):
    """「执行了、但页面毫无变化」要进 discards，同时照旧进 history。

    这是被拒之外的**第二种白费的尝试**。以前它完全不留痕：进 history（算成功），
    模型只在 recent_actions 里多看到一条 page_changed:false，读不出"这条路走不通"——
    实测（2026-09-24，E9 添加路径弹窗）模型连点三次同一个图标按钮，劝住它的不是反馈，
    而是"连续三条无变化即 blocked"的守卫。守卫是兜底，不该充当反馈。

    进 history 也不能省：history 是步数预算与那个连续无变化判定的输入，
    把这类尝试挪出去会让"空转"不再被守卫看见。
    """
    monkeypatch.setattr(loop, "field_text", Mock(return_value=("book", {"model": "t", "latency_ms": 1})))
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})

    assert len(runner.state["history"]) == 1
    assert runner.state["history"][0]["page_changed"] is False
    assert len(runner.state["discards"]) == 1
    recorded = runner.state["discards"][0]
    assert recorded["reason"] == "Executed, but nothing on the page changed."
    # target_level=True：拒绝来自目标【本身】，与 TargetUnavailable 同类，
    # 所以够阈值就该从候选里剔除；而不是"页面刚好在动"那种瞬时的、重选无妨的状况。
    assert recorded["target_level"] is True
    assert (recorded["node"], recorded["kind"]) == (10, "fill")


def test_waiting_is_not_recorded_as_a_discard(runner):
    """wait 本来就不改变页面，记进 discards 会把正常等待误判成死路。"""
    runner.state["decision"] = decision("wait")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})

    assert runner.state["history"][0]["kind"] == "wait"
    assert runner.state.get("discards", []) == []


def test_repeated_no_ops_withhold_that_target_from_the_choices(runner, monkeypatch):
    """同一个目标"执行了但没用"到阈值次数后，就不该再出现在候选里。

    按 (node, kind) 记账，不是只按 node：同一元素上"点击没用"不代表"填值也没用"
    （e2 与 e1 同节点不同 kind，必须留下）。
    """
    monkeypatch.setattr(loop, "field_text", Mock(return_value=("book", {"model": "t", "latency_ms": 1})))
    for _ in range(model.REFUSED_TARGET_THRESHOLD):
        runner.state["decision"] = decision()
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
        runner.state["decision"] = None

    assert len(runner.state["discards"]) == model.REFUSED_TARGET_THRESHOLD
    remaining = model._available_actions(runner.state["page"], runner.state["discards"])
    assert not [a for a in remaining if a["id"] == "e1"]
    assert [a for a in remaining if a["id"] == "e2"]


def test_a_field_with_no_value_is_a_discard_not_a_case_killing_error(runner, monkeypatch):
    """文本模型说"这个字段没有值"时，要作废这次决策并重选——不是把整条用例崩掉。

    这是**契约不一致**，不只是模型偶发选错：`questions.TEXT_VALUE` 把
    `{"text": null}` 定义成合法回复（"If a required value is missing"），
    而执行器收到它就抛异常。提示词承诺了一个协议，执行器不认。

    实测（2026-10-09，E9 新建流程 TC01）：goal 明说只填「标题」，模型挑了「签字意见」，
    文本模型照约定回 null，用例当场死在 ValueError 上——而那一刻**什么都没执行过**，
    本该只是一次"这条决策作废、重新选"。

    两条断言缺一不可：
      · `browser.act` 一次都没调 → 它确实发生在任何浏览器动作之前，所以重选是安全的
        （AGENTS.md 的判据：决策可以重发，动作不可以）；
      · 记进 discards 且 target_level=True → 模型看得到"这条路走不通"，
        够阈值后这个字段还会从候选里被剔掉，而不是空转到步数上限。
    """
    monkeypatch.setattr(
        loop, "field_text", Mock(side_effect=model.NoTextValue("no value", missing=True))
    )
    with pytest.raises(model.NoTextValue):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})

    runner.state["browser"].act.assert_not_called()
    assert runner.state["history"] == []
    assert runner.state["decision"] is None
    (recorded,) = runner.state["discards"]
    assert (recorded["node"], recorded["kind"]) == (10, "fill")
    assert recorded["target_level"] is True


def test_a_malformed_text_helper_answer_does_not_blame_the_target(runner, monkeypatch):
    """协议故障（回了个不合约定的结构）与目标无关，**不能牵连字段**。

    两种都记进 discards、都能重选；区别只在 target_level——把它也标成 True，
    够阈值后就会把一个**无辜的**字段从候选里剔掉，而模型再也选不到它。
    """
    monkeypatch.setattr(
        loop, "field_text", Mock(side_effect=model.NoTextValue("unparseable", missing=False))
    )
    with pytest.raises(model.NoTextValue):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})

    runner.state["browser"].act.assert_not_called()
    assert runner.state["discards"][0]["target_level"] is False


def test_repeated_missing_values_withhold_that_field_from_the_choices(runner, monkeypatch):
    """同一个字段被"没有值"拒到阈值次数后，就不该再出现在候选里。

    这才是这次修复**真正的目的**：不是"把异常改成重试"——那只会空转到步数上限，
    而是让模型在反馈里看到这条路走不通，且到阈值后连选都选不到。
    实测 TC01 的失败正是"模型挑错字段"，所以必须让它挑不到。
    """
    monkeypatch.setattr(
        loop, "field_text", Mock(side_effect=model.NoTextValue("no value", missing=True))
    )
    for _ in range(model.REFUSED_TARGET_THRESHOLD):
        runner.state["decision"] = decision()
        with pytest.raises(model.NoTextValue):
            runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
        runner.state["decision"] = None

    assert len(runner.state["discards"]) == model.REFUSED_TARGET_THRESHOLD
    remaining = model._available_actions(runner.state["page"], runner.state["discards"])
    assert not [a for a in remaining if a["id"] == "e1"]
    # 同节点的 click（e2）不受牵连：判据是 (node, kind) 而不是 node。
    assert [a for a in remaining if a["id"] == "e2"]


def test_discarded_attempts_are_sent_to_the_model(monkeypatch):
    """丢弃记录必须真的送进请求体——只在库里攒着等于没补。"""
    seen = {}

    def post(_url, _key, body, **_extra):
        seen["body"] = body
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "TYPE_TEXT"),
                "type_text_target": choice(["1"], "1"),
                "click_target": {"choice": "invented"},
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    discards = [
        {"action": "添 加", "kind": "click", "operation": "CLICK",
         "reason": "Target changed or is covered. Observe again."},
    ]
    model.choose(page(), "Find a book", [], discards)

    sent = seen["body"]["state"]["recent_discarded_attempts"]
    assert sent == [
        {"action": "添 加", "kind": "click", "attempts": 1,
         "reason": "Target changed or is covered. Observe again."},
    ]
    # operation 不进这份摘要：模型要的是"哪个目标、为什么被拒"。
    assert "operation" not in sent[0]


def test_discards_aggregate_by_target_so_the_effort_is_visible():
    """同一个目标被拒 30 次要读得出 30。

    截断成 6 条同项时，模型永远只看到"被拒 6 次"，读不出"已经耗了 30 步"——
    实测它就是这样一直在第 34-47 步之间打转，直到第 48 步才换目标。
    """
    discards = [{"action": "路径类型", "kind": "click", "reason": "covered"}] * 30
    discards.append({"action": "名称", "kind": "fill", "reason": "covered"})
    summary = model._discard_summary(discards)

    # 按次数降序：耗得最多的排最前，模型第一眼就看到它。
    assert summary[0] == {"action": "路径类型", "kind": "click", "attempts": 30, "reason": "covered"}
    assert summary[1] == {"action": "名称", "kind": "fill", "attempts": 1, "reason": "covered"}


def test_discard_summary_keeps_only_the_six_heaviest_targets():
    """超过 6 个目标时按次数截断——摘要不能无限长，但要留下最该被看见的那几个。"""
    discards = [
        {"action": f"目标{n}", "kind": "click", "reason": "covered"}
        for n, repeats in enumerate([7, 6, 5, 4, 3, 2, 1], start=1)
        for _ in range(repeats)
    ]
    summary = model._discard_summary(discards)

    assert len(summary) == 6
    assert [s["attempts"] for s in summary] == [7, 6, 5, 4, 3, 2]
    assert "目标7" not in [s["action"] for s in summary]


def _discard(node, kind, *, document=1.0, target_level=True):
    return {"node": node, "kind": kind, "document": document, "target_level": target_level}


def test_only_target_level_refusals_count_toward_removal():
    """"页面整体变了"是瞬时的，不该把目标剔掉；只有"目标本身不可用"才剔。"""
    mostly_transient = [_discard(28, "click", target_level=False)] * 5
    assert model._refused_targets(mostly_transient, [1.0]) == set()

    with_target_level = mostly_transient + [_discard(28, "click")] * 3
    assert model._refused_targets(with_target_level, [1.0]) == {(28, "click")}


def test_refusals_do_not_leak_across_documents():
    """node id 由 WeakMap 计数器发放、换文档会从 1 重发，跨文档剔会误杀新页面上的无辜元素。"""
    discards = [_discard(28, "click")] * 3
    assert model._refused_targets(discards, [2.0]) == set()          # 另一个文档：不剔
    assert model._refused_targets(discards, [1.0]) == {(28, "click")}  # 同一文档：剔


def test_refusal_keys_on_node_and_kind_together():
    """"点击被拒"不代表"填值也会被拒"，所以不能只按 node 剔。"""
    refused = model._refused_targets([_discard(28, "click")] * 3, [1.0])
    assert refused == {(28, "click")}
    assert (28, "fill") not in refused


def test_withheld_target_drops_exactly_that_action_and_keeps_other_ids():
    """剔除要精确：只掉被拒的那个 (node, kind)，其余动作必须保留快照原 id
    —— runner 就是按 id 把模型的选择映射回动作的（runner.py 的 next(... if a["id"] == selected)）。"""
    p = page()
    p["page_key"] = [1.0]
    key = (p["actions"][0]["node"], p["actions"][0]["kind"])

    kept = model._available_actions(p, [_discard(*key)] * 3)

    # 用 .get：wait/scroll 这类伪动作【没有】node 键（不是 None，而是根本没有），
    # 它们必须原样保留——剔除只针对能被拒的真实元素。
    assert [a["id"] for a in kept if a.get("node") is None] == ["wait"]
    assert all((a.get("node"), a.get("kind")) != key for a in kept)
    assert {a["id"] for a in kept} == {
        a["id"] for a in p["actions"] if (a.get("node"), a.get("kind")) != key
    }


def test_withheld_target_is_absent_from_the_request(monkeypatch):
    """剔除必须真的生效在请求体里——只在库里攒着等于没改。"""
    bodies = []

    def post(_url, _key, body, **_extra):
        bodies.append(body)
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "TYPE_TEXT"),
                "type_text_target": choice(["1"], "1"),
                "click_target": {"choice": "invented"},
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)

    p = page()
    p["page_key"] = [1.0]
    model.choose(p, "Find a book", [], [])
    first = len(bodies[0]["state"]["elements"])

    # 挑一个"该 node 只有这一个动作"的目标（Go 按钮），这样剔掉它之后元素表才会真的变短。
    # 换成 e2 那种与 e1 共用 node 的，node 会因还剩一个动作而继续留在 elements 里，
    # 用元素个数就测不出剔除生效了。
    go = next(a for a in p["actions"] if a["label"] == "Go")
    withheld = [_discard(go["node"], go["kind"])] * 3
    model.choose(p, "Find a book", [], withheld)

    assert len(bodies[1]["state"]["elements"]) == first - 1


def test_loading_waits_do_not_trigger_no_progress_stop(runner):
    for _ in range(5):
        runner.state["decision"] = decision("wait")
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert len(runner.state["history"]) == 5 and runner.state["status"] == "ready"


def test_stale_observation_preserves_executed_action(runner):
    runner.state["decision"] = decision("e3")
    runner.state["browser"].observe.side_effect = StalePage("changed")
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["action"] == "Go"
    runner.state["browser"].act.assert_called_once()


def test_observation_is_one_atomic_browser_read(monkeypatch):
    import jev_ultrafast.browser as browser

    p = page()
    cdp = Mock(return_value={"result": {"value": p}})
    monkeypatch.setattr(browser, "cdp", cdp)
    actual = browser_operation({"operation": "observe", "session": "test", "screenshot": False})
    assert actual["actions"] == p["actions"]
    assert cdp.call_count == 1
    assert cdp.call_args.args[0] == "Runtime.evaluate"


def test_executor_rejects_a_stale_page_before_browser_input(monkeypatch):
    import jev_ultrafast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    b.fresh = Mock(return_value=False)
    operation = Mock()
    monkeypatch.setattr(browser, "browser_operation", operation)
    with pytest.raises(StalePage):
        b.act(page()["actions"][0], page(), "book")
    operation.assert_not_called()


@pytest.mark.parametrize("response", [{"exceptionDetails": {}}, {"result": {}}])
def test_interrupted_dropdown_mutation_cannot_be_retried_as_stale(monkeypatch, response):
    import jev_ultrafast.browser as browser

    # A navigation can destroy the evaluation result after the change event already fired.
    if "exceptionDetails" in response:
        response["exceptionDetails"] = {"text": "Execution context destroyed"}
    cdp = Mock(return_value=response)
    monkeypatch.setattr(browser, "cdp", cdp)
    with pytest.raises(RuntimeError, match="Dropdown execution"):
        browser_operation({"operation": "act", "session": "test", "action": {
            "id": "e1", "kind": "select", "node": 1, "value": "Design",
        }})
    assert cdp.call_count == 1


def test_fingerprint_tracks_values_and_identity_not_screenshots():
    p = page()
    other = deepcopy(p)
    other["screenshot"] = "changed"
    assert fingerprint(p) == fingerprint(other)
    other["actions"][0]["node"] = 99
    assert fingerprint(p) != fingerprint(other)


@pytest.mark.parametrize(
    "content, missing",
    [
        ("Thinking: Zurich", False),                # 根本不是 JSON
        ('{"text":null}', True),                    # 合法回复：这个字段按 goal 没有值
        ('{"text":"Zurich","extra":true}', False),  # 结构不合约定（多了键）
        ('{"text":123}', False),                    # 类型不对
    ],
)
def test_text_helper_rejects_invalid_values(monkeypatch, content, missing):
    """四种坏结果都拒，但**只有 null 算"目标选错了"**。

    `missing` 就是这个责任方标记，调用方据它决定要不要把字段从候选里剔掉
    （见 agent.py 的 `target_level`）。把 '{"text":null}' 也标成 False，
    修好的那个 bug 会换个方式回来：模型每轮照样挑错字段，一直挑到步数上限。
    """
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", Mock(return_value={"choices": [{"message": {"content": content}}]}))
    # 断言具名异常而不是 ValueError：`NoTextValue` 是 ValueError 的子类，
    # 写父类会让"以后有人把它改成普通 ValueError"悄悄通过，而 runner 里那条
    # `except NoTextValue` 就再也不会命中——故障会退化成"又崩一次"。
    with pytest.raises(model.NoTextValue, match="nothing typed") as raised:
        model.field_text({"goal": "Find a flight"})
    assert raised.value.missing is missing


def test_navigation_during_prediction_reobserves_without_action(runner):
    runner.state["browser"].fresh.side_effect = StalePage("Document navigating")
    runner.command("tick")
    assert runner.state["status"] == "ready"
    assert runner.state["decision"] is None
    runner.state["browser"].act.assert_not_called()


def test_covered_target_is_dropped_only_when_an_uncovered_alternative_exists():
    """有未遮挡候选时剔掉被遮挡的——这正是提示词那条规则的字面条件，现在由框架强制。

    实测（2026-09-29，E9「添加路径」弹窗，连续三次全量跑）：弹窗打开后 123 条候选里
    94 条被标 covered，模型仍然去挑那个已被盖住的放大镜，执行层 100% 拒绝（当轮 7/7），
    随后空转到 BLOCKED。而这条规则在提示词里写得很死（questions.TARGET 的 NEVER 那句），
    说明"靠模型自觉"这条路已经到了它的上限。
    """
    actions = [
        {"id": "e1", "kind": "click", "label": "路径类型", "role": "button", "node": 30, "covered": True},
        {"id": "e2", "kind": "click", "label": "系统默认工作流", "role": "row", "node": 31},
        {"id": "wait", "kind": "wait", "label": "Wait"},
    ]
    kept = model._drop_covered(actions)
    # 被盖住的没了；未遮挡的与没有 node 的伪动作都在。
    assert [a["id"] for a in kept] == ["e2", "wait"]

    # 全部候选都被遮挡时原样保留——否则模型一个可选项都没有，比选错更糟。
    all_covered = [
        {"id": "e1", "kind": "click", "label": "A", "node": 30, "covered": True},
        {"id": "e2", "kind": "click", "label": "B", "node": 31, "covered": True},
    ]
    assert model._drop_covered(all_covered) == all_covered


def test_covered_drop_is_scoped_per_operation():
    """按操作分别判断：click 还有未遮挡候选，不等于 fill 也有——fill 不该被连坐。"""
    actions = [
        {"id": "e1", "kind": "click", "label": "按钮", "node": 30, "covered": True},
        {"id": "e2", "kind": "click", "label": "可点的", "node": 31},
        {"id": "e3", "kind": "fill", "label": "输入框", "node": 32, "covered": True},
    ]
    assert {a["id"] for a in model._drop_covered(actions)} == {"e2", "e3"}


def test_covered_disappears_from_the_request(monkeypatch):
    """与剔除一样：只在库里攒着等于没改，必须落到请求体的 criteria 里。"""
    bodies = []

    def post(_url, _key, body, **_extra):
        bodies.append(body)
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "click_target": choice(list(body["questions"]["click_target"]["criteria"]), "1"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)

    p = page()
    p["actions"] = [
        {"id": "e1", "kind": "click", "label": "被盖住的", "role": "button", "node": 30, "covered": True},
        {"id": "e2", "kind": "click", "label": "对话框里的行", "role": "row", "node": 31},
    ]
    model.choose(p, "Find a book", [], [])

    labels = [v["element"] for v in bodies[0]["questions"]["click_target"]["criteria"].values()]
    assert not any("被盖住的" in label for label in labels)
    assert any("对话框里的行" in label for label in labels)
