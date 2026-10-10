---
name: e9-graph-query
description: "通过远程 E9 知识图谱 MCP 查询 E9（Ecology）的代码结构与调用链——主要查前端 js/ts（页面路由、按钮点下去做什么、点完跳到哪、前置步骤），也查后端 Java。用户只要提到 E9、ecology、知识图谱、调用链、谁调用了、页面路由、这个按钮做什么、表单页地址、codebase-memory、search_graph，或使用 /e9-graph-query，就必须用本 skill。绝对禁止用 Grep/Glob/Read/Bash 查 E9 源码——本机没有 E9 源码，只能走 MCP。不要用于非 E9 仓库、不要调用 git 版 detect_changes、不要擅自重建或删除远程索引。主机有三张图（主干后端 / 主干前端 / 2605 基线），会话首次涉及查询必须先调 e9_list_repos 确认目标仓库。主要服务于 [nl-case-author](../nl-case-author/SKILL.md) 写用例前的功能分析。"
---

# 查 E9 知识图谱（本项目的功能分析底座）

你正在查询一套**已经建好、远程共享**的 E9 代码知识图谱。它回答的是「**这个功能在代码里是怎么实现的**」——比逐文件 grep 准、省 token，还能跨包找到调用方。

**权威依据**：服务端契约以对端交付的《E9图谱系统MCP客户端使用手册》为准（在 `api-test-E9` 仓库的 `E9_svn_analyse/docs/` 下），优先于任何客户端转述。本 SKILL 只讲「本项目的客户端怎么用」，冲突时以手册为准。

## 本项目的用法与 api-test-E9 那个 skill 的区别

同名能力在同作者的 `api-test-E9` 仓库里有一份面向**接口测试**的版本（`e9-codebase-memory`）。本项目的版本做过三处适配，别照着那边抄：

| | api-test-E9（接口测试） | 本项目（UI 自动化） |
|---|---|---|
| 主查哪张图 | 后端 `E9主干`（Java） | **前端 `E9主干前端`**（js/ts）——要的是页面路由与操作链路 |
| 拿来干什么 | 分析提交影响面、找接口实现 | **写/排 UI 用例**：路由、按钮语义、前置步骤 |
| 顺手带出的产物 | 影响面报告 | 用例的 `url` / `goal` / `expect` 三个字段 |

所以本 SKILL 的重心是**第三节「写 UI 用例时怎么用图谱」**；影响面分析那套（`e9_svn_diff` + `trace_path` inbound）在这里只作次要用途。

## 一、服务与端点

| 项 | 值 |
|---|---|
| 查询 MCP（`e9-graph`） | `http://<图谱主机>:9750/mcp` |
| 运维 MCP（`e9-ops`） | `http://<图谱主机>:9750/servers/e9-ops/mcp` |
| 已建成图 | `192.168.7.207_trunk`（别名 `E9主干`，**Java 后端**）、`192.168.7.207_trunk_frontend`（别名 `E9主干前端`，**前端 js/ts**）、`192.168.7.207_9.00.2605`（别名 `E9-2605基线`） |

端点以仓库根 `config.json` 的 `mcp` 块为唯一权威落点（`mcp.host` / `mcp.port` / `mcp.query_path` / `mcp.ops_path` / `mcp.graph_project`），`.mcp.json` 是把它接进 Claude Code 的注册文件（两者都已被 gitignore——**本仓库是 GitHub 公开仓库，内网地址绝不入库**）。

读取方式收敛在 `jev_ultrafast/framework/e9_config.py:mcp()`，它把 `host` + `port` + 两个 path 拼成 `query_url` / `ops_url`。换图谱服务器只改 `config.json` 的 `mcp.host`。

⚠️ **`E9主干` 与 `E9主干前端` 是同一个 trunk 的前后端两半**（同 SVN URL、同 revision，只差索引范围）。选图有两个维度：**哪套版本**（`E9主干` vs `E9-2605基线`）+ **哪一层**（后端 Java vs 前端 js/ts）。**本项目默认用 `E9主干前端`**；Java 类/方法/调用链才用 `E9主干`。**选错图的表现是查询为空，而不是报错**——最安静的一类错误。

## 二、第一步：确认目标仓库（会话首次必做）

1. 调运维端 `e9_list_repos()`（只读、零子进程、不需 confirm），拿 `name` / `alias` / `display` / `enabled` / `db_exists`。
2. **本项目默认目标是 `192.168.7.207_trunk_frontend`**（`E9主干前端`）。要查 Java 后端或 2605 基线时才另选，且**要问清是哪一层**。
3. 选定后用**规范图名**当 `project` / `name`，不要传别名。

