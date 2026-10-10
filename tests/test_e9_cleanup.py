"""收尾清理的离线契约（`framework/e9_cleanup.py`）。

这套判据是**安全边界**：写宽了会动到别人的流程，写窄了清不掉、环境一轮比一轮脏。
两条都要能离线钉住，不靠真环境的运气——所以这里只跟假会话/假待办打交道。

用的是**配置里的**流程名与登录名（不写死字面量）：判据认的必须是环境上真实的那一串，
写死会让换环境后静默清不到（而"清不到"不会报错）。测试里把账号换成假的，
是为了在**没有 config.json 的新克隆**里也能跑。
"""

import pytest

from jev_ultrafast.framework import e9_cleanup, e9_setup

# 假账号：名字形态与真实环境一致（AutoTest001…），但由测试自己造，
# 因此不依赖 config.json。
# ⚠️ 假账号必须覆盖**模块里全部参与清理的角色**：`auto_title_pattern()` 会挨个取登录名，
# 少一个就 KeyError（这条一开始就踩过——判据认的账号集合与清理的角色集合是同一份）。
ACCOUNTS = {role: {"user_name": f"AutoTest{i:03d}"} for i, role in enumerate(e9_cleanup.ROLES, 1)}
# 清量测试只挑三个角色跑，链路短一点、断言看得清（清理的算法与角色个数无关）。
_ROLES = tuple(e9_cleanup.ROLES[:3])
LOGIN_TO_ROLE = {account["user_name"]: role for role, account in ACCOUNTS.items()}


@pytest.fixture
def fake_accounts(monkeypatch):
    monkeypatch.setattr(e9_cleanup.e9_config, "load_account", lambda role: ACCOUNTS[role])
    return ACCOUNTS


def test_is_test_data_only_accepts_our_two_shapes(fake_accounts):  # noqa: ARG001
    """**只认两种形态**：接口建的 `jev-wf_…` 与 UI 自建的自动标题。"""
    pattern = e9_cleanup.auto_title_pattern()

    assert e9_cleanup.is_test_data("jev-wf_1010110133_e9-wf-submit-tc03", pattern=pattern)
    assert e9_cleanup.is_test_data(f"{e9_setup.WORKFLOW_NAME}-AutoTest001-2026-10-10",
                                   pattern=pattern)

    for other in (
        "",                                              # 取不到标题：宁可少清
        "请假申请-张三-2026-10-10",                        # 别人的流程
        "请假申请-AutoTest001-2026-10-10",                 # 别人的流程（登录名碰巧相同）
        f"{e9_setup.WORKFLOW_NAME}-AutoTest001",          # 少了日期段
        f"{e9_setup.WORKFLOW_NAME}-AutoTest001-2026-10-10-补",   # 日期后面还有东西
        f"{e9_setup.WORKFLOW_NAME}-张三-2026-10-10",       # 不是我们的账号
        f"前缀{e9_setup.WORKFLOW_NAME}-AutoTest001-2026-10-10",  # 前面还有字
    ):
        assert not e9_cleanup.is_test_data(other, pattern=pattern), other


def _cleanup_with_fake_todos(monkeypatch, todos, titles, *, fail_ids=()):
    """搭一套假环境：待办按角色存 id，提交一次就沿链路往下挪（挪到尽头就消失）。

    链路 `employee1 → employee2 → employee3 → 归档`，与真实会签/审批的推进方向一致
    ——"要提交几次才离开"正是它的性质，判据写错轮数就会清不干净。
    """
    monkeypatch.setattr(e9_cleanup.e9_workflow, "find_workflow_id", lambda *a, **k: 12522)
    monkeypatch.setattr(e9_cleanup, "title_of", lambda _s, _b, rid: titles.get(str(rid), ""))
    chain = dict(zip(_ROLES, (*_ROLES[1:], None), strict=True))

    def request_ids_of(_session, _base, login, handled=False):
        return set(todos[LOGIN_TO_ROLE[login]])

    def submit_request(_session, _base, request_id, _workflow, **_kwargs):
        if str(request_id) in fail_ids:
            raise RuntimeError("提交失败（测试构造）")
        for role, ids in todos.items():
            if str(request_id) in ids:
                ids.remove(str(request_id))
                if chain[role]:
                    todos[chain[role]].append(str(request_id))
                return True
        raise AssertionError("提交了一个不在任何待办里的实例")

    monkeypatch.setattr(e9_cleanup.e9_workflow, "request_ids_of", request_ids_of)
    monkeypatch.setattr(e9_cleanup.e9_workflow, "submit_request", submit_request)
    return e9_cleanup.cleanup(
        "http://e9.test",
        roles=_ROLES,
        sessions={role: object() for role in _ROLES},
        log=lambda *_args: None,
    )


def test_cleanup_drains_our_instances_and_leaves_others_alone(monkeypatch, fake_accounts):  # noqa: ARG001
    """清干净自己的、**一条都不碰别人的**；顺带钉住"要跑够轮数"。"""
    # 真实环境里 request_id 是数字串、标题另取；假数据照这个形状造。
    ours, auto, other = "101", "102", "103"
    todos = {"employee1": [ours, other, auto], "employee2": [], "employee3": []}
    titles = {ours: "jev-wf_1010_e9-wf-submit-tc03",
              auto: f"{e9_setup.WORKFLOW_NAME}-AutoTest001-2026-10-10",
              other: "请假申请-张三-2026-10-10"}

    summary = _cleanup_with_fake_todos(monkeypatch, todos, titles)

    # 两条测试实例各要走完 employee1→2→3 三段（会签/审批要逐级提交），共 6 次
    assert summary["提交次数"] == 6, summary
    assert summary["剩余测试数据"] == {}, summary
    assert [row["标题"] for row in summary["跳过(非测试数据)"]] == [titles[other]], summary
    # 别人的流程**原地不动**——这是这个模块存在的前提
    assert todos == {"employee1": [other], "employee2": [], "employee3": []}


def test_cleanup_records_failure_and_keeps_going(monkeypatch, fake_accounts):  # noqa: ARG001
    """一条提交不了，不该让整轮清理停摆：如实记下来，其余的照清。"""
    stuck, ok = "201", "202"
    todos = {"employee1": [stuck, ok], "employee2": [], "employee3": []}
    titles = {stuck: "jev-wf_1010_e9-wf-submit-tc04", ok: "jev-wf_1010_e9-wf-submit-tc05"}

    summary = _cleanup_with_fake_todos(monkeypatch, todos, titles, fail_ids={stuck})

    assert [row["request_id"] for row in summary["失败"]] == [stuck], summary
    assert "提交失败（测试构造）" in summary["失败"][0]["错误"], summary
    # 清不掉的还留在待办里（如实报"剩余"），清得掉的那条已经走完链路
    assert summary["剩余测试数据"] == {"employee1": [stuck]}, summary
    assert summary["提交次数"] == 3, summary