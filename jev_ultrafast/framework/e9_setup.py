"""命名前置：把源用例的「前置条件」翻译成一段可复用的接口准备。

**为什么是命名配方而不是写在用例里。** 源功能用例的前置常常长这样：
「人员01 创建并提交 → 审批2 会签 → 审批3 审批 → 归档」。这段链条要走 5 次接口、
涉及 4 个账号——把它塞进 YAML 既表达不了（goal 是自然语言）、也跑不动
（UI 用例只有一个登录身份）。所以用例只用**一个名字**声明要什么前置：

    setup: wf-approval2      # R1 已到「审批2」，人员02/03 有待办

配方在 fixture 里执行、走接口、在建好的实例上推进到你声明的那个节点。
**用例的 goal 只从"这个节点的操作者登录之后"开始描述。**

### 收集期 / 执行期的时序约束（**这是本模块最容易写错的地方**）

`goal` 里的 `{{ 变量 }}` 在**收集期**就替换完了，而配方在**执行期**才跑，
两者不可能用运行时返回值通信。所以流程实例的名字必须**双方都能在收集期算出来**：

  · loader 按 `{{ wf_request_name }}` 注入一个**逐用例唯一**的名字（含 `run_id` 与用例 id）；
  · 配方从 `case["_wf_request_name"]` 读同一个值（loader 把它挂在用例 dict 上）。

这与 `e9_api.EB_MODE_NAME` 用的是同一条思路（约定同一个常量），只是多了
`run_id` + 用例 id 来保证**同一轮收集里的每条用例各建各的实例**——
名字不唯一时，下一条用例在列表里"找到刚建的那条"就完全靠运气
（实测在一个被反复写过的环境里踩过，见 `loader._default_variables` 的注释）。
"""

import os
import time
from datetime import datetime

from . import e9_config, e9_workflow

# 前置配方的轮询节奏。每一步流转都是异步的（会签集齐、非会签放行都要等
# E9 那边把待办挪过去），所以要轮询；但也不必久等——实测正常 1–3 秒内到位，
# 10 秒还不动基本就是配置问题了。
POLL_ATTEMPTS = 12
POLL_INTERVAL_S = 1.0

# 流程实例名的前缀。用例的 goal 通过 {{ wf_request_name }} 引用整串。
#
# ⚠️ **刻意用纯 ASCII，一个中文字都不带。** 原先是 "UI自动化流程提交"——与
# **系统自动生成**的标题（`UI自动化流程提交-AutoTest001-2026-10-09`）前 7 个字一模一样。
# 后果是同一条待办列表里躺着两条都叫"UI自动化流程提交…"的流程，而 goal 要模型
# "逐字对上"，实测（2026-10-09，TC03）**模型连着两轮都挑走了那条自动标题**，
# 进去签了别人的流程。当时还试过在 goal 里加"要挑带下划线的那条、不要连字符那条"
# ——模型照样挑错。**与其教它分辨，不如让两类名字根本不像。**
#
# 纯 ASCII 之后，接口建的那条长这个样子：`jev-wf_1009171046_e9-wf-submit-tc03`，
# 与中文自动标题零重叠，不用教也分得开。
REQUEST_NAME_PREFIX = "jev-wf"

# 节点名关键字。**这些是环境相关的**——路径怎么命名由搭建的人决定，
# 所以允许用环境变量覆盖，默认值对应源用例里的叫法。
NODE2_NAME = os.environ.get("E9_WF_NODE2_NAME", "审批2").strip() or "审批2"
NODE3_NAME = os.environ.get("E9_WF_NODE3_NAME", "审批3").strip() or "审批3"
ARCHIVE_NAME = os.environ.get("E9_WF_ARCHIVE_NAME", "归档").strip() or "归档"

