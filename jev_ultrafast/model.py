"""TypeSafe makes choices; an optional small OpenAI-compatible model writes field values."""

import json
import math
import os
import time

import httpx

from .questions import NEXT_ACTION, TARGET, TEXT_VALUE

CLIENT = httpx.Client(http2=True, timeout=25)

# 一次决策最多发几次请求。只重试【决策】，绝不重试浏览器变更操作——这是两件不同的事：
#   · 这里重试的是一次只读的 TypeSafe 请求。走到重试时校验已经失败，而校验失败发生在
#     任何浏览器动作之前，所以重试不可能重复点击、重复提交。
#   · 变更操作的重试会真的再点一次，所以一律不重试（见 AGENTS.md 的「唯一重跑策略」）。
# 网络层的重试不在这里：post_json 已经处理了 429/529/503 与连接失败。
#
# 为什么需要：实测（2026-09）TypeSafe 偶尔返回自相矛盾的响应——choice 选了 DONE（0.37），
# 但 CLICK 的概率更高（0.38），validate_choice 拒收，整条用例就此死掉，尽管浏览器一动没动。
# 5 次运行里撞到 1 次。重发一次即可自愈。
DECISION_ATTEMPTS = 2


def post_json(url, key, body, *, trace=None):
    """发一个只读的模型请求；瞬时故障有界重试。

    重试的判据与 DECISION_ATTEMPTS 那条一致——**有没有东西被执行过**。这里没有：
    三个调用方（决策、文本生成、语义断言）都是只读请求，重试不会重复点击、
    也不会重复提交。所以传输层故障（连接失败/超时/重置）与 429/529/503 同等对待。

    以前连接失败是【直接抛】不重试的，而它和 429 一样是瞬时的、也一样安全。
    实测（2026-09）：演示用例的首次尝试就死在这里，靠 pytest 的用例级重跑才过——
    那次失败本可以在这一层自愈。

    `trace`：可选的一个 list，函数会把"发了几次、每次为什么重来"追加进去。
    为什么需要它——**这一层的重试原本在报告里一个字都没有**，而它的代价是实打实的：
    客户端超时是 25s，超时一次就变成"25s 超时 + 退避 + 再来一次 ≈ 48s"，
    报告里只看到 `决策耗时ms=47530` 和 `重发次数=1`（那个字段只记*决策层*重发），
    于是"这 47 秒到底怎么回事"只能靠猜。实测（2026-09-25）正是这样：两次几十秒的决策
    请求体反而是全表最小的，一眼看不出与规模有关。
    与 `decision_attempts` 同一条规矩：**重发要看得见，不静默自愈。**
    """
    for attempt in range(3):
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError as error:
            if trace is not None:
                trace.append({"第几次": attempt + 1, "结果": f"传输层失败：{type(error).__name__}"})
            if attempt < 2:
                time.sleep(0.5 * 2**attempt)
                continue
            raise RuntimeError("Model connection failed; no action executed.") from error
        if response.status_code in {429, 529, 503} and attempt < 2:
            if trace is not None:
                trace.append({"第几次": attempt + 1, "结果": f"HTTP {response.status_code}"})
            time.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            raise RuntimeError(f"Model provider returned HTTP {response.status_code}; no action executed.")
        if trace is not None:
            trace.append({"第几次": attempt + 1, "结果": "成功"})
        return response.json()
    raise RuntimeError("Model unavailable")


def validate_choice(answer, ids):
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Invalid TypeSafe response; no action executed.")
    return answer


# 操作 kind → 上报给模型的操作名。action_space() 与 _drop_covered() 共用这一份，
# 免得两处各写一遍、改一处漏一处。
OPERATION_KINDS = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = OPERATION_KINDS
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in ("role", "value", "checked", "selected", "expanded") if k in action}
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets, controls


def _decision_once(body, operations, targets, trace=None):
    """发一次决策请求并校验，返回 (result, operation_answer, target_answer)。

    任一步校验不过就抛错，由 choose() 决定是否重发。刻意不在这里吞掉异常：
    重发的安全性论证见 DECISION_ATTEMPTS 的注释。
    """
    result = post_json("https://api.typesafe.ai/v1/systemone", os.environ["TYPESAFE_API_KEY"], body,
                       trace=trace)
    operation_answer = validate_choice(result["answers"].get("operation", {}), operations)
    operation = operation_answer["choice"]
    target_answer = None
    if operation in targets:
        # Unused target heads cannot cause an action. Validate the head selected by the operation.
        target_answer = validate_choice(
            result["answers"].get(operation.lower() + "_target", {}), targets[operation]
        )
    return result, operation_answer, target_answer


