(() => {
  'use strict';
  const P = Platform, {el, api, button, field, input, select, checkbox, table} = P;
  const app = document.getElementById('app');
  const typeNames = {docker:'Docker 节点模板', netns:'CORE 原生节点模板'};
  const nodeModels = [['bare','裸节点'],['PC','PC'],['host','主机'],['router','路由器'],['mdr','无线多播路由器']];
  let images = [], registry = [], native = [];
  P.navigation('instances.html');

  async function load() {
    const [templates, registrations] = await Promise.all([api('/instances'), api('/targets/registry')]);
    images = (await api('/targets')).images;
    native = (await api('/instances/available-services')).system;
    registry = registrations;
    const docker = templates.filter(t => t.runtime_type === 'docker');
    const core = templates.filter(t => t.runtime_type === 'netns');
    const legacy = templates.filter(t => t.runtime_type !== 'docker' && t.runtime_type !== 'netns');
    app.replaceChildren(
      el('header', {class:'page-heading'}, el('h1', {}, '节点模板')),
      templateSection('docker', typeNames.docker, '镜像', docker, t => t.image_tag || '—', '新建 Docker 模板'),
      templateSection('netns', typeNames.netns, '节点型号', core, t => {
        const model = t.model || 'bare';
        return nodeModels.find(([id]) => id === model)?.[1] || model;
      }, '新建原生模板'),
      ...(legacy.length ? [templateSection('legacy', '待修正的旧模板', '运行时', legacy, t => t.runtime_type || '未声明')] : [])
    );
  }

  function templateSection(type, title, configLabel, templates, configSummary, createLabel) {
    const headingId = type + '-templates';
    const create = createLabel ? button(createLabel, () => edit(null, type), true) : null;
    const rows = templates.map(t => {
      const url = '/instances/' + encodeURIComponent(t.name);
      const actions = el('div', {class:'actions'},
        button('编辑', () => edit(t)),
        button('复制', async () => {await api(url + '/clone', 'POST', {}); await load();}),
        button('删除', async () => {
          if (!confirm('删除模板 ' + t.name + '？有历史或拓扑引用时会保留。')) return;
          await api(url, 'DELETE'); await load();
        }));
      const name = el('div', {}, t.name, t.migration_error ? el('small', {class:'template-migration muted'}, t.migration_error) : null);
      const services = (Array.isArray(t.services) ? t.services : []).filter(Boolean)
        .map(s => el('div', {}, s.service_name || s.name, s.mode_name ? ' / ' + s.mode_name : ''));
      return [name, configSummary(t), services.length ? el('div', {}, services) : '—', String(t.version || '未迁移'), actions];
    });
    return el('section', {class:'template-section', 'aria-labelledby':headingId},
      el('header', {}, el('h2', {id:headingId}, title), create),
      el('div', {class:'template-section-body'}, rows.length
        ? table(['模板', configLabel, '默认服务', '固定修订', '操作'], rows)
        : el('p', {class:'muted template-empty'}, '暂无' + title)));
  }

  async function edit(original, type) {
    const work = P.clone(original || {name:'', runtime_type:type, image_tag:type === 'docker' ? images[0]?.ref || '' : '', services:[], notes:''});
    const message = el('div', {'aria-live':'polite'}), content = el('div'), servicesArea = el('section'), bound = el('section');
    let target = null, selection = P.clone(work.service_selection || {}), version = work.target_version, bindToken = 0, validationToken = 0;
    const runtimeOptions = [['docker','Docker 镜像节点'],['netns','CORE 原生节点']];
    if (!Object.hasOwn(typeNames, work.runtime_type)) runtimeOptions.unshift(['','请选择运行时']);
    const title = original ? '编辑模板' : type === 'docker' ? '新建 Docker 模板' : '新建原生模板';
    const modal = P.modal(title, el('div', {}, message, content, bound, servicesArea));
    let userDirty=false;modal.dialog.addEventListener('input',()=>userDirty=true);modal.dialog.addEventListener('change',()=>userDirty=true);modal.guard(()=>userDirty,'放弃当前模板尚未保存的修改？');
    const guarded = fn => async (...args) => {try {await fn(...args);} catch (error) {P.localMessage(message, error.message, true);}};
    async function bind() {
      const token = ++bindToken;
      validationToken++;
      target = null;
      message.replaceChildren(); bound.replaceChildren(); servicesArea.replaceChildren();
      if (work.runtime_type === 'netns') {renderNative(); return;}
      if (work.runtime_type !== 'docker' || !work.image_tag) return;
      const matches = registry.filter(item => item.image_ref === work.image_tag);
      const candidate = matches.find(item => item.name === work.target_name) || matches[0];
      if (!candidate) {
        servicesArea.append(el('p', {class:'notice'}, '请先在镜像管理里确认服务清单并生效。空服务清单也可确认。'));
        return;
      }
      const versions = (await api('/versions/target/' + encodeURIComponent(candidate.name))).filter(v => v.activation_state !== 'pending');
      if (token !== bindToken) return;
      if (!versions.length) throw Error('镜像配置尚未生效');
      target = versions.find(v => v.version === Number(version || candidate.version)) || versions[0];
      work.target_name = candidate.name; work.target_version = target.version;
      bound.append(field('镜像配置修订', select(versions.map(v => [String(v.version), '修订 ' + v.version]), String(target.version), guarded(async value => {
        version = Number(value); selection = {}; await bind();
      }))));
      if(!target.catalog){servicesArea.append(el('p',{class:'notice'},'此镜像仍使用旧服务定义。请在镜像详情中确认新服务清单后，再新建或升级模板。原有模板与部署继续使用原实现。'));return;}
      if(!work.service_selection&&original){const old=new Set((original.services||[]).map(s=>s.name));selection={};for(const service of target.catalog.services)for(const mode of service.modes)if(old.has('imgmode-'+service.id+'-'+mode.id)||mode.implementation&&old.has(mode.implementation))selection[service.id]=mode.id;}
      renderServices();
    }
    async function validate(){if(!target?.catalog)return;const token=++validationToken;try{const result=await api('/image-services/'+(target.catalog.catalog_id||'template')+'/validate','POST',{services:target.catalog.services,selection});if(token!==validationToken||!modal.dialog.open)return;const errors=result.errors||[],warnings=result.warnings||[];P.localMessage(message,(errors.length?errors.join('；'):'启用组合检查通过')+(warnings.length?'。'+warnings.join('；'):''),!!errors.length);}catch(error){if(token===validationToken)P.localMessage(message,error.message,true);}}

    function renderServices(){const controls=target.catalog.services.map(service=>{const enabled=!!selection[service.id],mode=select([['','请选择模式'],...service.modes.map(m=>[m.id,m.name])],selection[service.id]||'',value=>{selection[service.id]=value||'__choose__';validate();});mode.disabled=!enabled;return [checkbox(enabled,value=>{if(value)selection[service.id]=service.modes.find(m=>m.default)?.id||'__choose__';else delete selection[service.id];renderServices();validate();}),service.name,mode,'当前镜像'];});servicesArea.replaceChildren(el('h3',{},'模板默认服务'),table(['启用','服务','启动模式','来源'],controls));validate();}
    function renderNative(){const selected=new Set((work.services || []).map(s=>s.name));servicesArea.replaceChildren(el('h3',{},'CORE 原生服务'),...native.map(name=>checkbox(selected.has(name),checked=>{if(checked)selected.add(name);else selected.delete(name);work.services=[...selected].map(name=>({name}));},name)));}
    function render() {
      const name = input(work.name, value => work.name = value);
      name.disabled = !!original;
      const runtime = original ? field('运行时', select(runtimeOptions, Object.hasOwn(typeNames, work.runtime_type) ? work.runtime_type : '', guarded(async value => {
        work.runtime_type = value; selection = {}; work.services = []; version = null;
        render(); await bind();
      }))) : null;
      let configuration = null;
      if (work.runtime_type === 'docker') {
        const options = [['','请选择镜像'], ...images.map(image => [image.ref, image.ref])];
        configuration = field('镜像', select(options, work.image_tag || '', guarded(async value => {
          work.image_tag = value; version = null; selection = {}; await bind();
        })));
      } else if (work.runtime_type === 'netns') {
        if (!nodeModels.some(([id]) => id === work.model)) work.model = 'bare';
        configuration = field('节点型号', select(nodeModels, work.model, value => work.model = value));
      }
      P.replace(content,
        el('div', {class:'form-grid'}, field('模板名称', name), runtime || configuration),
        original ? configuration : null,
        field('说明', input(typeof work.notes === 'string' ? work.notes : '', value => work.notes = value)));
    }
    modal.footer.append(button('取消', modal.close), button('保存模板', guarded(async () => {
      if (work.runtime_type === 'docker') {
        if (!work.image_tag) throw Error('请选择镜像');
        if (!target?.catalog) throw Error('请先在镜像管理中完成服务模式清单确认');
        const result = await api('/image-services/template/validate', 'POST', {services:target.catalog.services, selection});
        if (result.errors?.length) throw Error(result.errors.join('；'));
        work.service_selection = selection; work.services = [];
      } else if (work.runtime_type === 'netns') {
        work.image_tag = ''; delete work.target_name; delete work.target_version; delete work.service_selection;
      } else throw Error('请选择运行时');
      if (original) work.base_version = original.version;
      await api('/instances' + (original ? '/' + encodeURIComponent(original.name) : ''), 'POST', work);
      modal.finish(); P.notice('模板已保存；现有拓扑继续使用其固定修订。'); await load();
    }), true));
    render(); await bind();
  }
  load().catch(error=>P.notice(error.message,true));
})();
