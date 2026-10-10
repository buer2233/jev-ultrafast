"""单条自然语言用例的执行：登录注入 → 逐步执行（Allure 步骤）→ 终局断言。

步骤包装刻意放在这一层，而不是给 agent.py 打装饰器：
  · agent.py 是库，demo.py（inspector）也依赖它，不应被 Allure 污染；
  · 步骤标题需要动态内容（第几步、什么操作），装饰器做不到。
"""

import json
import os
import time
from pathlib import Path

import allure

import jev_ultrafast.agent as agent_module

from ..browser import StalePage
from ..model import NoTextValue
from . import e9_config, e9_login, encode, report_params, video
from .assertions import check, combine, resolve_threshold
from .config import (
    DEFAULT_MAX_STEPS,
    DEFAULT_TIMEOUT_S,
    DEFAULT_VIDEO_RECORD,
    DEFAULT_WAIT_STABLE,
    DEFAULT_WAIT_STEP_MS,
    parse_viewport,
)
from .judge import judge
from .reporting import (
    attach_decision,
    attach_environment,
    attach_execution,
    attach_screenshot,
    attach_trace,
    decision_params,
)

# 这两类"选择"不产生浏览器动作，只是把状态落成 done / blocked。
#
# ⚠️ 它们**不是页面元素**：`model.choose()` 通过 `operations.update(DONE=…, BLOCKED=…)`
# （model.py:151）把它们加进的是**操作字典**，返回的 choice 就是字面量 "DONE"。
# 所以任何 `next(a for a in page["actions"] if a["id"] == choice)` 都会对它们
# 抛 StopIteration —— 必须**先分流、再查表**（库自己也是这么做的，见 agent.py:93）。
TERMINAL_CHOICES = frozenset({"DONE", "BLOCKED"})


# --------------------------- 报告层开关的四级优先级 ---------------------------

def _option(pytest_config, name):
    """读一个 pytest 选项；选项不存在或没传时返回 None。

    **"没传"必须是 None**，否则实现不了"未传用配置默认、传了用传值"。
    conftest 里这三个选项都声明了 `default=None`，`store_true/store_false`
    共用一个 dest 时两个都不传也是 None。
    """
    if pytest_config is None:
        return None
    try:
        return pytest_config.getoption(name)
    except (ValueError, AttributeError):
        # 选项不存在（比如单测里传了个精简的假 config）——当作"没传"，不当作错误
        return None


def _first(*values):
    """取第一个【不是 None】的值。"""

    # 必须按 `is not None` 判，不能按真值判：`--no-wait-stable` 给的是 False，
    # 用真值判会让它被跳过，于是"显式关闭"反倒用上了环境变量/默认值的 True。
    for value in values:
        if value is not None:
            return value
    return None


def _as_bool(value):
    """把 'true'/'1'/'yes'/'on' 这类写法收成布尔。"""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_video_mode(value):
    """录屏档位：0 / 1 / -1，别的值**响亮报错**。

    不静默退回默认值：档位写错（比如把 -1 写成 1-）会让"仅保留失败用例"
    变成"留下全部录屏"，磁盘和证据口径都跟着错，而且报告上看不出来。
    """
    try:
        mode = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"录屏档位只能是 0 / 1 / -1，收到 {value!r}") from None
    if mode not in (-1, 0, 1):
        raise ValueError(f"录屏档位只能是 0 / 1 / -1，收到 {value!r}")
    return mode


def _as_count(value, *, what):
    """非负整数（毫秒门槛用）。"""
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what}只能是非负整数，收到 {value!r}") from None
    if count < 0:
        raise ValueError(f"{what}不能是负数，收到 {value!r}")
    return count


