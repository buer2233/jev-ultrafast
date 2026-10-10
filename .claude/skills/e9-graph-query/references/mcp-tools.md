# E9 知识图谱 MCP 工具说明（客户端侧参数速查）

服务端契约以对端交付的《E9图谱系统MCP客户端使用手册》为准；本文件只是客户端侧的速查，**冲突时以手册为准**。

除 `list_projects`（`offset` / `limit` / `metadata_only` / `include_details`）和 `index_repository`（必填是 `repo_path`，不是 `project`）外，**所有查询类工具都要求 `project`**：缺省取 `config.json` 的 `mcp.graph_project`，其它图传对应**规范图名**。

**`project` 规范图名与别名都认**（2026-09-20 起服务端有别名翻译代理）：传别名自动翻译成规范图名，传规范图名原样透传。未登记的图名回 `project not found or not indexed` 并附 `available_projects`——**是明确报错，不查空、也不会误建第二张图**。**但仍应显式传规范图名**：翻译只保证「传了不出错」，选哪张图仍要经 `e9_list_repos` + 判断这一步。

⚠️ **别名会让 qn 前缀剥错**：传别名查询能成功，但返回的 qn 前缀**仍是规范图名**。剥前缀若用 `qn[len(project)+1:]` 而 `project` 传的是别名，就会把前缀切成半截。**这就是「一律传规范图名」在本项目不只是命名洁癖、而是正确性要求的原因**。

## 发现与查询

| 工具 | 用来做什么 | 关键参数 |
|---|---|---|
| `list_projects` | 确认**可查询**的图（只列已建成 DB 的） | `include_details=true` 看节点/边数与 DB 字节；`offset` / `limit`（默认 50）分页 |
| `search_graph` | 按名称 / 全文 / 语义找符号 | `query`、`name_pattern`、`label`、`file_pattern`、`qn_pattern`、`semantic_query`（**数组**）、`limit`、`offset` |
| `trace_path` | 调用链 BFS | `function_name`（尽量用 qualified name）、`direction=inbound\|outbound\|both`（默认 `both`）、`depth`（默认 3）、`mode=calls\|data_flow\|cross_service`、`limit` + `cursor` 翻页、`include_tests`（默认 false，测试文件被过滤） |
| `get_code_snippet` | 按 qn 取源码片段 | 先 `search_graph` 拿到 `qualified_name`；`include_neighbors` |
| `get_file_outline` | 单文件声明大纲 | 按文件路径列出声明（省去整文件切片）；其余参数以 `tools/list` 的 schema 为准 |
| `compare_graphs` | 两个**已索引快照**的确定性差异 | 用于「换了版本后哪些符号变了」；参数以 `tools/list` 为准 |
| `query_graph` | 只读 Cypher | `query`（必填）、`graph=code\|missed`、`max_rows`（硬上限 10 万行，**不支持 offset**） |
| `get_graph_schema` | 节点/边类型与计数 | 返回 `node_labels[]` 与 `edge_types[]`，**是列表不是字典** |
| `get_architecture` | 模块、入口、热点、聚类 | `aspects`：`overview` / `clusters` / `all`、`path`（目录前缀） |
| `search_code` | 带图谱增强的文本搜索 | `pattern`、`file_pattern`、`path_filter`、`mode=compact\|full\|files`、`context`、`regex`、`limit`；**比全库 grep 好，但比 `search_graph` 重**——实测单个 pattern 可跑到 10–19 s，别无脑全库扫 |
| `index_status` | 索引状态与覆盖缺口 | `verbose`（默认 false，true 才带 git context 块） |
| `check_index_coverage` | 核对指定路径是否被完整索引 | `paths` 和/或 `scopes`，至少传一个；`scope_limit` / `scope_offset` 分页 |

`search_graph` 三种模式**可组合**：

- `query="update settings"`：BM25 全文，适合自然语言。
- `name_pattern=".*RequestManager.*"`：正则匹配名字。
- `semantic_query=["submit","workflow"]`：**必须是字符串数组**，不能传单个字符串。

