(() => {
  if (!document.body) return null;
  const cache = window.__jevFast ||= {ids:new WeakMap(), nodes:new Map(), next:1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e,cache.next++);
    const id=cache.ids.get(e); cache.nodes.set(id,e); return id;
  };
  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);
  const safe = e => !['password','file','hidden'].includes(e.type);
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  // ---------------------- 同源 frame 下钻 + 统一的几何/命中测试 ----------------------
  //
  // 为什么必须下钻：E9 的流程设计画布是**同源 iframe**
  // （`/workflow/workflowDesign/index.html`）。工具栏（创建/审批/自动处理/归档/分叉…）、
  // 画布上的节点、右侧「流程信息」面板**全在它里面**——只扫顶层文档的话，
  // 那些控件一个都进不了候选表，"建流程"这件事无从下手。
  //
  // 为什么几何要单独抽成一个函数：iframe 内元素的 `getBoundingClientRect()` 是
  // **它自己那个文档的视口坐标**，既不能拿去和顶层 `innerWidth/innerHeight` 比，
  // 也不能用顶层 `elementFromPoint` 判遮挡——顶层那一点上躺着的是 `<iframe>` 本身。
  // 换算与判据两边各写一份必然漂移，所以候选生成与执行前校验（browser.py 的 act）
  // **共用这一个函数**：候选收的，就是执行敢点的。
  const scanFrames = () => {
    const origin = new WeakMap();
    const documents = [document];
    origin.set(document, {dx: 0, dy: 0, frame: null});
    const visit = (doc, depth) => {
      if (depth > 3) return;                     // 递归深度设限，别在诡异页面上转不出来
      for (const frame of doc.querySelectorAll('iframe')) {
        let inner = null;
        try { inner = frame.contentDocument; } catch { inner = null; }
        if (!inner || !inner.body) continue;     // 跨域，或还没加载完
        const r = frame.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;   // 隐藏的 frame 整片跳过
        // 被 CSS transform 缩放的 frame **不登记坐标**：内层坐标与外层不再是简单平移。
        // 但文档照样收进来——正文与页面指纹该看到它（人眼看得见），只是不给点击坐标，
        // geometry() 会因此返回 null，"看不见这个 frame 里的元素"。宁可不可点，不能点偏。
        const scaled = Math.abs(r.width - frame.offsetWidth) > 1 ||
                       Math.abs(r.height - frame.offsetHeight) > 1;
        if (!scaled) {
          const parent = origin.get(doc);
          // 内层 (0,0) 对应 frame 的**内容框**原点：rect 是边框盒，所以要加 clientLeft/Top（=边框宽）。
          origin.set(inner, {dx: parent.dx + r.left + frame.clientLeft,
                             dy: parent.dy + r.top + frame.clientTop, frame: frame});
        }
        documents.push(inner);
        visit(inner, depth + 1);
      }
    };
    visit(document, 0);
    return {documents, origin};
  };
  // 元素 → {x, y, covered}（x/y 是**顶层文档视口坐标**）；取不到则 null（等于"看不见，别点"）。
  //
  // **取点用客户矩形而不是联合包围盒。** 行内元素**换行**时包围盒是多行的并集，
  // 它的中心会落在**两行之间的空隙**上——那里 elementFromPoint 命中的是父元素
  // （E9 的流程标题就是这种两行 `<a>`，命中的是它所在的 `<td>`），于是这个元素被判成
  // "covered"。后果不止是少一个候选：`model._drop_covered()` 在**同一种操作还有未遮挡
  // 候选时会把被遮挡的整类剔掉**，于是列表里十行流程标题从模型眼前整体消失。
  // 实测（2026-10-10，TC08）：45 步里一次都没点到过流程行，全在点筛选器空转；
  // 而用真鼠标点同一行的**第一行文字**，表单是会打开的（新标签页）——所以那是误判。
  //
  // covered 是"这个点上压着别的东西"，两层都要过：
  //   · 内层：自己文档的 elementFromPoint 必须落回自己（或其后代）；
  //   · 外层：顶层那个点必须落在这个 frame 上——有东西盖住整个 iframe 时，内层判不出来。
  // 与 browser.py 的判据一致（那里直接调这个函数），所以这里不放宽、也不多加：
  // 只是**逐点**地试（客户矩形中心 → 包围盒中心），第一个既在视口内又没被压住的点胜出；
  // 全被压住时，如实返回第一个点并标 covered（调用方照旧拒绝执行）。
  cache.geometry = (e, origins) => {
    const box = e.getBoundingClientRect();
    if (!box.width || !box.height) return null;
    const points = [...e.getClientRects()]
      .filter(r => r.width > 0 && r.height > 0)
      .map(r => ({x: r.x + r.width / 2, y: r.y + r.height / 2}));
    points.push({x: box.x + box.width / 2, y: box.y + box.height / 2});
    const doc = e.ownerDocument;
    const origin = doc === document ? null : (origins || scanFrames().origin).get(doc);
    if (doc !== document && !origin) return null;   // 跨域 / 被缩放 / 超出递归深度
    let blocked = null;
    for (const p of points) {
      let hit;
      if (doc === document) {
        if (p.x < 0 || p.y < 0 || p.x >= innerWidth || p.y >= innerHeight) continue;
        hit = {x: p.x, y: p.y, covered: !e.contains(document.elementFromPoint(p.x, p.y))};
      } else {
        const tx = origin.dx + p.x, ty = origin.dy + p.y;
        if (tx < 0 || ty < 0 || tx >= innerWidth || ty >= innerHeight) continue;
        hit = {x: tx, y: ty,
               covered: !e.contains(doc.elementFromPoint(p.x, p.y)) ||
                        document.elementFromPoint(tx, ty) !== origin.frame};
      }
      if (!hit.covered) return hit;
      if (!blocked) blocked = hit;
    }
    return blocked;
  };
  // 兜底取名：从"表单行"里找字段名。**只在上面整条链全部落空时才用**。
  //
  // 为什么需要：E9 这类后台把字段名放在**同一行的兄弟单元格**里
  // （`<td>路径名称:</td><td><input class="ant-input"></td>`），既没有 for/id 关联、
  // 也没有 aria-labelledby，于是 name() 返回空串——模型在候选表里只看到 "textbox"，
  // goal 说"填路径类型"它无从下手。实测它会退而填掉唯一认得出来的那个字段，然后判 DONE。
  //
  // 只取"叶子 + 短文字"的候选，且**排除输入控件自身**：否则会把当前值当字段名，
  // 而值是会变的，让 guard 跟着抖。
  // 上限 120 个节点是刻意的：行级容器很小，一旦扫到大容器就说明已经走到页面级，直接放弃，
  // 避免为了取名把整页遍历一遍。
  const rowLabel = e => {
    let best='', bestCount=Infinity;
    for (let depth=0, node=e.parentElement; depth<9 && node; depth++, node=node.parentElement) {
      const all=node.querySelectorAll('*');
      if (all.length>200) break;   // 已经走到页面级，再往上只会更糊
      const texts=[...all].filter(x=>!x.children.length && !x.closest('iframe,script,style') &&
          !/^(INPUT|TEXTAREA|SELECT|BUTTON|OPTION)$/.test(x.tagName) &&
          !x.closest('button,a,[role="button"]') &&
          // 控件【当前值】不是字段名。antd 这类自绘下拉把选中值渲染成一层 div
          // （`.ant-select-selection-selected-value`），不是 <select>/<option>，
          // 所以上面那条标签名过滤拦不住它。实测（2026-09-24，E9「添加路径」弹窗）：
          // 「对应表单」那一行的放大镜被取名叫「自定义表单」——那是左边下拉框的值，
          // 而候选表里【没有任何元素叫「对应表单」】，goal 说"点对应表单右边的放大镜"
          // 模型根本找不到，只能反复去点左边那个下拉框，空转 30 步。
          // 判据用 ARIA 角色而不是 antd 的类名：值渲染的容器必然带着控件角色。
          !x.closest('input,select,textarea,[contenteditable="true"],[role="combobox"],'+
            '[role="listbox"],[role="textbox"],[role="searchbox"]'))
        .map(x=>(x.textContent||'').trim()).filter(t=>t.length>0 && t.length<=14)
        .map(t=>t.replace(/[:：*]\s*$/,''));
      // 取**最窄的那一层**，而不是碰到的第一层。
      //
      // 实测 E9 的路径设置弹窗：控件的祖先链上 depth 0-6 一个文字都没有，字段名在
      // depth 7 的 `ant-row.wea-form-item`（整行只有 1 个字段名）；再往上 depth 9 是
      // 整个表单容器，一层里有 7 个字段名。若用"碰到有文字就返回"，走到表单那层会
      // 取到最上面那个「路径名称」，把**每个**字段都叫成"路径名称"——比不给名字更糟。
      // 一层里的字段名越少，就越接近"这个控件自己那一行"。
      if (texts.length && texts.length<bestCount) { bestCount=texts.length; best=texts[0]; }
    }
    return best;
  };
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(e.ownerDocument.getElementById(id),seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || rowLabel(e);
  };
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  // a[title] 覆盖「没有 href 但可点」的锚点：E9 的流程列表用 <a target="_blank" title="…">，
  // 既无 href 也无 ARIA role，只靠 a[href] 会整片漏掉。
  // `[title]` 再往前一步：图标按钮连 a 都不是（`<span title="创建">`）。收不收由 role() 决定，
  // 那里要求"有 title **且** 有 onclick/cursor 线索"，所以这一条不会把带 title 的装饰元素放进来。
  const selector='a[href],a[title],[title],button,input,textarea,select,summary,[contenteditable="true"],'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (['text','email','url','tel'].includes(e.type)) return 'textbox';
    }
    // 图标按钮：**只能靠 title 才叫得出名字**、靠 onclick/cursor 才看得出能点。
    // E9 的流程画布工具栏就是这样：`<span class="icon-workflow-chuangjian" title="创建">`，
    // 没有 role、没有 href、内容和 alt 都是空的——按标签名和 ARIA 一个都收不进来，
    // 而整个"建流程"的动作全靠它（创建/审批/自动处理/归档/分叉起始点/分叉中间点/分叉合并节点）。
    //
    // 两个条件缺一不可：只看 title 会把满页带 title 的装饰元素都收成按钮；
    // 只看 onclick 会收进一堆没有名字的，模型没法引用，纯噪声。
    // `cursor:not-allowed` 是页面在说"这个是禁用的"，与 :disabled / aria-disabled 同等对待
    // （实测「撤销」「恢复」没东西可撤销时就是这个状态）。
    const title=(e.getAttribute('title')||'').trim();
    const text=e.tagName==='INPUT' ? '' : e.textContent.replace(/\s+/g,' ').trim();
    const aria=(e.getAttribute('aria-label')||'').trim();
    const cursor=getComputedStyle(e).cursor;
    const clickable=e.onclick || cursor==='pointer';
    // 只认【图标】：整棵子树里一个字都没有，`title` 是它唯一的名字——页面在拿 title 当按钮名用。
    //
    // 为什么不再收"有文字 + 有 onclick/pointer"的那一类：**实测它是噪声的主要来源**。
    // 实测（E9 列表页，1120）：加上那一类候选从 72 涨到 107，而多出来的是
    // `<span class="wea-url" title="0902-xf">` 这种"文字在子元素里、本身不是控件"的包装，
    // 和已经收过的 `<a>` 重复；代价是原本稳定通过的 e9-wf-designer-create 变得时好时坏
    // （模型在 107 个候选里挑不动「对应表单」的放大镜）。
    // 而真正需要它救的只有「编辑」那种裸 `<span>编辑</span>`——那一个由下面的
    // "自定义按钮（叶子层）"一遍按 `onclick` 收，不依赖这里。
    //
    // 用 textContent（含后代）而不是"直接文字节点"：`wea-url` 的文字在子元素里，
    // 那种有自己的名字，不该按图标处理（少这一条会把一整列表格单元格收成图标）。
    if (cursor!=='not-allowed' && title && !text) return 'button';
    return null;
  };
  // 新鲜度比对专用的 URL：剥离 SPA 的会话随机参数。
  //
  // E9 每次交互都会改写 URL 里的 _key（以及加载时的 _rdm / preloadkey / timestamp），
  // 这些只是会话随机串，不代表页面语义变化。若直接拿 location.href 比对，
  // 每次点击后都会被判成"页面已变"，于是 page_changed 恒为 true——
  // 既让"连续三次无变化即停止"的保护失效，也让决策反复被判过期。
  //
  // 注意：返回给模型与用例断言用的 url 保持原样，不受影响。
  const VOLATILE_URL_PARAMS=/([?&])(_key|_rdm|preloadkey|timestamp|_time|_)=[^&#]*/g;
  const freshUrl=()=>location.href.replace(VOLATILE_URL_PARAMS,'$1');
  // 表单字段要连同源 frame 里的一起算：流程设计画布、富文本编辑区都在 frame 内。
// 漏了它们，"在 frame 里改了什么"就不算页面变化，新鲜度校验会放行已经过期的决策。
  cache.pageKey=()=>[performance.timeOrigin,freshUrl(),scrollX,scrollY,innerWidth,innerHeight,
    scanFrames().documents.flatMap(doc =>
      [...doc.querySelectorAll('input,textarea,select')].filter(safe)
        .map(e=>[identity(e),e.value,e.checked,e.selectedIndex,e.disabled,e.readOnly]))];
  cache.guard=e=>{
    if (!e?.isConnected || !visible(e)) return null;
    const scope=e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e),role(e),name(e),e.value??null,e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      e.getAttribute('href'),scope?.innerText?.slice(0,6000)||''];
  };
  const actions=[];
  // 顶层 + 每个可见同源 frame（见 scanFrames 的注释）。候选坐标一律换算到**顶层视口**，
  // 与执行层同一个来源（cache.geometry）——"候选收的 = 执行敢点的"。
  const frameView=scanFrames();
  const collect=doc => {
  for (const e of doc.querySelectorAll(selector)) {
    if (!safe(e) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    const rname=role(e);
    if (!rname) continue;
    // 富文本编辑器的 contenteditable body 交给下面那段 iframe 专用分支：它用 frameLabel
    // 取字段名，而且**刻意允许元素落在视口外**（高表单的签字意见就在首屏下方）。
    // 这里不重复收录，否则同一个编辑区会出现两个候选、名字还不一样。
    if (e.tagName==='BODY' && e.isContentEditable) continue;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
    // 几何 + 命中测试：中心点上最顶层的元素必须是自己（或后代），iframe 内则两层都要过。
    // 与执行层**共用同一个函数**（browser.py 的 act 调 window.__jevFast.geometry）。
    //
    // 为什么候选生成阶段也要算这一份：只满足"可见 + 在视口内"的元素，可能已经被打开中的
    // 弹窗盖住（实测 E9 后端引擎页：点开「添加路径」后，下层「添 加」按钮仍留在候选表里）。
    // 模型按 goal 挑中它 → 执行层的 elementFromPoint 拒绝执行 → 却没有信息告诉模型"换一个"，
    // 于是每轮重挑同一个元素，空转到步数上限。
    //
    // 这里只**标注**不**剔除**：执行前还会先 scrollIntoView 再算一次几何，两边未必完全等价，
    // 剔除会误杀本来能点的元素。标出来让模型自己绕开，执行层的校验一字不改。
    // 判据是 contains 而不是 ===：按钮的可点区域常常是它的子 span（实测 isSelf 为假）。
    const g=cache.geometry(e, frameView.origin);
    if (!g) continue;
    const r=e.getBoundingClientRect();
    const base={node:identity(e),role:rname,label:name(e)||rname,
      rect:{x:g.x-r.width/2,y:g.y-r.height/2,w:r.width,h:r.height},
      ...(g.covered?{covered:true}:{})};
    for (const key of ['checked','selected','expanded']) {
      const value=e.getAttribute('aria-'+key);
      if (value!==null) base[key]=value;
    }
    if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
    if (e.tagName==='SELECT') {
      for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
        actions.push({...base,kind:'select',value:o.value,
          current_value:[...e.selectedOptions].map(o=>o.label).join(', '),label:base.label+' → '+o.label});
    } else {
      const editable=!e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
        (['textbox','searchbox','spinbutton'].includes(rname) ||
          (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
      const value='value' in e ? String(e.value) :
        e.isContentEditable || rname==='combobox' ? e.innerText.trim() : '';
      actions.push({...base,kind:editable?'fill':'click',value});
      // 「Open X」这个合成点击只为**下拉/自动完成**而设：先点开候选列表再从里面选一项
      // （NEXT_ACTION 里那条"A typed query still needs its matching autocomplete suggestion
      // selected"说的就是它）。对普通文本框它没有任何用处——点了只是聚焦，还会和同一元素的
      // fill 候选**同名竞争**（type_text_target 里是「名称」，click_target 里是「Open 名称」），
      // 实测模型会挑中「Open 名称」而不去填值，连点三次然后收工。
      // 判据与 browser.py 的 after_input 一致：role=combobox 或带 aria-autocomplete 才算自动完成。
      const autocomplete=e.getAttribute('role')==='combobox' || e.getAttribute('aria-autocomplete');
      if (editable && autocomplete) actions.push({...base,kind:'click',value,label:'Open '+base.label});
    }
  }
  };
  for (const doc of frameView.documents) collect(doc);
  // 自定义按钮（叶子层）：页面自己声明能点、又**叫得出名字**，但没有 role / title 的元素。
  //
  // 为什么又要一遍：E9 流程设计页的「编辑」是 `<span>编辑</span>`——进编辑态的**唯一入口**，
  // 既没有 role 也没有 title，靠标签名和 ARIA 都收不进来（前几轮探针只能硬编码坐标点它）。
  //
  // 为什么单独一遍而不是加进 selector：主循环会对每个命中元素算 computed style，
  // 把 `[onclick]` 之类放宽到全文档会拖慢每一次观察。这一遍**只在叶子层**做，且
  // 先用"有没有名字"和"是不是已经收过"筛掉，再算 cursor——实测 E9 类型树有 24 个
  // 12×16 的无名 onclick 元素，它们会在第一步就被筛掉，不进候选表。
  for (const doc of frameView.documents) {
    for (const e of doc.querySelectorAll('*')) {
      if (e.children.length) continue;                 // 只看叶子：容器由里面的叶子代表
      if (e.closest('[aria-hidden="true"],[inert]')) continue;
      const aria=(e.getAttribute('aria-label')||'').trim();
      const title=(e.getAttribute('title')||'').trim();
      const text=(e.textContent||'').replace(/\s+/g,' ').trim();
      if (!aria && !title && !text) continue;          // 没有名字：模型引用不了，不收
      // 页面自己在说"这个能点"：挂了 `onclick`，或画成了 `cursor:pointer`。
      // 两条都要——实测 E9 流程设计页的「编辑」只有 pointer、没有 onclick，
      // 而调色板图标反过来只有 title。**噪声不在这里**：实测把这里从
      // "pointer 也算"改成"只认 onclick"，列表页候选数一条没变（107 进 107 出），
      // 真正的噪声源是 role() 里那条"有 title 又有文字也算按钮"（已删）。
      if (!e.onclick && getComputedStyle(e).cursor!=='pointer') continue;
      if (e.matches(selector)) continue;               // 主循环已经收过的，别重复。
      // ⚠️ 这里必须比【selector】，不能比 role(e)：role() 现在**也会**把这些自定义按钮
      // 判成 'button'（它认得"有名字 + 有 onclick"），但主循环的 selector 根本不匹配
      // 一个没有 title/role 的裸 `<span>编辑</span>`——用 role() 判会把它当成"已收过"而跳过，
      // 结果两边都不收。这个 bug 在合成页面上复现过（probe_leaf_pass）。
      //
      // 祖先里已经有人是候选的，也跳过：那说明真正该点的是那个祖先（或它里面被主循环
      // 收过的后代），本元素只是它的**文字**。少了这一条，实测（1120 下的 E9 列表页）
      // 「添 加」按钮会连同它里面的 `<span>添 加</span>` 各收一条，类型树的每一行、
      // 左侧菜单每一项也都会各多一条——候选表从 72 涨到 140，全是重复，模型反而挑不动了。
      // 判据只有一条：**祖先是不是主循环会收的那个元素**（比 selector）。
      // 是的话本元素只是它的文字，收了就是重复。
      //
      // ⚠️ 这里先后试过两条"看起来更聪明"的判据，都砍掉了——都伤到 E9 流程设计页的
      // 「编辑」（进入编辑态的唯一入口），而且在列表页上一分噪声也没减：
      //   · "祖上有 ARIA role" → 编辑的祖上有带 role 的元素，被当成"已收过"，
      //     可主循环其实没碰它（它没 role 没 title）——两边都不收；
      //   · "祖上有 title"     → 编辑的祖上也有 title，同样被误判成已收。
      // 教训：判断"是否已收过"只能以**主循环真正用过的那个 selector** 为准。
      let shadowed=false;
      for (let anc=e.parentElement, i=0; anc && i<6; i++, anc=anc.parentElement) {
        if (anc.matches(selector)) { shadowed=true; break; }
      }
      if (shadowed) continue;
      if (!visible(e)) continue;
      const g=cache.geometry(e, frameView.origin);
      if (!g) continue;
      const r=e.getBoundingClientRect();
      actions.push({node:identity(e),role:'button',label:name(e)||text.slice(0,20),kind:'click',
        value:'',rect:{x:g.x-r.width/2,y:g.y-r.height/2,w:r.width,h:r.height},
        ...(g.covered?{covered:true}:{})});
    }
  }
  // 可点整行：「行即按钮」的表格行。
  //
  // 为什么需要：E9 的「路径类型」「表单名称」选择弹窗把每一行做成 `<tr>` + 自身的 click 处理器，
  // 行内**一个可点元素都没有**（没有 <a>、没有 <button>）。上面的主循环只收
  // a/button/input/[role=…]，于是那 41 行整片不可见——而它们恰恰是那个弹窗里唯一能选的东西。
  // 实测（2026-09-24）：模型在弹窗里只看得到「名称」搜索框和一个同名图标按钮，
  // 反复点那个按钮（页面无变化）直到撞上"连续三条无变化即 blocked"的守卫。
  //
  // 判据是**页面自己声明的 `cursor:pointer`**：那是作者在说"这一行可以点"，
  // 不是我们按站点猜的。再叠加两条约束：
  //   · 行内没有可交互元素——否则那些元素已经是候选了。主列表每行都带 <a>，
  //     若一并收录，一张 121 行的表会平白多出 121 个重复候选（实测那张表 cursor 也不是 pointer，
  //     两道闸门都会拦住它）；
  //   · 几何与命中测试与主循环**同一套**（视口内 + elementFromPoint），covered 照标不误，
  //     执行层的遮挡校验一字未动。
  const ROW_INTERACTIVE='a,button,input,select,textarea,summary,[contenteditable="true"],'+
    '[role="button"],[role="link"],[role="checkbox"],[role="radio"],[role="option"],[role="gridcell"]';
  for (const doc of frameView.documents) {
  for (const e of doc.querySelectorAll('tr,[role="row"]')) {
    if (!visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    if (e.querySelector(ROW_INTERACTIVE)) continue;
    if (getComputedStyle(e).cursor!=='pointer') continue;
    const g=cache.geometry(e, frameView.origin);
    if (!g) continue;
    const r=e.getBoundingClientRect();
    // role 报 'row' 而不是借用 'button'：它确实不是按钮，点它靠的是行自己的处理器。
    // 这个字符串只给模型看（browser.py 只看 kind），所以不必进上面的 roles 白名单——
    // 进了反而会让主循环无条件收录所有 [role=row]，把 cursor 这道闸门绕过去。
    actions.push({node:identity(e),role:'row',label:name(e)||'row',kind:'click',value:'',
      rect:{x:g.x-r.width/2,y:g.y-r.height/2,w:r.width,h:r.height},
      ...(g.covered?{covered:true}:{})});
  }
  }
  // 同源 frame 内的富文本编辑区：CKEditor 等把 contenteditable body 放进 iframe，
  // 顶层文档看到的只是 <iframe> 本身。把该 iframe 视为可填写目标——点击它的中心
  // 就会聚焦内部编辑区，随后的 CDP insertText 即可写入。无需坐标换算，也不是站点专用脚本。
  const frameLabel=el=>{
    const text=e=>(e.textContent||'').trim();
    // 编辑器自身的工具条文字不是字段名。E9 的自定义按钮用 wea-cbi-text，
    // 不以 cke_ 开头，只靠前缀过滤会把它误当成标签（实测取到"常用批示语"）。
    const chrome=e=>/^cke_/.test(e.className||'')||/cbi/.test(e.className||'') ||
      !!e.closest('[class*=cke_],[class*=cbi],[class*=wea-rich-text]');
    let fallback='';
    // 祖先要走到能覆盖字段名的层级：E9 的 .sign-label 在编辑器外层第 10 层。
    for (let depth=0, node=el.parentElement; depth<12 && node; depth++, node=node.parentElement) {
      const leaves=[...node.querySelectorAll('*')].filter(e=>
        !e.children.length && !e.closest('iframe') && !chrome(e) &&
        text(e).length>0 && text(e).length<=14);
      // 优先 class 里带 label 的元素（E9 用 .sign-label 承载字段名"签字意见"）
      const named=leaves.find(e=>/label/i.test(e.className||''));
      if (named) return text(named);
      if (!fallback&&leaves.length) fallback=text(leaves[0]);
    }
    return fallback||el.getAttribute('title')||el.getAttribute('aria-label')||'rich text editor';
  };
  for (const e of document.querySelectorAll('iframe')) {
    let body=null;
    try { body=e.contentDocument?.body; } catch { body=null; }
    if (!body || body.getAttribute('contenteditable')!=='true' || !visible(e)) continue;
    // 与原生控件不同，这里【不要求】必须落在当前视口内：
    // 高表单的富文本字段（E9 的签字意见在首屏下方）是常规情况，若因此不提供候选，
    // 模型看不到该字段，就会跳过它直接点提交。执行侧会先 scrollIntoView 再输入。
    const r=e.getBoundingClientRect();
    if (r.width<=0 || r.height<=0) continue;
    // 该分支刻意允许元素落在视口外（见上方注释），所以只有中心点确实在视口内时才做
    // 命中测试——否则 elementFromPoint 返回 null，会被误标成"被遮挡"。
    const fx=r.x+r.width/2, fy=r.y+r.height/2;
    const inView=fx>=0 && fy>=0 && fx<innerWidth && fy<innerHeight;
    const covered=inView && !e.contains(document.elementFromPoint(fx,fy));
    actions.push({node:identity(e),role:'textbox',label:frameLabel(e),kind:'fill',
      value:(body.innerText||'').trim().slice(0,200),rect:{x:r.x,y:r.y,w:r.width,h:r.height},
      ...(covered?{covered:true}:{})});
  }
  const words=[]; let length=0;
  // 正文也要取 frame 里的：模型"看得见页面"靠的就是这一段。流程设计画布的工具栏、
  // 节点标题、右侧「流程信息」面板都在同源 frame 内，不取就等于对模型隐藏了半张页面。
  // 视口判据照旧（只收真正看得见的），但坐标要先换算到顶层——frame 内的 rect 是内层坐标。
  for (const doc of frameView.documents) {
    const offset=frameView.origin.get(doc);
    if (!offset) continue;   // 被缩放的 frame：坐标不可信，正文也一并不取（与 geometry 同口径）
    const walker=doc.createTreeWalker(doc.body,NodeFilter.SHOW_TEXT);
    const range=doc.createRange(); let node;
    while ((node=walker.nextNode()) && length<6000) {
      const value=node.textContent.trim(), parent=node.parentElement;
      if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
      range.selectNodeContents(node); const r=range.getBoundingClientRect();
      const left=r.left+offset.dx, top=r.top+offset.dy;
      if (r.width>0 && r.height>0 && top+r.height>0 && top<innerHeight &&
          left+r.width>0 && left<innerWidth) {
        words.push(value); length+=value.length;
      }
    }
  }
  const text=words.join('\n').slice(0,6000), height=document.documentElement.scrollHeight;
  const page_key=cache.pageKey(), guards={};
  for (const a of actions) if (!(a.node in guards)) guards[a.node]=cache.guard(cache.nodes.get(a.node));
  // Compare meaning and identity. Geometry is always resolved and hit-tested just before input.
  //
  // `covered` 必须跟着 `rect` 一起剥掉：它同样是**几何**的产物（命中测试的结果），
  // 不是页面的语义内容。留在 marker 里会让"某个元素恰好被别的元素压住"这种纯几何变化
  // 算成整页变化——fresh(page) 于是假性为 false，决策被反复判过期、白白重新观察。
  // 实测（2026-09-24，check_guards 的移动目标检查）：按钮平移 200px 压住旁边的输入框，
  // marker 就因为那一个 covered 位从 false 变 true 而整体不等。
  // 模型侧不受影响：covered 照样进候选表（model.choose 从 state["actions"] 取）。
  const semantics=actions.map(({rect,covered,...action})=>action);
  const marker=[performance.timeOrigin,freshUrl(),scrollX,scrollY,innerWidth,innerHeight,
    document.title,text,semantics,page_key[6]];
  // 资源计时条目数：给「页面还在加载吗」当一个**观测值**（判据在 browser.py 的 wait_until_stable）。
  // 它只增不减，所以「它还在涨」等价于「还有请求在回来」。停滞期里它照样涨——2026-09-29 实测：
  // 新 E9 环境上 /wui/engine.html 有 13.5 秒看着一动不动，而这期间条目从 105 涨到 110（请求在飞、
  // 服务器回得慢），于是那一帧会被误判成「稳定」，模型拿着半渲染的列表就选了 DONE。
  // **不进 marker**：marker 是 fresh() 的判据，把它算进去会让「又加载了一张图」被判成页面已变，
  // 决策被反复作废、白花钱。
  const resources=performance.getEntriesByType('resource').length;
  const omitted_actions=Math.max(0,actions.length-250);
  actions.splice(250);
  actions.forEach((a,i)=>a.id='e'+(i+1));
  if (scrollY+innerHeight<height-2) actions.push({id:'scroll_down',kind:'scroll',label:'Scroll down',delta:560});
  if (scrollY>0) actions.push({id:'scroll_up',kind:'scroll',label:'Scroll up',delta:-560});
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  return {url:location.href,fresh_url:freshUrl(),title:document.title,w:innerWidth,h:innerHeight,text,
    scroll:{y:scrollY,height},actions,marker,page_key,guards,omitted_actions,resources};
})()
