/* 旧浏览器兼容垫片（内网 Chromium<98；2026-11-27 现场 structuredClone 报错引入）。
   platform-ui.js 为全站首个脚本，垫片在此一处覆盖所有页面。数据均为 JSON 系，深拷贝回退安全。 */
if (typeof structuredClone !== 'function') window.structuredClone = value => JSON.parse(JSON.stringify(value));
if (!Array.prototype.at) Object.defineProperty(Array.prototype, 'at', {value(n){n = Math.trunc(n) || 0; return this[n < 0 ? this.length + n : n];}, writable: true, configurable: true});
if (!Object.hasOwn) Object.hasOwn = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
if (!Object.fromEntries) Object.fromEntries = entries => Object.assign({}, ...Array.from(entries, ([k, v]) => ({[k]: v})));
if (!Element.prototype.replaceChildren) Element.prototype.replaceChildren = function (...children) { while (this.firstChild) this.removeChild(this.firstChild); this.append(...children); };

/* Native controls shared by resource and topology editors. */
window.Platform = (() => {
  'use strict';
  const clone = value => structuredClone(value);
  const dirtyEditors = new Set();
  let leaving = false;
  function guardNavigation(isDirty, question = '存在尚未保存的修改，离开后将丢失，是否放弃？') {
    const editor = {isDirty, question}; dirtyEditors.add(editor);
    return () => dirtyEditors.delete(editor);
  }
  function confirmLeave() {
    const pending = [...dirtyEditors].filter(editor => editor.isDirty());
    return !pending.length || confirm(pending[pending.length - 1].question);
  }
  window.addEventListener('beforeunload', event => {
    if (!leaving && [...dirtyEditors].some(editor => editor.isDirty())) {event.preventDefault(); event.returnValue = '';}
  });
  window.addEventListener('pageshow', () => {leaving = false;});
  window.addEventListener('message', event => {
    if (event.origin !== location.origin || event.source !== window.parent || event.data?.type !== 'editor-navigation-request') return;
    const allowed = confirmLeave(); if (allowed) leaving = true;
    window.parent.postMessage({type:allowed ? 'editor-navigation-approved' : 'editor-navigation-denied', target:event.data.target, requestId:event.data.requestId}, location.origin);
  });
  document.addEventListener('click', event => {
    const link = event.target.closest?.('nav a[href]');
    if (!link || event.defaultPrevented || link.target === '_blank' || event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
    if (!confirmLeave()) event.preventDefault(); else leaving = true;
  });
  function workspaceContext(value) {
    const key = 'core-bridge-runtime-context';
    try {
      if (arguments.length) {if (value) sessionStorage.setItem(key, JSON.stringify(value)); else sessionStorage.removeItem(key); return value;}
      return JSON.parse(sessionStorage.getItem(key) || 'null');
    } catch {return null;}
  }
  function replace(node, ...children) {node.replaceChildren(...children.flat(Infinity).filter(child => child !== null && child !== undefined && child !== false));}
  function bytes(value) {
    if (!Number.isFinite(value) || value < 0) return '暂不可用';
    const units = ['B','KiB','MiB','GiB','TiB']; let index = 0;
    while (value >= 1024 && index < units.length - 1) {value /= 1024; index++;}
    return value.toFixed(index ? 1 : 0) + ' ' + units[index];
  }
  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (value !== undefined && value !== null) {
        if (key in node && !key.startsWith('aria-') && key !== 'role') node[key] = value;
        else node.setAttribute(key, value);
      }
    }
    children.flat(Infinity).forEach(child => { if (child !== null && child !== undefined) node.append(child instanceof Node ? child : document.createTextNode(String(child))); });
    return node;
  }
  function errorText(error) {
    if (typeof error === 'string') return error;
    if (Array.isArray(error)) return error.map(errorText).join('；');
    return error?.message || error?.msg || error?.detail && errorText(error.detail) || '请求失败，请查看当前处理阶段';
  }
  async function api(path, method = 'GET', body, raw = false) {
    const response = await fetch(path, {method, headers: raw ? {} : {'Content-Type':'application/json'}, body: body === undefined ? undefined : raw ? body : JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok || data.ok === false) throw Error(errorText(data.error || data.detail || response.statusText));
    return data.data ?? data;
  }
  const button = (text, run, primary = false) => el('button', {type:'button', class:primary?'btn primary':'btn', onclick:async event=>{
    const b=event.currentTarget;b.disabled=true;try {await run(event);} catch(error){notice(error.message,true);}finally{b.disabled=false;}
  }}, text);
  function field(label,control){control.setAttribute('aria-label',label);return el('label',{class:'form-field'},el('span',{},label),control);}
  function input(value, change, type='text') { return el('input',{type,value:value??'',oninput:event=>change(event.target.value)}); }
  function select(options,value,change) {
    const node=el('select',{onchange:event=>change(event.target.value)}, options.map(([v,label])=>el('option',{value:v},label)));
    node.value=value??'';return node;
  }
  function checkbox(value,change,label='') {return el('label',{class:'check-field'},el('input',{type:'checkbox',checked:!!value,onchange:event=>change(event.target.checked)}),label);}
  function table(headers, rows, layout = '') {
    const headings = headers.map(text => el('th', {scope:'col'}, text));
    const body = rows.length
      ? rows.map(row => el('tr', {}, row.map(cell => el('td', {}, cell))))
      : el('tr', {}, el('td', {colSpan:headers.length,class:'empty'}, '暂无记录'));
    return el('div', {class:'data-table-wrap'}, el('table', {class:layout ? 'data-table-' + layout : ''},
      el('thead', {}, el('tr', {}, headings)), el('tbody', {}, body)));
  }
  function notice(text, error=false) {const area=document.getElementById('message');if(area){area.className=error?'notice error':'notice';area.textContent=text;}}
  function localMessage(area,text,error=false){area.className=error?'notice error':'notice';area.textContent=text;}
  function modal(title, body, actions=[], closed=()=>{}) {
    const opener=document.activeElement;
    const dialog=el('dialog',{class:'resource-dialog','aria-label':title});
    let isDirty=()=>false, question='放弃当前弹窗尚未保存的修改？';
    const unregister=guardNavigation(()=>dialog.open && isDirty(), '存在尚未保存的修改，离开后将丢失，是否放弃？');
    const finish=()=>dialog.close();
    const close=()=>{if (!isDirty() || confirm(question)) finish();};
    const heading=el('header',{},el('h2',{},title),button('关闭',close));
    const content=el('div',{class:'dialog-content'},body);
    const footer=el('footer',{class:'actions'},actions);
    dialog.append(heading,content,footer);
    dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
    dialog.addEventListener('close',()=>{unregister();closed();dialog.remove();if(opener?.isConnected)opener.focus();else if(opener?.dataset.returnId)document.querySelector('[data-return-id="'+CSS.escape(opener.dataset.returnId)+'"]')?.focus();},{once:true});
    document.body.append(dialog);dialog.showModal();
    return {dialog,content,footer,close,finish,heading,guard:(check,prompt)=>{isDirty=check;if(prompt)question=prompt;}};
  }
  function fields(value) {
    if (value === null || value === undefined) return el('span',{},'—');
    if (typeof value !== 'object') return el('span',{},String(value));
    if (Array.isArray(value)) return value.length?el('div',{class:'field-list'},value.map((item,index)=>el('div',{},el('span',{class:'muted'},String(index+1)+'. '),fields(item)))):el('span',{},'无');
    return table(['项目','内容'],Object.entries(value).map(([name,item])=>[name,fields(item)]),'fields');
  }
  function download(blob,name){const url=URL.createObjectURL(blob),a=el('a',{href:url,download:name});a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  async function downloadConfig(catalog){const response=await fetch('/image-services/export/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({catalog})});if(!response.ok){const data=await response.json();throw Error(errorText(data.detail||data.error));}download(await response.blob(),'image-config.zip');}
  function ports(rows,changed=()=>{}) {
    const area=el('div');
    const headers=['起始端口','结束端口','协议','监听地址','IPv6 独占','操作'];
    function render(){
      const controls=rows.map((row,index)=>{
        const cells=[
          input(row.port,v=>{row.port=Number(v);changed();},'number'),
          input(row.end||row.port,v=>{row.end=Number(v);changed();},'number'),
          select([['tcp','TCP'],['udp','UDP']],row.protocol||'tcp',v=>{row.protocol=v;changed();}),
          input(row.address||'0.0.0.0',v=>{row.address=v;changed();})
        ];
        const exclusive=checkbox(row.ipv6_only,v=>{row.ipv6_only=v;changed();});
        [...cells,exclusive.querySelector('input')].forEach((control,column)=>{
          control.setAttribute('aria-label',headers[column]+' · 第 '+(index+1)+' 行');
        });
        return [...cells,exclusive,button('移除',()=>{rows.splice(index,1);render();changed();})];
      });
      area.replaceChildren(table(headers,controls,'ports'),
        button('添加端口',()=>{rows.push({port:80,protocol:'tcp',address:'0.0.0.0'});render();changed();}));
    }
    render();return area;
  }
  function keyValues(rows,changed=()=>{}) {
    const area=el('div');
    function render(){
      const controls=rows.map((row,index)=>{
        const name=input(row.name,v=>{row.name=v;changed();});
        const value=input(row.value,v=>{row.value=v;changed();});
        name.setAttribute('aria-label','变量名称 · 第 '+(index+1)+' 行');
        value.setAttribute('aria-label','变量值 · 第 '+(index+1)+' 行');
        return [name,value,button('移除',()=>{rows.splice(index,1);render();changed();})];
      });
      area.replaceChildren(table(['名称','值','操作'],controls,'variables'),
        button('添加变量',()=>{rows.push({name:'',value:''});render();changed();}));
    }
    render();return area;
  }
  function navigation(active){const nav=document.querySelector('nav.nav');if(!nav)return;nav.replaceChildren(el('a',{href:'/',class:'nav-home'},'CORE BRIDGE'),...[['overview.html','概览'],['target-admin.html','镜像管理'],['containers.html','容器管理'],['instances.html','节点模板'],['topologies.html','拓扑管理'],['monitor.html','数据监测'],['audit.html','审计日志']].map(([page,name])=>el('a',{href:'/static/'+page,class:page===active?'on':'','aria-current':page===active?'page':null},name)));}
  function describe(value){if(value===null||value===undefined)return '无';if(Array.isArray(value))return value.map(describe).join('；');if(typeof value==='object')return Object.entries(value).map(([key,item])=>key+'：'+describe(item)).join('；');return String(value);}
  function describeText(text){try{return describe(JSON.parse(text));}catch{return String(text);}}
  return {clone,el,api,button,field,input,select,checkbox,table,notice,localMessage,modal,fields,download,downloadConfig,ports,keyValues,navigation,errorText,describe,describeText,guardNavigation,workspaceContext,replace,bytes};
})();
