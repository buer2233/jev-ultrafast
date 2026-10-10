"""E9 工作流接口：前置流程实例的建立与流转推进。

**为什么需要这一层。** 本项目的源功能用例（如「流程提交」那一批）大多是
**跨多个操作者的流转链**：人员01 建 → 人员02/03 会签 → 人员04/05 审批 → 人员06 归档。
而 UI 用例一条只有**一个登录身份**（`runner._login_cookies` 在起浏览器之前注入一次
cookie，中途不换人）。所以"把流程推到某个操作者该动手的那个节点"这一步只能走接口，
在 fixture 里做掉；UI 只做"该操作者这一次的动作"。

这与 `e9_api.py` 对建模模块的处理是同一条理由：让 agent 在 UI 里现建前置数据，
既会吃掉用例的步数预算，又会让"前置没建好"和"被测功能有问题"混成同一个失败原因。

接口契约来源（**不猜参数名**）：
  · 同作者项目 api-test-E9 的 `page_api/workflow_api/workflow_base_api.py`
    （已实测跑通的会签用例 `test_workflow_countersign_api.py` 用的就是它）；
  · 与 E9 抓包一致的表单/JSON 编码与请求头约定，沿用 `e9_login` 的那一套。

⚠️ **前置依赖环境里存在一条合适的流程路径**（含会签节点与归档节点，且其审批人
就是 config.json 里配的那几个账号）。找不到时**优雅降级为 skip**，并说清缺什么——
不要伪造成功，也不要退化成"在 UI 里现搭一条路径"。
"""

import time

from . import e9_config, e9_login

# ── 接口路径 ────────────────────────────────────────────────────────────────
CREATE_LIST_PATH = "/api/workflow/paService/getCreateWorkflowList"
CREATE_FORM_PATH = "/api/workflow/paService/getCreateWorkflowRequestInfo"
CREATE_PATH = "/api/workflow/paService/doCreateRequest"
SUBMIT_PATH = "/api/workflow/paService/submitRequest"
STATUS_PATH = "/api/workflow/paService/getRequestStatus"
# 流程详情。**节点名只能从这里读**——`getRequestStatus` 有 currentNodeId 却没有
# currentNodeName（实测 2026-10-09），拿它读节点名会永远得到空串，
# 于是 wait_for_node 轮询到超时、报出"期望 审批2 实际 ''"这种误导性结论。
DETAIL_PATH = "/api/workflow/paService/getWorkflowRequest"

# 创建流程时带上它 = **创建即提交**，一次调用就把流程推到第一个审批节点。
# 少了这次往返，前置推进的代码会翻倍。
NEXT_FLOW = {"isnextflow": "1"}

# 提交时的动作标识。与前端 `formbtn.js` 的 `doSubmitNoBack` 一致
# （`doBeforeSubmit({actiontype:"requestOperation", src:"submit"})`）——
# 保存草稿走的是 `src:"save"`，**两者不是同一条链路**。
SUBMIT_PARAMS = {"src": "submit"}

# 流程流转是异步的：提交返回 SUCCESS 不代表节点已经切过去。
# 实测会签集齐后要等一小会儿 currentNodeName 才更新，所以状态查询带轮询。
STATUS_POLL_ATTEMPTS = 15
STATUS_POLL_INTERVAL_S = 1.0

# 主表里的**系统保留字段名**：它们不走 mainData，走顶层参数（或由服务端自己给默认值）。
# 塞进 mainData 会被判成参数错误。约定取自同作者 api-test-E9 已跑通的
# `test_case/test_workflow_case/test_workflow_r349363.py`，不是猜的。
RESERVED_FIELD_NAMES = {"requestname", "requestlevel", "messageType"}

# 主表字段的 htmlType=6 表示**附件**。带上它要求模板已配置「附件上传目录」，
# 而环境实测多数模板没配，带上必失败（`field_fj: 字段未设置附件上传目录`）。
ATTACH_HTML_TYPE = "6"


class E9WorkflowError(RuntimeError):
    """E9 工作流接口调用失败。"""


class WorkflowPathMissing(E9WorkflowError):
    """环境里找不到可用的流程路径。

    单独一个类型，是为了让 fixture 能把它翻译成 `pytest.skip`（环境没准备好）
    而不是 FAIL（用例有缺陷）——这两件事的处置完全不同。
    """


