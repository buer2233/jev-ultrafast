# 框架与改造详解

这是 [`README.md`](../README.md) 的展开：**改造的边界**、**为 UI 自动化测试加的特色**、
**框架怎么工作**，以及**开发者要跑哪些校验**。

> 只想快速上手 → [快速开始](../README.md#快速开始)；
> 只想看效率数字 → [效率实测](performance.md)；
> 只想看内核机制 → [设计说明](design.md)。

## 一、改造的边界：外挂的一层

本仓库 fork 自 [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast)（MIT）。
上游是 Browser Use 的**动态、索引化动作空间的浏览器 agent 内核**：给它一个目标，
[TypeSafe 的 Jev](https://docs.typesafe.ai/introduction) 一次请求同时选出「操作 + 该操作对应的目标」，
只有在操作是 `TYPE_TEXT` 时才由一个小模型生成文字。

| 部分 | 位置 | 说明 |
|---|---|---|
| **浏览器 agent 库**（上游内核） | `jev_ultrafast/`（除 `framework/`） | 观测 → 决策 → 执行，以及全部安全校验 |
| **自然语言驱动的 UI 自动化测试框架** | `jev_ultrafast/framework/` + `cases/` + `tests/` | 用例用自然语言写，pytest 执行，Allure 出报告 |

框架是**外挂的一层**：内核的状态机、新鲜度校验、遮挡校验的**职责边界与语义未变**，
新增能力只扩大「能看见 / 能操作什么」，不放松「怎么校验」。

## 二、改造目标

agent 会「做事」，但测试要的是「**可复现、可断言、可追溯**」。改造全部围绕这三件事：

- **用例是测试人员写的自然语言**，不是工程师维护的选择器脚本——页面改版不该让用例全红；
- **失败要能自证**：报告里得有概率、请求体与响应、真实操作坐标、录屏，而不是一句「失败了」；
- **终端判定必须确定**：语义性预期由模型给证据，但**阈值比较与通过判定留在代码里**；
- **不为跑通用例而放松安全校验**：模型输出永远不会变成选择器、坐标、shell 命令或可执行 JavaScript。

## 三、为 UI 自动化测试加的特色

### 用例层

- **一条 YAML 一条用例**：`id / name / url / login / goal / expect`，`goal` 就是自然语言；
- **三个 skill 直接干活**：[`/nl-case-author`](../.claude/skills/nl-case-author/SKILL.md) 把
  纯文本 / Excel / XMind / Word / CSV 的功能用例转成 YAML；[`/nl-case-run`](../.claude/skills/nl-case-run/SKILL.md)
  按你的描述挑用例、执行、出报告；[`/e9-graph-query`](../.claude/skills/e9-graph-query/SKILL.md)
  查 E9 知识图谱，供前两者定位页面路由与操作链路（本机没有 E9 源码，只能走 MCP）；
- **免登录开跑**：接口登录后把 cookie 注入浏览器，用例里只写 `login: admin`；
- **断言分两级**（可用类型见 [`framework/assertions.py`](../jev_ultrafast/framework/assertions.py)）：

  | 类型 | 判据 |
  |---|---|
  | `ai` | 语义断言：Jev 的 `noul` 给 0–1 的概率，**阈值比较在代码里**（默认 0.75） |
  | `url_contains` / `url_equals` | 终局 URL |
  | `text_contains` / `text_not_contains` | 终局页面文本（可要求出现次数） |
  | `element_exists` / `element_absent` / `element_value` / `element_count` / `element_enabled` | 终局元素表 |

- **只做用例级重跑**（`--reruns` 或 YAML 的 `reruns` 字段）：绝不做步骤级重试——
  浏览器变更操作重试可能重复提交。

### 报告层

- **每一步都是证据**：决策步骤带操作/目标概率、置信度、决策耗时、重发次数与完整请求体响应；
  执行步骤带目标索引、元素节点、角色、执行前值与浏览器返回；超过门槛（默认 200 ms）的等待
  独立成步骤，带真实区间与轮询次数；
- **执行录屏 + 步骤时间轴**（二开插件，必须在 `allure generate` **之后**装）：
  **点某一步，视频跳到那一刻并停住**；画面上的大号光标与圆形放大镜是按执行步骤记下的
  **真实操作坐标**画上去的——录屏里本来就没有系统光标（CDP 截的是渲染器合成结果）。
  录屏是**稀疏幻灯片**（只在页面重绘时出帧），所以等待模型决策的几秒画面是不动的，不影响定位；
- **三个开关**，优先级 `pytest 参数 > 环境变量 JEV_NL_* > config.json > 内置默认`：

  | 开关 | 取值 | 默认 | pytest 参数 | 环境变量 |
  |---|---|---|---|---|
  | 录屏档位 | `0` 不记录 / `1` 记录全部 / `-1` 仅保留失败用例 | `1` | `--video-record=0\|1\|-1` | `JEV_NL_VIDEO_RECORD` |
  | 页面稳定等待 | true / false | `true` | `--wait-stable` / `--no-wait-stable` | `JEV_NL_WAIT_STABLE` |
  | 等待成步骤门槛 | 毫秒 | `200` | `--wait-step-ms=200` | `JEV_NL_WAIT_STEP_MS` |

  生效值会写进报告的「执行摘要」附件，能事后核对。

> ⚠️ `--no-wait-stable` **省不了钱，可能更贵**：它省下的只是免费的页面观察，却可能让
> 「渲染中途决策 → 页面过期 → 重决策」变多，而**重决策是付费的**。定位是调试，不是回归省钱。

### 内核侧：为真实站点补的通用扩展

这些扩展**不针对任何特定站点**，实测来源是 E9 这类重前端系统：

- **弹窗新标签页**：点击 `target="_blank"` 打开的新标签页会被自动跟随，并排除会被立即关闭的临时空白页；
- **同源 iframe 内的富文本编辑区**（如 CKEditor）：会被发现并作为可填写目标暴露；
- **无 `href` 的锚点**（`<a title="…">`）：纳入元素发现范围；
- **屏幕外的字段**：执行前自动滚动到视野内再输入；
- **登录态注入**：见上文「免登录开跑」。

## 四、框架怎么工作

一条 YAML 用例 → 一个 pytest 用例 → 一个 Allure story。

**内核怎么决策**：每次观察产出一张带索引的元素表，一次 TypeSafe 请求同时问出「操作」与各操作
候选的「目标」（推测式分支），只消费匹配的那一个，然后交给浏览器执行。

```text
page → 元素表 → 一次请求 → { CLICK + click_target, TYPE_TEXT + type_text_target, … }
                                 ↓ 只执行被选中操作的那一个分支
```

**断言口径**：执行期用 Jev 的 `choice` 概率做连续决策；终局是 pytest 的确定性 `assert`——
语义性预期由 `noul` 提供 0–1 的证据，**阈值比较与判定留在代码里，不允许「问模型通过了吗」**。

**内核为什么快**（机制见 [设计说明](design.md)，实测见 [效率实测](performance.md)）：

- **一个决策周期一次网络往返**，操作与目标分支共享同一份观测状态；
- **默认循环里没有截图**：模型只消费结构化状态，截图只在 inspector 里开；
- **执行前重新校验**：重新解析几何、拒绝被遮挡的控件，动画不会触发新的预测；
- **只发可见文本**：屏幕外的正文与页脚不塞进模型上下文。

### 边界

**MVP 之外**：Shadow DOM、canvas、上传、嵌套滚动、任意键盘控件，以及**跨源** iframe。
`DONE` 只是一个选择，仍需独立断言校验真实结果。

**已补上**：见 [三 · 内核侧](#内核侧为真实站点补的通用扩展)。被拥有的标签页共用现有的 Chrome profile。

### 源码导读

| 文件 | 行数 | 职责 |
| --- | ---: | --- |
| [agent.py](../jev_ultrafast/agent.py) | 268 | 完整的主循环与文本辅助交接 |
| [snapshot.js](../jev_ultrafast/snapshot.js) | 435 | 原子 DOM 快照、索引化控件、新鲜度守卫 |
| [browser.py](../jev_ultrafast/browser.py) | 489 | 浏览器连接、当前几何、执行、登录态注入 |
| [model.py](../jev_ultrafast/model.py) | 372 | 动态的操作/目标分支与文本生成 |
| [questions.py](../jev_ultrafast/questions.py) | 39 | 给模型的指令文本 |
| [demo.py](../jev_ultrafast/demo.py) | 145 | 本地 inspector（`uv run jev` → http://127.0.0.1:8766） |
| [framework/](../jev_ultrafast/framework/) | 2654 | 配置、用例加载、断言判定、Allure 报告、E9 登录接入 |

想直接调内核（不用框架）：

```python
from jev_ultrafast import Agent

with Agent(
    "https://www.google.com/travel/flights?hl=en",
    "Find one-way flights from Zurich to London on September 20, 2026, "
    "for one adult in economy. Stop when matching flight options are visible.",
) as agent:
    for state in agent.run():
        print(state["elapsed_ms"], state["status"])
```

想跑一条真实站点的演示，用现在已有的两条路：

```bash
# ① 内核侧：起 inspector，在页面上选「Google Flights · real web」场景后 Start demo
uv run jev                       # http://127.0.0.1:8766

# ② 框架侧：公开网站上跑的一条完整演示用例（不需要 E9 环境）
uv run --env-file .env pytest tests/test_nl_cases.py --nl --case demo-weaver-site-tour
```

两条都只**搜索/浏览**，不会下单、不会改数据。

## 五、开发与校验

```bash
uv run ruff check .
uv run pytest                                   # 默认离线：143 passed，6 skipped
node --check jev_ultrafast/static/app.js
node --check jev_ultrafast/snapshot.js
uv build
```

**测试默认离线**，不调付费 API；自然语言用例要靠 `--nl` 显式请求才会跑。另外两条按需执行：

```bash
# 在本地浏览器里校验真实控件的新鲜度/遮挡/执行路径，不调用模型
uv run python scripts/check_guards.py           # 期望 PASS: 26 browser guard checks

# 二开插件的版本校验（不装）
uv run python scripts/install_report_plugin.py report/allure-report --check-only
```

`tests/test_report_params.py`、`tests/test_wait_steps.py`、`tests/test_report_plugin.py`
里有一批**反向断言**（「这么写会失效」）——它们钉住的是 allure 的内部行为与落盘过滤规则，
是二开的地基，升级 allure 时先看它们。工程约定（分层边界、断言口径、重跑策略、凭据边界）
见 [AGENTS.md](../AGENTS.md)。

## 六、相关文档

| 文档 | 内容 |
|---|---|
| [效率实测](performance.md) | 决策耗时分布、与旧方案的口径对比、复现命令 |
| [设计说明](design.md) | 内核机制：动态「操作 + 目标」、快照与身份、新鲜度与遮挡校验、等待策略 |
| [AGENTS.md](../AGENTS.md) | 工程约定：分层边界、断言口径、重跑策略、凭据与仓库边界 |
| [`examples/allure-report/`](../examples/allure-report/) | 真实项目演示报告（可逐步核对） |