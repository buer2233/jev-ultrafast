"""nl-case-author 的 evals：真跑读源脚本，再拿项目规则体检产出的 YAML。

两类断言：
  · 【读者契约】skill 说 read_source.py 支持哪些格式、拒绝哪些输入——逐条验。
    全部免费，不调用模型。
  · 【产出契约】skill 产出的 YAML 必须过 loader，并且守住 AGENTS.md 里写明的规则
    （不写内网字面量、不出现 ecid、`reruns` 与"预期成败"一致）。

用法：
    uv run python .claude/skills/nl-case-author/evals/run.py
    uv run python .claude/skills/nl-case-author/evals/run.py --list
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

SKIP = object()  # 三态结果里的"这次没法判定"，见 ev_converted_fixture_is_loadable

# "预期 pytest 会红"的标记：名字或标签里带这些词的，就是自检/对照用例。
# 加标记意味着**要承担"它红了不算回归"的责任**——所以只给真正的对照用例用。
EXPECTED_FAILURE_MARKERS = ("应失败", "expected-failure", "should-fail")

ROOT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent
READER = ROOT / ".claude" / "skills" / "nl-case-author" / "scripts" / "read_source.py"
SCRATCH = ROOT / "artifacts" / "skill-evals"
SOURCES = SCRATCH / "sources"
READER_OUT = ROOT / "artifacts" / "case-source"

sys.path.insert(0, str(ROOT))
from jev_ultrafast.framework.loader import CaseError, load_cases  # noqa: E402

# 读者抽出来的文本里必须出现的东西（fixtures.py 里那份用例的要点）
EXPECTED = ["流程管理", "TC-01", "新建流程并提交", "BYM专用测试", "同意流程", "签字意见"]


def read_source(name, *, with_openpyxl=False):
    """按 skill 文档的用法调用读源脚本，返回 (退出码, stdout, stderr, 产出文本或 None)。

    子进程的输出可能是 cp936（本机区域编码）。父进程按 UTF-8 解码时，
    subprocess 的读取线程会抛 UnicodeDecodeError 并【返回空的 stderr】——
    于是"脚本其实报了错并给了替代方案"看起来像"脚本什么都没说"。
    所以这里既给子进程设 PYTHONIOENCODING，又用 errors="replace" 兜底。
    """
    command = ["uv", "run"]
    if with_openpyxl:
        command += ["--with", "openpyxl"]
    command += ["python", str(READER), str(SOURCES / name)]
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    produced = READER_OUT / (Path(name).stem + ".txt")
    text = produced.read_text(encoding="utf-8") if produced.is_file() else None
    return result.returncode, result.stdout or "", result.stderr or "", text


def _asserts_expected(text, label):
    missing = [needle for needle in EXPECTED if needle not in (text or "")]
    if missing:
        return False, f"{label} 抽出的文本缺少 {missing}"
    return True, f"{label} 抽出 {len(text)} 字符，关键字段齐全"


# --------------------------------------------------------------- 读者契约

def ev_reads_xmind_zen():
    code, _, err, text = read_source("流程管理.xmind")
    if code != 0:
        return False, f"退出码 {code}: {err.strip()[-200:]}"
    ok, detail = _asserts_expected(text, "content.json")
    return (ok and "TC-02" in (text or "")), detail


def ev_reads_xmind8():
    """XMind 8 用的是 content.xml，是另一条解析分支，必须单独验。"""
    code, _, err, text = read_source("流程管理-xmind8.xmind")
    if code != 0:
        return False, f"退出码 {code}: {err.strip()[-200:]}"
    return _asserts_expected(text, "content.xml")


def ev_reads_docx():
    code, _, err, text = read_source("流程管理.docx")
    if code != 0:
        return False, f"退出码 {code}: {err.strip()[-200:]}"
    return _asserts_expected(text, "word/document.xml")


def ev_reads_csv():
    code, _, err, text = read_source("流程管理.csv")
    if code != 0:
        return False, f"退出码 {code}: {err.strip()[-200:]}"
    if " | " not in (text or ""):
        return False, "CSV 没有按 ' | ' 分列"
    return _asserts_expected(text, "csv")


def ev_reads_xlsx_with_openpyxl():
    """xlsx 需要 openpyxl。skill 要求用 uv --with 临时提供，不污染项目依赖。"""
    code, out, err, text = read_source("流程管理.xlsx", with_openpyxl=True)
    if code != 0:
        return False, f"退出码 {code}: {(err or out).strip()[-200:]}"
    if "## 工作表：流程管理" not in (text or ""):
        return False, f"没有抽出工作表标题；实际前 80 字：{(text or '')[:80]!r}"
    return _asserts_expected(text, "xlsx")


def ev_rejects_xls_with_alternative():
    """老 .xls 读不了，必须给出替代方案，而不是崩掉。"""
    code, out, err, _ = read_source("旧格式.xls")
    combined = out + err
    if code == 0:
        return False, "本该拒绝 .xls，却退出码 0"
    if "xlsx" not in combined or "CSV" not in combined:
        return False, f"拒绝信息里没有给出替代方案: {combined.strip()[-150:]!r}"
    return True, "拒绝 .xls 并指明另存为 xlsx 或 CSV"


def ev_rejects_image_without_guessing():
    """截图必须要求人工转写——凭图片猜断言是明令禁止的。"""
    code, out, err, _ = read_source("截图.png")
    combined = out + err
    if code == 0:
        return False, "本该拒绝图片输入，却退出码 0"
    if "手抄" not in combined and "转写" not in combined:
        return False, f"没有要求人工转写: {combined.strip()[-150:]!r}"
    if "不要凭图片猜测" not in combined:
        return False, "没有明确禁止凭图片猜测断言"
    return True, "拒绝图片并要求人工转写，且明确禁止猜测"


def ev_output_is_utf8():
    """产出必须是 UTF-8：这台机器的控制台是 cp936，落盘时最容易在这里回归。"""
    code, _, err, _ = read_source("流程管理.xmind")
    if code != 0:
        return False, f"退出码 {code}: {err.strip()[-200:]}"
    try:
        (READER_OUT / "流程管理.txt").read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        return False, f"产出不是合法 UTF-8: {error}"
    return True, "产出文件按 UTF-8 可解码"


# --------------------------------------------------------------- loader 契约

def _write_case(tmp_name, body):
    path = SCRATCH / "loader-cases" / tmp_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


VALID_CASE = """
version: 1
cases:
  - id: {id}
    name: 样例用例
    url: "{{{{ base_url }}}}/wui/index.html"
    goal: 打开页面并停止。
    expect:
      - type: ai
        claim: 页面上显示的是工作台