def resolve_report_options(pytest_config=None, case=None):
    """解析报告层三个开关的生效值。

    优先级（本仓库既有范式，见 `assertions.resolve_threshold`）::

        pytest 参数  >  环境变量 JEV_NL_*  >  config.json  >  framework/config.py 内置默认

    **每个用例解析一次**，不做成模块级常量：pytest 参数要到 `pytest_configure`
    之后才可用，而模块常量是 import 期求值的（实施方案 §5.2）。

    `case` 目前不参与解析——本期不加用例级（YAML）覆盖（实施方案 §5.4）。
    参数留在签名里，是为了将来加 `case.get("video_record")` 时不必改所有调用点。
    """
    file_options = e9_config.report_options()
    env = os.environ

    return {
        "video_record": _as_video_mode(
            _first(
                _option(pytest_config, "video_record"),
                env.get("JEV_NL_VIDEO_RECORD"),
                file_options.get("video_record"),
                DEFAULT_VIDEO_RECORD,
            )
        ),
        "wait_stable": _as_bool(
            _first(
                _option(pytest_config, "wait_stable"),
                env.get("JEV_NL_WAIT_STABLE"),
                file_options.get("wait_stable"),
                DEFAULT_WAIT_STABLE,
            )
        ),
        "wait_step_threshold_ms": _as_count(
            _first(
                _option(pytest_config, "wait_step_ms"),
                env.get("JEV_NL_WAIT_STEP_MS"),
                file_options.get("wait_step_threshold_ms"),
                DEFAULT_WAIT_STEP_MS,
            ),
            what="等待成步骤的门槛（毫秒）",
        ),
    }


def _latest_decision(agent):
    """取本轮决策。

    `state["decision"]` 会在 act 里被消费掉（置 None，防止重试重复点击），
    所以退回 `decisions` 的末条——那是这一轮真正发出的那次决策。
    """
    return agent.state.get("decision") or (
        agent.state["decisions"][-1] if agent.state["decisions"] else None
    )


def _reset_after_discard(agent, *, reobserve=True):
    """一次决策被丢弃后的统一收尾：丢弃决策 → （按需）重新观察 → 状态回 ready。

    与 `Agent.command("tick")` 的 except 分支逐行一致（`agent.py:59-64`）——
    那是库的既有语义。框架层复刻一份，是为了能自己控制步骤边界：
    预测与动作要落在**不同的** Allure 步骤里，才看得出"模型选了什么"和"实际做了什么"。

    刻意不直接调用 `command("tick")`：那样两者会挤在同一个步骤里，
    而且"执行"步骤会退化成事后补附件的空壳（时长恒为 0，见分析报告 §1.4）。

    `reobserve=False`：丢弃的原因是**文本模型没给出可写值**（`model.NoTextValue`）。
    那条路径在 `browser.act` 之前就返回了，页面按定义没变，重新观察只是白花一次快照。
    仍复用同一个函数而不是另写一份，是为了让"丢弃决策"只有一个出口——
    两条路径各写一遍，迟早会漂移，而漂移的表现是状态回不到 ready、循环空转。
    """
    state = agent.state
    state["decision"] = None
    state["status"] = "ready"
    if reobserve:
        state["page"] = state["browser"].observe(screenshot=agent.screenshots)
    state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)


WAIT_LABELS = {
    "stable": "页面稳定",
    "settle": "文档就绪",
    "input_sync": "输入后控件同步",
}


def _wait_params(wait):
    """把一个等待观测值翻成参数表内容；只放这个 kind 真有的字段。"""
    params = {"等待耗时ms": wait["elapsed_ms"]}
    if "stable" in wait:
        params["是否等到稳定"] = wait["stable"]        # 超时没等到也要看得见
    if "ready" in wait:
        params["文档已就绪"] = wait["ready"]
    if "trigger" in wait:
        params["触发动作"] = wait["trigger"]
    if "polls" in wait:
        params["轮询次数"] = wait["polls"]
    if "timeout" in wait:
        params["超时上限ms"] = wait["timeout"]
    return params


