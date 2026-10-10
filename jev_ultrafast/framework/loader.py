"""自然语言用例的加载与校验。

用例用 YAML 写：非技术同事可读可改，同时保持机器可校验。
在【加载期】就把错误报出来，而不是等跑到一半才炸。
"""

import os
import re
from datetime import datetime
from pathlib import Path

import yaml

from .assertions import CHECKERS

# 与 demo.py:51 的约束保持一致
MAX_GOAL_LENGTH = 2000
EXPECT_MODES = {"and", "or", "min_pass"}
SEVERITIES = {"blocker", "critical", "normal", "minor", "trivial"}

CASE_KEYS = {
    "id", "name", "feature", "epic", "severity", "tags", "url", "login", "goal",
    "expect", "expect_mode", "min_pass", "threshold", "timeout_s", "max_steps", "on_failure",
    # 逐用例的重跑次数（覆盖 pytest --reruns）。负向对照这类"本来就该失败"的用例
    # 设 reruns: 0，避免白跑一遍。只做【用例级】重跑，绝不做步骤级。
    "reruns",
    # 逐用例的浏览器窗口尺寸（[宽, 高] 或 "1920x1080"）。为什么需要见 config.parse_viewport：
    # 视口外的元素不会成为候选，站点有"比默认视口还宽"的编辑器时，用例必须如实放大窗口。
    "viewport",
    # 命名前置：这条用例动手之前，环境里必须先存在什么（走接口准备，在 fixture 里执行）。
    # 取值是 e9_setup.SETUPS 里的名字（"none" / "wf-approval2" / …）。
    # 为什么要有这个字段：源功能用例大多是跨多个操作者的流转链，而 UI 用例一条只有
    # 一个登录身份——"把流程推到你要动手的那个节点"表达不了，只能声明后由 fixture 备好。
    "setup",
    # 明确跳过，并**写出理由**。给"环境上根本表达不了"的用例留一条诚实出口。
    #
    # 为什么需要它：源用例里有些改写依赖一条**环境前提**，而前提不成立时，
    # 用例既跑不通、也不该被改成"测别的东西"来凑绿——那等于偷偷换掉了它在测什么。
    # 与其如此，不如让它以 SKIP 出现在报告里，理由写在报告脸上。
    # 实测（2026-10-09）：TC06 的改写前提是"新建表单有必填项不填会被拦"，
    # 而目标环境上「密级」「紧急程度」都有默认值，不填任何东西直接提交**会成功**——
    # 前提不成立。见 cases/e9/workflow_submit.yaml 的 TC06。
    "skip",
}
DEFAULTS_KEYS = {
    "login", "timeout_s", "max_steps", "expect_mode", "min_pass", "threshold",
    "tags", "feature", "epic", "severity", "viewport",
}

# 变量：{{ base_url }} 指向 E9，{{ fixture_url }} 指向本地样例页。
# 用例里【不得】写真实内网地址——本仓库是公网可访问的 fork。
VARIABLE_PATTERN = re.compile(r"\{\{\s*(\w+)\s*\}\}")


class CaseError(ValueError):
    """用例文件不合法。"""


