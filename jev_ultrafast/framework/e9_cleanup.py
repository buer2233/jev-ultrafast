"""用例收尾：把测试自己造出来的流程实例从各账号的待办里推走。

**为什么必须有它**（2026-10-10 实测）：一条用例失败，它建出来的实例就永远停在会签人的
待办里；下一轮又会建一条标题极像的。堆到十几条时，模型在列表里"逐字对上"那一条就明显
不稳——实测 employee2 的待办里躺过 16 条 `jev-wf_<run>_e9-wf-submit-tcNN`。
UI 上没有"撤销/删除"入口，所以走**接口**把残留逐级提交上去，一路推到归档。
数据是我们自己造的，清掉它不改变被测语义，只是把环境恢复干净。

**安全边界（改这里之前先读）**：只动**标题匹配测试模式**的实例，两种形态：

  · `jev-wf_…`——接口建的（`e9_setup.REQUEST_NAME_PREFIX`）；
  · `<路径名>-<登录名>-YYYY-MM-DD`——UI 自建的自动标题（见 `e9_setup.system_request_name`）。

其它任何流程一概不碰；查不到标题的也不碰（宁可少清，不可误动）。

**什么时候跑**：`tests/conftest.py` 的 `nl_setup` fixture 收尾（用例判定之后）。
它**绝不影响用例结论**：清不掉就挂一段说明，不抛异常。

本机调试时想留住现场：设 `JEV_NL_KEEP_TEST_DATA=1`（或 pytest 加 `--keep-test-data`）。
"""

import re

from . import e9_config, e9_setup, e9_workflow

# 参与清理的账号：用例会用到的那几个（发起人 / 会签 / 审批 / 归档）。
ROLES = tuple(dict.fromkeys((e9_setup.ROLE_REQUESTER, *e9_setup.ROLE_COUNTERSIGN,
                             *e9_setup.ROLE_APPROVER, e9_setup.ROLE_ARCHIVER)))

# 逐级提交要跑几轮：会签节点要**两个人都提交**才离开，所以同一个实例可能要提交好几次。
# 上限只是防呆——正常情况下三四轮就清干净。
MAX_ROUNDS = 6
# 单次清理的提交次数上限。正常十几条实例，给足余量又不会在异常时无限跑。
MAX_SUBMITS = 200


def auto_title_pattern():
    """UI 自建实例的标题：`<路径名>-<登录名>-<日期>`。

    用**配置里的路径名与登录名**拼，不写死字面量：换环境时只要 `e9_setup` 的常量对，
    这里跟着对（写死会让换环境后**静默清不到**，而"清不到"不会报错）。
    """
    logins = "|".join(re.escape(e9_config.load_account(role)["user_name"]) for role in ROLES)
    return re.compile(rf"^{re.escape(e9_setup.WORKFLOW_NAME)}-({logins})-\d{{4}}-\d{{2}}-\d{{2}}$")


def is_test_data(title, *, pattern=None):
    """这条流程是不是测试造出来的？——**只认上面那两种形态**，别的都不是。"""
    if not title:
        return False
    if title.startswith(f"{e9_setup.REQUEST_NAME_PREFIX}_"):
        return True
    return bool((pattern or auto_title_pattern()).match(title))


def title_of(session, base_url, request_id):
    """取实例标题；取不到返回空串（调用方按"不是测试数据"处理，不误动）。"""
    payload = e9_workflow._call(session, base_url, e9_workflow.DETAIL_PATH,
                               params={"requestId": int(request_id)})
    data = payload.get("data") if isinstance(payload, dict) else None
    return str((data or {}).get("requestName") or "")


def cleanup(base_url, *, roles=ROLES, sessions=None, log=print):
    """把各角色待办里的测试实例逐级提交上去，返回一份汇总（供报告与排障）。

    Args:
        base_url: E9 地址。
        roles: 参与清理的角色名（默认 `ROLES`）。
        sessions: 可注入的会话映射（单测用；不传就按角色各自登录）。
        log: 逐条进度写到哪里（默认 print；pytest 会捕获）。

    Returns:
        dict: `{"提交次数", "剩余测试数据", "跳过(非测试数据)", "失败"}`——
        后两项是**给排障看的**：跳过的说明边界生效了，失败的要能一眼看出是哪条。
    """
    sessions = sessions or {role: e9_workflow.session_for(role, base_url=base_url) for role in roles}
    logins = {role: e9_config.load_account(role)["user_name"] for role in roles}
    workflow_id = e9_workflow.find_workflow_id(sessions[roles[0]], base_url, e9_setup.PATH_KEYWORD)
    pattern = auto_title_pattern()

    submitted, skipped, failures = 0, [], []
    # 已经提交失败过的 id：**下一轮不再重试**。
    # 不记这一步的话，一条怎么都提交不掉的实例会在每一轮都被重试、各记一条失败，
    # 汇总里同一个 id 出现六次——看不出到底卡了几条。
    failed_ids = set()
    # 已经记过"不是我方数据"的 id：同理，别每轮各记一条（汇总里要一眼看得出有几条）。
    noted_skips = set()
    for _round in range(MAX_ROUNDS):
        acted = False
        for role in roles:
            ids = sorted(e9_workflow.request_ids_of(sessions[role], base_url, logins[role]))
            for request_id in ids:
                if request_id in failed_ids:
                    continue
                title = title_of(sessions[role], base_url, request_id)
                if not is_test_data(title, pattern=pattern):
                    if title and request_id not in noted_skips:
                        noted_skips.add(request_id)
                        skipped.append({"角色": role, "request_id": request_id, "标题": title})
                    continue
                acted = True
                if submitted >= MAX_SUBMITS:
                    failures.append({"角色": role, "request_id": request_id, "标题": title,
                                     "错误": f"达到单次清理上限 {MAX_SUBMITS} 次提交"})
                    return _summary(base_url, roles, sessions, logins, pattern,
                                    submitted, skipped, failures)
                try:
                    e9_workflow.submit_request(sessions[role], base_url, int(request_id),
                                               workflow_id, remark="自动化清理")
                except Exception as error:            # noqa: BLE001
                    # 提交不了就如实记下来继续：清不掉一条不该让整轮清理停摆。
                    failed_ids.add(request_id)
                    failures.append({"角色": role, "request_id": request_id, "标题": title,
                                     "错误": f"{type(error).__name__}: {str(error)[:100]}"})
                    continue
                submitted += 1
                log(f"  收尾清理：{role} 提交了 #{request_id} {title}")
        if not acted:
            break
    return _summary(base_url, roles, sessions, logins, pattern, submitted, skipped, failures)


def _summary(base_url, roles, sessions, logins, pattern, submitted, skipped, failures):
    remaining = {}
    for role in roles:
        ids = sorted(e9_workflow.request_ids_of(sessions[role], base_url, logins[role]))
        left = [r for r in ids if is_test_data(title_of(sessions[role], base_url, r), pattern=pattern)]
        if left:
            remaining[role] = left
    return {"提交次数": submitted, "剩余测试数据": remaining,
            "跳过(非测试数据)": skipped, "失败": failures}