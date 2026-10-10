# e9-graph-query 的 evals

本 skill 的 evals 验两类断言：**读者契约**（SKILL.md 自己立得住——frontmatter 合规、
链接可达、硬边界写清了）与**一致性契约**（SKILL 里写的图谱端点/图名不会与仓库的真实
配置各说各话）。**全离线、免费、不碰图谱主机。**

```bash
uv run python .claude/skills/e9-graph-query/evals/run.py           # 跑全部
uv run python .claude/skills/e9-graph-query/evals/run.py --list    # 只列用例
uv run python .claude/skills/e9-graph-query/evals/run.py --only qn-prefix-rule
```

顶层 runner 也会带上它：

```bash
uv run python scripts/run_skill_evals.py
```

## 为什么不验「查图谱能不能查对」

那需要联网打图谱主机，会让 evals 变成**依赖外部服务**的东西——而图谱在每天
01:00 起的维护窗口会返回 `busy`，一条会因为「人家在同步」而变红的断言不是好断言。
本项目「测试离线」是硬契约（`AGENTS.md`），evals 不该是第一处破例。

**真实查询能力由实际任务验证**：step 3 的用例转换任务就是它的验收场景——
那批用例的 `url` / `expect` 字段是不是真从图谱里查出来的、查得对不对，
在那边一次就能看出来。这里只守住「文档不会自己烂掉」。

## 断言的是什么

| 用例 | 断言 |
|---|---|
| `skill-frontmatter` | frontmatter 只有 `name` + `description`，且 `name` 与目录名一致 |
| `relative-links-resolve` | SKILL.md 里所有相对链接都指向真实存在的文件 |
| `references-linked` | `references/mcp-tools.md` 存在、够长、且被 SKILL.md 引用 |
| `registered-in-runner` | 已在 `scripts/run_skill_evals.py` 的 `FLAGS` 里登记 |
| `local-tool-ban-stated` | 「绝对禁止本地工具查 E9 源码」写清了，且点名禁用 `detect_changes` |
| `ui-coverage-gap-stated` | 写明图谱里**没有** UI 元素定位（id/xpath/选择器）、匿名 handler 会断链、替代不了 `snapshot.js` |
| `qn-prefix-rule` | 含点图名的 qn 剥前缀公式在，且写明了「不能按第一个点切」这个反例 |
| `endpoints-match-config` | SKILL 写的 `e9-graph` / `e9-ops` 与 `.mcp.json` 一致（**无 `.mcp.json` 则跳过**） |
| `no-foreign-references` | 没有把 api-test-E9 专有的路径与命令抄进来 |
| `route-recipes-present` | 五条查询配方齐全，且实测证据带文件出处 |
| `api-lookup-pitfalls-stated` | 配方 E 写清两条**静默失败**的坑（大小写不同的参数、假的默认数据）与自检字段 |
| `evals-readme-complete` | 本 README 列出了 `run.py` 里的每一个 case_id |

两条 SKIP 是**刻意的**，不是没写完：`.mcp.json` 与 `config.json` 都被 gitignore，
新克隆的工作区里没有它们。若把「文件不在」判成 FAIL，这条 eval 就会让
「新克隆能跑通」破功——而那是 `AGENTS.md` 明令守住的一条线。所以
`endpoints-match-config` 在缺文件时返回 `SKIP`，并把原因打出来。

## 这套 evals 查出来的真缺陷

1. **抄进来 4 类外来引用会静默指向不存在的文件**——`check_mcp_config_consistency`
   （只是 api-test-E9 的 `tools/` 脚本）、`.workbuddy/`（那边的目录约定）、
   `svn-impact`（那边的 skill）、`--graph`（那边接口框架 CLI 的参数）。
   本项目一个都没有。参考 SKILL 有 42KB，照着抄时这些很容易连带进来，
   而它们**不会报错、只是让读者照着敲一条敲不通的命令**——正是 `AGENTS.md`
   「仓库不得依赖不存在的路径」那一类。`no-foreign-references` 逐条钉住。
   本 skill 初稿就踩过一次：正文里写了 `api-test-E9/.workbuddy/skills/...`
   当对照，被这条自己查出来了（已改成不带路径的写法）。

2. **`name_pattern` 匹配的是符号名，不是 qn**——写 SKILL 时实测：
   `name_pattern="__route__.*workflow.*"` 对 `Route` 节点返回 **0 条**，
   而同样的条件写成 Cypher `MATCH (n:Route) WHERE n.qn CONTAINS '...'`
   才查得出来。这条差点被写成「路由节点查不到，图谱没覆盖路由」。
   结论已补进 `references/mcp-tools.md` 的 `search_graph` 误用提示里。

3. **`search_code` 比想象中重**——单个 pattern 实测跑到 **10–19 s**，
   还带 `warning_slow`。已在 references 里标明「比 `search_graph` 重，
   别无脑全库扫」，避免后来的读者拿它当廉价 grep 用。

4. **`evals-readme-complete` 初版自己有 bug，而且它差点漏掉的就是自己**——
   第一版用全文件正则扫 `("id",` 这样的元组来取 case_id，但
   `FOREIGN_REFERENCES` 的**元组长得一模一样**，于是把 4 个外来 token 也数了进去，
   报出「覆盖全部 **13** 条用例」（实际 11 条）。危害不是数字难看：
   README 真漏列某条用例时，只要恰好提过那个 token，这条断言就会**误报通过**。
   改成只解析 `CASES = [...]` 块之后数字才对上。
   同时补了**反向验证**——把副本 README 里 `qn-prefix-rule` 那行抽掉再跑，
   得到 `(False, "README 未列出这些用例：['qn-prefix-rule']")`。
   只测通过路径的断言等于没有断言。