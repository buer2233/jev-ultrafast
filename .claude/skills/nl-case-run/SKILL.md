---
name: nl-case-run
description: 执行本项目的自然语言 UI 用例并产出 Allure 测试报告。只要用户说"跑一下用例"、"执行 cases/e9 下的用例"、"测试一下新建流程"、"回归一遍"、"出测试报告"、"生成 allure 报告"、"这条用例跑得过吗"，就用本 skill。用户若只描述想测什么功能，本 skill 负责从现有用例里挑出对应的那条；如果发现该功能还没写成用例，先转 nl-case-author 把用户给的功能用例转成 YAML，再回来执行。
---

# 执行自然语言用例并出 Allure 报告

一条 YAML 用例 → 一个 pytest 用例 → 一个 Allure story。
执行会**调用付费模型 API**，E9 用例还会**在真实环境里产生真实数据**（提交流程等）。
这不是演练，动手前先跟用户对齐。

## 一、先确定"跑什么"

用户可能给的是文件、目录、用例 id，或者只是一句"测一下 XX 功能"。把意图落到具体用例 id 上：

```bash
# 列出当前所有用例（id | 名称 | 来自哪个文件 | 是否缺变量）
uv run python -c "
from jev_ultrafast.framework.loader import load_cases
for c in load_cases():
    print(c['id'], '|', c['name'], '|', c.get('__source__'), '|', c.get('_skip') or 'OK', '|', ','.join(c.get('tags') or []))
"
```

| 用户说的 | 怎么跑 |
|---|---|
| 指定了用例 id | `--case <id>`。写错会以明确报错中止并列出可用 id，不会静默跑成别的 |
| 指定了文件/目录 | `--cases-dir <目录>`（注意：它**替换**默认的 `cases/`，不是追加） |
| 描述了功能（"测新建流程"） | 按 `name` / `tags` / `feature` 挑出匹配的 id，**把选中的清单念给用户确认**再跑 |
| 说"全部/回归" | 不加 `--case`，跑 `cases/` 全部 |

`cases/e9/` 是需要 E9 环境与登录的用例；`cases/demo/` 是公开网站上的演示用例，不需要 config.json。

### 如果这个功能还没有用例

不要硬凑一条。向用户要**功能测试用例原文**（纯文本、Excel、XMind 都行），
然后转 [nl-case-author](../nl-case-author/SKILL.md) 把它转成 YAML，再回到本 skill 执行。

向用户要的时候说清楚要什么：操作步骤 + 预期结果 + 起始页面 + 用哪个账号。
缺了这些就只能靠猜，猜出来的断言是编的。

## 二、前置检查（不做这步会得到误导性的报错）

```bash
# 1) Chrome 调试端点：NL 用例要接管一个真实标签页
curl -s -m 3 http://127.0.0.1:9222/json/version | head -2
# 没响应就起一个：bash .claude/skills/run-jev/scripts/browser.sh

# 2) 模型密钥
test -f .env && echo ".env 在" || echo "缺 .env：cp .env.example .env 并填 TYPESAFE_API_KEY / TEXT_MODEL_API_KEY"

# 3) 跑 E9 用例还需要环境与账号
test -f config.json && echo "config.json 在" || echo "缺 config.json：cp config.example.json config.json 并填 base_url 与账号"
```

`config.json` 缺失时，依赖 `{{ base_url }}` 的 E9 用例会以明确原因 skip（不会报错，也不会假装通过）。

> **Chrome 报 `DevToolsActivePort not found`** 时，真正的原因通常是**没有可连的 Chrome**，
> 不是"权限没开"。先看端点，别先看权限。细节见 [run-jev](../run-jev/SKILL.md)。

## 三、执行

```bash
# 单条
uv run --env-file .env pytest tests/test_nl_cases.py \
  --nl --case <用例id> --reruns 1 --alluredir=report/allure-results

# 一整个目录
uv run --env-file .env pytest tests/test_nl_cases.py \
  --nl --cases-dir cases/e9 --reruns 1 --alluredir=report/allure-results
```

每个参数都不能省：

| 参数 | 作用 | 省了会怎样 |
|---|---|---|
| `--env-file .env` | 注入两个模型密钥 | 第一次决策就鉴权失败 |
| `--nl` | 解除 NL 用例的默认跳过 | 直接 skipped，什么都没跑 |
| `--reruns 1` | 用例级重跑 | 撞上偶发失败直接红 |
| `--alluredir=report/allure-results` | 写 Allure 原始结果 | 没有报告数据 |

**只做用例级重跑，绝不做步骤级重试**——浏览器变更操作重试可能重复提交、产生垃圾数据。

