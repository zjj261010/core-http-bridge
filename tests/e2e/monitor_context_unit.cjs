/* Names must remain scoped to the immutable drill identity, including reused session IDs. */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
(async()=>{
 let stored={sid:7,identity:'old-entity',drill_id:'old-drill'};
 const workspace={sessions:[{id:7,identity:'new-entity',drill_id:'new-drill',state:4,display_name:'当前拓扑'}],deployments:[
  {core_sid:7,drill_id:'old-drill',display_name:'旧拓扑',bindings:{1:{display_name:'旧节点',runtime_name:'n1'}},live:false},
  {core_sid:7,drill_id:'new-drill',display_name:'当前拓扑',bindings:{1:{display_name:'当前节点',runtime_name:'n1'}},live:true,current_status:'运行中'}]};
 const sandbox={window:{},Platform:{api:async()=>workspace,workspaceContext:function(value){if(arguments.length)stored=value;return stored;}}};
 vm.runInNewContext(fs.readFileSync('static/monitor-context.js','utf8'),sandbox);
 const M=sandbox.window.MonitorContext;await M.refresh();
 assert.equal(M.inherit(),null);assert.equal(stored,null);
 assert.equal(M.nodeName('old-drill',1,'n1'),'旧节点');assert.equal(M.nodeName('new-drill',1,'n1'),'当前节点');
 assert.equal(M.nodeName('missing-drill',1,'n1'),'n1');assert.equal(M.nodeName(null,1,'unknown'),'unknown');
 assert.equal(M.containerName('n1',['old-drill']),'旧节点');assert.equal(M.containerName('n1',['new-drill']),'当前节点');
 assert.equal(M.containerName('n1',['new-drill（会话7）']),'当前节点');
 assert.equal(M.containerName('n1',['new-drill（会话8）']),'n1');
 stored={sid:7,identity:'new-entity',drill_id:'wrong-drill'};assert.equal(M.inherit(),null);
 stored={sid:7,identity:'new-entity',drill_id:'new-drill'};assert.equal(M.inherit().id,7);
 assert(M.sessionLabel(workspace.sessions[0]).includes('当前拓扑'));assert(M.sessionLabel(workspace.sessions[0]).includes('运行中'));
 console.log('monitor context: stale identity rejected, drill-scoped names retained, valid context inherited');
})().catch(e=>{console.error(e);process.exitCode=1;});
