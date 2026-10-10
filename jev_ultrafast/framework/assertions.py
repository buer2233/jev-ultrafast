"""终局断言层：把 YAML 里的 expect 变成 pytest 能 assert 的确定性结果。

两级断言的边界：
  · 执行期（agent 主循环）用 Jev 的 choice 概率做连续决策——那是 model.choose() 的职责，本模块不碰。
  · 终局（用例结束）用 pytest 的确定性 assert。语义性预期交给 Jev 的 noul 提供【证据】，
    阈值比较留在代码里，因此"是否通过"仍然是确定性的。

每个 checker 只回答"这条预期是否成立 + 证据是什么"，不自己抛断言；
是否通过由 runner 用 assert 决定，失败才能被 pytest 正常捕获与报告。
"""

from .config import DEFAULT_AI_THRESHOLD


def _text_of(page):
    return " ".join((page.get("text") or "").split())


def _find(page, label, exact=False):
    """按 label 找可操作元素；大小写与空白不敏感。"""
    target = " ".join(label.split())
    for action in page.get("actions", ()):
        candidate = " ".join((action.get("label") or "").split())
        if (candidate == target) if exact else (target in candidate):
            return action
    return None


# ------------------------------- 确定性 checker -------------------------------

def check_url_contains(page, expect, _ctx):
    value = expect["value"]
    url = page.get("url") or ""
    ok = value in url
    return {
        "ok": ok,
        "detail": f"URL 期望包含 {value!r}，实际为 {url!r}",
        "evidence": {"url": url, "expected_contains": value},
    }


def check_url_equals(page, expect, _ctx):
    url = page.get("url") or ""
    if expect.get("ignore_query"):
        url = url.split("?")[0]
    ok = url == expect["value"]
    return {
        "ok": ok,
        "detail": f"URL 期望等于 {expect['value']!r}，实际为 {url!r}",
        "evidence": {"url": url, "expected": expect["value"]},
    }


def check_text_contains(page, expect, _ctx):
    value = expect["value"]
    text = _text_of(page)
    count = text.count(value)
    need = int(expect.get("count", 1))
    ok = count >= need
    return {
        "ok": ok,
        "detail": f"页面文本期望出现 {value!r} 至少 {need} 次，实际 {count} 次",
        "evidence": {"expected": value, "count": count, "required": need},
    }


def check_text_not_contains(page, expect, _ctx):
    value = expect["value"]
    text = _text_of(page)
    ok = value not in text
    return {
        "ok": ok,
        # detail 必须是一句【真话】：通过时不能照搬失败模板，否则报告会反着说话——
        # 实测（2026-09-29）一条全绿的用例，断言汇总里写着"不应出现 '流转设定'，但出现了"。
        "detail": (
            f"页面文本不应出现 {value!r}，实际未出现" if ok
            else f"页面文本不应出现 {value!r}，但出现了"
        ),
        "evidence": {"unexpected": value, "present": not ok},
    }


def check_element_exists(page, expect, _ctx):
    action = _find(page, expect["label"], expect.get("exact", False))
    if action and expect.get("role"):
        action = action if action.get("role") == expect["role"] else None
    return {
        "ok": action is not None,
        "detail": f"页面上{'存在' if action else '不存在'}元素 {expect['label']!r}",
        "evidence": {"label": expect["label"], "found": action is not None,
                     "matched": action.get("label") if action else None},
    }


def check_element_absent(page, expect, _ctx):
    action = _find(page, expect["label"], expect.get("exact", False))
    return {
        "ok": action is None,
        # 同 check_text_not_contains：通过时不能说"但找到了"。
        "detail": (
            f"页面上不存在元素 {expect['label']!r}" if action is None
            else f"页面上不应存在元素 {expect['label']!r}，但找到了 {action.get('label')!r}"
        ),
        "evidence": {"label": expect["label"], "found": action is not None},
    }


def check_element_value(page, expect, _ctx):
    action = _find(page, expect["label"], expect.get("exact", False))
    if action is None:
        return {
            "ok": False,
            "detail": f"找不到元素 {expect['label']!r}，无法校验其值",
            "evidence": {"label": expect["label"], "found": False},
        }
    actual = action.get("value") or ""
    if "equals" in expect:
        ok = actual == expect["equals"]
        wanted, relation = expect["equals"], "等于"
    else:
        ok = expect["contains"] in actual
        wanted, relation = expect["contains"], "包含"
    return {
        "ok": ok,
        "detail": f"元素 {expect['label']!r} 的值期望{relation} {wanted!r}，实际 {actual!r}",
        "evidence": {"label": expect["label"], "actual": actual, "expected": wanted},
    }