def _decision(body, operations, targets):
    """向 TypeSafe 要一次决策，响应不合法时有界重发。

    返回 (result, operation_answer, target_answer, 实际请求次数, 传输层尝试记录)。
    返回请求次数是为了让重发在报告里【看得见】——静默自愈会让偶发的服务端不一致
    永远不被发现，而这个项目的一贯做法是把这类事留痕（见 attach_decision）。
    传输层记录（trace）同理：它记的是【每一次 HTTP 尝试】的结果，与 `decision_attempts`
    （响应不合法导致的重发）是两件事，合起来才解释得清"这一次为什么花了 47 秒"。
    """
    trace = []
    for attempt in range(1, DECISION_ATTEMPTS + 1):
        try:
            result, operation_answer, target_answer = _decision_once(body, operations, targets, trace)
        except (ValueError, KeyError, TypeError):
            # 响应不合法：这次决策作废，但没有任何浏览器动作被执行过，重发是安全的。
            if attempt >= DECISION_ATTEMPTS:
                raise
            continue
        return result, operation_answer, target_answer, attempt, trace
    raise RuntimeError("Decision request never produced a usable response.")


def _discard_summary(discards):
    """把被拒的尝试按目标聚合成"试了几次、为什么被拒"。

    为什么聚合而不是截断列表：同一个目标被拒 30 次时，`[-6:]` 会把列表塞满 6 条
    一模一样的记录就不再更新——模型永远读到"被拒 6 次"，读不出"你已经在这上面耗了
    30 步"。实测（2026-09-24，E9 后端引擎页）正是这样：补了丢弃记录之后概率从 0.55
    掉到 0.45（说明信息确实影响了它），但列表饱和于 6 条同项，直到第 48 步才换目标。
    attempts 计数没有饱和问题，而且那个数字本身就是最强的"换条路"信号。
    """
    merged = {}
    for item in discards:
        key = (item.get("action"), item.get("kind"))
        slot = merged.setdefault(
            key,
            {"action": item.get("action"), "kind": item.get("kind"), "attempts": 0,
             "reason": item.get("reason")},
        )
        slot["attempts"] += 1
        slot["reason"] = item.get("reason")   # 保留最后一次的成因
    return sorted(merged.values(), key=lambda slot: -slot["attempts"])[:6]


# 同一个目标被执行层拒到几次之后，就没必要再让它出现在候选里了。
#
# 为什么不一开始就把"被遮住的"元素统统剔掉（那是更简单的做法）：候选是按**观察那一刻的
# 几何**算的，而执行层会先 scrollIntoView 再做命中测试，两边未必一致——无条件剔除会误杀
# 本来能点的元素。而"被拒过 k 次"是**实测证据**：那个目标已经被浏览器当场拒绝过了。
# 所以这里剔掉的不是"看起来点不了的"，是"确实点不了的"。
REFUSED_TARGET_THRESHOLD = 3


def _refused_targets(discards, page_key, threshold=REFUSED_TARGET_THRESHOLD):
    """本文档内被拒到 threshold 次以上的 (node, kind) 集合。

    三个条件缺一不可：
      · 只看 target_level 的丢弃——"页面整体变了"是瞬时的，重选无妨；
      · document 必须等于当前文档——node id 由 WeakMap 计数器发放，**换文档会从 1 重发**，
        不比对文档就会把新页面上的无辜元素按旧编号剔掉；
      · 按 (node, kind) 而不是只按 node——同一元素上"点击被拒"不代表"填值也会被拒"。
    """
    origin = page_key[0] if page_key else None
    counts = {}
    for item in discards:
        if not item.get("target_level") or item.get("document") != origin:
            continue
        node = item.get("node")
        if node is None:
            continue
        key = (node, item.get("kind"))
        counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count >= threshold}