def _headers(base_url, *, form=False, json_body=False, origin=False):
    """E9 接口共用的请求头。

    与 `e9_login._headers` 同一套约定（Referer 必须是站内，否则被判非法请求），
    这里多一档 JSON：`doCreateRequest` / `submitRequest` 收的是 JSON 体，
    而 `getCreateWorkflowList` 收的是表单体——**同一个模块里两种编码并存**，
    所以编码方式由调用方显式指定，不能按接口名猜。
    """
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": f"{base_url}/wui/index.html",
        "X-Requested-With": "XMLHttpRequest",
    }
    if form:
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"
    elif json_body:
        headers["Content-Type"] = "application/json;charset=utf-8"
    if origin:
        headers["Origin"] = base_url
    return headers


def _call(session, base_url, path, *, form=None, json_body=None, params=None, timeout=30):
    """发一次请求并解析 JSON；非 JSON 响应给出可诊断的报错。

    三种编码形态各自对应一组接口，**不是风格选择**：
      · `params=`（GET）——`getCreateWorkflowRequestInfo` / `getRequestStatus`；
      · `form=`（表单 POST）——`getCreateWorkflowList`；
      · `json_body=`（JSON POST）——`doCreateRequest` / `submitRequest`。
    同一个模块里三种并存，所以由调用方显式指定，不能按接口名猜。
    """
    base = base_url.rstrip("/")
    headers = _headers(base, form=form is not None, json_body=json_body is not None, origin=True)
    kwargs = {"headers": headers, "timeout": timeout}
    if form is not None:
        kwargs["data"] = form
    if json_body is not None:
        kwargs["json"] = json_body

    if params is not None:
        response = session.get(base + path, params=params, **kwargs)
    else:
        response = session.post(base + path, **kwargs)

    response.raise_for_status()
    try:
        return response.json()
    except ValueError as error:
        raise E9WorkflowError(
            f"E9 接口 {path} 返回的不是 JSON（HTTP {response.status_code}）：{response.text[:200]}"
        ) from error


def _is_success(payload):
    """PA 接口的成功判据。

    兼容三种形状：`code` 是字符串 `"SUCCESS"`、`code.statusCode == "1"`、
    或 `code.name == "SUCCESS"`。**不要只认一种**——同一个环境里不同接口的
    `code` 形状实测并不统一。
    """
    if not isinstance(payload, dict):
        return False
    code = payload.get("code")
    if isinstance(code, dict):
        if code.get("name"):
            return str(code["name"]) == "SUCCESS"
        if code.get("statusCode") is not None:
            return str(code["statusCode"]) in {"1", "SUCCESS"}
        return False
    return str(code) in {"SUCCESS", "1"}


def session_for(role, *, base_url=None, timeout=30):
    """按 config.json 里的角色建一个已登录的接口会话。"""
    base = (base_url or e9_config.base_url()).rstrip("/")
    if not base:
        raise E9WorkflowError("未配置 E9 地址：请设置 E9_BASE_URL 或在 config.json 填 base_url")
    account = e9_config.load_account(role)
    return e9_login.open_session(base, account["user_name"], account["password"], timeout=timeout)


def find_workflow_id(session, base_url, name_keyword):
    """在"当前账号可创建的流程列表"里按名字关键字定位 workflowId。

    Raises:
        WorkflowPathMissing: 列表里没有匹配项。**这是环境没准备好，不是代码错误**，
            fixture 会把它翻成 skip。
    """
    payload = _call(session, base_url, CREATE_LIST_PATH, form={})
    items = payload.get("data") if isinstance(payload, dict) else None
    if items is None and isinstance(payload, list):
        items = payload
    if not isinstance(items, list):
        raise E9WorkflowError(f"可创建流程列表的形状不认识：{str(payload)[:200]}")

    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("workflowName") or "")
        workflow_id = item.get("workflowId")
        if name and name_keyword in name and str(workflow_id).isdigit():
            return int(workflow_id)

    names = [str(i.get("workflowName") or "") for i in items if isinstance(i, dict)]
    raise WorkflowPathMissing(
        f"可创建流程列表里没有名字含 {name_keyword!r} 的流程（共 {len(names)} 条：{names[:10]}）。"
        f"请在环境里搭一条含会签节点的路径，或设置 E9_WF_PATH_NAME 指定关键字。"
    )