查询端 `project` 自 2026-09-20 起**规范图名与别名都能查**（服务端有别名翻译代理），传别名不会报错；但仍**显式传规范图名**——翻译只保证「传了不出错」，选哪张图靠的仍是这一步的判断。

**「有哪些图」有三个口径，别混用**：

- `e9_list_repos()`（运维端）= **登记清单 + 别名映射**，是「确认目标仓库」的权威入口。
- `list_projects`（查询端）= **已建成图谱 DB、可查询的图**。
- `e9_status()`（运维端）= **所有已登记条目**，含工作副本缺失、建图被中断的。

`db_exists=false`、或不在 `list_projects` 里的图，调查询工具会得 `project_not_found`——**登记不等于可查**，别据此断定「图名写错了」。

## 三、写 UI 用例时怎么用图谱 ⭐

这是本 skill 存在的理由。写一条自然语言 UI 用例前，先回答下面四个问题，**答案直接决定用例的字段怎么写**。

### 配方 A：这条用例该从哪个 URL 开始？→ 查路由表

E9 的 PC 门户是 SPA，页面路由**不是** HTTP 路径，而是 `/wui/index.html#/main/...` 的 hash。路由表在**模块的 `index.js` 里以一组 `<Route path="...">` 声明**。

```
search_graph(query="<模块>Route", label="Variable", file_pattern="**/<模块>/index.js")
→ get_code_snippet(<qn>)
```

**实测（2026-09-30，`src4js/pc4mobx/workflow/index.js:76-140` 的 `WorkflowRoute`）**：

| `breadcrumbName` | `path` | 完整路由（拼 `<base>/wui/index.html#/main/workflow/`） |
|---|---|---|
| 新建流程 | `add` | `#/main/workflow/add` |
| 待办事宜 | `listDoing` | `#/main/workflow/listDoing` |
| 已办事宜 | `listDone` | `#/main/workflow/listDone` |
| 我的请求 | `listMine` | `#/main/workflow/listMine` |
| 流程监控 | `monitor` | `#/main/workflow/monitor` |

`breadcrumbName` 是**中文页面名**、`path` 是**英文路由段**，两者一一对应——这正是「用例里写中文、`url` 里写英文」的桥。

> **注意门户壳**：员工工作台是 `/wui/index.html`，后端引擎是 `/wui/engine.html`，**两个壳不能互相替代**（把引擎的 hash 打进 index.html 只会渲染出门户首页）。查路由时先确认在那个壳里。

### 配方 B：点这个按钮会发生什么？→ 查按钮的动作函数

E9 的流程表单按钮**不是 `<button onclick="...">`**，而是 `util/formbtn.js` 里的一批导出函数。按动作名去找：

```
get_file_outline(project="192.168.7.207_trunk_frontend", file_path="src4js/pc/workflow/util/formbtn.js")
→ get_code_snippet(<qn>)
```

**实测（2026-09-30，`src4js/pc/workflow/util/formbtn.js`）**：

| 界面上看到的 | 函数 | 它做什么 |
|---|---|---|
| 「提交」 | `doSubmitNoBack:76-92` | `doBeforeSubmit({actiontype:"requestOperation", src:"submit", needwfback:"0"})` |
| 「保存」（存草稿、不提交） | `doSave_nNew:97-99` | `doBeforeSubmit({actiontype:"requestOperation", src:"save"})` |
| 「退回」 | `doReject_New:228` | — |
| 「收回」 | `doRetract:160` | — |

`src` 的取值（`submit` / `save` / …）就是**同一个表单页上不同按钮的分水岭**——写 goal 时说的「点提交」和「点保存」在代码里是两条不同的链路，别混。

### 配方 C：表单页自己的地址是什么？→ 查 URL 拼接函数

流程表单是**另一个 SPA 壳**，不在 `/wui/` 下：

**实测（2026-09-30，`src4js/pc/workflow/components/Req.js:301-303`）**：

```js
getMobxUrl(){ return location.href.replace("/spa/workflow/index_form.jsp", "/spa/workflow/static4form/index.html"); }
```

即表单页壳是 `/spa/workflow/static4form/index.html`。**这条对本项目特别重要**：`cases/e9/workflow_add.yaml` 的实测记录里写着「提交后 URL 仍在 `static4form`」——那正是这个壳，不是没跳转成功。

⚠️ 表单页的具体 hash 带 `requestid` 等动态段，**不稳定、不要写进用例的 `url`**；用例应从**稳定的列表页或新建页**出发，靠页面内的点击走到表单。

### 配方 D：前置步骤 / 环境依赖 → 查模块清单

