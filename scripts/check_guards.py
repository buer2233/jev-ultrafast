"""Local-browser freshness/execution regressions. No model calls or external websites."""

from urllib.parse import quote

from jev_ultrafast.browser import Browser, StalePage

HTML = """<!doctype html><title>Guard checks</title>
<style>body{margin:30px}button{width:180px;height:50px}#outside{position:absolute;top:3000px}</style>
<p id="context">Cart total: $10</p>
<button id="target" onclick="window.clicks=(window.clicks||0)+1">Continue</button>
<label>City<input id="field" value="Zurich"></label>
<label><input id="toggle" type="checkbox">Refundable</label>
<select aria-label="Category"><option>All</option><option>Design</option></select>
<p id="outside">Unrelated offscreen text</p>"""


def main():
    browser = Browser("data:text/html," + quote(HTML))
    passed = []
    try:
        page = browser.observe(screenshot=False)
        action = next(a for a in page["actions"] if a["label"] == "Continue")
        browser.evaluate("document.querySelector('#target').style.transform='translateX(200px)'")
        assert browser.fresh(page), "Movement should use fresh geometry, not another model call"
        result = browser.act(action, page)
        assert browser.evaluate("window.clicks") == 1
        passed.append("moving target clicked at its current location")

        # 动作点必须**带出来**：报告层的插件靠它把录屏画成"看得见光标"的回放
        # （CDP 录屏不含系统光标）。以前 `act` 只回 {"executed": id}，
        # 坐标算完就丢在 evaluate 里，事后补不回来。
        # 断言它等于元素**当前**中心：这正是"点的是移动后的位置"的量化版本。
        point = result.get("point") or {}
        where = browser.evaluate(
            "(() => { const r=document.querySelector('#target').getBoundingClientRect();"
            "return [Math.round(r.x+r.width/2), Math.round(r.y+r.height/2)]; })()")
        assert point.get("via") == "mouse", point
        assert [round(point["x"]), round(point["y"])] == where, (point, where)
        passed.append("the executed action reports the point it was dispatched at")

        browser.evaluate("document.querySelector('#outside').textContent='Updated outside the viewport'")
        assert browser.fresh(page)
        passed.append("unrelated offscreen text does not invalidate")

        mutations = {
            "visible context": "document.querySelector('#context').textContent='Cart total: $100'",
            "accessible label": "document.querySelector('#target').setAttribute('aria-label','Delete account')",
            "field property": "document.querySelector('#field').value='London'",
            "checkbox property": "document.querySelector('#toggle').checked=true",
            "disabled target": "document.querySelector('#target').disabled=true",
            "read-only field": "document.querySelector('#field').readOnly=true",
            "hidden target": "document.querySelector('#target').style.display='none'",
            "replaced node": "document.querySelector('#target').outerHTML=document.querySelector('#target').outerHTML",
            "dropdown option": "document.querySelector('select').options[1].text='Coastal'",
        }
        for label, expression in mutations.items():
            browser.evaluate("document.querySelector('#target').style.display='block'; "
                             "document.querySelector('#target').disabled=false")
            page = browser.observe(screenshot=False)
            browser.evaluate(expression)
            assert not browser.fresh(page), label
            passed.append(label + " invalidates")

        browser.evaluate("document.querySelector('#target').disabled=false; "
                         "document.querySelector('#target').style.display='block'")
        page = browser.observe(screenshot=False)
        action = next(a for a in page["actions"] if a["label"] == "Delete account")
        # A textless overlay does not alter the model's semantic state, but must block a click.
        browser.evaluate("const cover=document.createElement('div'); "
                         "cover.style.cssText='position:fixed;inset:0;z-index:9999;background:white'; "
                         "document.body.append(cover)")
        assert browser.fresh(page)
        try:
            browser.act(action, page)
        except (RuntimeError, StalePage):
            pass
        else:
            raise AssertionError("Covered target was clicked")
        assert browser.evaluate("window.clicks") == 1
        passed.append("overlay blocked before input")

        browser.evaluate("document.body.innerHTML=" + repr("""
          <form><p id="price">Total $10</p>
          <button type="button" id="buy">Buy</button>
          <label>Search <input id="query" role="combobox" aria-controls="suggestions"></label>
          <div role="listbox" id="suggestions"></div>
          <label><input id="check" type="checkbox">Enabled</label>
          <label><input id="radio" type="radio">Choice</label>
          <input id="readonly" aria-label="Read only" readonly>
          <input id="secret" type="password" value="never expose this">
          <button id="off" disabled>Disabled</button>
          <select id="category" aria-label="Category">
            <option>All</option><option>Design</option><option disabled>Unavailable</option>
          </select></form><aside id="unrelated">News</aside>
        """))
        page = browser.observe(screenshot=False)
        buy = next(a for a in page["actions"] if a["label"] == "Buy")
        browser.evaluate("document.querySelector('#unrelated').textContent='New unrelated news'")
        assert browser.fresh(page, buy)
        assert not browser.fresh(page)
        passed.append("click guard accepts unrelated visible updates; terminal guard rejects them")
        for label, expression in {
            "nearby price": "document.querySelector('#price').textContent='Total $100'",
            "form value": "document.querySelector('#query').value='changed'",
            "form toggle": "document.querySelector('#check').checked=true",
            "target replacement": "document.querySelector('#buy').outerHTML=document.querySelector('#buy').outerHTML",
        }.items():
            page = browser.observe(screenshot=False)
            buy = next(a for a in page["actions"] if a["label"] == "Buy")
            browser.evaluate(expression)
            assert not browser.fresh(page, buy), label
            passed.append(label + " invalidates action-specific guard")

        page = browser.observe(screenshot=False)
        actions = page["actions"]
        for role in ("checkbox", "radio"):
            assert {a["kind"] for a in actions if a.get("role") == role} == {"click"}
        assert {a["kind"] for a in actions if a["label"] == "Read only"} == {"click"}
        assert not any(a["label"] == "Disabled" or a.get("value") == "never expose this" for a in actions)
        assert [a["value"] for a in actions if a["kind"] == "select"] == ["Design"]
        passed.append("native controls expose only supported operations and safe values")

        select = next(a for a in actions if a["kind"] == "select")
        select_result = browser.act(select, page)
        assert browser.evaluate("document.querySelector('#category').value") == "Design"
        # select 是**直接设 value**，压根不发鼠标事件——那个点是"控件在哪"，
        # 不是"鼠标去过哪"。via 必须如实标成 js，不能冒充一次点击。
        assert (select_result.get("point") or {}).get("via") == "js", select_result
        passed.append("native dropdown selects an observed option")

        browser.evaluate("document.querySelector('#query').addEventListener('input',()=>setTimeout(()=>{"
                         "document.querySelector('#suggestions').innerHTML='<div role=option>Generated</div>'"
                         "},60))")
        page = browser.observe(screenshot=False)
        field = next(a for a in page["actions"] if a["kind"] == "fill")
        browser.act(field, page, text="Generated")
        page = browser.observe(screenshot=False)
        value = browser.evaluate("document.querySelector('#query').value")
        assert value == "Generated", repr(value)
        assert any(a.get("role") == "option" for a in page["actions"])
        passed.append("real text input waits for asynchronous combobox suggestions")
        browser.call("Page.navigate", url="about:blank")
        assert not browser.fresh(page, field)
        passed.append("navigation invalidates the old document")

        # 「行即按钮」的表格行：E9 的路径类型/表单选择弹窗把每行做成 <tr> + 自身 click 处理器，
        # 行内【一个可点元素都没有】。主循环只收 a/button/input/[role=…]，于是那 41 行整片不可见——
        # 而它们恰恰是那个弹窗里唯一能选的东西。实测（2026-09-24）模型因此反复点弹窗里那个
        # 同名图标按钮（页面无变化）直到被判 blocked。
        #
        # 判据是**页面自己声明的 cursor:pointer**（作者在说"这一行可以点"），叠加"行内没有可交互元素"
        # 以避免与已收录的链接/按钮重复。三种行必须分开：
        browser.call("Page.navigate", url="about:blank")
        browser.evaluate("document.body.innerHTML=" + repr("""
          <table><tbody>
            <tr id="pick" style="cursor:pointer"><td>系统默认工作流</td><td>同名描述</td></tr>
            <tr id="plain"><td>普通数据行</td><td>光标不是 pointer</td></tr>
            <tr id="linked" style="cursor:pointer"><td><a href="#">带链接的行</a></td><td>x</td></tr>
          </tbody></table>"""))
        browser.evaluate("document.querySelector('#pick').addEventListener('click',"
                         "()=>{window.rowPicked=true})")
        page = browser.observe(screenshot=False)
        rows = [a for a in page["actions"] if a.get("role") == "row"]
        assert len(rows) == 1, [(a.get("role"), a.get("label")) for a in page["actions"]]
        assert rows[0]["kind"] == "click", rows[0]
        assert "系统默认工作流" in rows[0]["label"], rows[0]
        browser.act(rows[0], page)
        assert browser.evaluate("window.rowPicked") is True
        passed.append("clickable table rows become candidates; clicking one runs the row's own handler")

        # **换行的行内元素**也必须可点。E9 的流程列表把标题渲染成两行的 `<a>`
        # （列窄、标题长，必然折行；实测客户矩形是 218–234 与 237–253，**中间空 3.2px**
        # ——`line-height:19.2px` 而行盒只有 16px 高）。取点若只用联合包围盒的中心，
        # 那个点正好落在**两行之间的空隙**上：elementFromPoint 命中的是它所在的 `<td>`，
        # 于是整行被判 "covered"；再叠上 `model._drop_covered()`（同操作还有未遮挡候选时，
        # 被遮挡的整类会被剔掉），十行标题会从模型眼前**整体消失**。
        # 实测（2026-10-10，TC08）：45 步一步都没点到过流程行，全在点筛选器空转；
        # 而真鼠标点它的第一行文字，表单是会打开的。这条钉住"取点用客户矩形"。
        browser.call("Page.navigate", url="about:blank")
        # 用 `white-space:pre-line` 的换行符强制折行：**不依赖字体度量**，
        # 换行位置在哪台机器上都一样（用窄容器去逼折行会随字体宽度漂）。
        browser.evaluate("document.body.innerHTML=" + repr("""
          <a href="#" id="wrapped" style="white-space:pre-line;line-height:40px;font-size:12px"
             onclick="window.wrappedClicked=true;return false">jev-wf_1009183640_
e9-wf-submit-tc08</a>"""))
        page = browser.observe(screenshot=False)
        wrapped = next((a for a in page["actions"] if a["label"].startswith("jev-wf_")), None)
        assert wrapped is not None, [a.get("label") for a in page["actions"]]
        # 前提断言：包围盒中心**真的落在两行之间的空隙里**。不成立的话这条检查就退化成
        # 普通元素，再也不会因为取点方式退化而变红——那就白写了。
        # 判据与 snapshot.js 的取点口径一致：只数**有面积**的客户矩形。
        gap = browser.evaluate("""(() => {
          const a = document.querySelector('#wrapped'), box = a.getBoundingClientRect();
          const cx = box.x + box.width / 2, cy = box.y + box.height / 2;
          const rects = [...a.getClientRects()].filter(r => r.width > 0 && r.height > 0);
          const inAny = rects.some(r =>
            cx >= r.left && cx <= r.right && cy >= r.top && cy <= r.bottom);
          return {lineBoxes: rects.length, centerInGap: !inAny};
        })()""")
        assert gap["lineBoxes"] >= 2 and gap["centerInGap"], gap
        assert not wrapped.get("covered"), wrapped
        browser.act(wrapped, page)
        assert browser.evaluate("window.wrappedClicked") is True
        passed.append("a wrapped inline link stays clickable (hit point comes from its line boxes)")

        # 控件【当前值】不能被当字段名。antd 这类自绘下拉把选中值渲染成一层 div，不是
        # <select>/<option>，所以按标签名过滤拦不住它；行内最窄的那层文字于是变成"值"。
        # 实测（2026-09-24，E9「添加路径」弹窗）：「对应表单」那一行的放大镜被取名叫
        # 「自定义表单」（左边下拉的当前值），候选表里【没有任何元素叫「对应表单」】——
        # goal 说"点对应表单右边的放大镜"，模型找不到，只能反复去点左边那个下拉框，空转 30 步。
        # 两行必须分开看：有值的行取名取【字段名】，空的下拉行同样取字段名（回归）。
        browser.call("Page.navigate", url="about:blank")
        browser.evaluate("document.body.innerHTML=" + repr("""
          <div class="row"><span>对应表单</span>
            <span role="combobox"><span>自定义表单</span></span>
            <button id="pick1"></button></div>
          <div class="row"><span>路径类型</span>
            <span role="combobox"></span>
            <button id="pick2"></button></div>"""))
        page = browser.observe(screenshot=False)
        buttons = {a["label"] for a in page["actions"] if a.get("role") == "button"}
        assert buttons == {"对应表单", "路径类型"}, buttons
        values = {a["label"] for a in page["actions"] if a.get("role") == "combobox"}
        assert values == {"自定义表单"}, values
        passed.append("a control's current value is never used as its neighbour's field name")

        # 同源 iframe 下钻 + 图标按钮。E9 的流程设计画布**整个**在同源 iframe 内
        # （`/workflow/workflowDesign/index.html`：工具栏「创建/审批/自动处理/归档/分叉起始点…」、
        # 画布节点、右侧「流程信息」面板），而工具栏是
        # `<span class="icon-workflow-…" title="创建">`——没有 role、没有 href、没有文字。
        # 不下钻 + 不认图标按钮，那些控件一个都进不了候选表，"建流程"无从下手。
        #
        # 这条检查同时钉住三件事：① 内层控件能进候选表；② 坐标换算到**顶层视口**
        # （换算错了，点出去会落在错的地方，而且报告里看不出）；③ 真点得动。
        browser.call("Page.navigate", url="about:blank")
        browser.evaluate("""(() => {
          const f = document.createElement('iframe');
          f.style.cssText = 'position:fixed;left:200px;top:100px;width:400px;height:200px;border:0';
          document.body.append(f);
          const d = f.contentDocument;
          d.body.style.margin = '0';
          // 两个图标分别走两条判据：`onclick`（E9 的工具栏就是这样，实测 15 个
          // `.icon-workflow-*` 都带 onclick）与 `cursor:pointer`。
          // ⚠️ 已知边界：只挂 addEventListener、又没有 cursor:pointer / tabindex 的元素，
          // 从 DOM 上**看不出能点**——这是诚实的限制，不是判据写漏了。
          d.body.innerHTML =
            '<span id="make" title="创建" style="position:absolute;left:20px;top:20px;' +
              'width:40px;height:40px;display:block"></span>' +
            '<span id="appr" title="审批" style="position:absolute;left:80px;top:20px;' +
              'width:40px;height:40px;display:block;cursor:pointer"></span>' +
            // 「自定义按钮」：有文字、有 onclick，但没有 role/title —— E9 的「编辑」就是这样
            // （`<span>编辑</span>`），前面几轮探针都只能靠硬编码坐标点它。
            '<span id="edit" style="position:absolute;left:140px;top:20px;width:40px;' +
              'height:40px;display:block">编辑</span>' +
            // 「无名元素」：有 onclick 但一个字都没有 —— 必须**不收**。E9 类型树有 24 个
            // 这样的展开箭头，收进来就是 24 个同名 "button"，把候选表搅浑。
            '<span id="arrow" style="position:absolute;left:200px;top:20px;width:12px;' +
              'height:16px;display:block"></span>' +
            '<input id="name" aria-label="路径名称" style="position:absolute;left:20px;' +
              'top:120px;width:200px;height:24px">';
          d.getElementById('make').onclick = () => { window.frameClicked = true; };
          d.getElementById('edit').onclick = () => {};
          d.getElementById('arrow').onclick = () => {};
        })()""")
        page = browser.observe(screenshot=False)
        labels = [a.get("label") for a in page["actions"]]
        assert any(a.get("label") == "审批" for a in page["actions"]), labels  # icon ①: cursor
        assert "编辑" in labels, labels          # 自定义按钮 ②：有名字 + onclick
        assert "button" not in labels, labels    # 无名的 onclick 元素必须不收
        make = next(a for a in page["actions"] if a.get("label") == "创建")  # icon ①: title
        rect = make["rect"]
        assert abs((rect["x"] + rect["w"] / 2) - 240) < 4, rect   # 200 + 20 + 20
        assert abs((rect["y"] + rect["h"] / 2) - 140) < 4, rect   # 100 + 20 + 20
        assert any(a.get("label") == "路径名称" for a in page["actions"]), \
            [a.get("label") for a in page["actions"]]
        browser.act(make, page)
        # 处理器是在【顶层文档】里创建的闭包，所以它的 `window` 指向顶层——标志就落在那里。
        # 这一条同时证明了"鼠标事件真的被路由进了 iframe"：没进去就点不到那个图标。
        assert browser.evaluate("window.frameClicked") is True
        passed.append("same-origin iframe controls become candidates at top-document "
                      "coordinates, and clicking one runs its handler")

        # 整个 iframe 被盖住时，**内层**的命中测试判不出来（内层那一点上就是它自己），
        # 所以 geometry() 还有一道"顶层那点必须落在这个 frame 上"。这条钉住它。
        browser.evaluate("""(() => {
          const cover = document.createElement('div');
          cover.style.cssText = 'position:fixed;left:200px;top:100px;width:400px;height:200px;' +
            'z-index:9999;background:#fff';
          document.body.append(cover);
        })()""")
        page = browser.observe(screenshot=False)
        blocked = next(a for a in page["actions"] if a.get("label") == "创建")
        assert blocked.get("covered") is True, blocked
        try:
            browser.act(blocked, page)
        except (RuntimeError, StalePage):
            pass
        else:
            raise AssertionError("A control inside a fully covered iframe was clicked")
        passed.append("an iframe covered from outside makes its inner controls un-clickable")
    finally:
        browser.close()
    print("\n".join(passed))
    print(f"PASS: {len(passed)} browser guard checks; no model calls")


if __name__ == "__main__":
    main()