def _wait_steps_split(waits, threshold_ms):
    """把一批等待观测值按门槛分成两拨：独立成步骤的 / 并进参数的。

    纯函数，不碰 agent，便于单测——**门槛分档是已拍板的决策**（§7.2），
    每轮都建步骤会把 50 ms 级的输入同步等待也变成步骤，报告就成了流水账。

    Returns:
        (达到门槛的 [(标签, 原始观测值)] 列表, 未达门槛的参数字典)
    """
    long_waits, short = [], {}
    for wait in waits:
        label = WAIT_LABELS.get(wait["kind"], wait["kind"])
        if wait["elapsed_ms"] < threshold_ms:
            # 同一个 kind 一轮里可能出现两次（`settle` 在首屏与 follow_new_tab 各一次），
            # 所以是**累加**而不是覆盖——覆盖会让第二次静默吃掉第一次的耗时。
            key = f"{label}等待ms"
            short[key] = short.get(key, 0) + wait["elapsed_ms"]
        else:
            long_waits.append((label, wait))
    return long_waits, short


def _collect_waits(agent, threshold_ms):
    """取走本轮累积的等待观测值并按门槛切分。

    **这是唯一的消费点**：每次调用都清空 `browser.waits`，避免上一轮的等待
    串进下一轮（串进来就会把耗时算到错的那一步上，而报告上看不出来）。
    """
    waits = list(agent.browser.waits)
    agent.browser.waits.clear()
    return _wait_steps_split(waits, threshold_ms)


def _wait_title(label, steps):
    """等待步骤的标题。

    `steps=None` 专用于首屏那一次：它发生在 `Browser.__init__` 里、循环还没开始，
    所以渲染成【前置】步骤，**不占步数编号**（否则第 1 步的编号会被它占掉，
    与「决策/执行」的编号就对不上了）。
    """
    if steps is None:
        return f"前置：等待{label}（首屏）"
    return f"第 {steps} 步 · 等待（{label}）"


def _emit_wait_steps(long_waits, steps):
    """把达到门槛的等待渲染成**独立步骤**。

    ⚠️ 必须在产生它的执行步骤的 `with` 块【之外】调用：块内还有活跃步骤时，
    `allure.step()` 会把新步骤挂成那个活跃步骤的**子步骤**，而等待步骤应该是**兄弟**。
    """
    for label, wait in long_waits:
        # 时间戳用真实区间（§7.3）。为什么不能简单地"在 with 内把 start/stop 都改掉"
        # ——`stop` 会被 `__exit__` 时的 `stop_step(now())` 冲掉，见 report_params 的
        # `step_with_real_times` 注释（实测探针 9）。
        report_params.step_with_real_times(
            _wait_title(label, steps),
            _wait_params(wait),
            wait["started_ms"],
            wait["started_ms"] + wait["elapsed_ms"],
        )


def _start_recording(case, agent, options):
    """按档位决定是否开始录屏；返回 Recorder 或 None。

    档位 0 必须"**不启动**"而不是"录了再删"——否则白付录屏开销
    （观察阶段 +3–6 ms/次、采集线程持续轮询，实施方案 §6.3）。
    """
    if not video.should_record(options["video_record"]):
        return None
    return video.Recorder(agent.browser, video.frames_dir_for(case["id"]))


def _stop_recording(recorder):
    """停录。把"收尾失败"带出来而不是吞掉——静默失败会让报告少一段视频而无迹可循。"""
    if recorder is None:
        return None
    try:
        return recorder.stop()
    except Exception as error:                     # noqa: BLE001
        return {
            "epoch": recorder.epoch, "end_epoch": time.time(), "frames": [], "times_ms": [],
            "errors": [f"停录失败：{type(error).__name__}: {error}"], "session_switches": 0,
            "viewport": getattr(recorder, "viewport", ()),
        }


