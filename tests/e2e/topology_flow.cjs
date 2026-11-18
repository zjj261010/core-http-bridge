/* One pass through the actual UI and isolated CORE/Docker. No route stubs. */
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE);
const root=process.env.BRIDGE_TEST_RUN_DIR,base=process.env.BRIDGE_TEST_URL;
assert(root && new URL(base).port==='18080','仅允许专用环境 18080');
const fixture=JSON.parse(fs.readFileSync(path.join(root,'topology-fixture.json'),'utf8'));
const results=[];let browser,sequence=0;
async function request(page,method,url,body){
  const response=await page.request.fetch(base+url,{method,data:body,timeout:120000});
  const value=await response.json();
  fs.writeFileSync(path.join(root,'evidence',`flow-http-${++sequence}.json`),JSON.stringify({method,url,status:response.status(),body,response:value},null,2));
  assert(response.ok(),JSON.stringify(value)); assert(value.ok!==false,JSON.stringify(value));return value.data??value;
}
async function scenario(name,run){
  if(process.env.BRIDGE_TOPOLOGY_FOCUS==='1'&&name.startsWith('导入导出'))return;
  const context=await browser.newContext({viewport:{width:1600,height:1100},acceptDownloads:true});
  const page=await context.newPage(),errors=[];page.setDefaultTimeout(15000);
  page.on('pageerror',e=>errors.push(e.message));let accept=true;
  page.on('dialog',d=>accept?d.accept():d.dismiss());
  await context.tracing.start({screenshots:true,snapshots:true,sources:true});
  let status='passed',detail;
  try{detail=await run(page,value=>accept=value);assert.deepEqual(errors,[]);}catch(e){status='failed';detail={message:e.message,stack:e.stack};}
  await page.screenshot({path:path.join(root,'screenshots',`topology-flow-${results.length+1}.png`),fullPage:true}).catch(()=>{});
  await context.tracing.stop({path:path.join(root,'evidence',`topology-flow-${results.length+1}.zip`)});
  results.push({name,status,detail,errors});fs.writeFileSync(path.join(root,process.env.BRIDGE_TOPOLOGY_FOCUS==='1'?'topology-flow-focused-results.json':'topology-flow-results.json'),JSON.stringify(results,null,2));console.log(status+': '+name);await context.close();
}
async function workspace(page){await page.goto(base+'/#topologies');const f=page.frameLocator('#tabFrame');await f.getByRole('button',{name:'新建拓扑',exact:true}).waitFor();return f;}
async function addHost(f,name){await f.getByRole('button',{name:'添加原生主机',exact:true}).click();await f.getByLabel('节点名称',{exact:true}).fill(name);}
async function link(f,a,b){await f.getByRole('button',{name:'连线',exact:true}).click();await f.getByRole('button',{name:'选择节点 '+a,exact:true}).click();await f.getByRole('button',{name:'选择节点 '+b,exact:true}).click();await f.getByRole('heading',{name:'链路属性',exact:true}).waitFor();}
async function save(page,f){const event=page.waitForResponse(r=>r.url()===base+'/topologies'&&r.request().method()==='POST');await f.getByRole('button',{name:'保存',exact:true}).click();const response=await event;const data=await response.json();assert(response.ok(),JSON.stringify(data));await f.locator('#saveState').filter({hasText:'已保存'}).waitFor();return data.data;}
async function start(page,f){const event=page.waitForResponse(r=>/\/topologies\/[^/]+\/deploy$/.test(r.url())&&r.request().method()==='POST',{timeout:120000});await f.getByRole('button',{name:'启动',exact:true}).click();const response=await event,value=await response.json();assert(response.ok(),JSON.stringify(value));await f.getByRole('button',{name:'停止运行',exact:true}).waitFor({timeout:120000});return value.data;}
async function deleteLive(f){await f.getByRole('button',{name:'删除会话',exact:true}).click();await f.locator('#saveState').filter({hasText:'已结束或被替换'}).waitFor({timeout:40000});}
(async()=>{
 browser=await chromium.launch({headless:true,executablePath:chromium.executablePath(),args:['--no-sandbox']});
 await scenario('无模板：原生节点与交换机同画布连线、保存重开、真实启动和 ICMP',async page=>{
  assert.equal((await request(page,'GET','/instances')).length,0);
  const f=await workspace(page);await f.getByRole('button',{name:'新建拓扑',exact:true}).click();
  assert(await f.getByRole('button',{name:'连线',exact:true}).isDisabled());
  await f.getByLabel('拓扑名称',{exact:true}).fill('无模板原生拓扑');
  await addHost(f,'主机甲');await addHost(f,'主机乙');await f.getByRole('button',{name:'添加交换机',exact:true}).click();
  await link(f,'主机甲','交换机 3');await link(f,'主机乙','交换机 3');
  await f.getByLabel('时延 µs',{exact:true}).fill('1000');
  await f.getByRole('button',{name:'选择节点 主机甲',exact:true}).click();
  const position=await f.getByRole('button',{name:'选择节点 主机甲',exact:true}).boundingBox();
  await page.mouse.move(position.x+40,position.y+25);await page.mouse.down();await page.mouse.move(position.x+75,position.y+70,{steps:5});await page.mouse.up();
  const saved=await save(page,f);assert.equal(saved.definition.nodes.length,3);assert.equal(saved.definition.links.length,2);assert.equal(saved.definition.links[1].options.delay,1000);assert(saved.definition.nodes[0].x>50);
  await f.getByRole('button',{name:'返回列表',exact:true}).click();await f.locator('.topology-card').filter({hasText:'无模板原生拓扑'}).getByRole('button',{name:'打开配置',exact:true}).click();
  assert.equal(await f.locator('.canvas-node').count(),3);assert.equal(await f.locator('svg line').count(),2);
  const run=await start(page,f);assert.equal(await f.getByRole('button',{name:'保存',exact:true}).count(),0);
  const connectivityEvent=page.waitForResponse(r=>r.url().endsWith('/'+run.core_sid+'/connectivity'));
  await f.getByRole('button',{name:'检查联通',exact:true}).click();const modal=f.locator('dialog[open]');await modal.waitFor({timeout:30000});
  const probes=(await (await connectivityEvent).json()).data;
  fs.writeFileSync(path.join(root,'evidence','native-connectivity.json'),JSON.stringify(probes,null,2));
  assert(probes.results.length>=2);assert(probes.all_reachable,JSON.stringify(probes));
  await modal.getByRole('button',{name:'关闭',exact:true}).click();
  await f.getByRole('button',{name:'停止运行',exact:true}).click();await f.getByRole('button',{name:'停止运行',exact:true}).waitFor({state:'detached',timeout:40000});
  await deleteLive(f);
  return {topology:saved.topology_id,sid:run.core_sid,icmp:probes.results.length,nodes:3,links:2};
 });
 await scenario('多 Docker/原生模板：选择指定模板、服务恢复、混合拓扑真实 HTTP',async page=>{
  const C=fixture.catalog;
  for(const name of ['Docker 模板甲','Docker 模板乙'])await request(page,'POST','/instances',{name,runtime_type:'docker',image_tag:fixture.ref,target_name:C.target_name,target_version:C.applied_target_version,services:[],service_selection:{http:'default'}});
  await request(page,'POST','/instances',{name:'原生模板',runtime_type:'netns',model:'bare',services:[]});
  const f=await workspace(page);await f.getByRole('button',{name:'新建拓扑',exact:true}).click();await f.getByLabel('拓扑名称',{exact:true}).fill('Docker 与原生混合拓扑');
  for(const name of ['Docker 模板甲','Docker 模板乙','原生模板'])await f.getByRole('button',{name:'添加模板 '+name,exact:true}).waitFor();
  await f.getByRole('button',{name:'添加模板 Docker 模板乙',exact:true}).click();await f.getByLabel('节点模板',{exact:true}).waitFor();
  assert.equal(await f.getByLabel('节点模板',{exact:true}).inputValue(),'Docker 模板乙');
  await f.getByLabel('配置来源',{exact:true}).selectOption('custom');await f.getByRole('row').filter({hasText:'HTTP 服务'}).getByRole('checkbox').uncheck();
  await f.getByRole('button',{name:'恢复模板默认',exact:true}).click();await f.getByLabel('配置来源',{exact:true}).waitFor();assert.equal(await f.getByLabel('配置来源',{exact:true}).inputValue(),'inherit');
  await addHost(f,'原生客户端');await f.getByRole('button',{name:'添加模板 Docker 模板甲',exact:true}).click();await f.getByRole('button',{name:'添加模板 原生模板',exact:true}).click();
  await link(f,'Docker 模板乙 1','原生客户端');await link(f,'Docker 模板甲 3','原生模板 4');
  const saved=await save(page,f);assert.equal(saved.definition.nodes[0].instance,'Docker 模板乙');assert.equal(saved.definition.nodes[2].instance,'Docker 模板甲');assert(!saved.definition.nodes[0].service_overrides);
  const run=await start(page,f),outputs=[];
  for(const {node2_id:source,iface1:{ip4:address}} of saved.definition.links){
    const value=await request(page,'POST','/api/sessions/'+run.core_sid+'/nodes/'+source+'/command',{command:`python3 -c "import urllib.request;print(urllib.request.urlopen('http://${address}:8080',timeout=5).status)"`,wait:true,shell:true});
    assert.equal(value.return_code,0,JSON.stringify(value));assert(value.output.includes('200'));outputs.push(value.output.trim());
  }
  assert(await f.getByLabel('节点名称',{exact:true}).isDisabled());await deleteLive(f);
  return {topology:saved.topology_id,sid:run.core_sid,http:outputs,templates:3};
 });
 await scenario('无保存配置但有运行会话：列表可见、同页查看、捕获保存并关联状态、历史仍可找回',async page=>{
  for(const t of await request(page,'GET','/topologies'))await request(page,'DELETE','/topologies/'+t.topology_id);
  const session=await request(page,'POST','/api/sessions',{}),sid=session.id;
  await request(page,'POST','/api/sessions/'+sid+'/nodes',{id:1,name:'未归档原生节点',type:0,model:'bare',services:[],position:{x:150,y:100}});
  const started=await request(page,'POST','/api/sessions/'+sid+'/start',{});assert(started.result);
  assert.equal((await request(page,'GET','/topologies')).length,0);
  const f=await workspace(page),live=f.locator('.topology-card').filter({hasText:'未保存配置'}).filter({hasText:'运行中'});assert.equal(await live.count(),1);
  await live.getByRole('button',{name:'查看运行',exact:true}).click();await f.getByRole('button',{name:'保存为拓扑',exact:true}).click();await f.getByLabel('拓扑名称',{exact:true}).fill('捕获运行拓扑');
  const responseEvent=page.waitForResponse(r=>r.url().endsWith('/'+sid+'/capture'));
  await f.getByRole('button',{name:'保存',exact:true}).click();const captureResponse=await responseEvent;assert(captureResponse.ok(),await captureResponse.text());await f.locator('#saveState').filter({hasText:'已保存'}).waitFor();
  await f.getByRole('button',{name:'返回列表',exact:true}).click();const card=f.locator('.topology-card').filter({hasText:'捕获运行拓扑'});await card.getByText('运行中',{exact:true}).waitFor();assert.equal(await f.locator('.topology-card').filter({hasText:'未保存配置'}).count(),0);
  await card.getByRole('button',{name:'查看运行',exact:true}).click();await deleteLive(f);await f.getByRole('button',{name:'返回列表',exact:true}).click();
  assert((await f.locator('.topology-card').filter({hasText:'历史配置'}).count())>=2);
  await f.locator('.topology-card').filter({hasText:'历史配置'}).first().getByRole('button',{name:'查看历史配置',exact:true}).click();await f.locator('#saveState').filter({hasText:'历史运行'}).waitFor();
  return {sid,captured:true,historyVisible:true};
 });
 await scenario('导入导出、WLAN 删除级联、未保存导航保护、旧链接与窄屏',async(page,accept)=>{
  const f=await workspace(page);
  const source={schema_version:2,name:'文件往返拓扑',extension:{vendor:'keep'},nodes:[{id:1,name:'导入主机',type:'default',model:'bare',services:[],x:60,y:80}],wlans:[{id:2,name:'导入无线网络',x:300,y:80,range:'275'}],links:[{node1_id:1,node2_id:2,extension:{retained:true}}]};
  const chooser=page.waitForEvent('filechooser');await f.getByRole('button',{name:'导入',exact:true}).click();await(await chooser).setFiles({name:'topology.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(source))});
  await f.getByRole('button',{name:'选择节点 导入无线网络',exact:true}).click();await f.getByLabel('无线范围 m',{exact:true}).fill('400');
  await f.getByRole('button',{name:'删除节点',exact:true}).click();assert.equal(await f.locator('svg line').count(),0);
  await f.getByRole('button',{name:'添加无线网络',exact:true}).click();
  const event=page.waitForEvent('download');await f.getByRole('button',{name:'导出',exact:true}).click();const download=await event;const filename=path.join(root,'fixtures','topology-ui-export.json');await download.saveAs(filename);
  const exported=JSON.parse(fs.readFileSync(filename));assert.deepEqual(exported.extension,{vendor:'keep'});assert.equal(exported.links.length,0);assert.equal(exported.wlans.length,1);
  await save(page,f);await f.getByLabel('拓扑名称',{exact:true}).fill('未保存的修改');
  accept(false);await page.locator('.side-tab[data-tab="images"]').click();assert.equal(await f.getByLabel('拓扑名称',{exact:true}).inputValue(),'未保存的修改');
  accept(true);await page.locator('.side-tab[data-tab="images"]').click();await page.waitForFunction(()=>document.body.dataset.activeTab==='images');
  await page.goto(base+'/#topo');await page.waitForFunction(()=>document.body.dataset.activeTab==='topologies');await page.frameLocator('#tabFrame').getByRole('button',{name:'新建拓扑',exact:true}).waitFor();
  assert.equal(await page.frameLocator('#tabFrame').getByRole('button',{name:'运行画布',exact:true}).count(),0);
  await page.setViewportSize({width:390,height:844});
  const mobile=page.frameLocator('#tabFrame');await mobile.getByRole('button',{name:'新建拓扑',exact:true}).click();await addHost(mobile,'窄屏原生节点');
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));assert(await mobile.locator('html').evaluate(n=>n.scrollWidth<=innerWidth+1));
  assert.equal(await mobile.locator('textarea').count(),0);
  return {exportedNodes:exported.nodes.length,wlan:exported.wlans.length,navigationProtected:true,viewport:390};
 });
 await browser.close();if(results.some(r=>r.status==='failed'))process.exitCode=1;
})().catch(async e=>{console.error(e);await browser?.close();process.exitCode=1;});