def _default_variables():
    from . import e9_api, e9_config, e9_setup
    from .config import E9_ENTRY_PATH, E9_WF_PATH_LIST_PATH

    # base_url 的读取收敛在 e9_config（环境变量 > 本地 config.json）
    base = e9_config.base_url()
    return {
        "base_url": base,
        "e9_entry": f"{base}{E9_ENTRY_PATH}" if base else "",
        # 后端引擎的流程列表（搭流程定义的入口）。与 e9_entry 分属两个 SPA，
        # 不能互相替代——见 config.E9_WF_PATH_LIST_PATH 的注释。
        "wf_path_list": f"{base}{E9_WF_PATH_LIST_PATH}" if base else "",
        # 本地样例页由 conftest 的 fixture 服务器提供
        "fixture_url": os.environ.get("JEV_FIXTURE_URL", ""),
        # 前置建模模块的名字。取值收敛在 e9_api，让 fixture（执行期建数据）
        # 与用例 goal（收集期替换变量）引用同一个常量，不会各写一份而漂移。
        "eb_mode_name": e9_api.EB_MODE_NAME,
        # **流程模板名**（路径名），界面上的中文叫法。UI 自己建实例的用例
        # （setup: none）要在「新建流程」列表里点它。
        # ⚠️ 不要拿 {{ wf_request_name }}（实例标题）来干这件事——那是实例的标题，
        # 只出现在待办/已办里，**模板列表里根本没有它**。
        "wf_path_name": e9_setup.PATH_NAME,
        # 路径在界面上的**完整显示名**。要在「新建流程」那个分组列表里点模板的用例
        # 必须用它，**不要用 wf_path_name**——那是给接口侧子串检索用的短关键字
        # （"流程提交"），而列表里有几百条流程，goal 写"名字含 X"会请模型去凑合。
        # 实测（2026-10-09，TC01）：模型因此点成另一条路径（workflowid=8025≠12522），
        # 整条用例作废。见 e9_setup.WORKFLOW_NAME 的注释。
        "wf_workflow_name": e9_setup.WORKFLOW_NAME,
        # 「密级」下拉的选项名。E9 每张表单都有这个必填下拉，而它的选项名与环境有关
        # （见 e9_setup.LEVEL_OPTION 的注释：不指名它，模型会反复点下拉框本身）。
        "wf_level_option": e9_setup.LEVEL_OPTION,
        # 本次收集的时间戳，用来给"每次运行都该新建的测试数据"取一个**唯一名字**。
        #
        # 为什么需要：搭流程这类用例每次运行都要新建一条路径，如果名字固定，
        # 环境里会堆出几十条同名的，下一条用例（要打开"刚建好的那条"）就无从分辨——
        # 实测在一个已被反复写过的环境里，模型打开的是哪一条完全靠运气。
        # 同一个用例文件里所有用例共用这一次的值（一次收集只算一次），
        # 所以"建流程"和"用这条流程"的两条用例能对上。
        "run_id": datetime.now().strftime("%m%d%H%M%S"),
    }


def _substitute(value, variables, missing):
    """替换 {{ 变量 }}；未解析的记入 missing，由调用方决定跳过而不是整体报错。

    这样"只想跑本地样例"时不会因为 E9_BASE_URL 没配而连收集都失败。
    """
    if not isinstance(value, str):
        return value

    def replace(match):
        name = match.group(1)
        resolved = variables.get(name)
        if not resolved:
            missing.add(name)
            return match.group(0)  # 保留原样，报错信息里能直接看到是哪个变量
        return resolved

    return VARIABLE_PATTERN.sub(replace, value)


def _substitute_deep(value, variables, missing):
    """递归替换嵌套结构（dict / list / str）里的 {{ 变量 }}。

    只对字符串做替换，其它类型原样返回——`count: 3` 这类数值不该被碰。
    """
    if isinstance(value, str):
        return _substitute(value, variables, missing)
    if isinstance(value, dict):
        return {key: _substitute_deep(item, variables, missing) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute_deep(item, variables, missing) for item in value]
    return value


def _require(mapping, key, where):
    value = mapping.get(key)
    if value in (None, "", []):
        raise CaseError(f"{where}: 缺少必填字段 {key!r}")
    return value


def _validate_expect(expect, where):
    if not isinstance(expect, list) or not expect:
        raise CaseError(f"{where}: expect 必须是非空列表")
    for index, item in enumerate(expect, 1):
        spot = f"{where} 第 {index} 条断言"
        if not isinstance(item, dict):
            raise CaseError(f"{spot}: 必须是映射")
        kind = item.get("type")
        if kind not in CHECKERS:
            raise CaseError(f"{spot}: 未知的 type={kind!r}；可用：{sorted(CHECKERS)}")
        if kind == "ai" and not item.get("claim"):
            raise CaseError(f"{spot}: ai 类型必须提供 claim")
        if kind in {"text_contains", "text_not_contains", "url_contains", "url_equals"} and "value" not in item:
            raise CaseError(f"{spot}: {kind} 必须提供 value")
        if kind in {"element_exists", "element_absent", "element_enabled"} and not item.get("label"):
            raise CaseError(f"{spot}: {kind} 必须提供 label")
        if kind == "element_value" and not (item.get("equals") or item.get("contains")):
            raise CaseError(f"{spot}: element_value 必须提供 equals 或 contains")
        if kind == "element_count" and not (
            item.get("equals") is not None or item.get("min") is not None or item.get("max") is not None
        ):
            raise CaseError(f"{spot}: element_count 必须提供 equals / min / max 之一")
        threshold = item.get("threshold")
        if threshold is not None and not 0 <= float(threshold) <= 1:
            raise CaseError(f"{spot}: threshold 必须在 0–1 之间")