def _drop_covered(actions):
    """同操作还有未遮挡候选时，把被遮挡的候选整条去掉。

    这不是新政策，是**把提示词里已有的规则改成框架强制**。`questions.TARGET` 里写着
    "NEVER choose a covered target while any uncovered target is offered"，而实测
    （2026-09-29，E9「添加路径」弹窗，连续三次全量跑）模型每次都违反它：弹窗一打开，
    123 条候选里 94 条被标 `covered`，模型仍然去挑那个已被盖住的放大镜，目标概率只剩
    0.26，执行层 100% 拒绝（当轮 7/7），随后空转直到判 BLOCKED。

    与 `_refused_targets` 的分工：那一条用"已被浏览器当场拒过 k 次"的**事后证据**，
    所以给到 3 次才剔；这一条用**同一次观察里算出来的几何**（snapshot.js 的命中测试，
    与候选表同源、同一时刻），属于事前证据，故可立即生效。

    只在"该操作还有未遮挡候选"时才剔——全部候选都被遮挡时原样保留，模型仍有得选。
    这也正是提示词那条规则的字面条件。
    """
    uncovered = {
        action.get("kind") for action in actions
        if action.get("kind") in OPERATION_KINDS and not action.get("covered")
    }
    if not uncovered:
        return actions
    return [
        action for action in actions
        if action.get("kind") not in OPERATION_KINDS
        or not action.get("covered")
        or action.get("kind") not in uncovered
    ]


def _available_actions(state, discards):
    """去掉已被证伪或被遮挡的目标；wait/scroll 这类没有 node 的伪动作一律保留。

    两道过滤都只做减法，且都不该误杀"本来能点的"：
      · `_refused_targets`：被执行层当场拒过若干次的目标（事后证据，见其注释）；
      · `_drop_covered`：同操作还有未遮挡候选时，剔掉被遮挡的那些（几何证据）。
    """
    refused = _refused_targets(list(discards), state.get("page_key"))
    if not refused:
        return _drop_covered(list(state["actions"]))
    remaining = [
        action for action in state["actions"]
        if action.get("node") is None or (action["node"], action.get("kind")) not in refused
    ]
    return _drop_covered(remaining)


def choose(state, goal, history, discards=()):
    elements, targets, controls = action_space(_available_actions(state, discards))
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
        "SELECT": "Select an observed dropdown value.",
    }
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(DONE="Every requirement is visibly satisfied.", BLOCKED="No supported operation can progress.")
    questions = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal, "rules": NEXT_ACTION}}
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    # covered 来自 snapshot.js 的命中测试：该元素此刻被别人盖住（典型是弹窗
                    # 打开后仍留在候选表里的下层控件）。必须原样透给模型——执行层会拒绝
                    # 点它，模型看不见就会被反复选中，空转到步数上限（见 TARGET 的规则）。
                    **{k: a[k] for k in ("role", "checked", "selected", "expanded", "covered") if k in a},
                }
                for index, a in candidates.items()
            },
            "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, TARGET]},
        }
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "page": {k: state[k] for k in ("url", "title", "text")},
            "elements": elements,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed")} for h in history[-10:]
            ],
            # 没落地的尝试（被遮住/页面已变，执行层拒了）。与 recent_actions 并列而不是混进去：
            # 后者是"真的发生过什么"，前者是"试过但没发生"。模型必须能分辨这两件事——
            # 少了它就会反复重选同一个被拒的目标（实测空转 87 步）。
            # attempts 是聚合后的次数（见 _discard_summary）：饱和的重复条目读不出严重程度。
            "recent_discarded_attempts": _discard_summary(list(discards)),
        },
        "questions": questions,
    }
    started = time.perf_counter()
    result, operation_answer, target_answer, attempts, transport = _decision(body, operations, targets)

    operation = operation_answer["choice"]
    target = None
    probabilities = {}
    if operation in targets:
        target = target_answer["choice"]
        choice = targets[operation][target]["id"]
        probabilities = {a["id"]: target_answer["probabilities"][index] for index, a in targets[operation].items()}
    else:
        choice = controls[operation]["id"] if operation in controls else operation
        probabilities[choice] = operation_answer["probabilities"][operation]
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "confidence": operation_answer["confidence"],
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "raw_answers": result["answers"],
        "model": result["model"],
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        # >1 表示这次决策重发过：响应不合法但浏览器没被动过。报告里应当看得见。
        "decision_attempts": attempts,
        # 传输层每一次 HTTP 尝试的结果。与 decision_attempts 分开记：
        # 那一个管"响应不合法"，这一个管"根本没拿到响应"（超时/429/连接失败）。
        # 少了它，一次"25s 超时 + 重试"在报告里只表现为耗时异常大的一个数字，无从解释。
        "transport_attempts": list(transport),
        "request": body,
    }


