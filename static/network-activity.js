/* Configuration and run history for topology traffic and passive probes. */
const NetworkActivity = (() => {
  const P = Platform, {el, api, button, field, input, select, table} = P;
  const titles = {traffic:'背景流量', probes:'Snort 监测点'};
  const active = new Set(['pending','waiting','starting','running']);
  const states = {pending:'待启动',waiting:'等待计划',starting:'启动或等目标就绪',running:'运行中',
    completed:'已完成',stopped:'已停止',failed:'失败',interrupted:'已中断'};
  const native = new Set(['default','pc','host','router']);
  const supported = new Set([...native, 'docker','podman']);
  const sameLink = (pair, link) => pair.includes(link.node1_id) && pair.includes(link.node2_id);

  function editor(kind, definition, nodes, changed, initialLink) {
    const body = el('div'), title = titles[kind], modal = P.modal(title, body);
    const nodeName = id => nodes.find(node => node.id === id)?.name || '#' + id;
    const list = () => definition.network_activity?.[kind] || [];
    const endpoint = ([a,b]) => nodeName(a) + ' ↔ ' + nodeName(b);
    function update(items) {
      definition.network_activity ??= {traffic:[],probes:[]};
      definition.network_activity[kind] = items;
      changed();
      render();
    }
    function render() {
      const rows = list().map(item => {
        const summary = kind === 'traffic' ?
          item.protocol.toUpperCase() + ' · ' + nodeName(item.source_id) + ' → ' + nodeName(item.target_id) + ':' + item.port :
          nodeName(item.node_id) + ' · ' + endpoint(item.link) + ' · ' + item.ruleset;
        return [item.name, summary, el('div', {class:'actions'},
          button('编辑', () => editItem(item)),
          button('移除', () => update(list().filter(row => row.id !== item.id))))];
      });
      body.replaceChildren(table(['名称', '配置', '操作'], rows));
    }
    async function editItem(existing) {
      const known = await api('/network-activity/capabilities');
      const source = nodes.filter(node => native.has(node.type));
      const defaults = kind === 'traffic' ?
        {name:'背景流量 ' + (list().length + 1),protocol:'http',port:80,path:'/',rate:5,delay:1,duration:30,
          source_id:source[0]?.id} :
        {name:'Snort ' + (list().length + 1),node_id:source.find(node => initialLink?.includes(node.id))?.id ?? source[0]?.id,
          link:initialLink,ruleset:known.rulesets[0]?.id};
      const item = P.clone(existing || defaults), content = el('div'), feedback = el('div', {role:'status'});
      const popup = P.modal((existing ? '编辑' : '新增') + title, el('div', {}, feedback, content));
      let dirty = false;
      popup.guard(() => dirty);
      popup.dialog.addEventListener('change', () => {dirty = true;});
      popup.dialog.addEventListener('input', () => {dirty = true;});
      const string = (label,key) => field(label,input(item[key],value => item[key] = value));
      const number = (label,key) => field(label,input(item[key],value => item[key] = value === '' ? null : Number(value),'number'));
      const nodeSelect = (label, choices, key, onChange) => field(label, select(
        [['','请选择节点'], ...choices.map(node => [String(node.id),node.name])],
        item[key] === undefined ? '' : String(item[key]), value => {
          item[key] = value ? Number(value) : undefined;
          onChange?.();
        }));
      function linkSelect(label,key,node) {
        const links = definition.links.filter(link => [link.node1_id,link.node2_id].includes(node));
        const pairs = links.map(link => [link.node1_id,link.node2_id]);
        const current = pairs.find(pair => item[key]?.every(id => pair.includes(id)));
        item[key] = current || (pairs.length === 1 ? pairs[0] : undefined);
        return field(label,select([['','请选择接口'], ...pairs.map(pair => [pair.join(','),endpoint(pair)])],
          item[key]?.join(',') || '', value => {item[key] = value ? value.split(',').map(Number) : undefined;}));
      }
      function renderFields() {
        const fields = [string('名称','name')];
        if (kind === 'traffic') {
          const http = item.protocol === 'http';
          const targets = nodes.filter(node => node.id !== item.source_id && (http ? supported : native).has(node.type));
          if (!targets.some(node => node.id === item.target_id)) item.target_id = undefined;
          fields.push(field('流量协议',select([['http','HTTP'],['tcp','TCP'],['udp','UDP']],item.protocol,value => {
            item.protocol = value;
            item.port = value === 'http' ? 80 : 5201;
            item.rate = value === 'http' ? 5 : 1_000_000;
            renderFields();
          })), nodeSelect('源节点',source,'source_id',renderFields), nodeSelect('目标节点',targets,'target_id',renderFields),
          linkSelect('目标接口','target_link',item.target_id), number('目标端口','port'),
          ...(http ? [string('请求路径','path')] : []),
          number(http ? '速率（请求/秒）' : '速率（bit/s）','rate'), number('启动延迟（秒）','delay'), number('持续时间（秒）','duration'));
        } else {
          fields.push(nodeSelect('监测节点',source,'node_id',renderFields), linkSelect('监测接口','link',item.node_id),
            field('Snort 规则集',select(known.rulesets.map(rule => [rule.id,rule.name]),item.ruleset,value => item.ruleset = value)));
        }
        const available = kind === 'probes' ? known.snort : item.protocol === 'http' ? known.http : known.iperf3;
        P.replace(content, !available ? el('p',{class:'notice error'},'当前宿主缺少运行依赖，仍可保存拓扑配置。') : null,
          el('div',{class:'form-grid'},fields));
      }
      popup.footer.append(button('取消',popup.close),button('确定',() => {
        const valid = kind === 'traffic' ? item.source_id && item.target_id && item.target_link &&
          Number.isInteger(item.port) && item.port >= 1 && item.port <= 65535 &&
          Number.isInteger(item.rate) && item.rate >= 1 && item.rate <= (item.protocol === 'http' ? 50 : 10_000_000) &&
          Number.isInteger(item.delay) && item.delay >= 0 && item.delay <= 3600 &&
          Number.isInteger(item.duration) && item.duration >= 1 && item.duration <= 3600 &&
          (item.protocol !== 'http' || /^\/(?!\/)[!-~]*$/.test(item.path)) :
          item.node_id && item.link && item.ruleset;
        if (!item.name.trim() || !valid) {
          P.localMessage(feedback,'请检查必填字段、接口和数值范围。',true);
          return;
        }
        item.name = item.name.trim();
        item.id ??= kind + '-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2,8);
        update(existing ? list().map(row => row.id === item.id ? item : row) : [...list(),item]);
        popup.finish();
      },true));
      renderFields();
    }
    modal.footer.append(button('新增' + title, () => editItem(), true));
    render();
    if (initialLink) editItem().catch(error => P.notice(error.message, true));
  }

  function metrics(item) {
    const m = item.metrics || {};
    if (item.kind === 'probe') return [
      '捕获 ' + (m.packets ?? '—') + ' 包', '丢弃 ' + (m.drops ?? '—') + ' 包', '告警 ' + (m.alerts ?? '—')
    ].join(' · ');
    if (item.protocol === 'http') return m.requests === undefined ? '—' :
      m.requests_per_second + ' 请求/秒 · 共 ' + m.requests + ' 次 · 错误 ' + m.errors +
      (m.skipped ? ' · 未发出 ' + m.skipped : '');
    return m.bits_per_second === undefined ? '—' : Math.round(m.bits_per_second) + ' bit/s' +
      (m.lost_percent === undefined ? '' : ' · 丢包 ' + m.lost_percent.toFixed(2) + '%') +
      (m.jitter_ms === undefined ? '' : ' · 抖动 ' + m.jitter_ms.toFixed(2) + ' ms');
  }

  function runtime(area, deploymentId) {
    let selected = '', stopped = false, fetching = false, last = '';
    const url = '/deployments/' + encodeURIComponent(deploymentId) + '/network-activity';
    function render(data) {
      const run = data.runs.find(row => row.run_id === selected) || data.runs[0];
      if (!run) {area.replaceChildren(el('p',{class:'muted'},'此运行暂无背景流量或 Snort 记录。'));return;}
      const name = id => run.node_names?.[String(id)] || '节点 ' + id;
      const runPicker = field('运行批次',select(data.runs.map(row => [row.run_id,row.created_at]),run.run_id,value => {
        selected = value; refresh(true);
      }));
      const alerts = data.alerts.filter(row => row.detail.run_id === run.run_id);
      const trafficRows = [], probeRows = [];
      for (const item of Object.values(run.items)) {
        const status = el('div',{},states[item.status] || item.status,
          item.error ? el('small',{class:'muted'},item.error) : null);
        if (item.kind === 'traffic') {
          const dest = item.endpoint ? name(item.target_id) + '（' + item.endpoint + '）' : name(item.target_id);
          const rate = item.rate + (item.protocol === 'http' ? ' 请求/秒' : ' bit/s');
          trafficRows.push([item.name, item.protocol.toUpperCase(), name(item.source_id) + ' → ' + dest,
            rate,status,metrics(item),active.has(item.status) ? button('停止任务',async () => {
              await api(url + '/' + encodeURIComponent(run.run_id) + '/' + encodeURIComponent(item.id) + '/stop','POST',{});
              await refresh(true);
            }) : '—']);
        } else {
          probeRows.push([item.name,name(item.node_id) + (item.interface ? ' · ' + item.interface : ''),
            el('span',{title:item.rules_sha256},item.ruleset), status,metrics(item)]);
        }
      }
      const filter = input(area.dataset.alertSearch || '',value => {area.dataset.alertSearch = value; renderAlerts();},'search');
      filter.placeholder = '搜索规则、地址或探针';
      const records = el('div');
      function renderAlerts() {
        const query = (area.dataset.alertSearch || '').trim().toLowerCase();
        const shown = alerts.filter(({detail}) => [detail.rule,detail.msg,detail.probe,detail.src_ap,detail.dst_ap]
          .some(value => String(value ?? '').toLowerCase().includes(query)));
        records.replaceChildren(table(['时间','规则','来源 → 目标','探针'],shown.map(({ts,detail}) => [ts,
          el('div',{},el('span',{},detail.msg || '规则命中'),el('small',{class:'muted'},detail.rule)),
          (detail.src_ap || '—') + ' → ' + (detail.dst_ap || '—'),
          detail.probe + ' · ' + detail.interface])));
      }
      area.replaceChildren(runPicker,
        el('h3',{},'背景流量'), table(['任务','协议','源 → 目标','设定速率','状态','实际数据','操作'],trafficRows),
        el('h3',{},'Snort 探针'), table(['探针','监听接口','规则集','状态','计数'],probeRows),
        el('h3',{},'Snort 告警'),field('搜索最近 200 条告警',filter),records);
      renderAlerts();
    }
    async function refresh(force = false) {
      if (fetching || stopped) return;
      fetching = true;
      try {
        const requested = selected;
        const data = await api(url + (requested ? '?run_id=' + encodeURIComponent(requested) : ''));
        const serialized = JSON.stringify(data);
        if (!stopped && selected === requested && serialized !== last && (force || !area.contains(document.activeElement))) {
          last = serialized;
          render(data);
        }
      } catch (error) {
        if (!stopped) P.localMessage(area,error.message,true);
      } finally {fetching = false;}
    }
    const timer = setInterval(refresh,2000);
    refresh();
    return () => {stopped = true;clearInterval(timer);};
  }

  function show(deploymentId) {
    const area = el('div');
    let stop = () => {};
    P.modal('网络任务与 Snort 告警', area, [], () => stop());
    stop = runtime(area,deploymentId);
  }

  return {editor,runtime,show,sameLink};
})();