def _validate_case(case, defaults, where):
    unknown = set(case) - CASE_KEYS
    if unknown:
        # 宁可报错也不要静默忽略：字段名拼错会让整条断言悄悄失效
        raise CaseError(f"{where}: 未知字段 {sorted(unknown)}；可用：{sorted(CASE_KEYS)}")

    mode = case.get("expect_mode", defaults.get("expect_mode", "and"))
    if mode not in EXPECT_MODES:
        raise CaseError(f"{where}: expect_mode={mode!r} 非法；可用 {sorted(EXPECT_MODES)}")
    if mode == "min_pass":
        need = case.get("min_pass", defaults.get("min_pass"))
        total = len(case.get("expect") or [])
        if need is None:
            raise CaseError(f"{where}: expect_mode=min_pass 时必须提供 min_pass")
        if not 1 <= int(need) <= total:
            raise CaseError(f"{where}: min_pass={need} 必须落在 1–{total} 之间")

    threshold = case.get("threshold", defaults.get("threshold"))
    if threshold is not None and not 0 <= float(threshold) <= 1:
        raise CaseError(f"{where}: threshold 必须在 0–1 之间")

    severity = case.get("severity")
    if severity and severity not in SEVERITIES:
        raise CaseError(f"{where}: severity={severity!r} 非法；可用 {sorted(SEVERITIES)}")

    reruns = case.get("reruns")
    if reruns is not None and (not isinstance(reruns, int) or isinstance(reruns, bool) or reruns < 0):
        raise CaseError(f"{where}: reruns 必须是非负整数，实际为 {reruns!r}")

    goal = case.get("goal") or ""
    if len(goal) > MAX_GOAL_LENGTH:
        raise CaseError(f"{where}: goal 长度 {len(goal)} 超过上限 {MAX_GOAL_LENGTH}")