# ── 两个名字，别混 ──────────────────────────────────────────────────────────
#
# **模板名**（路径名）：E9 里预先搭好的那条流程，在「新建流程」列表里出现，
#   点它才能打开表单。界面上的中文名，也是 `find_workflow_id` 的搜索关键字。
# **实例标题**（requestName）：点进表单后填在「标题」里的那串，出现在待办/已办列表。
#
# 两者的用途完全不重叠：**UI 自己建实例的用例（setup: none）要自己填标题**——
# 接口建实例时是接口把标题写进去的，模板列表里从来不会出现实例标题。
# 混用会导致用例在新建流程列表里找一个根本不存在的名字。
PATH_NAME = os.environ.get("E9_WF_PATH_NAME", "流程提交").strip() or "流程提交"

# 在"可创建流程列表"里按它定位 workflowId（子串匹配，所以要够独特）。
PATH_KEYWORD = PATH_NAME

# 路径在界面上的**完整显示名**。与 PATH_NAME 分工不同：那个是"够独特的搜索关键字"，
# 这个要跟界面上**一字不差**——因为它同时是**系统自动生成的标题**的前缀（见下）。
# 换环境时若那条路径改了名，这里要跟着改。
WORKFLOW_NAME = os.environ.get("E9_WF_WORKFLOW_NAME", "UI自动化流程提交").strip() or "UI自动化流程提交"

# ⚠️ **搭流程界面时要用 WORKFLOW_NAME 精确匹配，别用 PATH_NAME 那种子串。**
# 实测（2026-10-09，TC01）：goal 里写的是"名字**含**「流程提交」"，
# 而那个列表按分组排了几百条，模型点成了另一条（URL 里 workflowid=8025，不是 12522），
# 整条用例于是作废——**"含"这个措辞本身就在邀请它凑合**。
# PATH_NAME（"流程提交"）是给 `find_workflow_id` 做**接口侧**子串检索用的，
# 界面上的挑选要用这条全名。


def system_request_name(loginid, today=None):
    """算出 E9 **自动生成**的流程标题：`<路径名>-<登录名>-<日期>`。

    **为什么必须有这个公式。** 实测目标环境上「标题」在新建表单里是**只读**的：
    E9 的字段属性没显式设置时按只读处理（`formUtil.getFieldCurViewAttr` 里
    "字段放在模板上但未设置属性的默认给只读属性"）。这一点对**所有**流程都一样——
    连内置的「自由流程2」「BYM专用测试」打开新建表单，标题也是只读文字，
    点它不会出现输入框。

    后果很直接：**UI 自己建出来的实例，标题不是我们填的**，而是系统按上面的公式生成。
    所以「保存草稿 → 去待办里认领它」这类用例，只能照公式反推名字——
    改 goal 想让模型填标题是没用的，那一格根本没有输入框（实测模型会反复去点它，
    每次都拿不到值，直到撞上步数上限）。

    公式是实测出来的（三条流程一致）：`UI自动化流程提交-AutoTest001-2026-10-09`、
    `自由流程2-AutoTest001-2026-10-09`、`BYM专用测试-AutoTest001-2026-10-09`。
    走**接口**建实例的用例不受影响——接口可以把标题写成任意值，
    所以 `setup: wf-*` 那几条仍然用 `request_name_for` 的逐用例唯一名。
    """
    day = today or datetime.now().strftime("%Y-%m-%d")
    return f"{WORKFLOW_NAME}-{loginid}-{day}"

# **密级下拉的选项名**。
#
# 为什么需要它：E9 每张表单都有系统内置的「密级」（字段名 `requestlevel`），
# 接口定义里既无默认值也不标必填，**但前端不填就不让保存**。而它是个自定义下拉：
# 点开才出选项，且**选项在候选表里排到最后（实测第 51/52 位）、标签是「公开资源2」
# 这种和"密级"看不出关系的名字**——实测（2026-09-30）模型因此反复去点那个
# combobox 本身（第 6 位、名字带「密级」），点一下展开、再点一下收起，空转 20 步。
# 所以 goal 里要把选项名直接说出来。
#
# 默认值是实测到的名字；换环境用 `E9_WF_LEVEL_OPTION` 覆盖。
LEVEL_OPTION = os.environ.get("E9_WF_LEVEL_OPTION", "公开资源2").strip() or "公开资源2"