"""


def _loads(path):
    """只加载单个文件，避免和 cases/ 里的真实用例撞 id。"""
    from jev_ultrafast.framework.loader import load_file

    return load_file(path, {"base_url": "http://example.test"})


def ev_loader_rejects_unknown_field():
    path = _write_case("unknown-field.yaml", VALID_CASE.format(id="tmp-a").replace(
        "    goal:", "    goals: 写错字段名\n    goal:"))
    try:
        _loads(path)
    except CaseError as error:
        if "goals" not in str(error):
            return False, f"报错没指出是哪个字段: {error}"
        return True, f"字段名拼错被拒: {str(error)[:70]}"
    return False, "字段名拼错却被接受了（会静默失效）"


def ev_loader_rejects_duplicate_id():
    body = (
        VALID_CASE.format(id="tmp-dup")
        + VALID_CASE.format(id="tmp-dup").split("cases:\n")[1]
    )
    path = _write_case("dup-id.yaml", body)
    try:
        _loads(path)
    except CaseError as error:
        if "重复" not in str(error):
            return False, f"报错没指出重复: {error}"
        return True, f"id 重复被拒: {str(error)[:70]}"
    return False, "id 重复却被接受了"


def ev_loader_rejects_bad_expect():
    """断言类型写错必须在加载期报出来，而不是跑起来才发现没校验。"""
    body = VALID_CASE.format(id="tmp-bad").replace("type: ai", "type: ai_semantic")
    path = _write_case("bad-expect.yaml", body)
    try:
        _loads(path)
    except CaseError as error:
        if "ai_semantic" not in str(error):
            return False, f"报错没指出类型名: {error}"
        return True, f"未知断言类型被拒: {str(error)[:70]}"
    return False, "未知断言类型却被接受了"


# --------------------------------------------------------------- 产出契约

INTERNAL = re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+\.\d+\b")


def ev_repo_cases_follow_project_rules():
    """对仓库里真实的用例做体检：这些就是 skill 的产出形态，规则来自 AGENTS.md。

    必须扫【YAML 原文】，不能扫 load_cases() 的返回值：加载期会把 {{ base_url }}
    替换成真实地址，替换后的 url 里当然有内网 IP——那样检查会把"写法正确"
    误判成"写了内网地址"。（这个坑 eval 自己踩过一次，所以特意写下来。）
    """
    problems = []
    sources = sorted((ROOT / "cases").rglob("*.yaml"))
    for path in sources:
        raw = path.read_text(encoding="utf-8")
        if "ecid" in raw:
            problems.append(f"{path.name}: 出现 ecid（含随机数，不能用来定位）")
        for line in raw.splitlines():
            if INTERNAL.search(line):
                problems.append(f"{path.name}: 出现内网地址字面量 -> {line.strip()[:50]}")
    cases = load_cases()
    # `reruns` 按【预期 pytest 成败】定，**不按 `negative` 标签**。
    #
    # 2026-10-10 与用户确认后改的口径。原来写的是"负向对照必须 reruns: 0"，
    # 把两个不同的东西混成了一个词：
    #   · `demo-negative-missing-content`（通用演示 · 负向对照（应失败））——
    #     **预期 pytest 就是红的**，重跑只是白花一次决策 → 必须 0；
    #   · E9 的 TC07/TC08——"负向"说的是**断言方向**（断言页面上没有某样东西），
    #     正常是通过的。给它设 0 的代价实测过：一次 `Model connection failed`
    #     （传输层抖动，与它测的东西无关）就直接红，而重跑一次是安全的、
    #     也不会掩盖真实缺陷（重跑跑的仍是同一套只读判定）。
    for case in cases:
        text = f"{case.get('name', '')} {' '.join(case.get('tags') or [])}"
        expected_failure = any(marker in text for marker in EXPECTED_FAILURE_MARKERS)
        if expected_failure and case.get("reruns") != 0:
            problems.append(
                f"{case['id']}: 预期失败的自检/对照用例必须设 reruns: 0，实际 {case.get('reruns')!r}"
                "（重跑只会白花一次模型调用）"
            )
        elif not expected_failure and case.get("reruns") == 0:
            problems.append(
                f"{case['id']}: 不设 reruns: 0 —— '负向'说的是断言方向，不是'预期 pytest 失败'；"
                "瞬时的传输层/环境抖动与它无关，重跑一次是安全的。只有预期失败的自检用例才设 0。"
            )
    if problems:
        return False, "; ".join(problems)
    return True, (f"{len(sources)} 个文件 / {len(cases)} 条用例：无内网字面量、无 ecid、"
                  f"reruns 与'预期成败'一致（预期失败=0，其余不为 0）")


def ev_converted_fixture_is_loadable():
    """端到端：XMind 源用例 → 由 skill 转出的 YAML 必须能过 loader。

    这一步里的"转换"是【模型行为】，脚本替不了，所以产物要由 skill 真正跑一次
    才会出现。产物不存在时报 SKIP，并说清怎么产出——不假装通过。
    """
    converted = SCRATCH / "converted"
    produced = sorted(converted.glob("*.yaml")) if converted.is_dir() else []
    if not produced:
        return SKIP, (
            f"尚未产出。用 nl-case-author 把 {SOURCES / '流程管理.xmind'} 转成 "
            f"YAML 放到 {converted}/ 后重跑本 eval。"
        )
    try:
        cases = load_cases(converted)
    except CaseError as error:
        return False, f"产出的 YAML 过不了 loader: {error}"
    if not cases:
        return False, f"{converted} 下有 YAML 但一条用例都没加载出来"
    # 规则检查扫【YAML 原文】：load_cases 已经把 {{ base_url }} 替换成真实地址，
    # 拿替换后的值去查内网 IP，会把"写法正确"判成"写了内网地址"。
    problems = []
    for path in produced:
        raw = path.read_text(encoding="utf-8")
        if INTERNAL.search(raw):
            problems.append(f"{path.name}: 出现内网地址字面量")
        if "ecid" in raw:
            problems.append(f"{path.name}: 出现 ecid（含随机数，不能用来定位）")
    for case in cases:
        if not case.get("expect"):
            problems.append(f"{case['id']}: 没有断言")
    if problems:
        return False, "; ".join(problems)
    return True, f"产出 {len(cases)} 条用例，全部可加载且符合项目规则"


SKILL_MD = HERE.parent / "SKILL.md"


def ev_graph_analysis_section_present():
    """「先查图谱」这一步写进流程了——它是这份 skill 能不能产出可用 url 的前提。

    守三件事，缺任何一件这步就会被悄悄跳过：
      · 有独立的章节，且排在「写进哪个文件」**之前**（动手写 YAML 之前就该查完）；
      · 说清本机没有 E9 源码、禁止本地 Grep/Read（不说清，模型会去本地找，必然落空）；
      · 说清图谱里**没有** UI 元素定位（不说清，会拿图谱去替代 snapshot.js 观测）。
    """
    if not SKILL_MD.exists():
        return False, f"缺 {SKILL_MD}"
    text = SKILL_MD.read_text(encoding="utf-8")
    problems = []
    if "先查图谱" not in text:
        problems.append("没有「先查图谱」章节")
    if "e9-graph-query" not in text:
        problems.append("没有点名 e9-graph-query")
    if "本机没有 E9 源码" not in text:
        problems.append("没说清「本机没有 E9 源码」，模型会去本地 Grep")
    if "id / xpath / 选择器" not in text:
        problems.append("没说清图谱查不到 UI 元素定位")
    # 顺序：查图谱那节必须在「写进哪个文件」之前
    if text.find("先查图谱") > text.find("写进哪个文件"):
        problems.append("「先查图谱」排在写文件之后了——那时候 YAML 已经写完了")
    if problems:
        return False, "；".join(problems)
    return True, "「先查图谱」章节在位、顺序正确，本地工具禁令与 UI 定位缺口均已写明"


def ev_graph_handoff_link_resolves():
    """指向 e9-graph-query 的相对链接可达。

    这两份 SKILL 是配套的（一个查、一个写），任一方改名都会让另一边**静默指向
    不存在的路径**——读者照着点会 404，却不会报错。所以互相引用的链接要钉住。
    """
    if not SKILL_MD.exists():
        return False, f"缺 {SKILL_MD}"
    text = SKILL_MD.read_text(encoding="utf-8")
    targets = re.findall(r"\]\((\.\./e9-graph-query/[^)#\s]*)\)", text)
    if not targets:
        return False, "没有指向 e9-graph-query 的相对链接"
    missing = [href for href in targets if not (HERE.parent / href).exists()]
    if missing:
        return False, f"这些指向 e9-graph-query 的链接不可达：{missing}"
    return True, f"{len(targets)} 个指向 e9-graph-query 的链接全部可达"


def ev_setup_names_documented():
    """SKILL 里列的命名前置，与 `e9_setup.SETUPS` 里真实登记的**一一对上**。

    这类"文档列一份清单、代码列另一份"的地方必然漂移：加了配方没写文档，
    写用例的人就不知道有它，只好自己编个名字；文档写了没实现，写出来的用例
    跑到执行期才炸。两边都要能发现。
    """
    if not SKILL_MD.exists():
        return False, f"缺 {SKILL_MD}"
    try:
        from jev_ultrafast.framework.e9_setup import SETUPS
    except ImportError as error:
        return False, f"导入 e9_setup 失败：{error}"

    text = SKILL_MD.read_text(encoding="utf-8")
    undocumented = sorted(name for name in SETUPS if f"`{name}`" not in text)
    if undocumented:
        return False, f"这些前置在代码里登记了、SKILL 里没写：{undocumented}"
    return True, f"{len(SETUPS)} 个命名前置全部在 SKILL 里有说明"


def ev_ai_claim_pitfalls_documented():
    """三类会把 `ai` 分拉低的写法写进 SKILL 了。

    这三条以前只在 AGENTS.md 里，而 **AGENTS.md 是给改框架的人看的、
    SKILL 才是给写用例的人看的**。转换时踩了坑才发现知识在错的地方——
    实测 TC06 第一次跑 noul 0.54、第二次 0.08，两次都是这两类写法的锅。

    断言"这三条在文档里"，是因为它们**没有别的守卫**：
    措辞好坏只能在真跑时量，而真跑要花钱。
    """
    if not SKILL_MD.exists():
        return False, f"缺 {SKILL_MD}"
    text = SKILL_MD.read_text(encoding="utf-8")
    problems = []
    if "页面身份" not in text:
        problems.append("没写「不要断言页面身份」这条")
    if "复合句" not in text:
        problems.append("没写「复合句要拆开」这条")
    if "浮层 tooltip" not in text and "tooltip" not in text:
        problems.append("没写「浮层 tooltip 不进页面文本」这条观测缺口")
    if "0.54" not in text or "0.91" not in text:
        problems.append("没带实测数字（0.54 / 0.91），读者没法判断轻重")
    if problems:
        return False, "；".join(problems)
    return True, "三类拉低 ai 分的写法与实测数字都在文档里"


CASES = [
    ("reads-xmind-zen", "XMind Zen 的 content.json 能抽出话题层级", "free", ev_reads_xmind_zen),
    ("reads-xmind8-xml", "XMind 8 的 content.xml 也能解析", "free", ev_reads_xmind8),
    ("reads-docx", "docx 正文段落能抽出", "free", ev_reads_docx),
    ("reads-csv", "CSV 按列抽出且保留分隔", "free", ev_reads_csv),
    ("reads-xlsx-with-openpyxl", "xlsx 经 uv --with openpyxl 可读", "free", ev_reads_xlsx_with_openpyxl),
    ("rejects-xls-with-alternative", ".xls 被拒并给出替代方案", "free", ev_rejects_xls_with_alternative),
    (
        "rejects-image-without-guessing", "图片输入被拒且要求人工转写",
        "free", ev_rejects_image_without_guessing,
    ),
    ("output-is-utf8", "产出文本按 UTF-8 可解码", "free", ev_output_is_utf8),
    ("loader-rejects-unknown-field", "YAML 字段名拼错在加载期报错", "free", ev_loader_rejects_unknown_field),
    ("loader-rejects-duplicate-id", "id 重复在加载期报错", "free", ev_loader_rejects_duplicate_id),
    ("loader-rejects-bad-expect", "断言类型写错在加载期报错", "free", ev_loader_rejects_bad_expect),
    (
        "repo-cases-follow-project-rules", "仓库用例守住内网/ecid/负向对照三条规则",
        "free", ev_repo_cases_follow_project_rules,
    ),
    (
        "graph-analysis-section-present", "「先查图谱」章节在位且排在动手写之前",
        "free", ev_graph_analysis_section_present,
    ),
    (
        "graph-handoff-link-resolves", "指向 e9-graph-query 的相对链接可达",
        "free", ev_graph_handoff_link_resolves,
    ),
    (
        "setup-names-documented", "命名前置清单与 e9_setup.SETUPS 一一对上",
        "free", ev_setup_names_documented,
    ),
    (
        "ai-claim-pitfalls-documented", "三类拉低 ai 分的写法与实测数字已写进文档",
        "free", ev_ai_claim_pitfalls_documented,
    ),
    (
        "converted-fixture-is-loadable", "模型转出的 YAML 能过 loader（需先跑 skill）",
        "skill", ev_converted_fixture_is_loadable,
    ),
]


def main():
    parser = argparse.ArgumentParser(description="nl-case-author 的 evals")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--only", action="append", metavar="ID")
    args = parser.parse_args()

    if args.list:
        for case_id, what, cost, _ in CASES:
            print(f"  {case_id:<34} [{cost:^5}] {what}")
        return 0

    # 样本用 subprocess 生成，且带上 openpyxl——这正是 skill 文档里给 xlsx 的用法。
    # 在 eval 自己的进程里 import fixtures 是拿不到 openpyxl 的（项目没装），
    # xlsx 就会静默缺失，于是"能读 xlsx"这条断言永远测不到真东西。
    built = subprocess.run(
        ["uv", "run", "--with", "openpyxl", "python", str(HERE / "fixtures.py"), str(SOURCES)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    if built.returncode != 0:
        print(f"样本生成失败：{(built.stderr or built.stdout).strip()[-300:]}")
        return 1
    if not (SOURCES / "流程管理.xlsx").is_file():
        print("样本里没有 xlsx——openpyxl 路径没走通，reads-xlsx 那条会失败")
    print(f"样本已生成 -> {SOURCES}\n")

    selected = [c for c in CASES if not args.only or c[0] in set(args.only)]
    failures, skipped = [], []
    for case_id, what, cost, check in selected:
        try:
            result = check()
        except Exception as error:  # noqa: BLE001
            result = (False, f"{type(error).__name__}: {error}")
        if result[0] is SKIP:
            skipped.append(case_id)
            print(f"  SKIP  {case_id:<34} {result[1]}")
            continue
        ok, detail = result
        print(f"  {'PASS' if ok else 'FAIL'}  {case_id:<34} {detail}")
        if not ok:
            failures.append(case_id)

    counted = len(selected) - len(skipped)
    print(f"\n{counted - len(failures)}/{counted} 通过" + (f"，{len(skipped)} 条跳过" if skipped else ""))
    if failures:
        print(f"失败：{failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())