def field_context(goal, action, page, history):
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }


def reasoning_fields(base):
    """按端点选择真正能关掉思考的参数形态。

    不同网关认不同的字段名，发错了不会报错，只会静默继续思考：
      阿里云百炼/DashScope 兼容模式 → enable_thinking
      DeepSeek 官方                 → thinking.type
      OpenRouter / 通用             → reasoning.enabled
    实测：阿里云端点上 reasoning.enabled=false 被忽略（推理 token 31/39），
    改用 enable_thinking=false 后为 0/7，且省约 900 ms。
    """
    override = os.environ.get("TEXT_MODEL_REASONING", "none")
    if override not in ("none", ""):
        # 显式覆盖：允许诊断时切成 low/medium/high 等档位
        return {"reasoning": {"effort": override}}
    if "aliyuncs.com" in base or "dashscope" in base:
        return {"enable_thinking": False}
    if "api.deepseek.com" in base:
        return {"thinking": {"type": "disabled"}}
    return {"reasoning": {"enabled": False}}


class NoTextValue(ValueError):
    """文本模型没能给出可写入这个字段的值——**没有任何浏览器动作被执行过**。

    为什么要与"页面过期"分开：`questions.TEXT_VALUE` 明确把 `{"text": null}` 定义成
    **合法回复**（"If a required value is missing"），意思是"按这条 goal，这个字段没有
    可填的值"。执行器收到它就抛异常判死整条用例，等于**提示词承诺了一个协议、
    执行器不认**。实测（2026-10-09，E9 新建流程 TC01）：模型挑错了字段（goal 明说只填
    「标题」，它却选了「签字意见」），文本模型照约定回 `{"text": null}`，
    用例当场崩在 `ValueError` 上——**修的地方不是这句异常，是"崩"这个反应本身**：
    此刻什么都没执行过，重决策完全安全（见 AGENTS.md「决策可以重发，动作不可以」，
    判据是有没有东西被执行过）。

    `missing` 区分责任方，调用方据此决定要不要把目标从候选里剔掉：
      · `missing=True`：模型照约定回了 null——问题在**选中的目标**上（这个字段
        按 goal 确实没值），与 `browser.TargetUnavailable` 同类，够次数就该剔；
      · `missing=False`：返回结构不合约定（解析不了/空串/超长）——是**协议故障**，
        与目标无关，不该牵连目标，否则会把一个无辜的字段剔掉。
    """

    def __init__(self, message, *, missing):
        super().__init__(message)
        self.missing = missing


def field_text(context):
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("TYPE_TEXT needs TEXT_MODEL_API_KEY; no text is hardcoded or guessed by the executor.")
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("TEXT_MODEL", "deepseek-chat")
    reasoning = reasoning_fields(base)
    started = time.perf_counter()
    result = post_json(
        base + "/chat/completions",
        key,
        {
            "model": model,
            "max_tokens": 1024,
            "response_format": {"type": "json_object"},
            **reasoning,
            "messages": [
                {"role": "system", "content": TEXT_VALUE},
                {
                    "role": "user",
                    "content": json.dumps(context),
                },
            ],
        },
    )
    # 四种坏结果分开报，因为**责任方不同**（见 NoTextValue 的注释）。共同的判据是
    # 每条消息都带 "nothing typed"——它同时是既有测试的匹配串，也是这条契约的要点：
    # 走到这里一定什么都没写进去。
    try:
        output = json.loads(result["choices"][0]["message"]["content"])
    except (ValueError, TypeError, KeyError, IndexError):
        raise NoTextValue(
            "Text helper returned unparseable output; nothing typed.", missing=False
        ) from None
    if not isinstance(output, dict) or set(output) != {"text"}:
        raise NoTextValue(
            f"Text helper returned unexpected shape {str(output)[:120]!r}; nothing typed.",
            missing=False,
        )
    value = output["text"]
    if value is None:
        raise NoTextValue(
            "Text helper reported no value for this field; nothing typed.", missing=True
        )
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise NoTextValue(
            "Text helper returned no usable field value; nothing typed.", missing=False
        )
    return value, {
        "model": model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
    }
