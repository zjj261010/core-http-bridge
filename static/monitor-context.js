/* Read-only names and session context; raw events/metrics retain their original identities. */
window.MonitorContext = (() => {
  let workspace = {sessions:[], deployments:[]}, selected = null;
  const states = {1:'未启动',2:'配置中',3:'启动中',4:'运行中',5:'停止中',6:'已关闭'};
  const historyStates = {active:'活动',closed:'已结束',interrupted:'已中断',superseded:'已替代'};
  let history = null;
  const matchesDrill = (row, drill) => row.drill_id === drill || row.drill_id && drill === row.drill_id + '（会话' + row.core_sid + '）';
  const run = drill => drill ? workspace.deployments.find(r => r.live && matchesDrill(r,drill)) ||
    (history && matchesDrill(history,drill) ? history : workspace.deployments.find(r => matchesDrill(r,drill))) : null;
  const session = sid => workspace.sessions.find(s => s.id === Number(sid));
  async function refresh() {workspace = await Platform.api('/topology-workspace?summary=true'); return workspace;}
  async function loadHistory(drill) {
    const known = run(drill);
    if (known?.live || history?.drill_id === drill) return;
    const params = new URLSearchParams({drill_id:drill,limit:'1'});
    const {items} = await Platform.api('/deployment-history?' + params);
    if (!items.length) return;
    const row = items[0];
    const detail = await Platform.api('/deployments/' + encodeURIComponent(row.deployment_id));
    history = {...row,...detail};
  }
  function nodeName(drill, id, fallback) {
    const item = run(drill), binding = item?.bindings?.[String(id)];
    const frozen = item?.execution_definition || item?.snapshot || item?.source;
    return binding?.display_name || frozen?.nodes?.find(n => n.id === Number(id))?.name || fallback || '节点 ' + id;
  }
  function containerName(name, drills) {
    for (const drill of drills || []) {
      const binding = Object.values(run(drill)?.bindings || {}).find(b => b.runtime_name === name);
      if (binding?.display_name) return binding.display_name;
    }
    return name;
  }
  function sessionLabel(item) {
    const execution = workspace.deployments.find(r => r.live && r.drill_id === item.drill_id);
    return (item.display_name || '会话 ' + item.id) + ' · ' + (execution?.current_status || states[item.state] || '未知状态') + '（会话 ' + item.id + '）';
  }
  function historyLabel(drill) {
    return (run(drill.drill_id)?.display_name || drill.display_name || drill.name || '会话 ' + drill.core_sid) + ' · ' + (historyStates[drill.status] || drill.status) + ' · ' + drill.created_at;
  }
  function select(sid) {
    const item = session(sid);
    selected = item ? {sid:item.id,identity:item.identity,drill_id:item.drill_id} : null;
    Platform.workspaceContext(selected); return selected;
  }
  function matches(expected, item) {return item && item.id === Number(expected.sid) && item.identity === expected.identity && (!expected.drill_id || item.drill_id === expected.drill_id);}
  function inherit() {
    const saved = Platform.workspaceContext(); if (!saved) return null;
    const item = session(saved.sid);
    if (!matches(saved, item)) {Platform.workspaceContext(null); return null;}
    selected = {sid:item.id,identity:item.identity,drill_id:item.drill_id}; return item;
  }
  return {refresh,loadHistory,nodeName,containerName,sessionLabel,historyLabel,select,inherit,matches,run,session,
    get sessions(){return workspace.sessions;},get selected(){return selected;}};
})();
