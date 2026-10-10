**简体中文** · [English](README.en.md)

# jev-ui-test

**Jev 决策模型驱动的 UI 自动化测试框架：一句自然语言写用例、跑成回归，每一步决策都说得清为什么。**

用例用自然语言写（中文即可），pytest 执行，Allure 出报告。执行它的是浏览器 **agent**，而替它做每一步决定的
是 **[Jev 决策模型](https://typesafe.ai)**（TypeSafe System One，当前最新的一版）：它**不生成「下一步该做什么」
这句话，而是在候选元素里直接打分**，把「操作 + 目标」和**带校准的概率**一起返回。所以**没有选择器、
没有站点脚本、没有预置的字段值**。

它 fork 自 [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast)（MIT）：
上游是 Browser Use 的浏览器 agent 内核，本仓库把它改造成**跑测试**的框架——不重写内核，只在外围加一层。

**效率**：实测**每个操作决策中位 458 ms**，32 次操作决策里 **30 次在 1 秒以内**（p90 919 ms）；
而改造前用大模型直接驱动执行时，单个操作判断经常要 4–5 秒、个别到 10 秒。

[🎬 演示视频与报告](#演示视频与报告) · [📊 在线报告](https://buer2233.github.io/jev-ui-test/) · [快速开始](#快速开始) · [Jev 模型介绍和使用教程](docs/jev-model.md) · [框架与改造详解](docs/framework.md) · [效率实测](docs/performance.md)

---

## 它解决什么问题

| | 传统做法 | 这里 |
|---|---|---|
| **用例怎么写** | 选择器脚本，页面一改就全红 | 写**自然语言**（YAML），元素表每次重算 |
| **一步多慢** | 大模型直接驱动：单个操作判断 4–5 秒、个别 10 秒 | Jev 一次请求同时选出「操作 + 目标」，**中位 458 ms** |
| **失败怎么查** | 只有一张截图 | 报告带概率、请求体与响应、**操作坐标**、录屏 + 步骤时间轴 |
| **谁判定通过** | 模型说「完成了」 | **pytest 确定性断言**：语义证据给概率，阈值留在代码里 |

> 「4–5 秒 / 10 秒」是改造前的使用经验值，本仓库没有机器可读证据；458 ms 这一侧全部来自随仓库
> 分发的[演示报告](examples/allure-report/)，可逐步核对。口径见[效率实测](docs/performance.md)。

## 为什么用 Jev 决策模型驱动，而不是直接让大模型点

「大模型直接驱动」是把截图 / DOM 丢给一个通用大模型，让它输出下一步动作——选择器、坐标或一句自然语言。
这条路能走通，但每一步都在赌模型这次没编错：

| | 大模型直接驱动 | Jev 决策模型驱动（本框架） |
|---|---|---|
| **模型输出什么** | 一段自由文本或 JSON，还要再解析成动作 | 在**本次观察到的候选元素里选索引**，选出来就是动作 |
| **会不会点到不存在的元素** | 会——选择器、坐标都是模型「写」出来的 | **结构上不能**：`choice` 必须落在候选 id 里、概率须归一、被选中的那条必须是最大值，否则整条响应被拒收并重发（`model.py` 的 `validate_choice`） |
| **一步多久** | 4–5 秒，个别 10 秒 | Jev 一次请求同时给出「操作 + 目标」，**中位 458 ms**（p90 919 ms） |
| **不确定性怎么处理** | 文字里没有可用的置信度，只能「信」或「不信」 | 每步返回**带校准的概率**，可以当证据用 |
| **谁判定通过** | 常常是模型自己说「完成了」 | 概率只是**证据**，阈值与判定留在 pytest 代码里（`expect: type: ai`） |
| **失败怎么归因** | 一段模型输出 + 一张截图，得自己重看 | 报告逐行记候选数、目标索引、操作/目标概率、决策耗时、重发次数 |

**Jev 在其中起的关键作用**——它把「看页面 → 决定下一步」从**生成**换成了**打分**：

1. **动作空间是闭合的**：模型只能在当次快照出的候选里选，模型输出永远不会变成选择器、坐标或可执行 JS；
2. **一次往返出两个决策**：「操作 + 目标」在同一请求里返回（推测式 fan-out），只有被选中操作对应的那个目标头会被执行；
3. **概率可编程**：带校准的概率让框架能把**判定权留在代码里**——语义断言拿它当证据，阈值由人定；
4. **可审计**：每一步都是索引 + 概率 + 耗时的结构化记录，能逐行走查「它当时为什么这么点」。

## 演示视频与报告

<a href="examples/jev执行真实业务场景的测试报告录屏.mp4"><img src="examples/jev执行真实业务场景的测试报告录屏_60秒.gif" alt="60 秒动图：真实执行产出的 Allure 报告——左侧三条用例全绿，中间是执行录屏与步骤时间轴（合成光标 + 3× 放大镜），右侧是自然语言用例原文与本次运行参数" width="100%" /></a>

**↑ 60 秒动图**（报告首页 → 用例详情 → 执行录屏与步骤时间轴）；**[▶ 观看完整录屏](examples/jev执行真实业务场景的测试报告录屏.mp4)**（3 分 57 秒 · 1452×680 · 7 MB）。

**[📊 在线查看完整执行报告 →](https://buer2233.github.io/jev-ui-test/)** —— 一次**真实项目**的执行报告
（3 条用例全绿 · `3 passed`），对应 [`cases/e9/workflow_design.yaml`](cases/e9/workflow_design.yaml)；
报告源文件随仓库分发在 [`examples/allure-report/`](examples/allure-report/)。

怎么在本地看这份报告、怎么自己产出一份，见[快速开始](#快速开始)第 4 步。

## 快速开始

**1. 装**

```bash
git clone https://github.com/buer2233/jev-ui-test.git
cd jev-ui-test
uv sync
cp .env.example .env        # 填入 TYPESAFE_API_KEY 与 TEXT_MODEL_API_KEY
```

> 还没有 Jev 的 API Key？见 **[Jev 模型介绍和使用教程](docs/jev-model.md)**——注册就送 5 美元额度，够把演示用例跑好几遍。

需要 Python ≥ 3.12，Chrome 通过 [Browser Harness](https://github.com/browser-use/browser-harness)
连接（`uv sync` 已装）。连不上时跑 `uv run browser-harness --doctor` 并按提示允许远程调试。

> **Windows 上起不来时**：导入期报 `UnicodeDecodeError` / `codec can't decode byte`，是中文
> Windows 的区域编码问题（不是依赖问题）；报 `DevToolsActivePort not found`，多半是
> **没有可连接的 Chrome**（不是权限没开）。排查步骤见 [`.claude/skills/run-jev/SKILL.md`](.claude/skills/run-jev/SKILL.md)。

**2. 配**（只有 E9 环境才需要）

```bash
cp config.example.json config.json    # 填 base_url 与 admin / employee1~5 账号
```

> ⚠️ **`config.json` 已被 `.gitignore` 忽略，且必须保持忽略**——本仓库是公开仓库，
> 真实账号与内网地址提交上去就是凭据泄漏。入库的只有占位模板 `config.example.json`。

**3. 写用例**（`cases/**/*.yaml`，用户只写自然语言）

- **手上已有功能测试用例** → 用 `/nl-case-author`，它把源材料转成下面的 YAML；
- **想直接看一条能跑的** → [`examples/weaver_site_tour.yaml`](examples/weaver_site_tour.yaml)，
  公开网站上跑、不需要 E9 环境也不需要 `config.json`（展示副本，执行源在 `cases/demo/`）。

```yaml
cases:
  - id: e9-workflow-add-bym
    name: 新建 BYM专用测试 流程并提交
    url: "{{ base_url }}/wui/index.html#/main/workflow/add"
    login: employee1                 # 接口登录后把 cookie 注入浏览器，用例免登录开跑
    goal: |
      1. 打开新建流程页面，页面上按分组列出了各种流程名称；
      2. 点击「BYM专用测试」，它会打开该流程的表单页面；
      3. 在「签字意见」里输入「同意流程」；
      4. 点击「提交」。
      提交成功后表单页会自动关闭，回到新建流程列表页；此时停止。
    expect_mode: and                 # 多断言合成口径：and / or / min_pass，逐用例决定
    expect:
      - type: ai                     # 语义断言：Jev 的 noul 给概率，阈值比较在代码里
        claim: 页面上显示的是新建流程的流程名称列表
      - type: text_not_contains      # 确定性断言
        value: "流转设定"
      - type: url_contains
        value: "/wui/index.html#/main/workflow/add"
```

两条硬约束（[AGENTS.md](AGENTS.md) 有完整版）：**断言只对【终局页面】求值**；
**agent 没有「后退」动作**，站点内换页要靠页面上的导航。

**4. 跑 + 看报告**

**看线上那份**（随仓库分发的真实执行报告，3 条用例全绿）：<https://buer2233.github.io/jev-ui-test/>

**本地跑一遍并出报告**：

```bash
uv run pytest                     # 默认离线，自然语言用例不会跑：143 passed，6 skipped

# 显式请求才会执行（会调付费 API 并接管一个 Chrome 标签页）
uv run --env-file .env pytest tests/test_nl_cases.py --nl --reruns 1 \
  --alluredir=report/allure-results
allure generate report/allure-results -o report/allure-report --clean
uv run python scripts/install_report_plugin.py report/allure-report   # 二开插件：录屏 + 步骤时间轴
allure open report/allure-report
```

**看仓库里那份已生成的**（不用跑用例，allure CLI 需要 Java）：

```bash
uv sync
allure open examples/allure-report
```

别直接双击 `index.html`（浏览器会拦本地文件请求），也别用 `python -m http.server` 代替
——它不支持 HTTP Range，录屏进度条拖不动。

也可以用 `/nl-case-run`：你说「测一下新建流程」或「跑 cases/e9 下的用例」，它负责挑用例、
执行、出报告；如果该功能还没写成用例，会先转 `/nl-case-author`。

## 更多文档

| 文档 | 内容 |
|---|---|
| [Jev 模型介绍和使用教程](docs/jev-model.md) | Jev 是什么、与常规大模型的区别、为什么用它做 UI 自动化；注册、拿 API Key、免费额度、怎么查用量 |
| [框架与改造详解](docs/framework.md) | 改造边界、用例层 / 报告层 / 内核侧特色、框架怎么工作、源码导读、开发与校验 |
| [效率实测](docs/performance.md) | 决策耗时分布、与旧方案的口径对比、复现命令 |
| [设计说明](docs/design.md) | 内核机制：动态「操作 + 目标」、快照与身份、新鲜度与遮挡校验、等待策略 |
| [AGENTS.md](AGENTS.md) | 工程约定：分层边界、断言口径、重跑策略、凭据与仓库边界 |
| [`.claude/skills/`](.claude/skills/) | `/nl-case-author`、`/nl-case-run`、`/run-jev`、`/e9-graph-query` 四个 skill 的说明 |

## 上游项目

浏览器 agent 内核由上游维护，本仓库是 [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast) 的 fork。

---

## 友情链接

- [Linux do](https://linux.do/)