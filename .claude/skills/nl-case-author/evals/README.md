# nl-case-author 的 evals

两类断言：**读者契约**（`scripts/read_source.py` 说支持什么、拒绝什么）和
**产出契约**（转出来的 YAML 必须过 loader，并守住 AGENTS.md 里写明的规则）。

```bash
uv run python .claude/skills/nl-case-author/evals/run.py
uv run python .claude/skills/nl-case-author/evals/run.py --list
```

除最后一条外全部免费，不调用模型。

## 样本是现场生成的

`evals/fixtures.py` 把同一份功能用例（取自已实测的 E9 流程）落成 xmind / xmind8 /
docx / csv / txt / xlsx，另加两个"读不了"的输入（`.xls`、`.png`）。
不往仓库塞二进制：zip 格式入不了库，diff 不可读。

xlsx 由 `uv run --with openpyxl python fixtures.py` 生成——这正是 SKILL 里给 xlsx 的用法，
所以生成样本这一步顺带也在验它。

## 断言的是什么

| 用例 | 断言 |
|---|---|
| `reads-xmind-zen` / `reads-xmind8-xml` | 两条解析分支都抽得出话题层级（`content.json` / `content.xml`） |
| `reads-docx` / `reads-csv` / `reads-xlsx-with-openpyxl` | 三种格式都抽得出关键字段 |
| `rejects-xls-with-alternative` | `.xls` 被拒，且指明另存为 xlsx 或 CSV |
| `rejects-image-without-guessing` | 图片被拒，**要求人工转写并明确禁止猜测** |
| `output-is-utf8` | 产出按 UTF-8 可解码（本机控制台是 cp936，最易在这里回归） |
| `loader-rejects-unknown-field` / `-duplicate-id` / `-bad-expect` | 三类写法错误都在**加载期**报出来 |
| `repo-cases-follow-project-rules` | 仓库用例无内网字面量、无 ecid、负向对照 `reruns: 0` |
| `graph-analysis-section-present` | 「先查图谱」章节在位、**排在动手写 YAML 之前**，且写明本机无 E9 源码、图谱查不到 UI 元素定位 |
| `graph-handoff-link-resolves` | 指向 `e9-graph-query` 的相对链接可达（任一方改名不会静默 404） |
| `setup-names-documented` | SKILL 里列的命名前置与 `e9_setup.SETUPS` 一一对上（两份清单不会各说各话） |
| `ai-claim-pitfalls-documented` | 三类拉低 `ai` 分的写法（否定式/页面身份/复合句）与实测数字都写进了文档 |
| `converted-fixture-is-loadable` | 模型转出的 YAML 能过 loader（需先真的跑一次 skill，见下） |

最后一条需要模型的产出，脚本替不了，所以它默认 **SKIP** 并说清怎么产出。两步，**顺序不能反**：

```bash
# 1) 先生成样本：sources/ 下那几个 xmind/csv/docx 是 evals 自己造的合成样本，
#    新克隆的工作区里没有（artifacts/ 是 gitignore 的本机便签），跑一次 evals 就会建出来
uv run python scripts/run_skill_evals.py --skill nl-case-author

# 2) 再让 reader 把样本读成文本；产出写到 artifacts/case-source/（同样是运行期目录）
uv run python .claude/skills/nl-case-author/scripts/read_source.py \
  artifacts/skill-evals/sources/流程管理.xmind
```

## 这套 evals 查出来的真缺陷

它们不是走过场，写的时候连查出三个：

1. **XMind 8 的文件被读成空的，而且不报错。** `content.xml` 带默认命名空间
   （`urn:xmind:xmap:xmlns:content:2.0`），`findall("sheet")` 一条都匹配不到；
   子话题还隔着 `<children><topics>` 两层。已修。
2. **单文件内重复 id 不被检出。** `load_cases` 只查跨文件重复，同一文件里写两条同 id
   会双双加载成功，然后 `--case` 选中两条、Allure 历史混在一起。已在 `load_file` 里补上。
3. **报错信息不是 UTF-8。** 本机控制台是 cp936，脚本的中文报错以 cp936 写进 stderr，
   父进程按 UTF-8 解码时读取线程直接死掉，**返回空 stderr**——
   "脚本拒绝了 .xls 并给了替代方案"看起来像"脚本什么都没说"。已显式切到 UTF-8。

还有一条是 eval 自己踩的：**规则检查必须扫 YAML 原文，不能扫 `load_cases()` 的返回值**。
加载期会把 `{{ base_url }}` 替换成真实地址，拿替换后的 url 查内网 IP，
会把"写法正确"判成"写了内网地址"。这个坑踩了两次。

4. **「先查图谱」这一步会整段消失，而没人会发现。** 它不产出任何东西——
   查了不查，YAML 照样生成、照样过 loader、照样能跑。区别只在 `url` 是对的还是猜的。
   这类"做了没有产出、漏了不留痕迹"的步骤，**只有把它的存在本身写成断言才守得住**，
   所以 `graph-analysis-section-present` 连**章节顺序**一起查：
   排在「写进哪个文件」之后等于没查——那时 YAML 已经写完了。
   `graph-handoff-link-resolves` 守同一类风险的另一半：两份 SKILL 配套，
   任一方改名都会让另一边的引用**静默指向不存在的路径**。

5. **措辞的坑写在 AGENTS.md，而写用例的人只看 SKILL。** 真跑 TC06 时暴露的：
   第一版断言是**复合的页面身份句**（"页面上仍然是这条流程的表单填写界面，
   并且有提示信息说必填项没有填写"）→ noul **0.54**；改成单一事实后 → **0.08**，
   查下去才发现那条必填提示是**浮层 tooltip，压根没进页面可见文本**
   （截图里有，页面文本里一个字都没有）。最后改成断言看得见的东西 → **0.91**。
   三次测量说明两件事：
   **①「不要断言页面身份」这条规律以前只写在 AGENTS.md 里**——那是给改框架的人看的，
   写用例的人不会读到，所以坑照踩。现已补进 SKILL 并加本条断言守着。
   **②"调措辞调不上去"有两种成因**：句子不好，或者**要断的东西根本不可见**。
   分不开就会一直改句子（改到天亮也没用）。SKILL 里补了判别方法。