def _finish_video(recording, mode, snapshot, *, ok, note, numbered=True):
    """按三档决定编码并附加录屏。返回一段写进报告的说明。

    ⚠️ 顺序不能反：**先编码、再附加**。`allure.attach` 会把字节**拷贝**进
    `allure-results/`，附加过之后再删原文件也删不掉报告里那份。
    所以 `-1` 档"通过了"必须是**不编码、不附加**，而不是"生成再删"
    ——等价的结果，还省掉约 7 秒编码（§6.3）。

    附加用 `allure.dynamic.label` 记 epoch，**必须是 label 不能是 parameter**：
    parameter 会改 `historyId`，于是每次运行都变成一条新用例，History / Trend / Retries
    全废——**而报告页面看不出任何异常**（实测，分析报告 §4.2.3）。
    """
    if recording is None:
        return "档位 0：未录制"
    if not recording["frames"]:
        return f"未抓到任何帧（采集错误：{recording['errors'] or '无'}）"

    keep, reason = video.should_keep(
        mode, ok=ok, note=note, interrupted=video.any_interrupted(snapshot)
    )
    notes = [reason]
    if recording["errors"]:
        # 采集线程中途死掉会让视频莫名变短——必须说出来，否则看着"只是短了点"
        notes.append(f"采集期间出错：{recording['errors']}")
    if not keep:
        return "；".join(notes)

    mp4 = Path(recording["frames"][0]).parent / "video.mp4"
    info = encode.to_mp4(
        recording["frames"], recording["times_ms"], mp4,
        # 让最后一帧保持到录制结束：帧是重绘驱动的，可能只集中在前几秒，
        # 不给 end_ms 的话视频会在用例还没跑完时就结束。
        end_ms=round((recording["end_epoch"] - recording["epoch"]) * 1000, 3),
    )
    encode.attach_video(mp4, name="执行录屏" if numbered else "执行录屏（用例中途失败，无终局判定）")
    allure.dynamic.label("videoEpochMs", str(int(recording["epoch"] * 1000)))
    viewport = recording.get("viewport") or ()
    if len(viewport) == 2:
        # 插件要把步骤参数里的「操作坐标」换算成画面位置，分母就是这个视口尺寸。
        # 做成标签而不是让插件从帧尺寸推断：推断在等比缩放时恰好对，
        # 不成比例时会**静默偏**——插件拿它做一致性自查才拦得住。
        allure.dynamic.label("videoViewport", f"{viewport[0]}x{viewport[1]}")
    notes.append(f"已保留：{encode.format_summary(info)}")
    if recording.get("session_switches"):
        # 会话切换次数写进报告：它解释了"为什么帧在某个时刻变稀"，
        # 也是排查"录屏莫名变短"的第一个线索。
        notes.append(f"录制期间切换过 {recording['session_switches']} 次标签页会话")
    return "；".join(notes)


def _login_cookies(case, base_url):
    """按用例声明的 login 角色完成接口登录，返回可注入浏览器的 cookie 列表。"""
    role = case.get("login", "none")
    if role == "none":
        return []
    if not base_url:
        raise RuntimeError(f"用例 {case['id']} 声明了 login={role}，但未配置 E9_BASE_URL")
    account = e9_login.load_credentials(role)
    cookies = e9_login.login(base_url, account["user_name"], account["password"])
    # 只把 cookie 的【名字】写进报告，值绝不落盘
    allure.attach(
        json.dumps([c["name"] for c in cookies], ensure_ascii=False),
        "注入的 Cookie 名称", allure.attachment_type.JSON,
    )
    return cookies


def run_case(case):
    """执行一条用例。断言失败时由 pytest 抛出，这里不吞异常。

    只做两件事：解析开关、以及**保证原始帧目录一定被清掉**。
    真正的执行在 `_execute_case`。
    """
    # 报告层开关由 conftest 在收集期解析好塞进用例（那里有 pytest config）；
    # 直接调用 run_case 时（脚本、单测）退回自带默认，不让调用方必须提供。
    options = case.get("_report_options") or resolve_report_options()
    # 录屏状态握在一个可变的盒子里：外层要能在**异常路径**上也找到 recorder
    # 并清掉帧目录。原始帧约 92 MB/分钟，30 秒用例先落 ~46 MB，
    # 清不掉就会把 artifacts/ 撑爆——所以清理放在最外层 finally，与成败无关。
    state = {"recorder": None, "recording": None, "snapshot": None, "note": ""}
    try:
        _execute_case(case, options, state)
    except Exception:
        # 中途崩溃（act 报错、预算超限…）也要按档位决定留不留录屏——**崩掉那次最需要看回放**。
        # 崩溃时没有终局判定，按 `ok=False` 处理：没过就是没过。
        _finish_video_best_effort(state, options, ok=False)
        raise
    finally:
        if state["recorder"] is not None:
            encode.cleanup_frames(state["recorder"].frames_dir)


