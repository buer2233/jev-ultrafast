"""Contract tests for the HRM classification layer (`e9_hrm`).

Why these exist: 「分级保护」打开后，流程节点操作者会被密级卡住，而**报错指向节点**、
**根因在账号**。那条错误信息（"XXX 不符合流程密级"）不会告诉你去改人力资源，
所以这个模块的存在本身就是一条容易再次踩空的路径——用测试把它钉住。

两条最容易写错、且错了不报错的：

1. **保存必须回传整行。** 只传 id + classification 时接口照样返回成功，
   但其它字段被清空（实测：`loginid` 一丢，人就从列表里消失了）。所以这里
   断言"回传的行包含读回来的全部字段"，而不是只断言"改了 classification"。
2. **找不到登录名必须报错。** 静默跳过会让"配好了"和"根本没这个人"
   长得一模一样——而这两种情况的处置完全不同。

Everything is offline: `_call` is stubbed, so no E9 environment and no credentials.
"""

import json

import pytest

from jev_ultrafast.framework import e9_hrm

# 一行"长得像真数据"的整行对象：字段名与抓包一致，故意多放几个无关字段，
# 用来验证"整行回传"这条约束。
FULL_ROW = {
    "id": "887", "loginid": "AutoTest003", "lastname": "测试员工003",
    "classification": "0", "classificationspan": "核心",
    "seclevel": "0", "seclevelspan": "0",
    "departmentid": "1", "subcompanyid1": "1", "loginidspan": "AutoTest003",
    "randomFieldId": "887", "randomFieldCk": "true", "randomFieldOp": "true",
}


class FakeEnv:
    """记录每一次 `_call`，并按路径返回预置响应（含翻页）。"""

    def __init__(self, rows, *, sessionkey="SK1", save_status="1"):
        self.rows = [dict(r) for r in rows]
        self.sessionkey = sessionkey
        self.save_status = save_status
        self.calls = []
        self.saved = []

    def __call__(self, _session, _base_url, path, *, form=None, params=None, timeout=30):
        form = form or {}
        self.calls.append({"path": path, "form": form})
        if path.startswith(e9_hrm.BATCH_PATH):
            return {"sessionkey": self.sessionkey, "status": "1"}
        if path == e9_hrm.TABLE_PATH:
            current = int(form.get("current", 1))
            size = int(form.get("pageSize", e9_hrm.PAGE_SIZE))
            start = (current - 1) * size
            return {"datas": self.rows[start:start + size]}
        if path == e9_hrm.SAVE_PATH:
            self.saved = json.loads(form["datas"])
            return {"status": self.save_status}
        raise AssertionError(f"没预料到的调用：{path}")

    def paths(self):
        return [c["path"] for c in self.calls]


@pytest.fixture
def env(monkeypatch):
    def _make(rows, **kwargs):
        fake = FakeEnv(rows, **kwargs)
        monkeypatch.setattr(e9_hrm, "_call", fake)
        return fake

    return _make


# ── 密级取值 ────────────────────────────────────────────────────────────────

def test_level_names_and_codes_map_to_the_same_code():
    assert e9_hrm.normalize_level("核心") == "0"
    assert e9_hrm.normalize_level("0") == "0"
    assert e9_hrm.normalize_level("非密") == "3"
    assert e9_hrm.normalize_level(3) == "3"


def test_unknown_level_is_rejected_with_the_alternatives():
    with pytest.raises(e9_hrm.E9HrmError) as err:
        e9_hrm.normalize_level("绝密")
    message = str(err.value)
    assert "绝密" in message
    # 报错必须带上可用值，否则调用方只能去读源码
    assert "核心" in message and "非密" in message


# ── 读 ──────────────────────────────────────────────────────────────────────

def test_read_follows_paging_until_a_short_page(env):
    """一页塞满时要继续翻——只读第一页会让大环境里靠后的账号**查不到**，
    而表现是"找不到这个登录名"，看着像账号不存在。"""
    # 第一页刚好装满（多一个），目标落在第二页
    rows = [dict(FULL_ROW, id=str(i), loginid=f"u{i}") for i in range(e9_hrm.PAGE_SIZE)]
    rows.append(dict(FULL_ROW, id="999", loginid="target"))
    fake = env(rows)
    got = e9_hrm.read_resources(None, "http://x")
    assert [r["loginid"] for r in got][-1] == "target"
    assert fake.paths().count(e9_hrm.TABLE_PATH) == 2


# ── 写 ──────────────────────────────────────────────────────────────────────

def test_save_returns_the_whole_row_not_just_the_changed_field(env):
    """**这条是本模块的核心回归线。** 只回传 id + classification 接口也返回成功，
    但会把没传的字段清空（loginid 一丢，账号就从列表里消失）。"""
    fake = env([FULL_ROW])
    changed = e9_hrm.set_classifications(None, "http://x", {"AutoTest003": "非密"})

    assert changed == {"AutoTest003": ("0", "3")}
    assert len(fake.saved) == 1
    row = fake.saved[0]
    # 原样的字段一个都不能少
    for key in FULL_ROW:
        assert key in row, f"保存时丢了字段 {key}"
    assert row["classification"] == "3"
    # 显示值同步改：只改码不改 span，列表页会显示旧密级
    assert row["classificationspan"] == "非密"
    assert row["loginid"] == "AutoTest003"


def test_only_targeted_accounts_are_touched(env):
    other = dict(FULL_ROW, id="749", loginid="AutoTest002", classification="3")
    fake = env([FULL_ROW, other])
    e9_hrm.set_classifications(None, "http://x", {"AutoTest003": "重要"})

    assert [r["loginid"] for r in fake.saved] == ["AutoTest003"]
    assert e9_hrm.SAVE_PATH not in [c["path"] for c in fake.calls[:1]]


def test_no_save_call_when_nothing_changes(env):
    """幂等：已经是目标值就不要重复写。批量保存的代价是整页重写。"""
    fake = env([FULL_ROW])
    changed = e9_hrm.set_classifications(None, "http://x", {"AutoTest003": "核心"})
    assert changed == {}
    assert e9_hrm.SAVE_PATH not in fake.paths()


def test_missing_login_name_is_an_error_not_a_silent_skip(env):
    """'配好了' 与 '根本没这个人' 不能长得一样。"""
    env([FULL_ROW])
    with pytest.raises(e9_hrm.E9HrmError) as err:
        e9_hrm.set_classifications(None, "http://x", {"AutoTest003": "核心", "NoSuchUser": "核心"})
    assert "NoSuchUser" in str(err.value)


def test_save_failure_is_reported(env):
    fake = env([FULL_ROW], save_status="0")
    with pytest.raises(e9_hrm.E9HrmError):
        e9_hrm.set_classifications(None, "http://x", {"AutoTest003": "非密"})
    assert len(fake.saved) == 1


def test_empty_classification_counts_as_a_change(env):
    """**实测的那个缺陷就是空值**：空密度过不了密级比较，所以必须当成"要改"，
    不能因为 `'' == '0'` 之类的宽松比较被判成"没变"。"""
    blank = dict(FULL_ROW, classification="", classificationspan="")
    fake = env([blank])
    changed = e9_hrm.set_classifications(None, "http://x", {"AutoTest003": "核心"})
    assert changed == {"AutoTest003": ("", "0")}
    assert fake.saved[0]["classification"] == "0"