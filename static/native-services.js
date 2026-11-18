/* Shared service picker and configuration editor for CORE native nodes. */
const NativeServices = (() => {
  const P = Platform, {el, api, button, field, input, select, checkbox, table} = P;
  const origins = {builtin:'CORE 内置', custom:'CORE 自定义'};

  async function readCatalog() {
    try {return await api('/native-services');}
    catch {
      const {system} = await api('/instances/available-services');
      return system.map(name => ({name, group:'', origin:'builtin', dependencies:[], configurable:false}));
    }
  }

  function editor({catalog, names, configs = {}, inherited = {}, external = {}, editable = true,
    previewContext = () => ({}), changed}) {
    let selected = [...names], overrides = P.clone(configs);
    const list = el('div');
    const registered = new Map(catalog.map(item => [item.name,item]));
    function update() {
      const rows = selected.map(name => {
        const info = registered.get(name);
        const override = overrides[name];
        const unsupported = external[name];
        const status = unsupported ? '包含外部 CORE 文件模板' : !info ? '目录中不可用' :
          override ? origins[info.origin] + ' · 已配置' : origins[info.origin];
        const actions = el('div', {class:'actions'});
        if (info && info.configurable !== false && !unsupported && editable) actions.append(button('配置', () => configure(name, override, data => {
          if (data === undefined) delete overrides[name]; else overrides[name] = data;
          changed(selected, overrides, name);
          update();
        }, previewContext)));
        if (!editable && (override || unsupported)) actions.append(button('查看', () => viewConfig(name, override, unsupported)));
        if (editable) actions.append(button('移除', () => {
          const dependent = selected.filter(other => registered.get(other)?.dependencies.includes(name));
          if (dependent.length) throw Error('请先移除依赖此服务的已启用项：' + dependent.join('、'));
          selected = selected.filter(other => other !== name);
          delete overrides[name];
          changed(selected, overrides, name);
          update();
        }));
        return [el('div', {}, name, el('small', {class:'muted'}, status)), actions];
      });
      list.replaceChildren(table(['服务','操作'], rows));
    }
    const add = editable ? button('添加服务', () => picker(catalog, selected, additions => {
      selected = [...selected, ...additions];
      for (const name of additions) {
        if (Object.hasOwn(inherited,name)) overrides[name] = P.clone(inherited[name]);
      }
      changed(selected, overrides);
      update();
    }), true) : null;
    if (add && !catalog.length) add.disabled = true;
    update();
    return el('div', {class:'native-service-editor'}, add, list);
  }

  function picker(catalog, selected, apply) {
    const rows = el('div'), picked = new Set();
    const search = input('', render, 'search');
    search.placeholder = '按服务名称或分组搜索';
    const modal = P.modal('添加 CORE 原生服务', el('div', {}, field('搜索服务', search), rows));
    modal.footer.append(button('取消',modal.close), button('添加所选服务', () => {
      if (!picked.size) return;
      apply([...picked]);
      modal.finish();
    }, true));
    function render(value = '') {
      const query = value.trim().toLowerCase();
      const sections = Object.entries(origins).map(([origin, title]) => {
        const items = catalog.filter(item => item.origin === origin && !selected.includes(item.name) &&
          (item.name + ' ' + item.group).toLowerCase().includes(query));
        return items.length ? el('section', {class:'native-service-block'}, el('h3', {}, title),
          items.map(item => checkbox(picked.has(item.name), enabled => {
            if (enabled) picked.add(item.name); else picked.delete(item.name);
          }, item.name + (item.group ? ' · ' + item.group : '')))) : null;
      }).filter(Boolean);
      rows.replaceChildren(...(sections.length ? sections : [el('p', {class:'muted'}, '没有可添加的匹配服务')]));
    }
    render();
    search.focus();
  }

  async function configure(name, original, save, previewContext) {
    const url = '/native-services/' + encodeURIComponent(name);
    const defaults = await api(url + '/defaults');
    const work = P.clone(original || {config:{},files:{}});
    const settings = el('div'), files = el('div');
    const modal = P.modal('配置服务 · ' + name, el('div', {}, settings, files));
    let dirty = false, restored = false, cachedPreview;
    modal.guard(() => dirty, '放弃当前服务尚未保存的配置？');

    function renderSettings() {
      const controls = Object.entries(defaults.config).map(([id, option]) => {
        const change = value => {
          if (value === option.value) delete work.config[id]; else work.config[id] = value;
          dirty = true;
        };
        const value = work.config[id] ?? option.value;
        const label = option.label || id;
        if (option.type === 11) return checkbox(value === '1', checked => change(checked ? '1' : '0'), label);
        const control = option.select.length ? select(option.select.map(item => [item,item]), value, change) :
          input(value, change, option.type >= 1 && option.type <= 9 ? 'number' : 'text');
        if (option.type === 9) control.step = 'any';
        return field(label, control);
      });
      const modeNames = Object.keys(defaults.modes);
      const modes = modeNames.length ? field('应用参数预设', select([['','请选择预设'], ...modeNames.map(mode => [mode,mode])], '', mode => {
        if (!mode) return;
        Object.assign(work.config, defaults.modes[mode]);
        dirty = true;
        renderSettings();
      })) : null;
      P.replace(settings, el('section', {class:'native-service-block'}, el('h3', {}, '参数'), modes,
        controls.length ? el('div', {class:'form-grid'}, controls) : el('p', {class:'muted'}, '此服务没有可编辑参数')));
    }
    function renderFiles() {
      const rows = defaults.files.map(path => {
        const overridden = Object.hasOwn(work.files, path);
        const actions = el('div', {class:'actions'}, button('查看与编辑', () => editFile(path, {
          value:overridden ? work.files[path] : undefined,
          source:defaults.templates?.[path],
          generate:async () => {
            const request = {...previewContext(), config:work.config};
            const key = JSON.stringify(request);
            if (cachedPreview?.key !== key) {
              const data = await api(url + '/preview', 'POST', request);
              cachedPreview = {key, files:data.files};
            }
            if (!Object.hasOwn(cachedPreview.files, path)) throw Error('CORE 未生成该文件');
            return cachedPreview.files[path];
          },
          save:text => {work.files[path] = text; dirty = true; renderFiles();}
        })));
        if (overridden) actions.append(button('导出', () => downloadFile(path, work.files[path])), button('恢复默认', () => {
          delete work.files[path]; dirty = true; renderFiles();
        }));
        return [path, overridden ? '已覆盖' : '使用 CORE 默认', actions];
      });
      files.replaceChildren(el('section', {class:'native-service-block'}, el('h3', {}, '配置文件'),
        rows.length ? table(['文件','来源','操作'], rows) : el('p', {class:'muted'}, '此服务没有可替换文件')));
    }
    modal.footer.append(button('恢复 CORE 默认', () => {
      work.config = {}; work.files = {}; restored = true; dirty = true; renderSettings(); renderFiles();
    }), button('取消',modal.close), button('确定', () => {
      if (dirty) {
        const hasValues = Object.keys(work.config).length || Object.keys(work.files).length;
        save(hasValues ? work : restored || original ? null : undefined);
      }
      modal.finish();
    }, true));
    renderSettings();
    renderFiles();
  }

  async function editFile(path, {value, source, generate, save}) {
    const overridden = value !== undefined;
    const text = el('textarea', {value:value ?? '', rows:15, wrap:'off', spellcheck:false, class:'native-script-text'});
    const upload = el('input', {type:'file', accept:'text/*,.conf,.cfg,.txt,.sh,.ini,.yaml,.yml'});
    const feedback = el('div', {role:'status'});
    const sourceBlock = source === undefined ? null : el('details', {class:'native-service-block native-script-source'},
      el('summary', {}, 'CORE 源模板'), el('pre', {tabIndex:0}, source));
    const method = el('p', {class:'muted'}, overridden ? '当前使用已保存的文件覆盖。' : '当前使用 CORE 生成的文件。');
    const dialog = P.modal('配置文件 · ' + path, el('div', {},
      el('section', {class:'native-service-block'}, method, feedback, field('脚本内容', text), field('导入文本文件', upload)),
      sourceBlock));
    let baseline = text.value;
    dialog.guard(() => text.value !== baseline, '放弃当前文件的修改？');
    upload.addEventListener('change', async () => {
      const file = upload.files[0];
      if (!file) return;
      try {
        const imported = await file.text();
        if (dialog.dialog.open && upload.files[0] === file) text.value = imported;
      } catch (error) {
        if (dialog.dialog.open) P.localMessage(feedback,error.message,true);
      }
    });
    const download = button('导出当前内容', () => downloadFile(path, text.value));
    const apply = button('确定', () => {
      if (text.value !== baseline) save(text.value);
      dialog.finish();
    }, true);
    dialog.footer.append(download, button('取消',dialog.close), apply);
    if (overridden) return;
    text.disabled = upload.disabled = download.disabled = apply.disabled = true;
    feedback.textContent = '正在读取 CORE 生成的脚本…';
    try {
      const generated = await generate();
      if (!dialog.dialog.open) return;
      text.value = generated;
      baseline = text.value;
      feedback.textContent = '';
    } catch (error) {
      if (!dialog.dialog.open) return;
      method.textContent = '可查看 CORE 源模板，或导入完整的脚本内容。';
      P.localMessage(feedback, error.message, true);
    } finally {
      if (dialog.dialog.open) text.disabled = upload.disabled = download.disabled = apply.disabled = false;
    }
  }

  function downloadFile(path, text) {
    P.download(new Blob([text], {type:'text/plain;charset=utf-8'}), path.split('/').pop());
  }

  function viewConfig(name, data, external) {
    const config = Object.entries(data?.config || {});
    const files = Object.entries(data?.files || {});
    P.modal('服务配置 · ' + name, el('div', {},
      config.length ? table(['参数','生效值'], config) : null,
      files.length ? table(['文件','操作'], files.map(([path,text]) => [path,el('div', {class:'actions'},
        button('查看内容', () => P.modal('配置文件 · ' + path, field('脚本内容',
          el('textarea', {value:text, readOnly:true, rows:15, wrap:'off', class:'native-script-text'})))),
        button('导出', () => downloadFile(path,text)))])) : null,
      external?.length ? el('p', {class:'notice'}, 'CORE GUI 文件模板随原生 XML 导出：' + external.join('、')) : null));
  }

  return {editor,readCatalog};
})();