def _finish_video_best_effort(state, options, *, ok):
    """异常路径上尽力附上录屏。**绝不向上抛**——真正要报的失败是上面那个异常。"""
    if state["recorder"] is None or state["recording"] is None:
        return
    try:
        note = _finish_video(state["recording"], options["video_record"],
                             state["snapshot"] or {}, ok=ok, note=state["note"], numbered=False)
        allure.attach(note, "录屏说明", allure.attachment_type.TEXT)
    except Exception as video_error:               # noqa: BLE001
        # 不盖掉真正的失败，但也不让"录屏没附上"无迹可循
        allure.attach(f"崩溃路径下附加录屏失败：{video_error!r}",
                      "录屏说明", allure.attachment_type.TEXT)


def _execute_case(case, options, state):
    """`run_case` 的主体：登录 → 逐步执行 → 终局断言。"""
    from ..agent import Agent

    timeout_s = int(case.get("timeout_s", DEFAULT_TIMEOUT_S))
    max_steps = int(case.get("max_steps", DEFAULT_MAX_STEPS))
    threshold = resolve_threshold(case, case)
    screenshot_every_step = bool((case.get("on_failure") or {}).get("screenshot", False))

    cookies = []
    with allure.step("前置：接口登录并注入浏览器登录态"):
        cookies = _login_cookies(case, case["_base_url"])

    started = time.perf_counter()
    # 视口按用例指定（见 config.parse_viewport 的说明）：站点有"比默认视口还宽的编辑器"时
    # 必须如实放大窗口——视口外的元素不会成为候选，硬用小窗口只会让模型找不到按钮。
    viewport = parse_viewport(case.get("viewport"))
    agent = Agent(case["url"], case["goal"], cookies=cookies, screenshots=screenshot_every_step,
                  wait_stable=options["wait_stable"], viewport=viewport, max_steps=max_steps)
    state["recorder"] = recorder = _start_recording(case, agent, options)
    steps, note = 0, ""
    threshold_ms = options["wait_step_threshold_ms"]
    snapshot = None
    try:
        # 首屏等待发生在 Browser.__init__ 里，循环还没开始 —— 渲染成【前置】步骤，
        # 不占步数编号。先发射等待步骤（它们在时间上先发生），再把没达门槛的短等待
        # 并进"观察初始页面"的参数（它没有可挂的执行步骤，只能挂这里）。
        initial_long, initial_short = _collect_waits(agent, threshold_ms)
        _emit_wait_steps(initial_long, None)

        # 初始观察也留痕，便于报告里看到"起点长什么样"
        observe_ctx = report_params.step("观察初始页面")
        with observe_ctx:
            allure.attach(
                f"{agent.state['page'].get('title')}\n{agent.state['page'].get('url')}",
                "初始页面", allure.attachment_type.TEXT,
            )
            if initial_short:
                report_params.fill_outputs(observe_ctx.uuid, initial_short)

        while agent.state["status"] not in {"done", "blocked"}:
            if steps >= max_steps:
                note = f"达到用例步数上限 {max_steps}，提前停止"
                allure.attach(note, "提前停止", allure.attachment_type.TEXT)
                break
            if time.perf_counter() - started > timeout_s:
                note = f"达到用例超时 {timeout_s}s，提前停止"
                allure.attach(note, "提前停止", allure.attachment_type.TEXT)
                break

            steps += 1
            before_actions = len(agent.state["history"])
            # 只认本轮 act 产生的等待：清一次，免得上一轮、或决策步骤里那次重新观察
            # 留下的等待被算到本轮头上——那会把耗时归错步骤，而报告上看不出来。
            agent.browser.waits.clear()

            # ── 决策步骤 ────────────────────────────────────────────────
            # 注意用 `ctx = report_params.step(...)` 再 `with ctx:`，不能写
            # `with ... as ctx`：StepContext.__enter__ 没有 return，as 拿到的是 None。
            decide_ctx = report_params.step(
                f"第 {steps} 步 · 决策（Jev 概率判断）", {"步序": steps}
            )
            with decide_ctx:
                agent.command("predict")
                decision = _latest_decision(agent)
                if decision:
                    # 页面 URL 与候选元素数走【路 4】而不是路 1：predict 可能因为页面
                    # 不再新鲜而重新观察（agent.py:70-71），进入前取的那份未必是
                    # 决策真正依据的那份。这里 state["page"] 就是 choose() 用过的那份。
                    page_used = agent.state["page"]
                    report_params.fill_outputs(decide_ctx.uuid, {
                        "页面": page_used.get("url"),
                        "候选元素数": len(page_used["actions"]),
                        **decision_params(decision),
                    })
                    attach_decision(decision)

                # DONE/BLOCKED 不产生浏览器动作，但必须调 act 才能把状态落成 done/blocked。
                # 刻意留在【决策】步骤里：给它单开一个「执行」步骤，
                # 报告就会把"什么都没做"显示成执行了一个动作。
                if decision and decision["choice"] in TERMINAL_CHOICES:
                    try:
                        agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})
                    except StalePage:
                        # 决策被丢弃必须留痕：否则它与"决策是 DONE"在报告里长得一样，
                        # 会让人把"跑了 N 次决策"误读成"点了 N 次"（分析报告 §1.4 第 4 点）。
                        report_params.mark(
                            decide_ctx.uuid, f"决策 {decision['choice']} 因页面过期被丢弃，未生效"
                        )
                        _reset_after_discard(agent)

            if agent.state["status"] in {"done", "blocked"}:
                break
            if decision is None or decision["choice"] in TERMINAL_CHOICES:
                # 两种"这一轮没有动作"的情形：终止性决策（含被丢弃后重来）、决策被丢弃。
                # 都不建「执行」步骤——建了就会把上一步的动作重复显示成本步的结果。
                continue

            # ── 决策落定、执行之前：抓现场 ──────────────────────────────
            page_before = agent.state["page"]
            selected = decision["choice"]
            # 走到这里 selected 一定在动作空间里：DONE/BLOCKED 上面已分流，
            # 其余 choice 由 model.choose() 从 controls/targets 的 id 里选出（model.py:185-191）。
            action = next(a for a in page_before["actions"] if a["id"] == selected)

            # ── 执行步骤：真的包住这次执行（拆分前它只是个补附件的空壳）────────
            act_ctx = report_params.step(
                f"第 {steps} 步 · 执行 {decision['operation']} → {action['label']}",
                {
                    "操作": decision["operation"],
                    "目标索引": decision["target"] if decision["target"] is not None else "—",
                    "元素节点": action.get("node", "—"),
                    "角色": action.get("role", "—"),
                    "执行前值": action.get("value", ""),
                    "是否生成文本": action["kind"] == "fill",
                },
            )
            with act_ctx:
                stale = None
                # 文本模型没能给出可写值。它发生在 browser.act 之前，所以【什么都没执行】，
                # 与"页面过期"同属"可以重来"的一类——以前这种会一路抛到 conftest，
                # 把一条还能救的用例判成 broken（实测 TC01：模型挑错了字段，文本模型
                # 照约定回了 {"text": null}，用例当场死在 ValueError 上）。
                no_text = None
                try:
                    agent.command("act", {"fingerprint": page_before["fingerprint"]})
                except StalePage as error:
                    stale = error
                except NoTextValue as error:
                    no_text = error

                # act 抛 StalePage 有【两种】成因，报告写法必须分开——实测两种都出现过
                # （M1-1 那一次：10 个执行步骤里有 2 条留痕，但只少了 1 个动作）：
                #   · history 没长 → 动作确实没落地。act 的第一件事就是新鲜度校验
                #     （browser.py:250），在那之前不可能改页面。
                #   · history 长了   → 动作【已经】落地，被打断的是动作之后的观察与收尾
                #     （agent.py:142 的 observe）。说成"什么都没做"就是把已发生的事报成没发生。
                history = agent.state["history"]
                acted = len(history) > before_actions
                discarded = False

                if not acted and stale is None and no_text is None:
                    # 不变式：act 正常返回就必然追加过 history（agent.py:121）。
                    # 宁可响亮报错也不退化成 history[-1]——那样会把**上一步**的动作
                    # 显示成本步的结果，报告错得看不出来，比跑不起来更糟。
                    raise RuntimeError(
                        f"第 {steps} 步 act 正常返回却没有新增 history 记录；"
                        "报告的「执行」步骤将无法归属，拒绝继续（agent.py 的 act 语义变了？）"
                    )
                if no_text is not None:
                    # 报告里必须说清"这一步为什么没有动作"，否则它会与"动作静默失败"
                    # 长得一模一样——而两者的处置完全不同（前者该换个目标重选）。
                    report_params.mark(
                        act_ctx.uuid,
                        f"没有可写入的值，本次执行被丢弃（动作未落地）：{no_text}",
                    )
                    # reobserve=False：字段文本生成在 browser.act 之前，页面按定义没变。
                    _reset_after_discard(agent, reobserve=False)
                    discarded = True
                elif stale is not None:
                    report_params.mark(
                        act_ctx.uuid,
                        "页面已过期，本次执行被丢弃（动作未落地）" if not acted
                        else "动作已执行；随后的页面观察失败，本轮以重新观察收尾",
                    )
                    # 两种成因都要复位：act 在 observe 抛错时，状态还停在 "predicted"、
                    # page 还是动作前的那份（agent.py:142 之后的赋值都没跑到）。
                    _reset_after_discard(agent)
                    discarded = not acted
                else:
                    last = history[-1]
                    # 执行了但页面毫无变化：这一步白花了，必须留痕。
                    # 少了它，报告里"点了个按钮什么都没发生"与"点了个按钮生效了"长得一模一样，
                    # 只能靠人去翻「执行返回」里的 页面已变化=false——而 agent 侧的
                    # discarded 反馈正是靠这一步的 page_changed 记的（agent.py 的 act 分支）。
                    if last.get("page_changed") is False and action["kind"] != "wait":
                        report_params.mark(
                            act_ctx.uuid,
                            "已执行，但页面没有任何变化（该目标这一步无效果，已记入 discarded 反馈）",
                        )
                    if last.get("text"):
                        # 单独挂一份 TEXT：Allure 里 TEXT 是内联渲染的，不用下载 JSON 就能读；
                        # 同一份值也进「执行请求」附件，供需要完整上下文时看。
                        allure.attach(str(last["text"]), "写入的文本", allure.attachment_type.TEXT)
                    attach_execution(action, last)
                    # 动作落在画面的哪个点——插件靠它在录屏上**合成一个看得见的光标**
                    # （CDP 录屏不含系统光标，不合成就是"看不见鼠标点在哪"）。
                    # 用 fill_outputs 补而不是进上面的初始参数：这个点要 act 跑完才知道，
                    # 而初始参数只在 start_step 那一刻被读走（report_params 模块注释）。
                    # via 记着这个点的来路（mouse / wheel / js），scroll 步骤没有真实鼠标
                    # 位置、select 压根不发鼠标事件，报告里不该混为一谈。
                    point = (last.get("execute_result") or {}).get("point") or {}
                    if point:
                        report_params.fill_outputs(act_ctx.uuid, {
                            "操作坐标": f"{point.get('x')}, {point.get('y')}",
                            "坐标来源": {"mouse": "鼠标点击", "wheel": "滚轮",
                                         "js": "直接赋值（无鼠标事件）"}.get(point.get("via"), "—"),
                        })
                    if screenshot_every_step:
                        attach_screenshot(agent.state["page"])

                # 等待的收集放在最后：`_reset_after_discard` 里的 observe 也可能产生等待，
                # 放在它之前收集会把那部分漏掉（下一轮开头会被清空，等于静默丢弃）。
                long_waits, short_waits = _collect_waits(agent, threshold_ms)
                if short_waits:
                    report_params.fill_outputs(act_ctx.uuid, short_waits)

            # 等待步骤是执行步骤的【兄弟】，必须建在 with【之外】（见 _emit_wait_steps 注释）。
            # 也不能用 `continue` 跳过这里——那会把等待步骤漏掉。
            _emit_wait_steps(long_waits, steps)
            if discarded:
                continue

        snapshot = agent.snapshot()
    finally:
        # 录屏必须在 agent.close()【之前】停：close() 会关掉标签页与会话，
        # 之后 stopScreencast 就没得可停了；而且进程级的 drain_events 也就收不到帧了。
        state["recording"] = _stop_recording(recorder)
        state["snapshot"] = snapshot or {}    # 中途异常时是 None，_finish_video 已能容忍
        state["note"] = note
        agent.close()

    status = snapshot["status"]
    with allure.step("终局：记录执行结果"):
        allure.attach(
            json.dumps(
                {
                    "终止状态": status,
                    "浏览器动作数": len(snapshot["history"]),
                    "决策请求数": len(snapshot["decisions"]),
                    "文本模型调用数": len(snapshot["text_calls"]),
                    "耗时ms": snapshot.get("elapsed_ms"),
                    # 生效的开关写进报告：批量执行时"这次到底关没关等待"必须能核对，
                    # 否则事后只能靠回忆（M3 的判据）。
                    "录屏档位": options["video_record"],
                    "页面稳定等待": options["wait_stable"],
                    "等待成步骤门槛ms": options["wait_step_threshold_ms"],
                    # 视口尺寸：录屏/截图尺寸与「操作坐标」都按它换算，换算错了报告里看不出，
                    # 所以必须留下这一次真正用的那个值。
                    "视口": list(agent.browser.viewport),
                    "备注": note,
                },
                ensure_ascii=False, indent=2,
            ),
            "执行摘要", allure.attachment_type.JSON,
        )
        attach_trace(snapshot)

    page = snapshot["page"]
    ctx = {"threshold": threshold, "judge": lambda claims: judge(claims, page, history=snapshot["history"])}

    results = []
    for expect in case["expect"]:
        title = expect.get("desc") or expect.get("claim") or expect["type"]
        with allure.step(f"断言：{title}"):
            result = check(page, expect, ctx=ctx)
            results.append(result)
            allure.attach(
                json.dumps(result["evidence"], ensure_ascii=False, indent=2),
                "断言证据", allure.attachment_type.JSON,
            )

    verdict = combine(results, case.get("expect_mode", "and"), case.get("min_pass"))
    with allure.step("终局判定"):
        allure.attach(
            json.dumps(verdict, ensure_ascii=False, indent=2),
            "断言汇总", allure.attachment_type.JSON,
        )
        # 失败时补一张最终截图，报告里能直接看到当时页面
        attach_screenshot(page, when_failed=not verdict["ok"])
        # 录屏的取舍放在这里，而且必须在 `assert` **之前**：
        # 断言失败时也要把录屏附上——`-1` 档正是为这种运行留的。
        # `-1` 档"删除"的动作也必须写进报告，所以无论留不留都附一段说明（§6.4）。
        state["note"] = note
        allure.attach(
            _finish_video(state["recording"], options["video_record"], snapshot,
                          ok=verdict["ok"], note=note),
            "录屏说明", allure.attachment_type.TEXT,
        )
        assert verdict["ok"], (
            f"{verdict['detail']}；明细见「断言汇总」附件"
            + (f"；执行备注：{note}" if note else "")
        )


def attach_environment_once(case):
    """会话级环境信息，供 Allure 的 Environment 区块展示。"""
    attach_environment(case, agent_module)