常用 `label`：`Method` / `Field` / `Variable` / `File` / `Class` / `Route` / `Folder` / `Interface` / `Enum` / `Function` / `Module` / `Decorator`。**没有 `Package` 标签**（目录层级用 `Folder`）。边类型主要是 `CALLS` / `USAGE` / `DEFINES` / `IMPORTS` / `DEFINES_METHOD` / `WRITES` / `INHERITS` / `IMPLEMENTS` / `OVERRIDE` / `HANDLES` / `HTTP_CALLS`。精确计数用 `get_graph_schema` 现查，不要引用过期快照。**前端图用同一套标签**（节点以 `Function` / `Method` / `Variable` / `File` / `Module` 为主，也有 `Class` / `Route`）。

`trace_path` 返回 `ambiguous` 时：从 suggestions 里拷 `qualified_name`，再带完整 qn 重调，**不要用短名字硬猜**。

> **`search_graph` 的常见误用**：拿 `name_pattern` 去匹配 `Route` 节点的路由字符串。实测 `name_pattern="__route__.*workflow.*"` 返回 0 条，而同样的条件写成 Cypher `MATCH (n:Route) WHERE n.qn CONTAINS '...'` 能查出来——**`name_pattern` 匹配的是符号名，不是 qn**。要按 qn 内容过滤用 `qn_pattern` 或 `query_graph`。

## 含点 project 名的 qn 前缀剥法

qn（`qualified_name`）以图名开头，后跟一个点再接路径：

| project（规范图名） | qn 示例 |
|---|---|
| `192.168.7.207_trunk` | `192.168.7.207_trunk.src.com.wf.Job` |
| `192.168.7.207_trunk_frontend` | `192.168.7.207_trunk_frontend.src4js.pc.workflow.util.formbtn.doSubmit` |

剥前缀**必须按 `<project>.` 整串切**：

```python
short = qn[len(project) + 1:]   # "src4js.pc.workflow.util.formbtn.doSubmit"
```

**不能** `qn.split('.')[1:]`——现网图名本身都含点，按第一个点切会把图名切碎成 `168` / `7` / `207_trunk` / `src` …。单段图名（如 `e9`）两种写法结果相同，所以这个坑**极易漏掉**。

## 只读 Cypher 示例

```cypher
MATCH (c:Class) WHERE c.name CONTAINS 'WorkflowRequest' RETURN c.name, c.file_path LIMIT 20
```

```cypher
MATCH (f:Method)-[:CALLS]->(g) WHERE f.qualified_name ENDS WITH '.isUpdate' RETURN g.qualified_name LIMIT 30
```

```cypher
// 按 qn 内容过滤（name_pattern 做不到的那种）
MATCH (n:Route) WHERE n.qn CONTAINS 'index.html' RETURN n.qn, n.name LIMIT 40
```

超时就收窄 `WHERE`、改用有向 `MATCH`、加 `LIMIT`。**不要对全图几百万条边做无过滤扫描**。

## 不要默认调用

| 工具 | 原因 |
|---|---|
| `detect_changes` | 内部走 `git diff`。E9 是 SVN，**会失败**。 |
| `index_repository` | 会锁项目、耗大量内存，**改的是共享图谱**。除非用户明确要求重建。 |
| `delete_project` | 破坏性。 |
| `manage_adr` 的 update | 会改共享 ADR。查询可以，**写入先问**。 |
| `ingest_traces` | 写入运行时边，先问。 |

## 索引覆盖

**前后端分属两张图，各自都不是全量**：

| 层 | 图（`project`） | 进图 | 不进图 |
|---|---|---|---|
| 后端 | `192.168.7.207_trunk` / `192.168.7.207_9.00.2605` | `src/**/*.java` | JSP（0 节点）、SQL（仅空壳节点）、`*.min.js`、静态资源、`data/`、`cloudstore/` 等排除目录 |
| 前端 | `192.168.7.207_trunk_frontend`（与主干**同 URL、同 revision**） | `src4js` / `mobilemode` / `js` / `formmode` 的 js/ts，约占全仓库 js/ts 的 **80%** | 其余目录（`spa` / `mobile` / `wui` / `workflow` 等约 6,700 个 js/ts）、`*.min.js`、js 匿名 `function(){…}` / 裸箭头回调（**不建节点**）、JSP、HTML 的 DOM/ID |