def load_file(path, variables):
    """加载单个 YAML 文件，返回用例列表。"""
    from . import e9_setup  # 延迟导入：避免 loader ↔ e9_setup 在导入期互相拉扯

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise CaseError(f"{path.name}: 顶层必须是映射")

    unknown = set(raw) - {"version", "defaults", "cases"}
    if unknown:
        raise CaseError(f"{path.name}: 未知的顶层字段 {sorted(unknown)}；可用 version / defaults / cases")

    defaults = raw.get("defaults") or {}
    unknown_defaults = set(defaults) - DEFAULTS_KEYS
    if unknown_defaults:
        raise CaseError(f"{path.name}: defaults 中未知字段 {sorted(unknown_defaults)}")

    cases = raw.get("cases")
    if not isinstance(cases, list) or not cases:
        raise CaseError(f"{path.name}: cases 必须是非空列表")

    output, seen_here = [], {}
    for index, case in enumerate(cases, 1):
        where = f"{path.name} cases[{index}]"
        if not isinstance(case, dict):
            raise CaseError(f"{where}: 必须是映射")

        merged = {**{k: v for k, v in defaults.items() if k not in {"tags"}}, **case}
        merged["tags"] = list(dict.fromkeys([*(defaults.get("tags") or []), *(case.get("tags") or [])]))

        _require(merged, "id", where)
        # 同一文件内也要查重。load_cases 只在【跨文件】层面查，一个文件里写了
        # 两条同 id 的用例会双双加载成功，然后 --case 选中两条、Allure 历史混在一起。
        # 这类错误必须和别的用例错误一样在加载期报出来。
        if merged["id"] in seen_here:
            raise CaseError(
                f"{where}: id 重复 {merged['id']!r}，本文件第 {seen_here[merged['id']]} 条已用过"
            )
        seen_here[merged["id"]] = index
        _require(merged, "name", where)
        _require(merged, "url", where)
        _require(merged, "goal", where)
        _validate_case(merged, defaults, where)
        _validate_expect(merged.get("expect"), where)

        # 逐用例独有的变量：流程实例名。
        #
        # 为什么必须在**收集期**算出来、并挂在用例上（`_wf_request_name`）：
        # goal 里的 {{ 变量 }} 收集期就替换完了，而前置配方在执行期才跑，
        # 两者无法用运行时返回值通信。所以名字由双方用同一个公式算。
        #
        # 为什么名字里要带 run_id **和用例 id**：同一轮收集里多条用例各自要建一条实例，
        # 名字不唯一时，"在列表里找到刚建的那条"就完全靠运气——实测在一个被反复写过的
        # 环境里踩过（见 _default_variables 里 run_id 的那段注释）。
        request_name = e9_setup.request_name_for(variables.get("run_id", ""), merged["id"])
        merged["_wf_request_name"] = request_name
        case_variables = {**variables, "wf_request_name": request_name}

        # 逐用例独有的变量：**系统自动生成的**流程标题（UI 自建实例的用例只能靠它认领）。
        #
        # 为什么与 request_name 是两个变量：`setup:` 走**接口**建实例，标题想写什么就写什么，
        # 所以能用逐用例唯一的 request_name；而 `setup: none` 的用例是**UI 自己建**的，
        # 那台环境的「标题」字段是只读的（见 e9_setup.system_request_name），
        # 标题由系统按公式生成——两者不是同一个东西，混用会让断言永远不成立。
        #
        # 取不到账号时不注入这个键：`_substitute` 会把它记进 missing，用例以
        # "变量未配置"明确 skip。**不要注入空串**——空串能替换成功，断言会变成
        # "页面里含空字符串"这种恒真式，比 skip 危险得多。
        role = merged.get("login")
        if role and role != "none":
            try:
                from . import e9_config

                merged["_wf_system_request_name"] = e9_setup.system_request_name(
                    e9_config.load_account(role)["user_name"]
                )
                case_variables["wf_system_request_name"] = merged["_wf_system_request_name"]
            except (KeyError, OSError):
                pass   # 角色没配 → 不注入，交给 missing 机制 skip

        missing = set()
        merged["url"] = _substitute(merged["url"], case_variables, missing)
        merged["goal"] = _substitute(merged["goal"], case_variables, missing)
        # 断言里的字符串也要替换：像「列表里有标题含 <实例名> 的那条流程」这种断言，
        # 实例名是收集期才算出来的，不替换就只能把名字写死——而写死的名字
        # 下一条用例就撞上了（名字必须逐用例唯一，见上面 _wf_request_name）。
        merged["expect"] = _substitute_deep(merged["expect"], case_variables, missing)
        if missing:
            # 不在这里抛错：让用例照常被收集，执行时以明确原因 skip。
            merged["_skip"] = (
                f"用例依赖的变量未配置：{sorted(missing)}。"
                + ("请在 .env 中设置 E9_BASE_URL。" if "base_url" in missing else "")
            )
        elif merged.get("skip"):
            # 用例自己声明的跳过（环境上表达不了，理由写在 YAML 里）。
            # **后置于变量缺失**：变量没配是执行环境的问题，理由要排在前面。
            merged["_skip"] = str(merged["skip"])
        merged["login"] = merged.get("login", "none")
        merged["expect_mode"] = merged.get("expect_mode", defaults.get("expect_mode", "and"))
        if merged["expect_mode"] != "min_pass":
            merged.pop("min_pass", None)
        merged["__source__"] = path.name
        output.append(merged)
    return output


def load_cases(directory=None):
    """加载目录下所有用例，并校验 id 全局唯一。"""
    root = Path(directory) if directory else Path(__file__).resolve().parents[2] / "cases"
    if not root.exists():
        return []

    variables = _default_variables()
    cases, seen = [], {}
    for path in sorted(root.rglob("*.yaml")):
        for case in load_file(path, variables):
            if case["id"] in seen:
                raise CaseError(
                    f"用例 id 重复：{case['id']!r} 同时出现在 {seen[case['id']]} 和 {case['__source__']}"
                )
            seen[case["id"]] = case["__source__"]
            cases.append(case)
    return cases