def check_element_count(page, expect, _ctx):
    """按 label 子串数元素；可选 `role=` 只数某个角色的那批。

    **为什么需要 role 过滤**：页面上同一个标题可能同时出现在**两个不同的表面**——
    E9 的待办列表里有一行，右下角「有流程到达」通知浮层里还有一条（实测 2026-10-10）。
    通知不可关、不自动消失，会把「这条流程已经离开我的待办」这类断言**污染成永远红**。
    两者的区别是角色：列表行是 `link`，通知条目是别的角色。**断言该落在列表上**，
    就按 `role: link` 数——这不是放宽，是把话说准。
    """
    needle = expect["label_contains"]
    wanted_role = expect.get("role")
    matches = [
        a for a in page.get("actions", ())
        if needle in (a.get("label") or "")
        and (wanted_role is None or a.get("role") == wanted_role)
    ]
    count = len(matches)
    if "equals" in expect:
        ok, wanted = count == int(expect["equals"]), expect["equals"]
    elif "min" in expect:
        ok, wanted = count >= int(expect["min"]), f"≥{expect['min']}"
    else:
        ok, wanted = count <= int(expect["max"]), f"≤{expect['max']}"
    scope = f"（限定 role={wanted_role}）" if wanted_role else ""
    return {
        "ok": ok,
        "detail": f"匹配 {needle!r} 的元素数量{scope}期望 {wanted}，实际 {count}",
        "evidence": {"label_contains": needle, "role": wanted_role, "count": count,
                     "expected": wanted, "labels": [a.get("label") for a in matches][:10]},
    }


def check_element_enabled(page, expect, _ctx):
    action = _find(page, expect["label"], expect.get("exact", False))
    usable = action is not None and not action.get("disabled")
    return {
        "ok": usable,
        "detail": f"元素 {expect['label']!r} 期望可用，实际{'找到且可用' if usable else '未找到或被禁用'}",
        "evidence": {"label": expect["label"], "found": action is not None,
                     "disabled": bool(action.get("disabled")) if action else None},
    }


# ------------------------------- 语义 checker -------------------------------

def check_ai(page, expect, ctx):
    """语义断言：Jev 的 noul 提供概率，阈值比较在代码里。"""
    threshold = float(expect.get("threshold", ctx["threshold"]))
    result = ctx["judge"]([expect["claim"]])[0]
    probability = result["noul"]
    ok = probability >= threshold
    return {
        "ok": ok,
        "detail": (
            f"语义断言{'通过' if ok else '未通过'}：{expect['claim']}；"
            f"noul={probability:.3f}，阈值={threshold}"
        ),
        "evidence": {"claim": expect["claim"], "noul": round(probability, 4),
                     "threshold": threshold, "model": result.get("model"),
                     "latency_ms": result.get("latency_ms")},
    }


CHECKERS = {
    "ai": check_ai,
    "url_contains": check_url_contains,
    "url_equals": check_url_equals,
    "text_contains": check_text_contains,
    "text_not_contains": check_text_not_contains,
    "element_exists": check_element_exists,
    "element_absent": check_element_absent,
    "element_value": check_element_value,
    "element_count": check_element_count,
    "element_enabled": check_element_enabled,
}


def check(page, expect, *, ctx):
    """执行单条断言，返回 {"ok", "detail", "evidence"}。"""
    kind = expect.get("type")
    checker = CHECKERS.get(kind)
    if checker is None:
        raise ValueError(f"未知的断言类型 {kind!r}；可用：{sorted(CHECKERS)}")
    return checker(page, expect, ctx)


def combine(results, mode="and", min_pass=None):
    """把多条断言结果合成为一个终局判定。

    模式由【每条用例】自己声明，不设全局默认口径——"全部满足"与"满足其一"
    是业务语义，不是框架偏好（见 AGENTS.md「断言」一节）。
    """
    passed = [bool(r["ok"]) for r in results]
    total = len(passed)
    if mode == "and":
        ok, detail = all(passed), f"{sum(passed)}/{total} 条断言通过（要求全部通过）"
    elif mode == "or":
        ok, detail = any(passed), f"{sum(passed)}/{total} 条断言通过（要求任一通过）"
    elif mode == "min_pass":
        need = int(min_pass or 1)
        ok, detail = sum(passed) >= need, f"{sum(passed)}/{total} 条断言通过（要求至少 {need} 条）"
    else:
        raise ValueError(f"未知的 expect_mode={mode!r}；可用 and / or / min_pass")
    return {"ok": ok, "detail": detail, "mode": mode, "results": results}


def resolve_threshold(expect, case):
    """三级优先级：断言级 > 用例级 > 全局默认。"""
    for value in (expect.get("threshold"), case.get("threshold")):
        if value is not None:
            return float(value)
    return DEFAULT_AI_THRESHOLD