**前端图能查**：具名 js/ts 函数的节点与 CALLS / USAGE 边、`trace_path` 出入边、`get_file_outline`（对 js 有效）、`get_code_snippet`（能取前端源码，**含后端 URL 字面量**）。

**前端图不能查**：匿名事件 handler / 内联箭头回调（**「点击按钮后做什么」在这里断链**）、`.jsp`（0 节点）、`.html` 的 DOM/ID/onclick、**UI 元素定位（id / xpath / 选择器）**。

**跨图不能一条查询打通**：CBM **没有跨图边**。「前端函数 → 后端接口」要手工桥接（前端 `apis/*.js` 的 `WeaTools.callApi('/api/...')` URL 字面量 ↔ 后端 Route 节点），且 URL 需归一化——后端 Route 少 `/api` 前缀（被网关剥掉）、动态段（`${actiontype}`）对不上，**只能标明是推测性关联**。

前端符号查不到时，**先确认用的是不是 `E9主干前端` 图**（用后端图查前端 js 必然为空），再考虑未覆盖区（约 20% js/ts 未进图）——说明「可能未覆盖」，**不要断言「不存在」**。图外路径：用 `e9_svn_diff` 说明「图谱看不到」，不要假装 `search_graph` 已经覆盖，**也不要用本地 Read/Grep 去补**（本机没有 E9 源码）。

## 客户端怎么接到这套 MCP

本项目的注册文件是仓库根 `.mcp.json`：

```json
{
  "mcpServers": {
    "e9-graph": { "type": "http", "url": "http://<图谱主机>:9750/mcp" },
    "e9-ops":   { "type": "http", "url": "http://<图谱主机>:9750/servers/e9-ops/mcp" }
  }
}
```

- 权威落点是 `config.json` 的 `mcp` 块；`.mcp.json` 是把它接进 Claude Code 的注册文件，**两者都被 gitignore**（本仓库是 GitHub 公开仓库，内网地址绝不入库）。入库的只有脱敏模板 `.mcp.example.json`。
- 程序里读配置用 `jev_ultrafast/framework/e9_config.py:mcp()`，它给出拼好的 `query_url` / `ops_url`。
- **两个端点都要配**：别名映射靠运维端的 `e9_list_repos`；只配查询端也能查（服务端会翻译别名），但拿不到 `name↔alias` 映射。
- **本机不需要 E9 源码副本**，也不需要安装 CBM 二进制——`get_code_snippet` 读的是图谱主机上的 `code_repo`。

---

## 运维 MCP 工具（`e9-ops`）

查询端负责「读代码」，运维端负责「维护图谱」。**图谱更新不是本项目的职责**——更新作业占用全局单锁、影响所有人且无法远程中止；日常同步由图谱主机的计划任务完成。本项目遇到图谱落后应**告知用户并交给维护方**。

