"""框架层对"一次决策被丢弃"的处置：状态回到 ready，页面该不该重看要分清。

为什么单独一篇：runner 里有**三个丢弃出口**——终止性决策被丢弃、页面过期、
文本模型没给出可写值（`model.NoTextValue`）——它们共用 `_reset_after_discard`。
这个函数只有一处容易写错，就是**页面到底有没有可能变**：

  · 写宽了（该重看却不重看）：下一轮拿着动作前的过期快照决策，
    而框架正是靠新快照的 fingerprint 判断"这一步改没改变页面"的；
  · 写窄了（不该重看却重看）：不会出错，但白花一次快照，还会往本轮的等待步骤里
    塞一条本不存在的"页面稳定"——报告里看不出它是白来的。

离线单测，不起浏览器。`_FakeAgent` 只给这个函数真正用到的那几个字段。
"""

import time
from unittest.mock import Mock

from jev_ultrafast.framework.runner import _reset_after_discard


class _FakeAgent:
    def __init__(self, page):
        self.screenshots = False
        self.state = {
            "browser": Mock(observe=Mock(return_value=page)),
            "page": page,
            "decision": {"choice": "e1"},
            "status": "predicted",
            "started_at": time.perf_counter() - 0.5,
            "elapsed_ms": 0,
        }


def test_default_reobserves_because_the_page_may_have_changed():
    """页面过期那一路：动作**可能已经落地**，所以必须重新观察。

    这是默认值。实测（M1-1）act 抛 StalePage 有两种成因，其中一种是"动作已执行、
    随后的观察失败"——那一支要是跳过重看，就正好把已发生的变化挡在视野外。
    """
    agent = _FakeAgent({"fingerprint": "before"})
    _reset_after_discard(agent)

    agent.state["browser"].observe.assert_called_once()
    assert agent.state["page"] == {"fingerprint": "before"}
    assert agent.state["decision"] is None
    assert agent.state["status"] == "ready"


def test_missing_text_skips_the_reobserve_because_nothing_was_executed():
    """文本模型没给出值：拒绝发生在 `browser.act` **之前**，页面按定义没变。

    所以这一支传 `reobserve=False`。它省下的不只是那次快照——它还保证本轮
    不会平白多出一条"页面稳定"等待步骤（等待是 observe 的副产物）。
    """
    page = {"fingerprint": "before"}
    agent = _FakeAgent(page)
    _reset_after_discard(agent, reobserve=False)

    agent.state["browser"].observe.assert_not_called()
    assert agent.state["page"] is page
    assert agent.state["decision"] is None
    assert agent.state["status"] == "ready"


def test_both_paths_leave_the_agent_ready_to_decide_again():
    """两条路都必须让循环能接着跑：`decision` 清空 + `status` 回 ready。

    少任何一个，下一轮 `Agent.command("predict")` 都会撞在
    "Observe and choose before acting" 或 "This run has stopped" 上——
    而那两个报错完全指不出根因在这里。
    """
    for kwargs in ({}, {"reobserve": False}):
        agent = _FakeAgent({"fingerprint": "before"})
        agent.state["elapsed_ms"] = -1
        _reset_after_discard(agent, **kwargs)

        assert agent.state["decision"] is None
        assert agent.state["status"] == "ready"
        # 耗时也要跟着刷新：它进「执行摘要」，停在旧值会让报告里的节奏读错。
        assert agent.state["elapsed_ms"] >= 500