# 角色 ↔ 源用例里的人名。会签/审批的**具体人选由环境里那条路径决定**，
# 这里只是"本项目约定的对应关系"，必须与环境里的路径配置一致，
# 否则配方推到的节点跟用例断言的操作者对不上（表现为用例莫名失败）。
#
# ⚠️ **归档节点由人员05 兼**，不是源用例里的「人员06」。
# 实测目标环境上只有 `AutoTest001~005` 五个账号（`AutoTest006` 登录失败、
# 人力资源里也查不到），所以源用例的「创建→会签→审批→**归档**」四段里，
# 最后一段只能落回链条上已有的账号。选人员05 是因为他是链条末端、
# 且归档节点在 E9 里是**自动归档**（实测不会给任何人产生新待办），
# 因此与他在审批3 的角色不冲突（TC04 的「人员05 待办不再含 R1」仍然成立）。
# 换环境时若补出第 6 个账号：改这里、`cases/e9/workflow_submit.yaml` 的 TC05/TC08。
ROLE_REQUESTER = "employee1"   # 人员01 发起人
ROLE_COUNTERSIGN = ("employee2", "employee3")   # 人员02、03 会签
ROLE_APPROVER = ("employee4", "employee5")      # 人员04、05 非会签
ROLE_ARCHIVER = "employee5"    # 归档节点查看人（源用例的「人员06」，本环境由 05 兼）

# ⚠️ **另一条环境前提：这些账号的「人员密级」必须已设置。**
# 环境启用「分级保护」时，流程节点操作者会被密级校验拦住，报
# 「XXX 不符合流程密级」——**报错指向节点，根因却在账号**。空值过不了这个比较，
# 所以"没设"和"设得太低"表现出来的都是同一条错误。
# 用 `e9_hrm.set_classifications(admin_session, base, {...})` 配，见 e9_hrm 的模块注释。


class SetupError(RuntimeError):
    """前置准备失败。"""


def request_name_for(run_id, case_id):
    """算出某条用例该用的流程实例名。

    **加载器与前置配方都调这一个函数**——两边各写一份公式迟早会漂移，
    而漂移的表现是"用例在列表里找不到自己那条流程"，极难定位。
    """
    return f"{REQUEST_NAME_PREFIX}_{run_id}_{case_id}"


class _Context:
    """一次前置准备的上下文：缓存会话与已定位的 workflowId，避免重复登录。

    登录一次要三步接口调用（RSA → checkLogin → remindLogin），
    一条用例里 4 个角色各登一次已经很贵，不能再让每个配方各登一遍。
    """

    def __init__(self, base_url, request_name):
        self.base_url = base_url
        self.request_name = request_name
        self._sessions = {}
        self._workflow_id = None

    def session(self, role):
        if role not in self._sessions:
            self._sessions[role] = e9_workflow.session_for(role, base_url=self.base_url)
        return self._sessions[role]

    def workflow_id(self):
        """定位流程路径 id（只查一次）。"""
        if self._workflow_id is None:
            self._workflow_id = e9_workflow.find_workflow_id(
                self.session(ROLE_REQUESTER), self.base_url, PATH_KEYWORD
            )
        return self._workflow_id

    def _login(self, role):
        return e9_config.load_account(role)["user_name"]

    def _with_todo(self, role, request_id):
        return e9_workflow.in_todo(
            self.session(role), self.base_url, self._login(role), request_id
        )

    def _with_done(self, role, request_id):
        return e9_workflow.in_done(
            self.session(role), self.base_url, self._login(role), request_id
        )

    def await_todo(self, roles, request_id):
        """等到这些角色的**待办**里都出现这条流程。

        **判据为什么不是节点名**：见 `e9_workflow.wait_for_node` 的注释——
        节点名与 currentNodeId 都会被 E9 报错（同一条 status 两条流程给出不同答案）。
        而"某人的待办里有没有它"是**用例真正依赖的事实**（TC03/TC04/TC07 断言的就是
        这个），也是 E9 自己在用的数据，不会撒谎。
        """
        missing = list(roles)
        for attempt in range(POLL_ATTEMPTS):
            missing = [r for r in roles if not self._with_todo(r, request_id)]
            if not missing:
                return
            if attempt < POLL_ATTEMPTS - 1:
                time.sleep(POLL_INTERVAL_S)
        raise SetupError(
            f"流程 {request_id} 未出现在 {missing} 的待办里（已等 "
            f"{POLL_ATTEMPTS * POLL_INTERVAL_S}s）。"
            f"多半是环境里那条路径的节点操作者与用例假设不一致，"
            f"或这些账号的「人员密级」没配（见 e9_hrm 的模块注释）。"
        )

    def await_left_todo(self, role, request_id):
        """等到某角色的待办里**不再**有这条流程（= 他提交的那一步真的生效了）。"""
        for attempt in range(POLL_ATTEMPTS):
            if not self._with_todo(role, request_id):
                return
            if attempt < POLL_ATTEMPTS - 1:
                time.sleep(POLL_INTERVAL_S)
        raise SetupError(f"提交后 {role} 的待办里仍有流程 {request_id}——提交没生效。")

    def create_and_submit(self, role=ROLE_REQUESTER):
        """建一条实例并推到第一个审批节点，返回 requestId。"""
        request_id = e9_workflow.create_request(
            self.session(role), self.base_url, self.workflow_id(), self.request_name, submit=True
        )
        self.await_todo(ROLE_COUNTERSIGN, request_id)
        return request_id

    def submit(self, role, request_id):
        e9_workflow.submit_request(
            self.session(role), self.base_url, request_id, self.workflow_id()
        )
        self.await_left_todo(role, request_id)