问「这一步之前页面上必须先有什么」「这个列表数据从哪来」：

```
get_architecture(project="192.168.7.207_trunk_frontend", path="src4js/pc4mobx/<模块>", aspects=["structure"])
search_graph(file_pattern="src4js/pc/workflow/util/*.js", query="doing list refresh")
```

**前端只有具名函数进图**——`doing.js`（待办模块：`reLoad` / `showallreceived` / `doForward`）这类**工具模块**是能查的，而「点某一行之后执行的那段内联回调」**查不到**（见第五节）。所以配方 D 的产出是「**有哪些前置动作/入口存在**」，不是「点完之后的完整链条」。

### 配方 E：要自己发 E9 接口时，怎么把端点与参数名找出来 ⭐

排障和搭测试环境时经常要绕开 UI 直接调接口（例如给账号配密级、建流程定义）。
**别猜端点名，也别去翻后端 Java**——最短的路是前端的 `apis/` 目录：

```
query_graph(project="192.168.7.207_trunk_frontend",
  query="MATCH (f:File) WHERE f.path STARTS WITH 'src4js/pc4backstage/<模块>/apis' RETURN f.path")
get_file_outline(project="...", file_path="src4js/pc4backstage/<模块>/apis/<名字>.js")
```

前端每个 API 函数都是一行 `WeaTools.callApi('/api/...', 'GET'|'POST', params)`，
`get_file_outline` 拿到函数名，`get_code_snippet` 拿到**端点字面量**。
`<模块>` 就是页面 hash 里那一段（`#/hrmengine/...` → `hrmengine`，
`#/workflowengine/...` → `workflow`）。

**参数名要再往里一层**——去读调用它的 store，别读函数本身：

```
trace_path(project="...", function_name="<那个 API 函数>", direction="inbound")
get_code_snippet(project="...", qualified_name="<store 里的方法>")
```

422 字符的名字长度、`groupid` 这种**只存在于页面 HTML 里**的值、请求体的扁平化规则
（`add_<key>_<i>`）都在 store 那一层，不在 API 函数里。

⚠️ **两条踩过的坑**：
① **参数名大小写不是风格问题**：`workflowId` 与 `workflowid` 是两个不同的参数，
传错的那个被静默忽略，返回一个**形状正确但内容是默认值**的响应——
看起来像"这个环境没配数据"。
② **读不对时的表现是"返回了假数据"，不是报错**。四条不同流程读出**完全相同**的
一行，那不是数据，是默认模板；这时该回头查参数名，而不是继续分析数据。
`operatelevel` / `logArray[].belongTypeTargetId` 这类回显字段是自检点：
`belongTypeTargetId` 等于你传的 id 才算认对了对象。

### 查完落到用例的哪个字段

| 图谱查到的 | 写进用例 |
|---|---|
| 路由（配方 A） | `url: "{{ base_url }}/wui/index.html#/main/workflow/listDoing"` |
| 按钮的语义与前置（配方 B/D） | `goal` 里的步骤描述——**用界面上的中文名**（「提交」「待办事宜」），不要写函数名 |
| 表单页/结果页 URL 特征（配方 C） | `expect` 里的 `url_contains` |
| 页面上确定存在的文字（`breadcrumbName`、页签名） | `expect` 里的 `text_contains` |
| 这一步**不该**出现的东西 | `expect` 里的 `text_not_contains` / `element_absent` |

⚠️ **图谱给不了 UI 元素定位**（id / xpath / 选择器一个都没有，见第五节）。所以图谱**替代不了** `snapshot.js` 的真实 DOM 观测——它只帮你把 `url` 和 `goal` 写对，**不要**指望从图里抄出选择器。

## 四、查询工具速查

参数细节读 [references/mcp-tools.md](references/mcp-tools.md)。先 `tools/list` 发现工具、按拿到的 schema 调用，**不要编造参数名**。查询类工具都要 `project`：

| 工具 | 用来做什么 |
|---|---|
| `search_graph` | 找符号：`query=`（BM25 自然语言）/ `name_pattern=`（正则）/ `semantic_query=`（**必须是数组**），可加 `label` / `file_pattern` / `qn_pattern` |
| `trace_path` | 调用链：`function_name=<qn>`、`direction=inbound\|outbound\|both`、`depth`（默认 3） |
| `get_code_snippet` | 按 qn 取源码片段（**先用 search_graph 拿到唯一 qn**） |
| `get_file_outline` | 单文件声明大纲——**配方 B 的主力**，比整文件切片省 token |
| `get_architecture` | 模块结构 / 入口 / 热点 / 聚类；`path=` 限定目录 |
| `query_graph` | 只读 Cypher，用于多跳与聚合 |
| `check_index_coverage` | 核对某路径是否被索引——**说「图上没有」之前必须先做** |
| `get_graph_schema` | 节点/边类型与计数（返回的是**列表**不是字典） |

