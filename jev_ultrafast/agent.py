"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import time
from pathlib import Path

from .browser import Browser, StalePage, TargetUnavailable
from .model import NoTextValue, action_space, choose, field_context, field_text
from .questions import MAX_STEPS


class Agent:
    def __init__(self, url, goals, *, cookies=None, record_dir=None, screenshots=False,
                 wait_stable=True, viewport=None, max_steps=MAX_STEPS):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        plan = [task]
        self.pending_text = None
        # 步数预算。默认是库自己的 `questions.MAX_STEPS`（演示用 60），**调用方可以覆盖**。
        #
        # 为什么必须可覆盖：框架层早就按用例解析了 `max_steps`（runner.py），但那个预算只在
        # 框架的循环里生效；库里这道墙读的是模块常量，于是用例写 `max_steps: 90` 也会在
        # **第 60 步被这里抛异常打断**，报的还是"demo budget"——与用例里那个数字对不上，
        # 排查时根本看不出是同一件事（实测 2026-09-25：两条用例都"broken"在这里）。
        # 顺带把失败形态也修好了：库里抛异常 = broken，而框架那道墙是**优雅停止 + 备注**。
        if max_steps is None or int(max_steps) <= 0:
            raise ValueError(f"max_steps 必须是正整数，收到 {max_steps!r}")
        self.max_steps = int(max_steps)
        # wait_stable 默认 True，现有行为不变；关掉它的代价见
        # framework/config.py 的 DEFAULT_WAIT_STABLE 注释（可能更贵，不是省钱开关）。
        self.browser = Browser(url, cookies=cookies, wait_stable=wait_stable, viewport=viewport)
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            goal="\n".join(plan),
            page=page,
            decision=None,
            history=[],
            # 【未落地】的尝试，独立于 history。
            # history 只装成功的动作（见 act 分支的 history.append），所以少了这份记录时，
            # 模型看到的历史里完全没有"我试过这个、被拒了"——实测因此空转 87 步：
            # 每轮都在同一个局面上从零推理，自然每轮得出同一个结论。
            discards=[],
            status="ready",
            plan=plan,
            plan_index=0,
            decisions=[],
            text_calls=[],
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
        )
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def snapshot(self):
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
        }

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        if name == "tick":
            try:
                self.command("predict", {})
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except StalePage:
                state["decision"] = None
                state["status"] = "ready"
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
        elif name == "predict":
            if not state["browser"]:
                raise ValueError("Start a demo first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            if not state["browser"].fresh(state["page"]):
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["decision"] = None
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has stopped. Start a fresh demo.")
            if len(state["decisions"]) >= self.max_steps * 2:
                raise ValueError("Reached the demo's model-call budget")
            state["decision"] = choose(state["page"], state["goal"], state["history"], state["discards"])
            state["decisions"].append(
                {
                    **state["decision"],
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                }
            )
            state["status"] = "predicted"
        elif name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            selected = decision["choice"]
            if selected in {"DONE", "BLOCKED"}:
                if not state["browser"].fresh(page):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                state["status"] = "done" if selected == "DONE" else "blocked"
                state["plan_index"] = int(selected == "DONE")
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
            action = next(a for a in page["actions"] if a["id"] == selected)
            if len(state["history"]) >= self.max_steps:
                state["status"] = "blocked"
                raise ValueError(f"Stopped at the {self.max_steps}-action budget")
            text, helper = None, None
            try:
                if action["kind"] == "fill":
                    if not state["browser"].fresh(page):
                        raise StalePage("Page changed before text generation. Choose again.")
                    context = field_context(state["goal"], action, page, state["history"])
                    if self.pending_text and self.pending_text[0] == context:
                        _, text, helper = self.pending_text
                    else:
                        text, helper = field_text(context)
                        self.pending_text = (context, text, helper)
                        state["text_calls"].append({**helper, "field": action["label"], "value": text})
                # Browser.act checks freshness immediately before input, including after text generation.
                # 接住返回值写进 history：以前它是丢掉的，于是报告里"执行"步骤
                # 拿不到浏览器到底执行了什么（browser.act 返回 {"executed": <action id>}）。
                execute_result = state["browser"].act(action, page, text=text)
            except (StalePage, NoTextValue) as error:
                # 记下【没落地】的尝试，然后原样重抛。
                #
                # 两类都进这个分支，共同点是**没有任何东西被执行过**（这才是"可以重来"的判据）：
                #   · StalePage——页面在决策之后变了，动作被新鲜度校验挡下；
                #   · NoTextValue——文本模型没能给出可写值（见 model.NoTextValue），
                #     它发生在 field_text 里，也就是 browser.act 之前。
                # 以前第二类会把整条用例崩掉；它本该是一次"这条决策作废、重新选"。
                #
                # 为什么必须在这里记：act 一抛错就走不到下面的 history.append，而 history 只装成功。
                # 少了这份记录，模型看到的"最近动作"里完全没有"我试过这个、被拒了"——
                # 实测因此空转 87 步：每轮都在同一个局面上从零推理，自然每轮得出同一个结论。
                #
                # 为什么用【独立列表】而不是塞进 history：history 同时被当"成功计数"用在两处——
                # 步数预算（上面 `len(state["history"]) >= self.max_steps`）与
                # "最近三条 page_changed 都是 False 就判 blocked"。把丢弃混进去，会让
                # 空转自动吃满步数预算，还会污染那个连续无变化的判定。
                #
                # pending_text 刻意【不清】：过期重试复用已生成文本靠的就是它，
                # 而清空会让下一次重试白花钱再问一遍文本模型。
                #
                # 用 setdefault 而不是 state["discards"]：这里是【记录】路径，而且正处在
                # 异常里。key 不在（例如测试手工搭的最小 state）时抛 KeyError，会把
                # "页面已变"这个真实信号换成一个与之无关的报错——本仓库最防的那类错报。
                state.setdefault("discards", []).append(
                    {
                        "action": action["label"],
                        "kind": action["kind"],
                        "operation": decision["operation"],
                        "reason": str(error),
                        # node + document 一起记，才能安全地"精确剔除"：
                        # node id 由 snapshot.js 的 WeakMap 计数器发放，**换文档（导航）后会从 1 重新
                        # 开始发**，所以单靠 node 跨文档剔会误杀新页面上的无辜元素。
                        # performance.timeOrigin 每个文档唯一，配它一起比就只在本文档内生效。
                        "node": action.get("node"),
                        "document": (page.get("page_key") or [None])[0],
                        # 只有"目标本身不可用"才该被剔除；"页面刚好动了"是瞬时的，重选无妨。
                        # NoTextValue 例外地也看 `missing`：模型照约定回 null，说明**问题就在
                        # 这个字段上**（goal 里它没有值），够次数就该从候选里剔；
                        # 而解析失败那类（missing=False）是协议故障，与字段无关，
                        # 牵连它会把一个无辜的字段剔掉。
                        "target_level": isinstance(error, TargetUnavailable)
                        or bool(getattr(error, "missing", False)),
                    }
                )
                raise
            self.pending_text = None
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            # Record execution before observing. A stale post-action observation must not erase the action.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "action": action["label"],
                    "kind": action["kind"],
                    "choice": selected,
                    "probability": decision["probabilities"][selected],
                    "confidence": decision["confidence"],
                    "latency_ms": decision["latency_ms"],
                    "text": text,
                    "text_helper": helper["model"] if helper else None,
                    "text_latency_ms": helper["latency_ms"] if helper else 0,
                    "operation": decision["operation"],
                    "target": decision["target"],
                    # 元素标识与执行结果：不补，history 就不自包含——报告里只看到
                    # "点了个链接"，看不到点的是哪个节点、浏览器有没有真的执行它。
                    # 事后补不回来：执行后 state["page"] 已被替换。
                    #
                    # 用 .get() 而不是 []：这里是【记录】路径，而且是在 browser.act()
                    # 已经执行【之后】才跑的。在这里抛 KeyError，会把一个确实发生过的
                    # 动作报成失败——正是本仓库最防的那类错报。（真实动作一定有
                    # node/role，见 snapshot.js:73 的 base={node,role,label,...}；
                    # 但"一定有"是上游的约定，不该由记录层用异常来兜。）
                    "node": action.get("node"),
                    "role": action.get("role"),
                    "value_before": action.get("value", ""),
                    "execute_result": execute_result,
                    "page_changed": None,
                    "url": page["url"],
                    "usage": decision["usage"],
                    "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                    "elapsed_ms": state["elapsed_ms"],
                }
            )
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            changed = state["page"]["fingerprint"] != page["fingerprint"]
            state["history"][-1].update(
                page_changed=changed,
                url=state["page"]["url"],
                elapsed_ms=state["elapsed_ms"],
            )
            # 【执行了、但页面毫无变化】的尝试，与"被拒"的尝试记进同一个 discards 通道。
            #
            # 为什么这条必须补：discards 原来只装 act 抛 StalePage 的那一类（动作没落地），
            # 而"真的点下去了、什么都没发生"是另一种同样白费的尝试，却完全不留痕——
            # 它进的是 history（成功），于是模型看到的只是 recent_actions 里多了一条
            # page_changed:false，读不出"这条路走不通"。实测（2026-09-24，E9 添加路径弹窗）：
            # 模型连点三次同一个图标按钮，劝住它的不是反馈而是"连续三条无变化即 blocked"
            # 的守卫——守卫是兜底，不该充当反馈。补上之后模型在第三次之前就能看到 attempts。
            #
            # target_level=True 的理由与 TargetUnavailable 相同：拒绝来自目标【本身】
            # （它确实什么也做不了），不是"页面刚好在动"那种瞬时状况，所以够 3 次就该从候选里剔除。
            # wait 例外：它本来就不改变页面，记进去会把正常等待误判成死路。
            if not changed and action["kind"] != "wait":
                state.setdefault("discards", []).append(
                    {
                        "action": action["label"],
                        "kind": action["kind"],
                        "operation": decision["operation"],
                        "reason": "Executed, but nothing on the page changed.",
                        "node": action.get("node"),
                        "document": (page.get("page_key") or [None])[0],
                        "target_level": True,
                    }
                )
            if state["record"]:
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                    base64.b64decode(state["page"]["screenshot"])
                )
            repeated = state["history"][-3:]
            state["status"] = (
                "blocked"
                if len(repeated) == 3 and all(h["page_changed"] is False and h["kind"] != "wait" for h in repeated)
                else "ready"
            )
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def run(self):
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def close(self):
        self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
