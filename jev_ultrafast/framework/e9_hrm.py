"""E9 人力资源接口：账号的**人员密级**（分级保护）读写。

**为什么需要这一层。** 环境一旦启用「分级保护」（
`/api/hrm/classifiedProtection/getBasicInfo` 的 `isOpenClassification`），
系统里的人员就带上一个密级，资源（流程/文档）也带密级，判据是
**「资源密级不能超过人员密级」**。

对工作流的影响很直接：**流程节点的操作者必须满足该流程的密级要求**，
否则提交时报

    审批3节点操作者批次条件不满足或节点操作者无法找到：
    操作组 审批3审批人 批次 0 测试员工004，测试员工005 不符合流程密级

报错指向"节点操作者"，但**根因在账号属性上**——路径怎么配都救不回来。
而密级是账号级数据，用例（`cases/*.yaml`）改不了，所以只能落在环境准备这一层。

实测（2026-10-09）：`测试员工003/004/005` 的 `classification`
为空，配成「核心」后，同一条路径的提交立刻从"不符合流程密级"变成成功，
且会签节点的第二人也开始正常收到待办。

### 接口契约（抓自批量编辑人员信息页的一次真实保存）

    POST /api/hrm/batchMaintenanceAdjustEdit/resource/batch?is_multilang_set=true
        → {sessionkey}
    POST /api/ec/dev/table/datas  dataKey=<sessionkey>
        → datas: 整行对象数组
    POST /api/hrm/batchMaintenanceAdjustEdit/resource/saveBatch
        datas=<JSON 数组>（**必须回传整行**，不是只传要改的字段）

密级码（与 `/api/hrm/classifiedProtection/getUserClassification` 的
`id` / `showName` 一一对应）：0=核心 1=重要 2=一般 3=非密。
**数字越小密级越高**——容易记反，所以对外只暴露中文名。
"""

CLASSIFICATION_NAMES = {"0": "核心", "1": "重要", "2": "一般", "3": "非密"}
CLASSIFICATION_IDS = {name: code for code, name in CLASSIFICATION_NAMES.items()}

# 批量编辑页的三个接口。**不是** `saveBatchEdit`（那是"批量编辑"页，
# 与本页的 `batchMaintenanceAdjustEdit` 不是一个模块，实测打不通）。
BATCH_PATH = "/api/hrm/batchMaintenanceAdjustEdit/resource/batch"
SAVE_PATH = "/api/hrm/batchMaintenanceAdjustEdit/resource/saveBatch"
TABLE_PATH = "/api/ec/dev/table/datas"

# 分批读取：一次要多少行。E9 侧默认 10，必须显式抬高，否则 870 个账号要翻 87 页。
PAGE_SIZE = 500
MAX_PAGES = 40


class E9HrmError(RuntimeError):
    """人力资源接口调用失败。"""


def _call(session, base_url, path, *, form=None, params=None, timeout=30):
    from . import e9_workflow  # 复用同一套请求头/编码约定，避免两份实现漂移

    return e9_workflow._call(session, base_url, path, form=form, params=params, timeout=timeout)


def read_resources(session, base_url):
    """读回全部人力资源行（整行对象，含 `classification` / `seclevel`）。

    Returns:
        list[dict]: 每一项都是保存时要原样回传的整行。
    """
    payload = _call(session, base_url, f"{BATCH_PATH}?is_multilang_set=true", form={})
    key = payload.get("sessionkey") if isinstance(payload, dict) else None
    if not key:
        raise E9HrmError(f"批量编辑接口没返回 sessionkey：{str(payload)[:200]}")

    rows = []
    for page in range(1, MAX_PAGES + 1):
        table = _call(session, base_url, TABLE_PATH, form={
            "dataKey": key, "current": str(page), "sortParams": "[]", "pageSize": str(PAGE_SIZE),
        })
        batch = table.get("datas") if isinstance(table, dict) else None
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
    return rows


def normalize_level(level):
    """把 `'核心'` / `'0'` 统一成码。名字写错时**报错**，不要静默当默认值。"""
    text = str(level).strip()
    if text in CLASSIFICATION_IDS:
        return CLASSIFICATION_IDS[text]
    if text in CLASSIFICATION_NAMES:
        return text
    raise E9HrmError(
        f"未知的人员密级 {level!r}；可用：{sorted(CLASSIFICATION_IDS)} 或 {sorted(CLASSIFICATION_NAMES)}"
    )


def set_classifications(session, base_url, wanted):
    """按登录名批量设置人员密级。

    Args:
        wanted: `{loginid: level}`，level 为 `'核心'` 或 `'0'` 这类码。

    Returns:
        dict: `{loginid: (旧码, 新码)}`，**只含实际被改动的账号**——
        没找到的登录名会被列出并单独报错，不要静默跳过（静默跳过会让
        "配好了"与"根本没这个人"长得一模一样）。
    """
    targets = {login: normalize_level(level) for login, level in wanted.items()}
    rows = read_resources(session, base_url)

    changed, missing = [], set()
    for row in rows:
        login = str(row.get("loginid") or "")
        if login not in targets:
            continue
        missing.add(login)
        old = str(row.get("classification") or "")
        new = targets[login]
        if old == new:
            continue
        row["classification"] = new
        # 显示值一并改：只改码不改 span，列表页会显示旧密级，
        # 下次有人"照着页面"核对时会误判成没生效。
        row["classificationspan"] = CLASSIFICATION_NAMES[new]
        row.setdefault("randomFieldCk", "true")
        row.setdefault("randomFieldOp", "true")
        changed.append({"loginid": login, "from": old, "to": new, "_row": row})

    not_found = sorted(set(targets) - missing)
    if not_found:
        raise E9HrmError(f"人力资源里找不到这些登录名：{not_found}")

    if changed:
        import json

        payload = _call(session, base_url, SAVE_PATH, form={
            "datas": json.dumps([c.pop("_row") for c in changed], ensure_ascii=False),
        })
        if isinstance(payload, dict) and payload.get("status") not in (None, "1", 1):
            raise E9HrmError(f"保存人员密级失败：{str(payload)[:200]}")

    return {c["loginid"]: (c["from"], c["to"]) for c in changed}