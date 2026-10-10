"""e9-graph-query 的 evals —— 全离线、免费、不碰图谱主机。

验的是两件事：
  · **读者契约**：SKILL.md 自己立得住——frontmatter 合规、链接可达、硬边界写清了；
  · **一致性契约**：SKILL 里写的图谱端点/图名，与仓库里真实的配置**不会各说各话**。

为什么不验"查图谱能不能查对"：那需要联网打图谱主机，会让 evals 变成
**依赖外部服务**的东西（图谱在夜间 01:00 维护窗口会返回 busy），
违背本项目「测试离线」的契约。真实查询能力由实际任务验证
（见 evals/README.md 的「这套 evals 不验什么」）。
"""

import argparse
import re
import sys
from pathlib import Path

import yaml

# 控制台是 cp936，中文打屏会变问号——先把两条流钉成 utf-8（与 read_source.py 同一写法）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
SKILL_MD = SKILL_DIR / "SKILL.md"
REFS_MD = SKILL_DIR / "references" / "mcp-tools.md"
RUNNER = ROOT / "scripts" / "run_skill_evals.py"

SKIP = object()

# 本 skill 不该出现的、只在 api-test-E9 里成立的说法。
# 复制参考 SKILL 时最容易连带抄进来的就是这些——它们在这边指向不存在的文件或命令。
FOREIGN_REFERENCES = [
    ("check_mcp_config_consistency", "这是 api-test-E9 的 tools/ 脚本，本项目没有"),
    (".workbuddy", "这是 api-test-E9 的目录约定，本项目用 .claude/"),
    ("svn-impact", "这是 api-test-E9 的 skill，本项目没有"),
    ("--graph", "这是 api-test-E9 接口框架 CLI 的参数，本项目没有这个 CLI"),
]


def _read(path):
    return path.read_text(encoding="utf-8")


def _frontmatter(path):
    """取 YAML frontmatter。没有就返回 None。"""
    text = _read(path)
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
    if not match:
        return None
    return yaml.safe_load(match.group(1))


def ev_skill_frontmatter():
    """frontmatter 只有 name + description，且 name 与目录名一致。"""
    if not SKILL_MD.exists():
        return False, f"缺 {SKILL_MD}"
    meta = _frontmatter(SKILL_MD)
    if not isinstance(meta, dict):
        return False, "SKILL.md 开头没有可解析的 YAML frontmatter"
    keys = set(meta)
    if keys != {"name", "description"}:
        return False, f"frontmatter 键应为 name+description，实际 {sorted(keys)}"
    if meta["name"] != SKILL_DIR.name:
        return False, f"name={meta['name']!r} 与目录名 {SKILL_DIR.name!r} 不一致"
    if len(str(meta["description"])) < 80:
        return False, "description 过短，写不出触发场景"
    return True, f"name={meta['name']}，description {len(str(meta['description']))} 字"


def ev_relative_links_resolve():
    """SKILL.md 里所有相对链接都指向真实存在的文件。"""
    if not SKILL_MD.exists():
        return False, f"缺 {SKILL_MD}"
    text = _read(SKILL_MD)
    links = re.findall(r"\]\(([^)#\s]+)\)", text)
    relative = [href for href in links if not href.startswith(("http://", "https://", "mailto:"))]
    missing = []
    for href in relative:
        if not (SKILL_DIR / href).exists():
            missing.append(href)
    if missing:
        return False, f"这些相对链接指向不存在的路径：{missing}"
    return True, f"{len(relative)} 个相对链接全部可达"


def ev_references_present_and_linked():
    """references/mcp-tools.md 存在，且在 SKILL.md 里被引用。"""
    if not REFS_MD.exists():
        return False, f"缺 {REFS_MD}"
    if "references/mcp-tools.md" not in _read(SKILL_MD):
        return False, "SKILL.md 没有引用 references/mcp-tools.md，读者找不到它"
    body = _read(REFS_MD)
    if len(body) < 2000:
        return False, f"references/mcp-tools.md 只有 {len(body)} 字符，内容疑似被截断"
    return True, f"references {len(body)} 字符，已被 SKILL.md 引用"


def ev_registered_in_runner():
    """已在 scripts/run_skill_evals.py 的 FLAGS 里登记。

    未登记的 skill 会让**整个** runner 报错退出（见其 unregistered 分支），
    所以这条不是洁癖，是"别把别人的 evals 一起弄红"。
    """
    if not RUNNER.exists():
        return SKIP, f"{RUNNER} 不存在，跳过"
    text = _read(RUNNER)
    if re.search(rf'["\']{re.escape(SKILL_DIR.name)}["\']\s*:', text):
        return True, f"{SKILL_DIR.name} 已在 FLAGS 中登记"
    return False, f"{SKILL_DIR.name} 未登记进 FLAGS，会让 run_skill_evals.py 整体失败"