报告目录会**跨次累积**。要一次干净的运行（结果目录里只有本次），先 `rm -rf report/allure-results`。

## 四、出报告

```bash
rm -rf report/allure-report
allure generate report/allure-results -o report/allure-report --clean
```

报告是静态 HTML，直接给用户路径 `report/allure-report/index.html`；
需要交互浏览就用 `allure open report/allure-report`。

要把报告给**别的电脑**看（`allure open` 与 IDE 内置服务器都只绑 127.0.0.1，别人连不上）：
`uv run python scripts/serve_report.py`——绑 `0.0.0.0:8899` 并打印可分享的 IP 地址；缺省
**跟随最新**（`/` 每次请求都跳到当时最新一份，同事存一个地址即可），传报告目录名则钉住那一份。
打不开先查防火墙（提示与放行命令在脚本输出 / `--help` 里）。

报告里能看到：epic → feature → story 三层分组（story 就是用例的 `name`）、
每个决策周期的 step 及其操作概率、写入的文本、断言证据与截图、以及断言汇总。

> **报告生成了但是空的（总数 0）**：Allure CLI 是 2.13.8，`allure-pytest` 必须 <2.14。
> 2.14+ 会写 `titlePath` 字段，旧 CLI 认不出来就静默丢掉全部结果，还照样打印
> "Report successfully generated"。依赖已经钉在 `allure-pytest==2.13.5`，不要升级。
> 这条有 eval 守着：它断言报告里**真有用例、真有步骤与附件**，不只看命令的退出码。

这套说法都有 eval 逐条守着，见 [evals/](evals/)：

```bash
uv run python .claude/skills/nl-case-run/evals/run.py          # 免费档
uv run python .claude/skills/nl-case-run/evals/run.py --paid   # 含真跑用例（花钱）
```

## 五、读结果并如实汇报

先看 pytest 的汇总行，再看 Allure。汇报时必须包含这几项：

1. **通过 / 失败 / 跳过 各几条**，以及失败的是哪条；
2. **有没有 `RERUN` 标记**。第一次失败、重跑才过的用例，在 pytest 输出里是 `R`。
   这类用例报"通过"是**不准确的**——它在告诉你这条用例不稳定，必须单独说明；
3. **失败用例的断言证据**（Allure 里「断言汇总」和「断言证据」两个附件）。
   断言失败是**真实结果**，不要为了让报告变绿去调阈值或放宽断言；
4. 执行摘要里的**浏览器动作数 / 决策请求数 / 耗时**。

### 已知的不稳定项

E9 那条 `e9-workflow-add-bym` 存在约 **1/3~2/5** 的偶发提交失败：
「提交」点击确实执行了、页面也变了，但 E9 没完成提交，表单页仍开着，于是三条断言都如实报失败。
根因尚未定位，目前靠 `--reruns 1` 兜住。

看到这条用例带 `RERUN` 通过时，**明确告诉用户这是重跑兜住的，次数多了要当问题查**，
不要说成"稳定通过"。

演示用例 `demo-weaver-site-tour` 也有偶发失败（2026-09-24 实测：**同一份代码一次失败一次通过**）。
机制已定位，**不是框架的锅**：模型在某一轮以近似平票选了 `BLOCKED`（两次运行分别 0.48 / 0.49），
丢弃后继续，下一轮点了不同链接，轨迹从此分叉；失败那次在 `TYPE_TEXT` 之后**直接判了 DONE**，
没提交搜索，于是三条断言全灭（URL 停在 `/case/`）。

诊断方法：看失败是**哪一步**开始的。若首次分叉是"点了哪个链接"这类**内容**差异，
就是这条随机性，不是回归——`report/allure-results` 里逐轮比 `Jev 决策概率` 附件即可确认平票。

> 判据别只看"过了/没过"。要区分随机性与回归，**必须比"决策数 − 动作数"**：
> 决策被丢弃、执行被丢弃都会让动作数少于决策数，而这些事件在报告里
> 由 `备注` 参数显式标出（如「决策 BLOCKED 因页面过期被丢弃，未生效」）。
> 每次运行通常有 **2–5 次**这类丢弃，属正常范围。

## 六、不要做的事

- **不要**为了让用例通过而放宽断言、调低阈值、或删掉断言。用例失败是信息，不是障碍。
- **不要**自己重跑失败的用例去"碰运气"直到变绿——那是在污染结论。
- **不要**在用户没明确要求时执行 E9 用例：它会真的创建并提交流程。
- **不要**把失败用例的截图或证据贴到仓库里：`report/` 与 `artifacts/` 都在 gitignore，保持这样。