| 工具 | 用途 | 只读 | 关键参数 |
|---|---|---|---|
| **`e9_list_repos`** | **会话第一步**：登记清单 + 别名映射 | ✅ | 无 |
| `e9_status` | 工作副本 SVN 版本与图谱状态 | ✅ | 可选 `name`；缺省返回全部概览 |
| `e9_svn_log` | 指定 revision 的提交元数据 + 变更路径 | ✅ | `revision`（必填）+ 可选 `name`。禁止 `confirm` / path / 凭据 |
| `e9_svn_diff` | 指定 revision 的 unified diff | ✅ | `revision`（必填）；可选 `max_bytes`（默认 256KiB，上限 1MiB）、`name` |
| `e9_list_revisions`（别名 `list_revisions`） | 锚点邻域 + boundary | ✅ | `revision`（必填）；`before` / `after`；可选 `name` |
| `e9_list_revisions_in_range` | 闭区间内实际提交 | ✅ | `from_revision` + `to_revision`（必填）；可选 `name` |
| `e9_svn_update` | 对工作副本执行 `svn update`，**不重建图谱** | ❌ | `confirm="e9-sync"`、可选 `name` |
| `e9_check_sync` | 检查图是否最新，可批量/单个更新；**「更新」的唯一合法入口** | `dry_run=true` 只读；更新 ❌（**异步**） | `name` / `dry_run` / `confirm`；第二段确认加 `allow_full=true`。⚠️ `incremental_only` **缺省 `true`、不要传** |
| `e9_add_repo` | 首建全量入口：新增仓库（稀疏 checkout）+ 首次建图 | `dry_run=true` 只读；真发起 ❌（**异步**） | `svn_url`（必填）+ `confirm="e9-sync"`；`would_be_refused` 为 true 时必被拒 |
| `e9_job_status` | 轮询异步作业状态 | ✅ | `job_id`（缺省列最近摘要） |
| ⛔ `e9_reindex` / `e9_sync` | **禁止调用**：同步阻塞、不返回 `job_id`、无进度 | ❌（同步） | 仅供识别，勿调用 |

> 运维端 `tools/list` 有 14 条，其中 `list_revisions` / `list_revisions_in_range` 是 `e9_list_revisions*` 的**别名条目**——「14」是接口条数，不是 14 种能力。

### 更新图谱时必守的三条

1. **唯一入口 `e9_check_sync`**（异步，返回 `job_id`）。发起前先 `dry_run=true` 只读预检。
2. **全量走两段式确认**：缺省发起时若本次无法保证走增量，服务端**先返回 `full_rebuild_required`**（不执行、图未触碰，`graph_untouched=true`）——**停下把 `estimated_full_minutes` 念给用户，同意后**才带 `allow_full=true` 重发。**禁止不询问就重发**（服务端每次授权都有审计留痕），也**禁止当错误上报**。
3. **放行全量的唯一开关是 `allow_full`**。传 `incremental_only=false` 仍会被同步拒绝（`full_rebuild_forbidden`），**不要重试、不要换参数名绕过**。

### 失败语义归一（**不要当故障上报**）

| 错误码 | 类别 | 处置 |
|---|---|---|
| `busy` / `svn_unavailable` | 可重试 | **不是故障**。「图谱主机正在执行同步任务，请稍后重试。」**契约：不得自动重试**——重发只会加重锁竞争 |
| `full_rebuild_required` / `full_rebuild_blocked` | 需确认 | **不是故障、也不是终局拒绝**。问用户，同意才带 `allow_full=true` 重发 |
| `project_not_found` / `revision_not_in_project` / `invalid_revision` / `name_conflict` / `full_rebuild_forbidden` | 需修正 | 重试无用，须改参数：用 `e9_list_repos` 核对图名 |
| `working_copy_missing` / `graph_db_missing` / `incremental_unavailable` / `incremental_violated` | 需人工 | 图谱主机侧状态问题，**联系管理员** |
| `mcp_call_failed` 等 | 未知 | **先按「排障」一节走一遍本地代理排查** |

**每日维护窗口**：每天凌晨 **01:00** 起，`daily_full: true` 的图会串行更新（走增量约 6 分钟、全量约 30 分钟，三图合计约 40–90 分钟）。此时发起会撞 `busy`——**是既定窗口，不是故障**。

**`E9主干前端` 的 `graph_stale` 恒为 `true`——稳定属性，不是故障**：该图 `recording_status=truncated` ⇒ `graph_complete=false`，`e9_check_sync(dry_run=true)` 会**永远**把它列进 `stale[]`、`expected_route` 恒为 `full`。实测真实代价与提示严重不符（夜间走增量、约 73 秒）。**不要上报成「图谱损坏」，也不要用全量重建去「修」**（重建后依然是 `truncated`）。