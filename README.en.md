[简体中文](README.md) · **English**

# jev-ui-test

**A UI test automation framework driven by the Jev decision model: one sentence of plain language per case, run as regressions, and every decision can explain why.**

Cases are written in natural language (Chinese is enough), run by pytest, reported by Allure. What executes them is a browser **agent**, and what makes every decision for it is the **[Jev decision model](https://typesafe.ai)** (TypeSafe System One, the latest version): it **does not generate the sentence "what to do next" — it scores the candidate elements directly**, returning "operation + target" together with **calibrated probabilities**. Hence **no selectors, no per-site scripts, no pre-baked field values**.

It is forked from [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast) (MIT):
upstream is Browser Use's browser-agent kernel, and this repository turns it into a framework for **running tests** — the kernel is not rewritten, only a layer is added around it.

**Speed**: measured **458 ms median per action decision**, with **30 of 32 action decisions under one second** (p90 919 ms);
before the rewrite, driving execution directly with a large model often took 4–5 seconds per operation decision, occasionally 10.

[🎬 Demo video and report](#demo-video-and-report) · [📊 Live report](https://buer2233.github.io/jev-ui-test/) · [Quick start](#quick-start) · [Jev model: introduction & tutorial](docs/jev-model.md) · [Framework & internals](docs/framework.md) · [Measured speed](docs/performance.md)

---

## What it solves

| | The usual way | Here |
|---|---|---|
| **Writing a case** | Selector scripts — one page change turns everything red | Write **natural language** (YAML); the element table is recomputed on every run |
| **Cost per step** | Large model driving directly: 4–5 seconds per operation decision, 10 at the extreme | Jev picks "operation + target" in one request — **458 ms median** |
| **Debugging a failure** | One screenshot only | The report carries probabilities, request and response bodies, **click coordinates**, recording + step timeline |
| **Who declares pass/fail** | The model says "it is done" | **Deterministic pytest assertions**: semantic evidence yields a probability, the threshold stays in code |

> "4–5 s / 10 s" is pre-rewrite experience; this repository holds no machine-readable evidence for it. The 458 ms side comes entirely
> from the [demo report](examples/allure-report/) shipped with the repository, and can be checked step by step. Methodology in [Measured speed](docs/performance.md).

## Why drive it with the Jev decision model instead of letting a large model click directly

"Large model driving directly" means handing a screenshot / the DOM to a general-purpose large model and letting it output the next action — a selector, a coordinate, or one sentence of natural language. That path can work, but every step is a bet that the model did not make something up this time:

| | Large model driving directly | Driven by the Jev decision model (this framework) |
|---|---|---|
| **What the model outputs** | Free-form text or JSON that still has to be parsed into an action | **An index chosen from the candidate elements observed this step** — the choice itself is the action |
| **Can it hit an element that does not exist** | Yes — selectors and coordinates are "written" by the model | **Structurally no**: `choice` must be one of the candidate ids, the probabilities must be normalised, and the chosen one must be the maximum — otherwise the whole response is rejected and re-sent (`validate_choice` in `model.py`) |
| **How long one step takes** | 4–5 seconds, 10 at the extreme | Jev returns "operation + target" in one request — **458 ms median** (p90 919 ms) |
| **How uncertainty is handled** | There is no usable confidence in the text; you either trust it or you do not | Every step returns **calibrated probabilities**, usable as evidence |
| **Who declares pass/fail** | Often the model itself saying "it is done" | The probability is only **evidence**; the threshold and the verdict stay in pytest code (`expect: type: ai`) |
| **How a failure is attributed** | One model output plus one screenshot, to be re-read by hand | The report records candidate count, target index, operation/target probabilities, decision time and retry count, row by row |

**What Jev does that is decisive here** — it turns "read the page → decide the next step" from **generation** into **scoring**:

1. **The action space is closed**: the model can only choose among the candidates snapshotted for this step, and its output can never become a selector, a coordinate or executable JS;
2. **Two decisions in one round trip**: "operation + target" come back in the same request (speculative fan-out), and only the target head belonging to the chosen operation is executed;
3. **The probabilities are programmable**: calibrated probabilities let the framework **keep the verdict in code** — semantic assertions use them as evidence, the threshold is set by a human;
4. **It is auditable**: every step is a structured record of index + probability + elapsed time, so each row can be walked to explain "why it clicked that at that moment".

## Demo video and report

<a href="examples/jev执行真实业务场景的测试报告录屏.mp4"><img src="examples/jev执行真实业务场景的测试报告录屏_60秒.gif" alt="60-second animation: an Allure report produced by a real run — three green cases on the left, the execution recording with its step timeline in the middle (synthetic cursor + 3× magnifier), the natural-language case text and this run's parameters on the right" width="100%" /></a>

**↑ a 60-second animation** (report home → case detail → execution recording and step timeline); **[▶ watch the full recording](examples/jev执行真实业务场景的测试报告录屏.mp4)** (3 min 57 s · 1452×680 · 7 MB).

**[📊 View the full execution report online →](https://buer2233.github.io/jev-ui-test/)** — the execution report of a **real project** (3 green cases · `3 passed`), corresponding to [`cases/e9/workflow_design.yaml`](cases/e9/workflow_design.yaml); the report's source files ship with the repository in [`examples/allure-report/`](examples/allure-report/).

To view this report locally, or to produce one yourself, see step 4 of [Quick start](#quick-start).

## Quick start

**1. Install**

```bash
git clone https://github.com/buer2233/jev-ui-test.git
cd jev-ui-test
uv sync
cp .env.example .env        # fill in TYPESAFE_API_KEY and TEXT_MODEL_API_KEY
```

> Do not have a Jev API key yet? See the **[Jev model: introduction & tutorial](docs/jev-model.md)** — signing up gives you $5 of credit, enough to run the demo cases several times.

Python ≥ 3.12 is required; Chrome is connected through [Browser Harness](https://github.com/browser-use/browser-harness)
(already installed by `uv sync`). When it cannot connect, run `uv run browser-harness --doctor` and allow remote debugging as prompted.

> **When it will not start on Windows**: a `UnicodeDecodeError` / `codec can't decode byte` at import time is the regional
> encoding issue of Chinese Windows (not a dependency problem); a `DevToolsActivePort not found` mostly means
> **there is no Chrome to connect to** (not that permissions are off). Troubleshooting steps are in [`.claude/skills/run-jev/SKILL.md`](.claude/skills/run-jev/SKILL.md).

**2. Configure** (only the E9 environment needs this)

```bash
cp config.example.json config.json    # fill in base_url and the admin / employee1~5 accounts
```

> ⚠️ **`config.json` is ignored by `.gitignore`, and must stay ignored** — this repository is public, so
> real accounts and intranet addresses committed to it are a credential leak. Only the placeholder template `config.example.json` is tracked.

**3. Write a case** (`cases/**/*.yaml`; users write nothing but natural language)

- **Already have functional test cases on hand** → use `/nl-case-author`, which converts the source material into the YAML below;
- **Want to read a runnable one directly** → [`examples/weaver_site_tour.yaml`](examples/weaver_site_tour.yaml),
  runs on a public website and needs neither the E9 environment nor `config.json` (a display copy; the executed source is in `cases/demo/`).

```yaml
cases:
  - id: e9-workflow-add-bym
    name: 新建 BYM专用测试 流程并提交
    url: "{{ base_url }}/wui/index.html#/main/workflow/add"
    login: employee1                 # the API login injects the cookie into the browser, so the case starts logged in
    goal: |
      1. 打开新建流程页面，页面上按分组列出了各种流程名称；
      2. 点击「BYM专用测试」，它会打开该流程的表单页面；
      3. 在「签字意见」里输入「同意流程」；
      4. 点击「提交」。
      提交成功后表单页会自动关闭，回到新建流程列表页；此时停止。
    expect_mode: and                 # how multiple assertions combine: and / or / min_pass, decided per case
    expect:
      - type: ai                     # semantic assertion: Jev's noul returns a probability, the threshold comparison lives in code
        claim: 页面上显示的是新建流程的流程名称列表
      - type: text_not_contains      # deterministic assertion
        value: "流转设定"
      - type: url_contains
        value: "/wui/index.html#/main/workflow/add"
```

Two hard constraints ([AGENTS.md](AGENTS.md) has the full version): **assertions are evaluated only against the 【final page】**;
**the agent has no "back" action**, so changing pages within a site must rely on the navigation on the page.

**4. Run + read the report**

**Read the online one** (the real execution report shipped with the repository, 3 green cases): <https://buer2233.github.io/jev-ui-test/>

**Run it locally and produce a report**:

```bash
uv run pytest                     # offline by default, natural-language cases do not run: 143 passed, 6 skipped

# only an explicit request executes them (they call a paid API and take over one Chrome tab)
uv run --env-file .env pytest tests/test_nl_cases.py --nl --reruns 1 \
  --alluredir=report/allure-results
allure generate report/allure-results -o report/allure-report --clean
uv run python scripts/install_report_plugin.py report/allure-report   # the in-house plugin: recording + step timeline
allure open report/allure-report
```

**Read the one already generated in the repository** (no need to run any case; the allure CLI needs Java):

```bash
uv sync
allure open examples/allure-report
```

Do not double-click `index.html` (the browser blocks local file requests), and do not substitute
`python -m http.server` — it does not support HTTP Range, so the recording's progress bar cannot be dragged.

You can also use `/nl-case-run`: say "test the new-workflow page" or "run the cases under cases/e9", and it picks the cases,
executes them and produces the report; if that feature has not been written as a case yet, it hands over to `/nl-case-author` first.

## More docs

| Document | Contents |
|---|---|
| [Jev model: introduction & tutorial](docs/jev-model.md) | What Jev is, how it differs from a conventional large model, why this project uses it for UI automation; signing up, getting an API key, the free credit, how to check usage |
| [Framework & internals](docs/framework.md) | The boundary of the rewrite, the case-layer / report-layer / kernel-layer features, how the framework works, a source tour, development and checks |
| [Measured speed](docs/performance.md) | Decision-time distribution, how it is compared against the old approach, reproduction commands |
| [Design notes](docs/design.md) | Kernel mechanics: dynamic "operation + target", snapshots and identity, freshness and occlusion checks, waiting strategy |
| [AGENTS.md](AGENTS.md) | Engineering conventions: layer boundaries, assertion policy, retry policy, credentials and repository boundaries |
| [`.claude/skills/`](.claude/skills/) | The four skills: `/nl-case-author`, `/nl-case-run`, `/run-jev`, `/e9-graph-query` |

## Upstream

The browser-agent kernel is maintained upstream; this repository is a fork of [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast).

---

## Links

- [Linux do](https://linux.do/)