/* A topology stays in one workspace from composition through execution. */
(() => {
  'use strict';
  const P = Platform, {el, api, button, field, input, select, checkbox, table} = P;
  const app = document.getElementById('app');
  const stateNames = {1:'未启动', 2:'配置中', 3:'启动中', 4:'运行中', 5:'停止中', 6:'已关闭'};
  const historyNames = {defined:'未启动', running:'曾运行', stopped:'已停止', closed:'已结束', interrupted:'已中断', superseded:'已被新运行替代', failed:'启动失败'};
  let data = {topologies:[], deployments:[], sessions:[], native_models:{}}, templates = [], native = [];
  let current = null, definition = null, dirty = false, selected = null, selectedLink = null;
  let view = 'edit', context = null, connect = false, firstEndpoint = null, optionsToken = 0;
  let query = '', filter = 'all', paletteQuery = '', auxiliary = false, opening = 0;
  const camera = {x:0, y:0, scale:1};
  P.navigation('topologies.html');
  const message = () => el('div', {'aria-live':'polite'});
  const allNodes = () => [...(definition?.nodes || []), ...(definition?.wlans || [])];
  const topologyRef = run => run?.saved_topology_id || run?.topology_id || run?.captured_topology_id;
  const runName = run => run?.display_name || run?.topology_name || run?.source?.name || run?.snapshot?.name || '历史拓扑';
  const editable = () => !!definition && view === 'edit';
  const stateLabel = session => stateNames[session.state] || '未知状态';
  const stamp = text => text ? new Date(text).toLocaleString('zh-CN', {hour12:false}) : '—';
  function changed() {
    dirty = true;
    const marker = document.getElementById('saveState');
    if (marker) marker.textContent = '有未保存修改';
  }
  function disabled(control, reason) { control.disabled = true; control.title = reason; return control; }
  function discard() { return !dirty || confirm('放弃当前尚未保存的拓扑修改？'); }
  async function refresh(renderPage = true) {
    const result = await api('/topology-workspace');
    data = result;
    if (renderPage) render();
  }
  function newDefinition(name = '新拓扑') { return {schema_version:2, name, nodes:[], links:[], wlans:[]}; }
  function normalize(def) {
    const result = P.clone(def);
    if (result.format !== 'core-xml') {
      result.nodes ??= []; result.links ??= []; result.wlans ??= [];
      for (const node of [...result.nodes, ...result.wlans]) {
        const x = Number(node.x ?? node.position?.x ?? 100), y = Number(node.y ?? node.position?.y ?? 100);
        node.x = Number.isFinite(x) ? x : 100;
        node.y = Number.isFinite(y) ? y : 100;
      }
    }
    return result;
  }
  function setDefinition(def, record = null, mode = 'edit', runContext = null) {
    Object.assign(camera, {x:0, y:0, scale:1});
    definition = normalize(def); current = record; view = mode; context = runContext;
    dirty = false; selected = allNodes()[0]?.id ?? null; selectedLink = null;
    connect = false; firstEndpoint = null; auxiliary = false;
    const session = mode === 'live' ? contextSession() : null;
    P.workspaceContext(session ? {sid:session.id, identity:session.identity, drill_id:session.drill_id, name:current?.name || definition.name} : null);
    render();
  }
  function open(record) {
    if (!discard()) return;
    opening++;
    setDefinition(record?.definition || newDefinition(), record ? P.clone(record) : null);
  }
  async function openSession(session, run = null) {
    if (!discard()) return;
    const token = ++opening;
    if (!session.identity) throw Error('会话身份暂不可用，请刷新后再打开。');
    const def = await api('/topology-workspace/sessions/' + session.id + '?identity=' + encodeURIComponent(session.identity));
    if (token !== opening) return;
    const record = data.topologies.find(r => r.topology_id === topologyRef(run)) || null;
    setDefinition(def, record, 'live', {sid:session.id, identity:session.identity, deployment_id:run?.deployment_id, topology_id:topologyRef(run)});
  }
  function openHistory(run) {
    if (!discard()) return;
    opening++;
    const def = P.clone(run.native_xml ? {format:'core-xml', name:'历史拓扑', xml_text:run.native_xml} : run.execution_definition || run.snapshot || run.source);
    if (!def) throw Error('此旧记录没有保存拓扑配置，请从原文件导入。');
    def.name = runName(run);
    setDefinition(def, data.topologies.find(r => r.topology_id === topologyRef(run)) || null, 'history', {deployment_id:run.deployment_id, topology_id:topologyRef(run)});
  }
  function associated(session) {
    return data.deployments.find(r => r.live && r.core_sid === session.id && r.drill_id === session.drill_id);
  }
  function contextSession() {
    return context?.sid ? data.sessions.find(s => s.id === context.sid && s.identity === context.identity) : null;
  }
  function relatedRuns() {
    return data.deployments.filter(r => current ? topologyRef(r) === current.topology_id || r.deployment_id === context?.deployment_id : r.deployment_id === context?.deployment_id);
  }
  function render() {
    optionsToken++;
    const heading = el('header', {class:'page-heading'}, el('div', {}, el('h1', {}, '拓扑管理'),
      el('p', {class:'muted'}, '添加节点、连接链路、配置服务，在同一个工作区保存和运行。')),
      el('div', {class:'actions'}, button('刷新状态', async () => {
        await refresh(false);
        if (view === 'live' && contextSession()) {
          const def = await api('/topology-workspace/sessions/' + context.sid + '?identity=' + encodeURIComponent(context.identity));
          definition = normalize(def);
        }
        render();
      }), button('导入', importFile), button('新建拓扑', () => open(), true)));
    app.replaceChildren(heading);
    if (data.runtime_error) app.append(el('p', {class:'notice error'}, data.runtime_error));
    if (!definition) renderList(); else renderWorkspace();
  }
  function cards() {
    const items = data.topologies.map(record => {
      const running = data.deployments.filter(r => topologyRef(r) === record.topology_id && r.live);
      const live = running.find(r => r.current_state === 4) || running[0];
      const sessions = live && data.sessions.find(s => s.id === live.core_sid && s.drill_id === live.drill_id);
      return {name:record.name, kind:'saved', record, session:sessions, run:live,
        status:data.runtime_error ? '运行状态待确认' : live ? live.current_status : '未运行',
        nodes:record.definition?.nodes?.length + (record.definition?.wlans?.length || 0) || 0,
        time:record.updated_at};
    });
    for (const session of data.sessions) {
      const run = associated(session);
      if (topologyRef(run) && data.topologies.some(r => r.topology_id === topologyRef(run))) continue;
      items.push({name:run ? runName(run) : '会话 ' + session.id, kind:'session', session, run,
        status:stateLabel(session), nodes:session.nodes, time:run?.created_at});
    }
    // A historical topology must remain discoverable even when its session and saved definition are gone.
    const archived = new Set();
    for (const run of data.deployments.slice().reverse()) {
      const key = topologyRef(run) || run.deployment_id;
      if (archived.has(key) || run.live || data.topologies.some(r => r.topology_id === topologyRef(run))) continue;
      archived.add(key);
      const def = run.source || run.snapshot || run.execution_definition;
      items.push({name:runName(run), kind:'history', run, status:'历史记录 · 会话已结束',
        nodes:(def?.nodes?.length || 0) + (def?.wlans?.length || 0), time:run.created_at});
    }
    return items;
  }
  function renderList() {
    const entries = cards();
    const search = input(query, value => { query = value; updateCards(); }); search.placeholder = '搜索拓扑名称';
    search.setAttribute('aria-label', '搜索拓扑');
    const choice = select([['all','全部拓扑'],['saved','已保存配置'],['session','未保存的活动会话'],['history','历史拓扑']], filter, value => {filter = value; updateCards();});
    const container = el('div', {class:'topology-cards'});
    app.append(el('section', {class:'surface'}, el('div', {class:'page-heading'}, el('div', {}, el('h2', {}, '拓扑列表'),
      el('p', {class:'muted'}, data.topologies.length + ' 个保存配置 · ' + data.sessions.length + ' 个活动会话 · 状态更新 ' + stamp(data.updated_at))),
      el('div', {class:'actions'}, search, field('显示范围', choice))), container));
    function updateCards() {
      const visible = entries.filter(item => (filter === 'all' || item.kind === filter) && item.name.toLowerCase().includes(query.toLowerCase()));
      container.replaceChildren(...visible.map(item => {
        const card = el('article', {class:'topology-card'}, el('div', {class:'page-heading'}, el('h3', {}, item.name),
          el('span', {class:'tag ' + (item.session?.state === 4 ? 'ok' : 'ot')}, item.status)),
          el('p', {class:'muted'}, (item.kind === 'saved' ? '已保存配置' : item.kind === 'session' ? '未保存配置 · 会话 ' + item.session.id : '历史配置') + ' · ' + item.nodes + ' 个节点'),
          el('p', {class:'muted'}, '更新于 ' + stamp(item.time)));
        const actions = el('div', {class:'actions'});
        if (item.record) actions.append(button('打开配置', () => open(item.record), true));
        if (item.session) actions.append(button('查看' + (item.session.state === 4 ? '运行' : '会话'), () => openSession(item.session, item.run), !item.record));
        if (item.kind === 'history') actions.append(button('查看历史配置', () => openHistory(item.run), true));
        if (item.kind === 'session') card.append(el('p', {class:'muted'}, '当前会话尚未归入保存的拓扑。打开后可另存配置。'));
        card.append(actions); return card;
      }));
      if (!visible.length) container.append(el('div', {class:'empty'}, entries.length ? '没有匹配的拓扑，请调整筛选。' : '还没有拓扑。点击“新建拓扑”添加节点，或导入已有文件。'));
    }
    updateCards();
    const history = el('details', {class:'surface'}, el('summary', {}, '全部运行历史（' + data.deployments.length + '）'));
    history.append(runTable(data.deployments)); app.append(history);
  }
  function renderWorkspace() {
    const session = contextSession(), run = session ? associated(session) : null;
    if(view === 'live' && !session)P.workspaceContext(null);
    const status = view === 'edit' ? (dirty ? '有未保存修改' : current ? '已保存 · 修订 ' + current.revision : '尚未保存') :
      view === 'history' ? '历史运行 · 只读' : session ? (run?.current_status || stateLabel(session)) + ' · 实际配置' : '会话已结束或被替换 · 当前保留最后查看的配置';
    const actions = el('div', {class:'actions'}, button('返回列表', () => {
      if (!discard()) return; opening++; definition = null; current = null; context = null; dirty = false; render();
    }));
    if (view === 'edit') {
      actions.append(button('保存', save, true), button('导出', exportFile),
        allNodes().length || definition.format === 'core-xml' ? button('启动', start) : disabled(button('启动', start), '请先添加节点'));
      if (current) actions.append(button('删除拓扑', async () => {
        if (!confirm('删除保存的拓扑配置？现有会话和运行历史继续保留。')) return;
        await api('/topologies/' + current.topology_id, 'DELETE');
        definition = null; current = null; dirty = false; await refresh();
      }));
    } else {
      actions.append(button('导出', exportFile), button(view === 'live' && !current ? '保存为拓扑' : '复制为可编辑拓扑', () => {
        const run = data.deployments.find(r => r.deployment_id === context?.deployment_id);
        const def = run?.native_xml ? {schema_version:2, format:'core-xml', xml_text:run.native_xml} : normalize(definition);
        def.name = current?.name || definition.name || '会话拓扑';
        const capture = view === 'live' && !current && contextSession() ? {...context, capture:true} : null;
        setDefinition(def, null, 'edit', capture); changed(); P.notice('已转为可编辑配置。保存后出现在拓扑列表，原会话不变。');
      }, true));
      if (current) actions.append(button('编辑下次运行配置', () => open(current)));
      if (session?.state === 1) actions.append(button(run?.status === 'stopped' ? '再次运行' : '启动会话', resume, true));
      if (session?.state === 4) actions.append(button('检查联通', async () => {
        const result = await sessionAction('connectivity');
        const labels = {reachable:'可达',unreachable:'不可达',unsupported:'当前环境不支持'};
        P.modal('联通检查结果', el('div', {}, el('p', {class:'notice'}, result.all_reachable ? '全部探测可达' : '部分探测未通过，请查看下方结果'),
          table(['源节点','目标地址','结果','详情'], result.results.map(r => [nodeName(r.source),r.target,labels[r.result] || r.result,r.detail]))));
      }), button('停止运行', async () => {
        if (!confirm('停止当前拓扑运行？保留拓扑配置和历史记录。')) return;
        await sessionAction('stop'); await refresh(); P.notice('运行已停止，拓扑配置与历史记录已保留。');
      }));
      if (session) actions.append(button('删除会话', async () => {
        if (!confirm('删除此会话及其运行资源？保存的拓扑和运行历史继续保留。')) return;
        await sessionAction('delete'); await refresh(); P.notice('会话已删除，当前保留最后查看的配置。');
      }));
    }
    app.append(el('section', {class:'surface topology-toolbar'}, actions,
      view === 'edit' ? field('拓扑名称', input(definition.name, value => {definition.name = value; changed();})) : el('h2', {}, definition.name || current?.name),
      el('p', {id:'saveState', class:'muted', role:'status'}, status),
      view === 'edit' ? el('p', {class:'muted'}, '保存不会改变正在运行的会话；启动时创建一次新的运行。') : null));
    if (definition.format === 'core-xml') {
      app.append(el('section', {class:'surface'}, el('h2', {}, '原生 XML 拓扑'), el('p', {class:'notice'},
        '原始 XML 配置完整保留，可导出、保存并启动。启动后在此查看实际节点和链路。修改原始 XML 请使用外部编辑器后重新导入。')));
    } else {
      const palette = nodePalette();
      const center = el('section', {class:'topology-center'});
      const connectButton = button(connect ? '退出连线' : '连线', () => {connect = !connect; firstEndpoint = null; render();}, connect);
      connectButton.setAttribute('aria-pressed', String(connect));
      if (!editable() || allNodes().length < 2) disabled(connectButton, editable() ? '至少添加两个节点后可连线' : '实际运行与历史配置为只读，请编辑下次运行配置');
      center.append(el('div', {class:'actions'}, connectButton,
        button(auxiliary ? '收起节点与链路表' : '节点与链路表', () => {auxiliary = !auxiliary; render();})),
        el('p', {class:'canvas-hint', role:'status'}, connect ? firstEndpoint === null ? '连线模式：点击第一个节点，Esc 取消。' : '已选择 ' + nodeName(firstEndpoint) + '，点击另一个节点完成连接。' :
          editable() ? '拖动节点调整位置，点击节点或链路编辑属性。' : '点选节点或链路查看属性。'), canvas());
      const inspector = el('aside', {class:'topology-inspector', 'aria-label':'属性面板'});
      if (selectedLink !== null && definition.links[selectedLink]) linkInspector(inspector, definition.links[selectedLink]);
      else if (selected !== null) {
        const node = allNodes().find(n => n.id === selected);
        if (node) {
          inspector.append(el('h3', {}, '节点属性'));
          const basics = el('fieldset', {disabled:!editable()}, field('节点名称', input(node.name, value => {node.name = value; changed(); updateNodeLabel(node);})));
          inspector.append(basics);
          const details = el('fieldset', {disabled:!editable()}); inspector.append(details);
          nodeEditor(details).catch(error => {if (details.isConnected) P.localMessage(details, error.message, true);});
          if (editable()) inspector.append(button('删除节点', () => removeNode(node.id)));
          else if (view === 'live' && session?.state === 4 && ['default','router','host','PC','pc','docker','podman'].includes(node.type)) inspector.append(nodeOperations(node, session));
        }
      } else inspector.append(el('h3', {}, '属性'), el('p', {class:'muted'}, '点击节点或链路查看属性。'));
      app.append(el('div', {class:'topology-workspace'}, palette, center, inspector));
      if (auxiliary) app.append(auxiliaryTables());
    }
    const history = el('details', {class:'surface'}, el('summary', {}, '当前拓扑的运行记录（' + relatedRuns().length + '）'));
    history.append(runTable(relatedRuns())); app.append(history);
  }
  function nodeName(id) { return allNodes().find(n => n.id === id)?.name || '#' + id; }
  function updateNodeLabel(node) {
    const control = document.querySelector('[data-node-id="' + node.id + '"]');
    if (control) {control.querySelector('strong').textContent = node.name; control.setAttribute('aria-label', '选择节点 ' + node.name);}
  }
  function nodeKind(node) {
    if (definition.wlans?.includes(node)) return '无线网络';
    if (node.instance) return templates.find(t => t.name === node.instance)?.runtime_type === 'netns' ? '原生模板' : 'Docker 模板';
    return {switch:'交换机', hub:'集线器', wlan:'无线网络', default:'原生节点', router:'原生路由器', host:'原生主机', docker:'Docker 节点'}[node.type] || '原生节点';
  }
  function nodePalette() {
    const area = el('aside', {class:'topology-palette', 'aria-label':'节点库'}, el('h3', {}, '节点库'));
    if (!editable()) {area.append(el('p', {class:'muted'}, '当前为只读视图。编辑配置后可添加节点。')); return area;}
    const contents = el('div');
    const search = input(paletteQuery, value => {paletteQuery = value; fill();}); search.placeholder = '搜索模板或节点'; search.setAttribute('aria-label', '搜索节点库');
    area.append(search, contents);
    function fill() {
      contents.replaceChildren();
      const matches = name => name.toLowerCase().includes(paletteQuery.toLowerCase());
      for (const [kind, label] of [['docker','Docker 模板'],['netns','原生模板']]) {
        const items = templates.filter(t => t.runtime_type === kind && matches(t.name));
        contents.append(el('h4', {}, label + '（' + items.length + '）'));
        for (const template of items) {
          const control = button(template.name, () => addNode(template)); control.setAttribute('aria-label', '添加模板 ' + template.name);
          contents.append(control, el('small', {class:'muted'}, '修订 ' + template.version + (template.image_tag ? ' · ' + template.image_tag : '')));
        }
        if (!items.length) contents.append(el('p', {class:'muted'}, templates.some(t => t.runtime_type === kind) ? '没有匹配模板' : '暂无此类模板'));
      }
      contents.append(el('h4', {}, '原生节点 · 无需模板'));
      for (const [label, model, services] of [['原生主机','bare',[]],['原生路由器','bare',['IPForward']]]) {
        if (matches(label)) contents.append(button('添加' + label, () => addNative(label, {type:'default', model, services:services.filter(s => native.includes(s))})));
      }
      contents.append(el('h4', {}, '网络构件'));
      for (const [type, label] of [['switch','交换机'],['hub','集线器'],['wlan','无线网络']]) {
        if (matches(label)) contents.append(button('添加' + label, () => addNative(label, {type, model:'bare', ...(type === 'wlan' ? {range:'275', bandwidth:'54000000', delay:'5000', jitter:'0', error:'0'} : {})})));
      }
    }
    fill(); return area;
  }
  function nextId() { return Math.max(0, ...allNodes().map(n => Number(n.id))) + 1; }
  function addNode(template) {
    if (!editable()) return;
    const id = nextId();
    definition.nodes.push({id, name:template.name + ' ' + id, instance:template.name, instance_version:template.version,
      x:50 + ((id - 1) % 3) * 180, y:60 + Math.floor((id - 1) / 3) * 100});
    selected = id; selectedLink = null; changed(); render();
  }
  function addNative(label, fields) {
    if (!editable()) return;
    const id = nextId(), node = {id, name:label + ' ' + id, x:50 + ((id - 1) % 3) * 180, y:60 + Math.floor((id - 1) / 3) * 100, ...P.clone(fields)};
    (fields.type === 'wlan' ? definition.wlans : definition.nodes).push(node);
    selected = id; selectedLink = null; changed(); render();
  }
  function removeNode(id) {
    definition.nodes = definition.nodes.filter(n => n.id !== id); definition.wlans = definition.wlans.filter(n => n.id !== id);
    definition.links = definition.links.filter(l => l.node1_id !== id && l.node2_id !== id);
    selected = null; selectedLink = null; firstEndpoint = null; changed(); render();
  }
  function chooseNode(id) {
    if (!connect || !editable()) {selected = id; selectedLink = null; render(); return;}
    if (firstEndpoint === null) {firstEndpoint = id; selected = id; selectedLink = null; render(); return;}
    if (firstEndpoint === id) {P.notice('请选择另一个节点；按 Esc 可取消连线。', true); return;}
    if (definition.links.some(l => [l.node1_id,l.node2_id].includes(firstEndpoint) && [l.node1_id,l.node2_id].includes(id))) {
      P.notice('这两个节点已有链路。点击画布上的链路即可修改。', true); return;
    }
    const link = {node1_id:firstEndpoint, node2_id:id};
    for (const [key, nodeId] of [['iface1',firstEndpoint],['iface2',id]]) {
      const node = allNodes().find(n => n.id === nodeId);
      if (['switch','hub','wlan'].includes(node.type) || definition.wlans.includes(node)) continue;
      const used = definition.links.flatMap(l => [['node1_id','iface1'],['node2_id','iface2']].filter(([end]) => l[end] === nodeId).map(([,iface]) => l[iface]?.id)).map((id,index) => id ?? index);
      const ifaceId = Math.max(-1, ...used) + 1;
      const host = (nodeId - 1) % 250 + 1, network = 90 + Math.floor((nodeId - 1) / 250) % 150;
      link[key] = {id:ifaceId, ip4:'10.' + network + '.' + (ifaceId % 250) + '.' + host, ip4_mask:24};
    }
    definition.links.push(link); selectedLink = definition.links.length - 1; selected = null;
    connect = false; firstEndpoint = null; changed(); P.notice('链路已添加，可在右侧调整接口地址和参数。'); render();
  }
  function canvas() {
    const nodes = allNodes();
    const byId = new Map(nodes.map(node => [node.id, node]));
    const controls = new Map();
    const area = el('div', {class:'topology-canvas', role:'region', tabIndex:0,
      'aria-label':'拓扑画布', 'aria-describedby':'canvas-controls-hint'});
    const stage = el('div', {class:'topology-stage'});
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '1');
    svg.setAttribute('height', '1');
    stage.append(svg);

    const zoomLabel = el('output', {class:'topology-zoom-label', 'aria-label':'缩放比例'});
    const minZoom = 0.05, maxZoom = 3;
    function updateCamera() {
      stage.style.transform = 'translate(' + camera.x + 'px, ' + camera.y + 'px) scale(' + camera.scale + ')';
      area.style.backgroundPosition = camera.x + 'px ' + camera.y + 'px';
      const grid = Math.max(6, 18 * camera.scale);
      area.style.backgroundSize = grid + 'px ' + grid + 'px';
      zoomLabel.value = Math.round(camera.scale * 100) + '%';
    }
    function zoomAt(x, y, scale) {
      scale = Math.max(minZoom, Math.min(maxZoom, scale));
      const ratio = scale / camera.scale;
      camera.x = x - (x - camera.x) * ratio;
      camera.y = y - (y - camera.y) * ratio;
      camera.scale = scale;
      updateCamera();
    }
    function zoomBy(factor) {
      zoomAt(area.clientWidth / 2, area.clientHeight / 2, camera.scale * factor);
    }
    function resetCamera() {
      Object.assign(camera, {x:0, y:0, scale:1});
      updateCamera();
    }
    function fitNodes() {
      if (!nodes.length) {resetCamera(); return;}
      const left = Math.min(...nodes.map(node => node.x));
      const top = Math.min(...nodes.map(node => node.y));
      const width = Math.max(...nodes.map(node => node.x + controls.get(node.id).offsetWidth)) - left;
      const height = Math.max(...nodes.map(node => node.y + controls.get(node.id).offsetHeight)) - top;
      camera.scale = Math.min(1, Math.max(1, area.clientWidth - 48) / width, Math.max(1, area.clientHeight - 48) / height);
      camera.x = (area.clientWidth - width * camera.scale) / 2 - left * camera.scale;
      camera.y = (area.clientHeight - height * camera.scale) / 2 - top * camera.scale;
      updateCamera();
    }
    const zoomButton = (label, text, action) => el('button', {type:'button', class:'btn', title:label, 'aria-label':label, onclick:action}, text);
    const toolbar = el('div', {class:'topology-view-controls', role:'group', 'aria-label':'画布视图'},
      zoomButton('缩小', '−', () => zoomBy(1 / 1.2)), zoomLabel,
      zoomButton('放大', '+', () => zoomBy(1.2)),
      zoomButton('适应画布', '适应画布', fitNodes), zoomButton('重置视图', '重置视图', resetCamera));

    let gesture = null;
    function beginGesture(event, node = null) {
      if (gesture || !event.isPrimary) return;
      const target = event.currentTarget;
      gesture = {id:event.pointerId, target, x:event.clientX, y:event.clientY, node,
        offsetX:camera.x, offsetY:camera.y, scale:camera.scale, nodeX:node?.x, nodeY:node?.y, moved:false};
      target.setPointerCapture(event.pointerId);
      if (!node) {
        event.preventDefault();
        area.focus({preventScroll:true});
      }
    }
    area.addEventListener('pointerdown', event => {
      if (event.button === 1 || (event.button === 0 && !event.target.closest('.canvas-node, svg path'))) beginGesture(event);
    });
    area.addEventListener('pointermove', event => {
      if (gesture?.id !== event.pointerId) return;
      const dx = event.clientX - gesture.x, dy = event.clientY - gesture.y;
      if (!gesture.moved && Math.hypot(dx, dy) < 4) return;
      gesture.moved = true;
      if (gesture.node) {
        const {node, target, nodeX, nodeY, scale} = gesture;
        node.x = Math.round(nodeX + dx / scale);
        node.y = Math.round(nodeY + dy / scale);
        target.style.left = node.x + 'px';
        target.style.top = node.y + 'px';
        drawEdges();
      } else {
        area.classList.add('panning');
        camera.x = gesture.offsetX + dx;
        camera.y = gesture.offsetY + dy;
        updateCamera();
      }
    });
    function finishGesture(event, cancelled) {
      if (gesture?.id !== event.pointerId) return;
      const {node, target, nodeX, nodeY, moved} = gesture;
      gesture = null;
      area.classList.remove('panning');
      if (!node || !moved) return;
      if (cancelled) {
        node.x = nodeX; node.y = nodeY;
        target.style.left = node.x + 'px'; target.style.top = node.y + 'px';
        drawEdges();
      } else {
        if (node.x !== nodeX || node.y !== nodeY) changed();
        selected = node.id; selectedLink = null; render();
      }
    }
    area.addEventListener('pointerup', event => finishGesture(event, false));
    area.addEventListener('pointercancel', event => finishGesture(event, true));
    area.addEventListener('lostpointercapture', event => finishGesture(event, true));
    area.addEventListener('dragstart', event => event.preventDefault());
    area.addEventListener('wheel', event => {
      const mode = event.deltaMode;
      if (!event.deltaY) return;
      event.preventDefault();
      if (gesture) return;
      const rect = area.getBoundingClientRect();
      const unit = mode === 1 ? 16 : mode === 2 ? area.clientHeight : 1;
      const delta = Math.max(-250, Math.min(250, event.deltaY * unit));
      zoomAt(event.clientX - rect.left - area.clientLeft, event.clientY - rect.top - area.clientTop, camera.scale * Math.exp(-delta / 500));
    }, {passive:false});
    area.addEventListener('keydown', event => {
      if (event.target !== area || gesture) return;
      const direction = {ArrowLeft:[40,0], ArrowRight:[-40,0], ArrowUp:[0,40], ArrowDown:[0,-40]}[event.key];
      if (direction) {
        camera.x += direction[0]; camera.y += direction[1]; updateCamera();
      } else if (event.key === '+' || event.key === '=') zoomBy(1.2);
      else if (event.key === '-') zoomBy(1 / 1.2);
      else if (event.key === '0') resetCamera();
      else return;
      event.preventDefault();
    });
    function drawEdges() {
      svg.replaceChildren();
      definition.links.forEach((link, index) => {
        const a = byId.get(link.node1_id), b = byId.get(link.node2_id);
        if (!a || !b) return;
        const ax = a.x + 65, ay = a.y + 29, bx = b.x + 65, by = b.y + 29;
        const line = document.createElementNS(svg.namespaceURI, 'line');
        for (const [key, value] of Object.entries({x1:ax, y1:ay, x2:bx, y2:by, 'stroke-width':selectedLink === index ? 5 : 3, stroke:selectedLink === index ? 'var(--accent)' : '#94a3b8'})) line.setAttribute(key, value);
        const hit = document.createElementNS(svg.namespaceURI, 'path');
        hit.setAttribute('d', 'M' + ax + ',' + ay + ' L' + bx + ',' + by);
        hit.setAttribute('stroke', 'transparent'); hit.setAttribute('stroke-width', '16'); hit.setAttribute('fill', 'none');
        hit.setAttribute('vector-effect', 'non-scaling-stroke');
        hit.setAttribute('role', 'button'); hit.setAttribute('tabindex', '0'); hit.setAttribute('aria-label', '链路 ' + a.name + ' — ' + b.name);
        const choose = () => {selectedLink = index; selected = null; connect = false; firstEndpoint = null; render();};
        hit.addEventListener('click', choose); hit.addEventListener('keydown', e => {if (e.key === 'Enter' || e.key === ' ') {e.preventDefault(); choose();}});
        svg.append(line, hit);
      });
    }
    for (const node of nodes) {
      const movable = editable() && !connect;
      const control = el('button', {type:'button', class:'canvas-node' + (movable ? ' movable' : '') + (selected === node.id ? ' selected' : '') + (firstEndpoint === node.id ? ' endpoint' : ''),
        'data-node-id':node.id, 'aria-label':'选择节点 ' + node.name, 'aria-pressed':String(selected === node.id), style:'left:' + node.x + 'px;top:' + node.y + 'px'},
        el('strong', {}, node.name), el('small', {}, nodeKind(node)));
      control.addEventListener('pointerdown', event => {
        if (movable && event.button === 0) beginGesture(event, node);
      });
      control.addEventListener('click', () => chooseNode(node.id));
      controls.set(node.id, control);
      stage.append(control);
    }
    drawEdges();
    if (!nodes.length) stage.append(el('p', {class:'canvas-empty'}, '从左侧添加模板、原生主机或网络构件，再点击“连线”连接两个节点。'));
    area.append(stage);
    updateCamera();
    return el('div', {}, toolbar,
      el('p', {id:'canvas-controls-hint', class:'muted topology-view-hint'}, '拖动空白处平移，滚轮缩放。聚焦画布后可用方向键、+、− 和 0。'), area);
  }
  function nativeEditor(area, node) {
    const wlan = definition.wlans.includes(node);
    area.append(el('p', {class:'muted'}, nodeKind(node) + ' · 不依赖节点模板'));
    if (wlan) {
      for (const [key, label] of [['range','无线范围 m'],['bandwidth','无线带宽 bps'],['delay','无线时延 µs'],['jitter','无线抖动 µs'],['error','无线错误率 %']]) {
        area.append(field(label, input(node[key], value => {node[key] = value; changed();}, 'number')));
      }
    } else if (['default','router','host','PC'].includes(node.type || 'default')) {
      area.append(field('节点型号', select([['bare','裸节点（服务完全按下方选择）'],['PC','PC'],['host','主机'],['router','路由器'],['mdr','无线多播路由器']], node.model || 'bare', value => {
        node.model = value; const defaults = data.native_models[value] || [];
        node.services = defaults.filter(name => native.includes(name));
        const missing = defaults.filter(name => !native.includes(name));
        changed(); render();
        if (missing.length) P.notice('当前环境未提供这些型号默认服务：' + missing.join('、') + '。已跳过，请检查下方启用项。', true);
      })), el('h4', {}, 'CORE 原生服务'));
      const chosen = new Set(node.services || []);
      const names = [...new Set([...native, ...chosen])].sort();
      area.append(el('p', {class:'muted'}, '仅显示 CORE 原生服务。需关闭全部服务时请选择“裸节点”型号。'),
        el('div', {class:'native-service-list'}, names.map(name => checkbox(chosen.has(name), enabled => {
          if (enabled) chosen.add(name); else chosen.delete(name);
          node.services = [...chosen]; changed();
        }, name))));
    } else area.append(el('p', {class:'muted'}, '网络构件不使用镜像服务或主机服务。'));
  }
  async function nodeEditor(area){const token=optionsToken,node=allNodes().find(n=>n.id===selected);if(!node)return;area.append(el('h3',{},node.name+' · 参数'));if(!node.instance){nativeEditor(area,node);return;}
    const result=await api('/instances/'+encodeURIComponent(node.instance)+'/service-options?version='+node.instance_version);if(token!==optionsToken||!area.isConnected)return;
    const templatePicker=select(templates.map(t=>[t.name,t.name+' · '+(t.runtime_type==='docker'?'Docker':'原生')]),node.instance,async value=>{try{const template=templates.find(t=>t.name===value);node.instance=value;node.instance_version=template.version;for(const key of ['service_overrides','services','image','image_ref','target_name','target_version','effective_services','service_selection','model','type'])delete node[key];changed();render();}catch(error){P.notice(error.message,true);}});
    area.append(field('节点模板',templatePicker),el('p',{class:'muted'},'固定修订 '+node.instance_version+'；模板更新不会自动改变此节点。'));
    const latest=templates.find(t=>t.name===node.instance);if(latest?.version>node.instance_version)area.append(button('查看并采用模板新修订',async()=>{const newer=await api('/instances/'+encodeURIComponent(node.instance)+'/service-options?version='+latest.version);const comparison=el('section',{class:'surface'},el('h3',{},'模板更新差异'),P.fields({原默认服务:result.template.services,新默认服务:newer.template.services,节点覆盖将被清除:node.service_overrides!==undefined}),button('取消',()=>comparison.remove()),button('采用新默认',()=>{node.instance_version=latest.version;for(const key of ['service_overrides','services','image','image_ref','target_name','target_version','effective_services','service_selection','model','type'])delete node[key];changed();render();},true));area.append(comparison);}));
    if(!result.catalog){area.append(el('p',{class:'notice'},'旧模板按原有实现执行。迁移服务清单并保存新模板后可配置节点覆盖。'));return;}
    const native=result.catalog.origin==='core';let chosen=P.clone(node.service_overrides??result.selection),custom=node.service_overrides!==undefined&&node.service_overrides!==null;const serviceArea=el('div'),checks=message();
    async function validate(){if(native){P.localMessage(checks,'CORE 原生服务单独选择。');return;}try{const validated=await api('/image-services/node/validate','POST',{services:result.catalog.services,selection:chosen});if(token!==optionsToken||!area.isConnected)return;P.localMessage(checks,[...(validated.errors||[]),...(validated.warnings||[]),...(validated.errors?.length?[]:['启动顺序：'+validated.order.map(id=>result.catalog.services.find(s=>s.id===id)?.name).join(' → ')])].join('；'),!!validated.errors?.length);}catch(error){if(token===optionsToken&&area.isConnected)P.localMessage(checks,error.message,true);}}
    function update(){node.service_overrides=P.clone(chosen);delete node.services;delete node.effective_services;delete node.service_selection;changed();validate();}
    function renderServices(){serviceArea.replaceChildren(table(['启用','服务','有效模式','来源'],result.catalog.services.map(service=>{const mode=select([['','请选择模式'],...service.modes.map(m=>[m.id,m.name])],chosen[service.id]||'',value=>{chosen[service.id]=value||'__choose__';update();});mode.disabled=!custom||!chosen[service.id];const enabled=checkbox(!!chosen[service.id],value=>{chosen[service.id]=value?(service.modes.find(m=>m.default)?.id||'__choose__'):null;update();renderServices();});enabled.querySelector('input').disabled=!custom;return [enabled,service.name,mode,custom?'此节点覆盖':'模板默认'];})));}
    area.append(field('配置来源',select([['inherit','使用模板默认'],['custom','自定义此节点']],custom?'custom':'inherit',value=>{custom=value==='custom';chosen=P.clone(result.selection);if(custom)node.service_overrides=P.clone(chosen);else delete node.service_overrides;delete node.services;changed();renderServices();validate();})),button('恢复模板默认',()=>{delete node.service_overrides;delete node.services;changed();render();}),serviceArea,checks);renderServices();if(view!=='edit')area.disabled=true;else validate();
  }
  function linkInspector(area, link) {
    area.append(el('h3', {}, '链路属性'), el('p', {class:'muted'}, nodeName(link.node1_id) + ' ↔ ' + nodeName(link.node2_id)));
    const form = el('fieldset', {disabled:!editable()});
    for (const [key, endpoint] of [['iface1','node1_id'],['iface2','node2_id']]) {
      form.append(el('h4', {}, nodeName(link[endpoint]) + ' · 接口'));
      // Rendering imported data must not introduce empty overrides or erase automatic IP assignment.
      const legacy = property => link[key]?.[property] ?? link[key + '_' + property];
      function write(property, value) {
        link[key] ??= {}; link[key][property] = value;
        delete link[key + '_' + property]; changed();
      }
      const ip4 = input(legacy('ip4') || '', value => write('ip4', value.trim() || null));
      ip4.placeholder = '留空使用自动分配';
      form.append(field('IPv4 · ' + nodeName(link[endpoint]), ip4),
        field('IPv4 掩码位 · ' + nodeName(link[endpoint]), input(legacy('ip4_mask') ?? 24, value => write('ip4_mask', Number(value)), 'number')),
        field('IPv6 · ' + nodeName(link[endpoint]), input(link[key]?.ip6 || '', value => write('ip6', value.trim() || null))),
        field('IPv6 掩码位 · ' + nodeName(link[endpoint]), input(link[key]?.ip6_mask ?? 64, value => write('ip6_mask', Number(value)), 'number')));
      const extra = el('details', {}, el('summary', {}, '接口高级参数'));
      for (const [property, label, type] of [['id','接口 ID','number'],['name','接口名称','text'],['mac','MAC 地址','text'],['mtu','MTU（留空自动）','number']]) {
        extra.append(field(label + ' · ' + nodeName(link[endpoint]), input(property === 'mtu' && !link[key]?.mtu ? '' : link[key]?.[property] ?? '', value => write(property, value === '' ? null : type === 'number' ? Number(value) : value), type)));
      }
      form.append(extra);
    }
    const advanced = el('details', {}, el('summary', {}, '更多链路参数'));
    for (const [key, label] of [['delay','时延 µs'],['bandwidth','带宽 bps'],['loss','丢包率 %'],['jitter','抖动 µs'],['dup','重复率 %'],['buffer','缓冲队列'],['burst','突发大小']]) {
      const control = field(label, input(link.options?.[key] ?? link[key] ?? 0, value => {
        link.options ??= {}; link.options[key] = Number(value); delete link[key]; changed();
      }, 'number'));
      (['delay','bandwidth'].includes(key) ? form : advanced).append(control);
    }
    form.append(advanced); area.append(form);
    if (editable()) area.append(button('删除链路', () => {
      definition.links.splice(selectedLink, 1); selectedLink = null; changed(); render();
    }));
  }
  function auxiliaryTables() {
    return el('section', {class:'surface'}, el('h3', {}, '节点与链路表'),
      table(['节点','类型','模板','操作'], allNodes().map(node => [node.name, nodeKind(node), node.instance || '无需模板',
        button('选择节点', () => {selected = node.id; selectedLink = null; render();})])),
      table(['端点 A','端点 B','操作'], definition.links.map((link, index) => [nodeName(link.node1_id), nodeName(link.node2_id),
        button('选择链路', () => {selectedLink = index; selected = null; render();})])));
  }
  function runTable(runs) {
    return table(['拓扑','运行时间','状态','操作'], runs.slice().reverse().map(run => {
      const record = data.topologies.find(r => r.topology_id === topologyRef(run));
      const session = run.live && data.sessions.find(s => s.id === run.core_sid && s.drill_id === run.drill_id);
      const actions = el('div', {class:'actions'}, button('查看本次配置', () => openHistory(run)));
      if (session) actions.append(button('查看会话', () => openSession(session, run)));
      return [runName(run), stamp(run.created_at),
        el('div', {}, run.current_status, el('small', {class:'muted'}, session ? '会话 ' + session.id : '最后记录：' + (historyNames[run.status] || run.status)),
          run.error ? el('p', {class:'notice error'}, run.error.length > 180 ? run.error.slice(0,180) + '…' : run.error) : null,
          run.diagnostics?.length || run.error?.length > 180 ? el('details', {}, el('summary', {}, '启动诊断'), el('pre', {class:'topology-diagnostics'}, (run.diagnostics || [run.error]).join('\n'))) : null), actions];
    }));
  }
  async function sessionAction(action) {
    const session = contextSession();
    if (!session) throw Error('会话已结束或被替换，请刷新状态。');
    return api('/topology-workspace/sessions/' + session.id + '/' + action, 'POST', {identity:session.identity});
  }
  async function checkedRuntime(expected, states = [4]) {
    const fresh = await api('/topology-workspace');
    const session = fresh.sessions.find(s => s.id === expected.sid && s.identity === expected.identity);
    if (!session) throw Error('会话已结束或被替换，请刷新状态。');
    if (!states.includes(session.state)) throw Error('会话状态已变化，请刷新后操作。');
    data = fresh;
    return session;
  }
  function nodeOperations(node, session) {
    const expected = {sid:session.id, identity:session.identity};
    const terminalUrl = '/static/term.html?' + new URLSearchParams({sid:session.id,nid:node.id,name:node.name,identity:session.identity});
    const terminal = el('a', {href:terminalUrl,target:'_blank',rel:'noopener',class:'btn'}, '终端');
    return el('section', {class:'node-operations'}, el('h4', {}, '节点操作'),
      el('div', {class:'actions'}, terminal, button('执行命令', () => commandDialog(node, expected))));
  }
  function commandDialog(node, expected) {
    const output = el('pre', {class:'node-command-output','aria-live':'polite'}), message = el('div', {role:'status'});
    const command = input('', () => {}); command.placeholder = '输入节点内执行的命令';
    const modal = P.modal(node.name + ' · 执行命令', el('div', {}, field('节点命令', command), message, output));
    const execute = button('执行', async () => {
      const text = command.value.trim(); if (!text) {P.localMessage(message,'请先输入命令',true); return;}
      try {
        await checkedRuntime(expected);
        const actual = await api('/topology-workspace/sessions/' + expected.sid + '?identity=' + encodeURIComponent(expected.identity));
        if (!actual.nodes.some(n => n.id === node.id)) throw Error('节点已不存在，请刷新拓扑。');
        P.localMessage(message, '正在执行…');
        const result = await api('/api/sessions/' + expected.sid + '/nodes/' + node.id + '/command', 'POST', {command:text,wait:true,shell:true});
        output.textContent = '$ ' + text + '\n' + (result.output || '') + '\n退出码：' + result.return_code;
        P.localMessage(message, result.return_code === 0 ? '执行完成' : '命令返回非零退出码', result.return_code !== 0);
      } catch (error) {P.localMessage(message,error.message,true);}
    }, true);
    command.addEventListener('keydown', event => {if(event.key === 'Enter'){event.preventDefault();execute.click();}});
    modal.footer.append(execute); command.focus();
  }
  async function resume() {
    const expected = {...context};
    if (!confirm('按当前会话配置再次运行？不会自动采用模板的新修订，也不会创建新的 CORE 会话。')) return;
    await checkedRuntime(expected, [1]);
    const result = await api('/api/sessions/' + expected.sid + '/start', 'POST', {});
    if (!result.result) {
      P.modal('运行诊断', el('pre', {class:'topology-diagnostics'}, (result.exceptions || ['启动未完成']).join('\n')));
      throw Error('启动未完成，请查看运行诊断。');
    }
    await refresh(false);
    const session = data.sessions.find(s => s.id === expected.sid && s.identity === expected.identity);
    if (!session) throw Error('会话身份已变化，请刷新状态。');
    await openSession(session, associated(session)); P.notice('已恢复运行。');
  }
  function validateLocal() {
    if (!definition.name?.trim()) throw Error('请填写拓扑名称');
    if (definition.format === 'core-xml') return;
    for (const node of allNodes()) {
      if (!node.name?.trim()) throw Error('请填写节点 #' + node.id + ' 的名称');
      if (!node.instance && ['PC','host','router','mdr'].includes(node.model) && !node.services?.length) {
        throw Error(node.name + ' 未选择服务。关闭全部原生服务时，请将节点型号改为“裸节点”。');
      }
    }
  }
  async function save() {
    if (!editable()) throw Error('请先打开可编辑配置');
    validateLocal();
    const record = !current && context?.capture ?
      await api('/topology-workspace/sessions/' + context.sid + '/capture', 'POST', {identity:context.identity, definition}) :
      await api(current ? '/topologies/' + current.topology_id : '/topologies', current ? 'PUT' : 'POST', {definition, revision:current?.revision});
    current = record; definition = normalize(record.definition); dirty = false;
    P.notice('拓扑已保存。'); await refresh(); return record;
  }
  async function start() {
    if (!editable()) return;
    validateLocal();
    const active = current && data.deployments.filter(run => topologyRef(run) === current.topology_id && run.live).length;
    if (!confirm('启动“' + definition.name + '”？\n' + allNodes().length + ' 个节点，' + (definition.links?.length || 0) + ' 条链路。\n' +
      (dirty || !current ? '当前修改会先保存。\n' : '') + (active ? '此拓扑已有 ' + active + ' 个活动会话。\n' : '') + '将创建新的 CORE 会话，已有运行不变。')) return;
    if (dirty || !current) await save();
    try {
      const run = await api('/topologies/' + current.topology_id + '/deploy', 'POST', {revision:current.revision, start:true});
      await refresh(false);
      const session = data.sessions.find(s => s.id === run.core_sid && s.drill_id === run.drill_id);
      if (!session) {render(); throw Error('已提交启动，但会话状态尚未确认，请刷新状态。');}
      // This is a deliberate transition after saving, so no unsaved-change prompt is needed.
      await openSession(session, run); P.notice('拓扑已启动，当前显示会话 ' + run.core_sid + ' 的实际配置。');
    } catch (error) {await refresh(); throw error;}
  }
  function importFile() {
    const control = el('input', {type:'file', accept:'.json,.xml', onchange:async event => {
      try {
        const file = event.target.files[0]; if (!file) return;
        const text = await file.text();
        let def = file.name.toLowerCase().endsWith('.xml') ? {schema_version:2, format:'core-xml', name:file.name.replace(/\.xml$/i, ''), xml_text:text} : JSON.parse(text);
        def = def.definition || def;
        if (!def || typeof def !== 'object' || Array.isArray(def) || (def.format !== 'core-xml' && !Array.isArray(def.nodes))) throw Error('文件中没有有效的拓扑节点列表');
        if (!discard()) return;
        opening++; setDefinition(def); changed(); P.notice('文件已导入。检查节点和链路后保存；尚未创建运行会话。');
      } catch (error) {P.notice(error.message, true);}
    }}); control.click();
  }
  function exportFile() {
    if (!definition) return;
    const xml = definition.format === 'core-xml';
    P.download(new Blob([xml ? definition.xml_text : JSON.stringify(definition, null, 2)], {type:xml ? 'application/xml' : 'application/json'}),
      (current?.name || definition.name || 'topology') + (xml ? '.xml' : '.json'));
  }
  P.guardNavigation(() => dirty, '放弃当前尚未保存的拓扑修改？');
  window.addEventListener('keydown', event => {if (event.key === 'Escape' && connect) {connect = false; firstEndpoint = null; render();}});
  async function load() {
    const results = await Promise.allSettled([api('/topology-workspace'), api('/instances'), api('/instances/available-services')]);
    if (results[0].status === 'rejected') throw results[0].reason;
    data = results[0].value;
    templates = results[1].status === 'fulfilled' ? results[1].value : [];
    native = results[2].status === 'fulfilled' ? results[2].value.system : [];
    const sid = Number(new URLSearchParams(location.search).get('session'));
    const session = data.sessions.find(s => s.id === sid);
    if (session?.identity) await openSession(session, associated(session)); else render();
    if (results[1].status === 'rejected' || results[2].status === 'rejected') P.notice('部分节点库暂不可用，已保存的拓扑仍可查看，请稍后刷新页面。', true);
  }
  load().catch(error => P.notice(error.message, true));
})();