常用 `label`：`Function` / `Method` / `Variable` / `Class` / `File` / `Module` / `Route` / `Folder`。**没有 `Package` 标签**，目录层级用 `Folder`。前端图用同一套标签。

`trace_path` 返回 `ambiguous` 时，从 suggestions 里拷 `qualified_name` 再重调，**不要用短名字硬猜**。

`search_graph` 为空时：放宽正则 → 去掉 `label` → 改用 `query=`。**若查的是前端 js/ts 符号，先核对用的是不是 `E9主干前端`**——拿后端图查前端必然为空（那是选错图，不是没覆盖）。仍为空，多半落在未覆盖区（第五节）——**明确说图谱未覆盖，不得转用本地 Grep/Read 查 E9 源码**。

## 五、索引覆盖边界（**UI 场景下最要命的一节**）

前后端各查各的图，**两张图都不是全量**：

| 层 | 图 | 覆盖 | 主要缺口 |
|---|---|---|---|
| 后端 | `E9主干` / `E9-2605基线` | `src/**/*.java` | JSP、SQL、静态资源 |
| 前端 | `E9主干前端` | 4 个前端主目录（`src4js` / `mobilemode` / `js` / `formmode`）的 js/ts，约占全仓库 js/ts 的 **80%** | `spa` / `mobile` / `wui` / `workflow` 等目录约 6,700 个 js/ts（**未进前端图**）、JSP、HTML、`*.min.js`、匿名回调 |

**前端图能查 / 不能查**——对写 UI 用例的人来说，右边这列比左边重要：

| ✅ 能查 | ❌ 不能查 |
|---|---|
| 具名 js/ts 函数（`function loadList(){}`、`var f = x => …`、`export const g = () => …`） | **匿名事件 handler / 内联箭头回调**（不建节点）——**「点击按钮后做什么」的链条在这里断掉** |
| 前端函数间的 CALLS / USAGE 边、`trace_path` 出入边 | **页面显示什么**：`.jsp` 是 0 节点；`.html` 只有 File + Module 节点，**无 DOM / ID / onclick** |
| 模块结构与文件大纲（`get_file_outline` 对 js 有效） | **UI 元素定位**（id / xpath / 选择器）——写 UI 用例必需的这类信息**图上没有** |
| 代码片段（`get_code_snippet` 能取前端源码，含后端 URL 字面量） | `*.min.js`（CBM 按设计跳过） |

**依旧不在任何图内**：JSP（0 节点）、HTML 的 DOM/ID、SQL（仅空壳节点）、`*.min.js`、js 匿名函数与裸箭头、静态资源，以及**未进前端图的那约 20% js/ts**。前端问题在这些位置查不到时，**要说「可能落在未覆盖区」，不要断言「不存在」**。

**前后端桥接要手工做**：CBM **没有跨图边**，一条查询打不通「前端函数 → 后端接口」。前端 `apis/*.js` 是天然桥接点（`WeaTools.callApi('/api/...')` 一类的 URL 字面量），但 **URL 不能直接匹配**——前端写 `/api/workflow/...`，后端 Route 是 `/workflow/...`（少 `/api` 前缀，被网关剥掉），动态段也对不上。做跨图关联必须写归一化规则，并**标明这是推测性关联**。

## 六、硬性规范

1. **仅通过 MCP 查询，禁止本地工具。** 查定义、调用方、被调用、架构、路由时，**必须且仅能**通过 `e9-graph` MCP 查询。**绝对禁止**用 Grep/Glob/Read/Bash 查 E9 代码或 SVN 提交信息——**本机根本没有 E9 源码副本**（源码在图谱主机的 `code_repo` 上），本地查必然落空并浪费时间。
2. **先确认仓库，再带对参数。** 见第二节。空结果多半是漏了这一步。
3. **先解析名字再追踪。** `trace_path` / `get_code_snippet` 需要唯一符号。禁止瞎猜 qn。
4. **不要改共享索引。** 除非用户明确要求重建或删除 E9 图谱，否则不要调用 `index_repository`、`delete_project`。
5. **没有覆盖检查，不下穷尽结论。** 说「图上没有这个功能」之前，对相关路径调 `check_index_coverage`。结果干净只表示「没有记录到的缺口」，不是完整性证明。
6. **默认只读。** search / trace / query_graph / snippet / architecture / coverage / list_projects / index_status 可直接用；**写入类工具先问用户**。
7. **不要调用 `detect_changes`**——它内部跑 `git diff`，E9 是 SVN，必然失败。