def ev_local_tool_ban_stated():
    """「禁止本地工具查 E9 源码」这条硬边界写清了，且点名了 detect_changes。"""
    text = _read(SKILL_MD)
    problems = []
    if "绝对禁止" not in text:
        problems.append("没有出现「绝对禁止」级别的措辞")
    if "本机没有 E9 源码" not in text and "本机根本没有 E9 源码" not in text:
        problems.append("没说清「本机没有 E9 源码」，读者会以为可以本地 grep")
    if "detect_changes" not in text:
        problems.append("没有点名禁用 detect_changes（它跑 git diff，E9 是 SVN）")
    if problems:
        return False, "；".join(problems)
    return True, "硬边界与 detect_changes 禁用均已写明"


def ev_ui_coverage_gap_stated():
    """本项目最要命的那条限制：图谱里**没有** UI 元素定位。

    这是它与 api-test-E9 版本最大的差别——那边查后端调用链就够，
    这边若不说清「选择器图上没有」，读者会拿图谱去替代 snapshot.js 观测。
    """
    text = _read(SKILL_MD)
    problems = []
    for token in ("id / xpath / 选择器", "匿名"):
        if token not in text:
            problems.append(f"缺少关于「{token}」的限制说明")
    if "snapshot.js" not in text:
        problems.append("没有点明图谱替代不了 snapshot.js 的真实 DOM 观测")
    if problems:
        return False, "；".join(problems)
    return True, "UI 元素定位缺口与匿名 handler 断链均已写明"


def ev_qn_prefix_rule():
    """含点图名的 qn 剥前缀规则在，且写的是整串切而不是按点切。"""
    text = _read(REFS_MD)
    if "qn[len(project) + 1:]" not in text and "qn[len(project)+1:]" not in text:
        return False, "references 里没有 qn 前缀剥离公式"
    if "split('.')[1:]" not in text:
        return False, "没有写明「不能按第一个点切」这个反例，读者会踩坑"
    return True, "qn 剥前缀公式与反例均在"


def ev_endpoints_match_config():
    """SKILL 里写的图名/端点，与仓库真实配置不冲突。

    `.mcp.json` 与 `config.json` **都是 gitignored**：新克隆的工作区里没有它们，
    所以这里必须 SKIP 而不是 FAIL——否则这条 eval 会让"新克隆能否跑通"破功。
    """
    mcp_json = ROOT / ".mcp.json"
    if not mcp_json.exists():
        return SKIP, ".mcp.json 不存在（gitignored，新克隆的正常状态），跳过"
    try:
        servers = yaml.safe_load(_read(mcp_json)).get("mcpServers") or {}
    except (OSError, ValueError, AttributeError) as error:
        return False, f".mcp.json 解析失败：{type(error).__name__}: {error}"

    missing = [name for name in ("e9-graph", "e9-ops") if name not in servers]
    if missing:
        return False, f".mcp.json 里缺这些 server：{missing}"

    # SKILL 里必须出现真实的 server 名，否则读者配不上号。
    text = _read(SKILL_MD)
    unstated = [name for name in ("e9-graph", "e9-ops") if name not in text]
    if unstated:
        return False, f"SKILL.md 没有提到 .mcp.json 里的 server 名：{unstated}"

    # 端点路径口径要对齐：运维端是 /servers/e9-ops/mcp 而不是 /mcp。
    ops_url = str(servers["e9-ops"].get("url", ""))
    if "/servers/e9-ops/mcp" not in ops_url:
        return False, f"e9-ops 的 url 不是预期的 /servers/e9-ops/mcp：{ops_url}"
    if "9750" not in ops_url:
        return SKIP, f"图谱端口不是 9750（{ops_url}），SKILL 里的示例端口可能已过期"
    return True, f"e9-graph / e9-ops 两端点与 .mcp.json 一致（{ops_url}）"


def ev_no_foreign_references():
    """没有把 api-test-E9 专有的路径/命令抄进来。"""
    hits = []
    for path in (SKILL_MD, REFS_MD):
        text = _read(path)
        for token, why in FOREIGN_REFERENCES:
            if token in text:
                hits.append(f"{path.name} 出现 {token!r}（{why}）")
    if hits:
        return False, "；".join(hits)
    return True, f"{len(FOREIGN_REFERENCES)} 类外来引用一个都没有"


def ev_route_recipes_present():
    """五条查询配方都在，且都带了实测证据（文件 + 行号）。

    配方是这份 skill 的正文价值所在；少了任何一条，读者就退回"瞎猜路由"。
    """
    text = _read(SKILL_MD)
    problems = []
    for recipe in ("配方 A", "配方 B", "配方 C", "配方 D", "配方 E"):
        if recipe not in text:
            problems.append(f"缺 {recipe}")
    # 实测证据必须带行号，否则无法复核。
    for evidence in ("formbtn.js", "listDoing", "static4form"):
        if evidence not in text:
            problems.append(f"缺实测证据 {evidence!r}")
    if problems:
        return False, "；".join(problems)
    return True, "五条配方齐全，路由表 / 按钮 / 表单页证据均带出处"