def build_main_data(session, base_url, workflow_id):
    """从表单定义构造 `mainData`。

    前置数据的目的是"让流程能建出来"，**字段内容与用例无关**，所以策略是：
      1. 有 `isMand` 的字段 → 回填占位值；
      2. **一个 `isMand` 都没有时 → 退回"带上全部字段"**（见下）；
      3. 显式用 `E9_WF_MAINDATA` 指定时优先用它。

    ⚠️ **第 2 条不是保险，是必需**：实测（2026-09-30，`0108测试`）
    `getCreateWorkflowRequestInfo` 报"必填 0 个"，而前端表单里「密级」明明带红色 *、
    不填就点不动保存。也就是说 **`isMand` 与前端实际校验是两套规则**。
    只按 `isMand` 过滤会得到空 `mainData`，接口直接回
    `{code: PARAM_ERROR, errMsg: {mainData: 新建流程主表数据不允许为空}}`——
    **前置必然失败**，而且报错完全看不出是"必填标记不可信"。

    退回全字段的代价是带上一堆空值字段；E9 对多余字段是容忍的（与 `submit_request`
    统一带 `workflowId` 同一个道理：多传兼容，少传致命）。
    """
    import json
    import os

    override = os.environ.get("E9_WF_MAINDATA", "").strip()
    if override:
        try:
            data = json.loads(override)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass  # 解析不了就退回自动构造，不要让一个坏环境变量把前置炸掉

    payload = _call(session, base_url, CREATE_FORM_PATH, params={"workflowId": int(workflow_id)})
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []

    main_table = data.get("workflowMainTableInfo") or {}
    fields = []
    for record in main_table.get("requestRecords") or []:
        if isinstance(record, dict):
            fields.extend(record.get("workflowRequestTableFields") or [])

    named = [f for f in fields if isinstance(f, dict) and f.get("fieldName")]
    mandatory = [f for f in named if f.get("isMand") is True]
    # 没有权威的必填标记时，宁可多带也不能空手——空 mainData 一定会被拒。
    selected = mandatory or named

    main_data = []
    for field in selected:
        name = field["fieldName"]
        # 系统保留字段：`requestname` 走顶层 `requestName` 参数，
        # `requestlevel` / `messageType` 同理。塞进 mainData 会被判参数错误。
        if name in RESERVED_FIELD_NAMES:
            continue
        html_type = str(field.get("fieldHtmlType"))
        # 附件字段（htmlType=6）**一律跳过**：它要求模板已配置「附件上传目录」，
        # 而实测多数模板没配，带上就是 `field_fj: 字段未设置附件上传目录`。
        # 前置数据的目的是"让流程能建出来"，不需要真传个附件上去。
        if html_type == ATTACH_HTML_TYPE:
            continue
        # 下拉/单选类字段要给一个**合法选项**，不能随便塞字符串。
        options = field.get("selectvalues") or []
        if options:
            value = options[0]
        elif html_type in ("1", "3"):
            value = "1"
        else:
            value = "0"
        item = {"fieldName": name, "fieldValue": value}
        if field.get("fieldId") is not None:
            item["fieldId"] = str(field["fieldId"])
        item["fieldType"] = field.get("fieldType") or "1"
        main_data.append(item)
    return main_data


def create_request(session, base_url, workflow_id, request_name, *, submit=False):
    """创建流程；`submit=True` 时创建即提交（推到第一个审批节点）。

    Returns:
        int: 新建流程的 requestId。
    """
    payload = {
        "workflowId": int(workflow_id),
        "requestName": request_name,
        "mainData": build_main_data(session, base_url, workflow_id),
    }
    if submit:
        payload["otherParams"] = dict(NEXT_FLOW)

    response = _call(session, base_url, CREATE_PATH, json_body=payload)
    if not _is_success(response):
        raise E9WorkflowError(f"创建流程失败：{str(response)[:300]}")
    raw_id = (response.get("data") or {}).get("requestid")
    if not isinstance(raw_id, (int, str)) or str(raw_id).strip() == "":
        # 缺 requestid、或返回了非数字/空串——都当失败，不要让它变成下游的
        # "str + int" 之类更难查的错误。
        raise E9WorkflowError(f"创建流程未返回可用的 requestid：{str(response)[:300]}")
    return int(raw_id)


