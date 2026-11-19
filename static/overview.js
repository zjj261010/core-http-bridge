(() => {
  'use strict';
  const {api, el} = Platform;
  const byId = name => document.getElementById('overview-' + name);
  const jobStates = {
    queued:['排队中','attention'], running:['处理中','dk'], succeeded:['已完成','ok'],
    partial:['部分完成','attention'], failed:['失败','fail'], interrupted:['已中断','attention'],
  };
  const runStates = {
    defined:['未启动','ot'], running:['曾运行','ot'], stopped:['已停止','ot'],
    closed:['已结束','ot'], failed:['启动失败','fail'], interrupted:['已中断','attention'],
    superseded:['已替代','ot'],
  };
  const clock = new Intl.DateTimeFormat('zh-CN', {
    hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false,
  });
  const badge = ([text, tone]) => el('span', {class:'tag ' + tone}, text);
  const empty = text => el('p', {class:'overview-empty'}, text);
  const nextPage = (query, tab) => '/?' + new URLSearchParams(query) + '#' + tab;

  Platform.navigation('overview.html');

  function renderRuns(history, workspace) {
    if (!history) {
      byId('runs').replaceChildren(empty('运行记录暂不可用。'));
      return;
    }
    if (!history.items.length) {
      byId('runs').replaceChildren(empty('暂无运行记录。'));
      return;
    }
    const running = new Map((workspace?.deployments || []).filter(run => run.live)
      .map(run => [run.deployment_id, run]));
    const rows = history.items.map(run => {
      const live = running.get(run.deployment_id);
      const [status, tone] = live ? [live.current_status, live.current_state === 4 ? 'ok' : 'dk'] :
        run.status === 'running' && (!workspace || workspace.runtime_error) ? ['状态待确认','attention'] :
        runStates[run.status] || ['历史记录','ot'];
      const name = run.display_name || '会话 ' + (run.core_sid ?? '—');
      const info = [run.created_at, Number.isInteger(run.node_count) ? run.node_count + ' 个节点' : null]
        .filter(part => part !== null && part !== undefined).join(' · ');
      const action = live ? '查看会话' : '查看配置';
      return el('li', {class:'overview-run'},
        el('div', {class:'overview-item-info'}, el('strong', {}, name), info ? el('small', {}, info) : null),
        el('div', {class:'overview-run-end'}, badge([status,tone]),
          el('a', {class:'overview-run-link', href:nextPage({deployment:run.deployment_id}, 'topologies'),
                   target:'_top', 'aria-label':action + ' ' + name}, action)));
    });
    byId('runs').replaceChildren(el('ul', {class:'overview-list'}, rows));
  }

  function renderEnvironment(workspace, tools) {
    const core = !workspace ? ['状态未知','ot'] : workspace.runtime_error ? ['连接异常','attention'] : ['已连接','ok'];
    const toolState = available => available === true ? ['已就绪','ok'] :
      available === false ? ['缺少依赖','attention'] : ['状态未知','ot'];
    const rows = [
      ['CORE 守护进程', core],
      ['HTTP 背景流量', toolState(tools?.http)],
      ['TCP / UDP 背景流量', toolState(tools?.iperf3)],
      ['Snort 监测', toolState(tools?.snort)],
    ];
    byId('environment').replaceChildren(...rows.map(([name, value]) =>
      el('div', {}, el('dt', {}, name), el('dd', {}, badge(value)))));
  }

  function renderJobs(jobs) {
    if (!jobs?.length) {
      byId('jobs').replaceChildren(empty(jobs ? '暂无导入记录。' : '导入记录暂不可用。'));
      return;
    }
    const mostRecent = [...jobs].sort((a,b) => String(b.created_at || '').localeCompare(String(a.created_at || '')))
      .slice(0, 3);
    const rows = mostRecent.map(job => {
      const file = job.image_input?.image_path?.split('/').pop();
      const name = job.image_input?.declared_ref || job.result?.package || file || '镜像导入任务';
      return el('li', {class:'overview-job'},
        el('div', {class:'overview-item-info'}, el('strong', {}, name), el('small', {}, job.created_at || '')),
        badge(jobStates[job.status] || ['状态未知','ot']));
    });
    byId('jobs').replaceChildren(el('ul', {class:'overview-list'}, rows));
  }

  async function refresh() {
    const button = byId('refresh'), updated = byId('updated');
    button.disabled = true;
    updated.textContent = '正在刷新…';
    try {
      const responses = await Promise.allSettled([
        api('/topology-workspace?summary=true'), api('/deployment-history?limit=5'),
        api('/instances'), api('/jobs'), api('/network-activity/capabilities'),
      ]);
      const [workspace, history, templates, jobs, tools] =
        responses.map(result => result.status === 'fulfilled' ? result.value : null);
      byId('running').textContent = workspace && !workspace.runtime_error ?
        workspace.sessions.filter(session => session.state === 4).length : '—';
      byId('topologies').textContent = workspace?.topologies.length ?? '—';
      byId('templates').textContent = templates?.length ?? '—';
      byId('imports').textContent = jobs?.filter(job => ['queued','running'].includes(job.status)).length ?? '—';
      renderRuns(history, workspace);
      renderEnvironment(workspace, tools);
      renderJobs(jobs);
      const partial = responses.some(result => result.status === 'rejected') || !!workspace?.runtime_error;
      updated.toggleAttribute('data-incomplete', partial);
      updated.textContent = (partial ? '部分数据暂不可用 · ' : '') + '更新于 ' + clock.format(new Date());
    } catch (error) {
      updated.toggleAttribute('data-incomplete', true);
      updated.textContent = '读取概览失败 · ' + error.message;
    } finally {
      button.disabled = false;
    }
  }

  byId('refresh').addEventListener('click', refresh);
  refresh();
})();