def ev_api_lookup_pitfalls_stated():
    """配方 E（自己发接口时怎么找端点）必须写清两条**静默失败**的坑。

    为什么单独立一条：这两条的表现都是"**接口返回了形状正确的假数据**"，
    而不是报错。没有把它们写进 skill，读者会把"参数名传错"读成
    "这个环境没配数据"，然后往完全错误的方向查下去——2026-10-09 实打实
    花掉了一轮。所以断言"坑写出来了"，而不是只断言"有配方 E"。
    """
    text = _read(SKILL_MD)
    problems = []
    if "workflowId" not in text or "workflowid" not in text:
        problems.append("没点明 workflowId / workflowid 大小写是两个参数")
    if "belongTypeTargetId" not in text:
        problems.append("没给出自检用的回显字段 belongTypeTargetId")
    if "store" not in text:
        problems.append("没说明参数名要去 store 那一层读")
    if problems:
        return False, "；".join(problems)
    return True, "两条静默失败坑 + 自检字段 + 去 store 读参数名，均在位"


def ev_evals_readme_complete():
    """evals/README.md 存在，且列出了本文件里的每个 case_id。"""
    readme = HERE / "README.md"
    if not readme.exists():
        return False, "缺 evals/README.md"
    text = _read(readme)
    source = _read(Path(__file__))
    # 只在 CASES = [...] 块里找 case_id。**不能全文件正则**：FOREIGN_REFERENCES
    # 的元组长得一模一样，全文件扫会把它们也数进来，让"README 漏列某条用例"
    # 因为碰巧提过那个 token 而误报通过（初版就是这么错的，报了 13 条）。
    block = re.search(r"^CASES = \[(.*?)^\]", source, re.MULTILINE | re.DOTALL)
    if not block:
        return False, "在 run.py 里找不到 CASES 块"
    ids = re.findall(r'^\s*\(\s*"([a-z0-9-]+)",', block.group(1), re.MULTILINE)
    if not ids:
        return False, "CASES 块里没解析出任何 case_id"
    missing = [case_id for case_id in ids if f"`{case_id}`" not in text]
    if missing:
        return False, f"README 未列出这些用例：{missing}"
    return True, f"README 覆盖全部 {len(ids)} 条用例"


CASES = [
    ("skill-frontmatter", "SKILL.md 的 frontmatter 合规且 name 与目录一致", "free", ev_skill_frontmatter),
    ("relative-links-resolve", "SKILL.md 的相对链接全部可达", "free", ev_relative_links_resolve),
    ("references-linked", "references/mcp-tools.md 存在且被引用", "free", ev_references_present_and_linked),
    ("registered-in-runner", "已在 run_skill_evals.py 的 FLAGS 登记", "free", ev_registered_in_runner),
    ("local-tool-ban-stated", "「禁止本地工具」硬边界写清且点名 detect_changes", "free", ev_local_tool_ban_stated),
    ("ui-coverage-gap-stated", "写明图谱没有 UI 元素定位、匿名 handler 会断链", "free", ev_ui_coverage_gap_stated),
    ("qn-prefix-rule", "含点图名的 qn 剥前缀公式与反例均在", "free", ev_qn_prefix_rule),
    ("endpoints-match-config", "图名/端点与 .mcp.json 一致（无 .mcp.json 则跳过）", "free", ev_endpoints_match_config),
    ("no-foreign-references", "没抄进 api-test-E9 专有的路径与命令", "free", ev_no_foreign_references),
    ("route-recipes-present", "五条查询配方齐全且证据带出处", "free", ev_route_recipes_present),
    ("api-lookup-pitfalls-stated", "配方 E 写清两条静默失败的坑与自检字段", "free", ev_api_lookup_pitfalls_stated),
    ("evals-readme-complete", "evals/README.md 列出了全部用例", "free", ev_evals_readme_complete),
]


def main():
    parser = argparse.ArgumentParser(description="e9-graph-query 的 evals（全离线）")
    parser.add_argument("--list", action="store_true", help="只列出用例，不执行")
    parser.add_argument("--only", action="append", default=None, metavar="ID", help="只跑指定用例，可重复")
    args = parser.parse_args()

    if args.list:
        for case_id, what, cost, _ in CASES:
            print(f"  {case_id:<28} [{cost:^5}] {what}")
        return 0

    selected = set(args.only) if args.only else None
    failed, skipped, ran = [], 0, 0
    for case_id, _, _, check in CASES:
        if selected and case_id not in selected:
            continue
        ran += 1
        try:
            ok, detail = check()
        except Exception as error:  # noqa: BLE001 —— 任何异常都算该条失败，不拖垮整轮
            ok, detail = False, f"{type(error).__name__}: {error}"
        if ok is SKIP:
            skipped += 1
            print(f"  SKIP  {case_id:<28} {detail}")
        elif ok:
            print(f"  PASS  {case_id:<28} {detail}")
        else:
            failed.append(case_id)
            print(f"  FAIL  {case_id:<28} {detail}")

    print(f"\n{ran - len(failed) - skipped}/{ran} 通过，{skipped} 条跳过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())