# ── 配方注册表 ──────────────────────────────────────────────────────────────

SETUPS = {}


def setup(name):
    """把一个函数登记成命名前置。被登记的函数签名固定为 `(ctx) -> dict`。"""

    def register(func):
        SETUPS[name] = func
        return func

    return register


@setup("none")
def _none(ctx):
    """没有前置：用例从"新建流程"这一步自己开始（TC01/TC02 用）。"""
    return {}


@setup("wf-approval2")
def _wf_approval2(ctx):
    """R1 已到「审批2」会签节点，人员02、人员03 有待办。（TC03、TC07 用）"""
    return {"request_id": ctx.create_and_submit()}


@setup("wf-approval3")
def _wf_approval3(ctx):
    """R1 已到「审批3」非会签节点：审批2 的两人都已提交。（TC04 用）"""
    request_id = ctx.create_and_submit()
    for role in ROLE_COUNTERSIGN:
        ctx.submit(role, request_id)
    # 会签集齐后，待办应当落到审批3 的操作者身上——用这个判"真的流转了"。
    ctx.await_todo(ROLE_APPROVER, request_id)
    return {"request_id": request_id}


@setup("wf-archived")
def _wf_archived(ctx):
    """R1 已走完全流程：审批3 非会签，由人员04 一人提交即离开。（TC05、TC08 用）"""
    request_id = ctx.create_and_submit()
    for role in ROLE_COUNTERSIGN:
        ctx.submit(role, request_id)
    ctx.await_todo(ROLE_APPROVER, request_id)
    # ROLE_APPROVER[0] 提交后，`submit` 内部已等到他的待办清空——
    # 那正是"非会签节点一人提交即放行"的可观察证据。
    # 不再去断言「归档」这个**节点名**：本环境的归档节点操作者是「所有人」，
    # 而节点名本身也会被 E9 报错（见 wait_for_node 的注释）。
    ctx.submit(ROLE_APPROVER[0], request_id)
    return {"request_id": request_id}


def run(name, base_url, request_name):
    """执行一个命名前置，返回它产出的上下文（供报告与排障引用）。

    Raises:
        SetupError: 名字不认识，或前置准备失败。
        e9_workflow.WorkflowPathMissing: 环境里没有合适的流程路径
            —— fixture 会把它翻成 skip。
    """
    if name in (None, "", "none"):
        return {}
    if name not in SETUPS:
        raise SetupError(f"未知的前置名 {name!r}；可用：{sorted(SETUPS)}")
    return SETUPS[name](_Context(base_url, request_name))