def submit_request(session, base_url, request_id, workflow_id, *, remark="自动化提交"):
    """提交（会签/审批通过都是提交）。

    `workflowId` 一并带上：部分 e-cology 版本强制校验它，缺失时返回
    `{code: PARAM_ERROR, errMsg:{errorParam_requestid:0}}`——**报错指向 requestid，
    真实缺失的却是 workflowId**，极难排查。不强制的版本多传也兼容，故统一带上。
    """
    payload = {
        "requestId": int(request_id),
        "workflowId": int(workflow_id),
        "remark": remark,
        "otherParams": dict(SUBMIT_PARAMS),
    }
    response = _call(session, base_url, SUBMIT_PATH, json_body=payload)
    if not _is_success(response):
        raise E9WorkflowError(f"提交流程 {request_id} 失败：{str(response)[:300]}")
    return True


def current_node(session, base_url, request_id):
    """查流程当前节点名与状态。

    节点名取自 DETAIL_PATH —— 见该常量的注释：`getRequestStatus` 没有这个字段。
    `status` 两边都有，但语义是**出口名**（形如「创建1 至 审批2」），不是节点名，
    所以只拿它做诊断，不要当节点判据。
    """
    payload = _call(session, base_url, DETAIL_PATH, params={"requestId": int(request_id)})
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return "", ""
    return str(data.get("currentNodeName") or ""), str(data.get("status") or "")


def wait_for_node(session, base_url, request_id, keyword, *, attempts=STATUS_POLL_ATTEMPTS):
    """轮询到当前节点名含 `keyword`；超时返回最后一次读到的节点名。

    **不要只查一次就断言**：流转是异步的，实测会签集齐后节点名要过一会儿才更新。
    返回而不是抛异常，是为了让调用方能给出"期望 X 实际 Y"的可诊断报错。

    ⚠️ **节点名会撒谎，别拿它当唯一判据。** 实测（2026-10-09）同一条 `status`
    （`创建1 至 审批2`）的两条流程，一条报 `审批2`、另一条报 `创建1`——而后者
    的待办确实已经在 审批2 的两个会签人手里。`currentNodeId` 一起报错，
    所以不是"字段选错了"，是这套记账本身不可靠。
    需要判断"到底流转了没有"时用 `in_todo()`（谁待办里有它 —— 那才是用例
    真正依赖的事实），节点名只当诊断信息。
    """
    node = ""
    for attempt in range(attempts):
        node = current_node(session, base_url, request_id)[0]
        if keyword in node:
            return node
        if attempt < attempts - 1:
            time.sleep(STATUS_POLL_INTERVAL_S)
    return node


# ── 待办/已办归属 ───────────────────────────────────────────────────────────
#
# 这是判断"流转到哪了"的**可靠**依据：节点名是 E9 的内部记账（见 wait_for_node
# 的注释），而"某个账号的待办里有没有这条"是它自己在用的数据。

TODO_PATH = "/api/workflow/paService/getToDoWorkflowRequestList"
DONE_PATH = "/api/workflow/paService/getHandledWorkflowRequestList"


def request_ids_of(session, base_url, login_name, *, handled=False):
    """取某账号待办（或已办）里的全部 requestId，统一成字符串集合。"""
    path = DONE_PATH if handled else TODO_PATH
    form = {"operator": login_name, "page": 1, "size": 200, "requestSource": "pc"}
    if handled:
        form["isHandled"] = "true"
    payload = _call(session, base_url, path, form=form)
    rows = payload if isinstance(payload, list) else (payload.get("data") or [])
    ids = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        raw = row.get("requestId") or row.get("requestid")
        if raw not in (None, ""):
            ids.add(str(raw))
    return ids


def in_todo(session, base_url, login_name, request_id):
    """某账号的**待办**里有没有这条流程。"""
    return str(request_id) in request_ids_of(session, base_url, login_name)


def in_done(session, base_url, login_name, request_id):
    """某账号的**已办**里有没有这条流程。"""
    return str(request_id) in request_ids_of(session, base_url, login_name, handled=True)