## 七、运维端（`e9-ops`）

运维 MCP 提供图谱主机的维护能力。**本项目日常只需要它的两个只读能力**：

| 工具 | 用途 | 只读 |
|---|---|---|
| **`e9_list_repos`** | **会话第一步**：登记清单 + 别名映射 | ✅ |
| `e9_status` | 工作副本 SVN 版本与图谱状态 | ✅ |
| `e9_svn_log` / `e9_svn_diff` | 某一笔提交改了什么（配合覆盖检查） | ✅ |
| `e9_check_sync` | 检查图是否最新；**`dry_run=true` 只读** | 更新 ❌（异步） |

**图谱更新不是本项目的职责。** 更新作业会**占用全局单锁、影响所有人**，且无法远程中止；`e9_add_repo` 首建全量约 **1 小时**，`e9_check_sync` 走全量约 **25–45 分钟**。本项目遇到图谱落后，应当**告知用户并交给图谱维护方**，不要自己发起。若用户明确要求更新：

- 唯一合法入口是 `e9_check_sync`（**异步**，返回 `job_id` 用 `e9_job_status` 轮询）——`e9_sync` / `e9_reindex` 同步阻塞、**禁止调用**；
- 发起前先 `e9_check_sync(dry_run=true)` 只读预检；
- 需要全量时服务端会先返回 `full_rebuild_required`（**不执行、图未触碰**）——**停下把 `estimated_full_minutes` 念给用户，同意后**才带 `allow_full=true` 重发。**禁止不询问就重发**，也**禁止当错误上报**；
- `busy` / `svn_unavailable` 是「服务端正在忙」**不是故障**——标准回话「图谱主机正在执行同步任务，请稍后重试」，**契约上不得自动重试**；
- ⚠️ `E9主干前端` 的 `graph_stale` **恒为 `true`**（`recording_status=truncated` ⇒ `graph_complete=false`），这是**稳定属性、不是损坏**；实际代价约 1–2 分钟，**不要上报成故障、也不要用全量重建去「修」**。

## 八、排障：连不上时先查本地代理

任一端点报 `mcp_call_failed` / 连接错误、或症状是「`initialize` 成功但后续调用裸 404」时，**先排除本地代理挟持**——本机若设了 `HTTP_PROXY` / `HTTPS_PROXY`，Python `requests` 默认 `trust_env=True` 会把图谱主机这种**内网直连地址也经代理转发**，MCP 会话因此失效。

> **本机（Windows）已知有 Clash 之类代理常驻在 `:7890`**，这类症状在本机**先按代理排查**，不要误判为图谱服务端故障。

典型症状是**「假成功 + 裸 404」**：`initialize` 返回 200 且拿到 `mcp-session-id`，但同一 session 的后续调用返回**裸 `404 Not Found`（响应体只有 9 字节纯文本）**——这是**代理**返回的 404，不是图谱主机的路由问题。响应头带 `server: uvicorn` **也不能**据此断定是图谱主机（两者同为 uvicorn 系，该头会透传）。

处置：检查 `env | grep -i proxy`（bash）或 `$env:HTTP_PROXY`（PowerShell）→ 把图谱主机加入绕过列表，**无需改任何代码**：

```bash
# bash：同一条命令内生效
NO_PROXY="<图谱主机>" no_proxy="<图谱主机>" uv run pytest tests/test_nl_cases.py --nl
```

```powershell
# PowerShell：设一次，当前会话内后续调用即走直连
$env:NO_PROXY = "<图谱主机>"; $env:no_proxy = "<图谱主机>"
```

图谱主机地址以 `config.json` 的 `mcp.host` 为准。`NO_PROXY` 是**进程级**变量，新开终端要重设。复测通过后**继续原任务**，并如实告知用户「这是本地环境问题，不是图谱服务端故障」。

## 九、回答风格

- 先给结论（这个功能在哪个页面、走哪条链路），再给证据。
- 引用 **qualified name** 和 `file_path:行号`，不要只写短函数名（E9 里 `isUpdate` 会撞名）。
- **标注把握**：图谱支撑 ／ 不在索引 ／ 覆盖缺口。三者要分清，不要把「查不到」说成「不存在」。
- 写给 UI 用例的结论，**落到界面上的中文名**（「待办事宜」而不是 `ListDoing`）——用例的读者是测试同事，不是开发。

断言写在 [evals/](evals/) 里，免费档即可跑：

```bash
uv run python .claude/skills/e9-graph-query/evals/run.py
```