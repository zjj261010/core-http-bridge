/* One consolidated UI acceptance for the user-workflow repairs. Real CORE/Docker, no route stubs or API setup. */
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE);
const root=process.env.BRIDGE_TEST_RUN_DIR,base=process.env.BRIDGE_TEST_URL;
assert(root&&new URL(base).port==='18080','仅允许专用环境');
const fixture=JSON.parse(fs.readFileSync(path.join(root,'context.json'),'utf8'));
let browser,context,page,readyImage=false,readyTemplate=false,readyRun=false;const results=[],answers=[],handled=new Map(),dialogs=[],unexpected=[],errors=[],state={};
const f=()=>page.frameLocator('#tabFrame');
async function nav(name){await page.locator('.side-tab[data-tab="'+name+'"]').click();await page.waitForFunction(n=>document.body.dataset.activeTab===n,name);}
async function answer(action,accept){const entry={accept};answers.push(entry);const event=page.waitForEvent('dialog',{timeout:15000});try{await action();const dialog=await event;await handled.get(dialog);}finally{const i=answers.indexOf(entry);if(i>=0)answers.splice(i,1);}}
async function scenario(name,run){let status='passed',detail;const startErrors=errors.length,startUnexpected=unexpected.length;
 try{detail=await run();assert.equal(errors.length,startErrors,JSON.stringify(errors.slice(startErrors)));assert.equal(unexpected.length,startUnexpected,JSON.stringify(unexpected.slice(startUnexpected)));}catch(e){status=e.blocked?'blocked':'failed';detail={message:e.message,stack:e.stack};}
 await page.screenshot({path:path.join(root,'screenshots','repair-'+(results.length+1)+'.png'),fullPage:true}).catch(()=>{});
 results.push({name,status,detail});fs.writeFileSync(path.join(root,'workflow-repair-browser.json'),JSON.stringify({results,dialogs,unexpected,errors},null,2));console.log(status+': '+name);
}
function need(value,label){if(!value){const e=Error('前置条件未完成：'+label);e.blocked=true;throw e;}}
async function openImage(){await nav('images');await f().getByRole('row').filter({hasText:fixture.image}).getByRole('button',{name:'详情',exact:true}).click();return f().getByRole('dialog',{name:'镜像详情',exact:true});}
(async()=>{
 browser=await chromium.launch({headless:true,executablePath:chromium.executablePath(),args:['--no-sandbox']});
 context=await browser.newContext({viewport:{width:1536,height:1000},acceptDownloads:true});page=await context.newPage();page.setDefaultTimeout(15000);
 page.on('pageerror',e=>errors.push(e.message));
 page.on('dialog',d=>{const expected=answers.shift();dialogs.push({type:d.type(),message:d.message(),answer:expected?.accept?'accept':'dismiss',expected:!!expected});if(!expected)unexpected.push(d.message());const done=expected?.accept?d.accept():d.dismiss();handled.set(d,done);});
 page.on('response',async response=>{if(response.status()>=400){let body;try{body=await response.json();}catch{}fs.appendFileSync(path.join(root,'evidence','http-errors.jsonl'),JSON.stringify({url:response.url(),status:response.status(),body})+'\n');}});
 await context.tracing.start({screenshots:true,snapshots:true,sources:true});
 await scenario('R3/R4：真实上传镜像、大小一致、无null文字，模式Esc取消与确认保护',async()=>{
  await page.goto(base+'/');await f().getByRole('button',{name:'导入',exact:true}).click();let d=f().getByRole('dialog',{name:'导入',exact:true});await d.getByLabel('内容',{exact:true}).selectOption('image');await d.getByLabel('导入名称',{exact:true}).fill(fixture.prefix);await d.getByLabel('文件',{exact:true}).setInputFiles(path.join(fixture.bundle,'image.tar'));await d.getByRole('button',{name:'导入',exact:true}).click();
  d=f().getByRole('dialog',{name:'镜像详情',exact:true});await d.waitFor({timeout:120000});assert(!(await d.innerText()).includes('nullnull'));
  const size=await f().getByRole('row').filter({hasText:fixture.image}).filter({has:f().getByRole('button',{name:'详情',exact:true})}).getByRole('cell').nth(1).innerText();
  const detailSize=await d.getByRole('row').filter({hasText:'本地大小'}).getByRole('cell').nth(1).innerText();assert.equal(size,detailSize);
  await d.getByRole('button',{name:'新增服务',exact:true}).click();await d.getByLabel('服务名称',{exact:true}).fill('HTTP 页面服务');await d.getByLabel('用途说明',{exact:true}).fill('页面验收网页');await d.getByRole('button',{name:'暂存此服务',exact:true}).click();
  await d.getByRole('row').filter({hasText:'HTTP 页面服务'}).getByRole('button',{name:'详情',exact:true}).click();let child=f().getByRole('dialog',{name:'HTTP 页面服务 · 启动模式',exact:true});await child.getByRole('button',{name:'新增模式',exact:true}).click();await child.getByLabel('模式名称',{exact:true}).fill('未暂存模式');await child.getByLabel('启动方式',{exact:true}).selectOption('manual');
  await answer(()=>page.keyboard.press('Escape'),false);assert.equal(await child.getByLabel('模式名称',{exact:true}).inputValue(),'未暂存模式');
  await answer(()=>child.getByRole('button',{name:'关闭',exact:true}).click(),true);assert.equal(await f().locator('dialog[open]').count(),1);
  await d.getByRole('row').filter({hasText:'HTTP 页面服务'}).getByRole('button',{name:'详情',exact:true}).click();child=f().getByRole('dialog',{name:'HTTP 页面服务 · 启动模式',exact:true});assert.equal(await child.getByRole('cell',{name:'未暂存模式',exact:true}).count(),0);
  await child.getByRole('button',{name:'新增模式',exact:true}).click();await child.getByLabel('模式名称',{exact:true}).fill('自动网页');await child.getByLabel('工作目录',{exact:true}).fill('/tmp');
  await child.getByLabel('选择脚本文件',{exact:true}).setInputFiles(path.join(root,'fixtures/serve.sh'));await child.getByRole('cell',{name:'serve.sh',exact:true}).waitFor();
  await child.getByLabel('导入用途',{exact:true}).selectOption('validate');await child.getByLabel('选择脚本文件',{exact:true}).setInputFiles(path.join(root,'fixtures/ready.sh'));await child.getByRole('cell',{name:'ready.sh',exact:true}).waitFor();
  await child.getByRole('button',{name:'确定并暂存回镜像详情',exact:true}).click();await d.getByRole('button',{name:'确定',exact:true}).click();await d.getByRole('checkbox').check();await d.getByRole('button',{name:'确认保存并生效',exact:true}).click();await d.getByText(/^已生效。/).waitFor({timeout:45000});await d.getByRole('button',{name:'关闭',exact:true}).click();readyImage=true;
  return {size,detailSize,modalDiscardProtected:true};
 });
 await scenario('R3：镜像未暂存表单和已暂存修改的关闭、跨模块与浏览器刷新保护',async()=>{
  need(readyImage,'镜像配置');const d=await openImage();await d.getByRole('row').filter({hasText:'HTTP 页面服务'}).getByRole('button',{name:'编辑',exact:true}).click();await d.getByLabel('用途说明',{exact:true}).fill('未暂存的说明');
  await answer(()=>d.getByRole('button',{name:'关闭',exact:true}).click(),false);assert.equal(await d.getByLabel('用途说明',{exact:true}).inputValue(),'未暂存的说明');
  await d.getByRole('button',{name:'暂存此服务',exact:true}).click();await answer(()=>page.locator('.side-tab[data-tab="instances"]').click(),false);await d.getByRole('row').filter({hasText:'未暂存的说明'}).waitFor();assert(page.url().endsWith('#images'));
  await answer(()=>{page.reload({waitUntil:'domcontentloaded',timeout:5000}).catch(()=>{});},false);await d.getByRole('row').filter({hasText:'未暂存的说明'}).waitFor();
  await answer(()=>page.locator('.side-tab[data-tab="instances"]').click(),true);await f().getByRole('button',{name:'新建 Docker 模板',exact:true}).waitFor();
  await nav('images');await f().getByRole('row').filter({hasText:fixture.image}).getByRole('button',{name:'详情',exact:true}).click();const reopened=f().getByRole('dialog',{name:'镜像详情',exact:true});assert(!(await reopened.innerText()).includes('未暂存的说明'));await reopened.getByRole('button',{name:'关闭',exact:true}).click();return {close:true,navigation:true,reload:true};
 });
 await scenario('R3：模板编辑取消离开保留内容，完成真实模板创建',async()=>{
  need(readyImage,'镜像配置');await nav('instances');await f().getByRole('button',{name:'新建 Docker 模板',exact:true}).click();const d=f().getByRole('dialog',{name:'新建 Docker 模板',exact:true});await d.getByLabel('模板名称',{exact:true}).fill('网页模板');await d.getByLabel('镜像',{exact:true}).selectOption(fixture.image);await d.getByRole('row').filter({hasText:'HTTP 页面服务'}).getByRole('checkbox').check();
  await answer(()=>page.locator('.side-tab[data-tab="topologies"]').click(),false);assert.equal(await d.getByLabel('模板名称',{exact:true}).inputValue(),'网页模板');
  await d.getByRole('button',{name:'保存模板',exact:true}).click();await d.waitFor({state:'detached'});await f().getByRole('row').filter({hasText:'网页模板'}).waitFor();readyTemplate=true;return {template:'网页模板'};
 });
 await scenario('R2：页面编排真实混合拓扑，节点命令HTTP 200及Docker/原生终端',async()=>{
  need(readyTemplate,'模板');await nav('topologies');await f().getByRole('button',{name:'新建拓扑',exact:true}).click();await f().getByLabel('拓扑名称',{exact:true}).fill('保留名称的真实拓扑');await f().getByRole('button',{name:'添加模板 网页模板',exact:true}).click();await f().getByLabel('节点名称',{exact:true}).fill('应用服务器');await f().getByRole('button',{name:'添加原生主机',exact:true}).click();await f().getByLabel('节点名称',{exact:true}).fill('测试终端');await f().getByRole('button',{name:'添加交换机',exact:true}).click();
  for(const node of ['应用服务器','测试终端']){await f().getByRole('button',{name:'连线',exact:true}).click();await f().getByRole('button',{name:'选择节点 '+node,exact:true}).click();await f().getByRole('button',{name:'选择节点 交换机 3',exact:true}).click();}
  await f().getByRole('button',{name:'保存',exact:true}).click();await f().locator('#saveState').filter({hasText:'已保存'}).waitFor();
  await answer(()=>f().getByRole('button',{name:'启动',exact:true}).click(),true);await f().getByRole('button',{name:'停止运行',exact:true}).waitFor({timeout:90000});
  await f().getByRole('button',{name:'选择节点 测试终端',exact:true}).click();await f().getByRole('button',{name:'执行命令',exact:true}).click();const d=f().getByRole('dialog',{name:'测试终端 · 执行命令',exact:true});
  await d.getByLabel('节点命令',{exact:true}).fill('python3 -c "import urllib.request;print(urllib.request.urlopen(\'http://10.90.0.1:8080\',timeout=5).status)"');await d.getByLabel('节点命令',{exact:true}).press('Enter');await d.getByText('执行完成',{exact:true}).waitFor({timeout:20000});const output=await d.locator('pre').innerText();assert(output.includes('\n200\n'));await d.getByRole('button',{name:'关闭',exact:true}).click();
  for(const [name,marker] of [['测试终端','native-terminal-ok'],['应用服务器','docker-terminal-ok']]){await f().getByRole('button',{name:'选择节点 '+name,exact:true}).click();const event=page.waitForEvent('popup');await f().getByRole('link',{name:'终端',exact:true}).click();const terminal=await event;terminal.setDefaultTimeout(20000);await terminal.getByText('connected',{exact:true}).waitFor();await terminal.getByPlaceholder('Type command, Enter to execute...',{exact:true}).fill('printf \' '+marker+'\\n\'');await terminal.getByPlaceholder('Type command, Enter to execute...',{exact:true}).press('Enter');await terminal.locator('#output').getByText(marker,{exact:true}).waitFor();await terminal.screenshot({path:path.join(root,'screenshots',marker+'.png')});await terminal.close();}
  readyRun=true;return {http:output,nativeTerminal:true,dockerTerminal:true};
 });
 await scenario('R5：监测自动选中当前拓扑，事件、指标和容器展示用户名称',async()=>{
  need(readyRun,'运行拓扑');await nav('monitor');await f().locator('#contextMessage').filter({hasText:'保留名称的真实拓扑'}).waitFor();const sid=await f().locator('#sessSel').inputValue();assert(sid);state.sid=sid;assert((await f().locator('#sessSel option:checked').innerText()).includes('运行中'));
  await f().getByRole('button',{name:'▶ 订阅',exact:true}).click();await f().locator('#evLog').filter({hasText:'应用服务器'}).waitFor({timeout:20000});await f().locator('#evLog').filter({hasText:'测试终端'}).waitFor();await page.waitForTimeout(5500); // Wait one documented sampler period before the first history query.
  await f().getByRole('button',{name:'查询',exact:true}).click();await f().locator('#histTbl').filter({hasText:'应用服务器'}).waitFor({timeout:20000});await f().locator('#coreTbl').filter({hasText:'应用服务器'}).waitFor({timeout:20000});
  return {sid,selection:await f().locator('#sessSel option:checked').innerText(),events:(await f().locator('#evLog').innerText()).slice(-2500),history:await f().locator('#histTbl').innerText()};
 });
 await scenario('R6：停止状态一致，一次确认恢复原会话而非新增会话',async()=>{
  need(readyRun,'运行拓扑');await nav('topologies');await f().locator('.topology-card').filter({hasText:'保留名称的真实拓扑'}).getByRole('button',{name:'查看运行',exact:true}).click();await answer(()=>f().getByRole('button',{name:'停止运行',exact:true}).click(),true);await f().getByRole('button',{name:'再次运行',exact:true}).waitFor({timeout:30000});assert((await f().locator('#saveState').innerText()).startsWith('已停止'));
  await f().getByRole('button',{name:'返回列表',exact:true}).click();const card=f().locator('.topology-card').filter({hasText:'保留名称的真实拓扑'});await card.getByText('已停止',{exact:true}).waitFor();await card.getByRole('button',{name:'查看会话',exact:true}).click();
  await answer(()=>f().getByRole('button',{name:'再次运行',exact:true}).click(),true);await f().getByRole('button',{name:'停止运行',exact:true}).waitFor({timeout:90000});
  await nav('monitor');await f().locator('#contextMessage').filter({hasText:'保留名称的真实拓扑'}).waitFor();assert.equal(await f().locator('#sessSel').inputValue(),state.sid);assert.equal(await f().locator('#sessSel option').count(),2);
  return {sid:state.sid,sessionCount:1};
 });
 await scenario('R1：删除会话和保存配置后历史保留原名，MTU=0复制保存重开',async()=>{
  need(readyRun,'运行拓扑');await nav('topologies');await f().locator('.topology-card').filter({hasText:'保留名称的真实拓扑'}).getByRole('button',{name:'查看运行',exact:true}).click();await answer(()=>f().getByRole('button',{name:'删除会话',exact:true}).click(),true);await f().locator('#saveState').filter({hasText:'已结束或被替换'}).waitFor({timeout:40000});await f().getByRole('button',{name:'返回列表',exact:true}).click();await f().locator('.topology-card').filter({hasText:'保留名称的真实拓扑'}).getByRole('button',{name:'打开配置',exact:true}).click();await answer(()=>f().getByRole('button',{name:'删除拓扑',exact:true}).click(),true);
  await f().getByLabel('搜索拓扑',{exact:true}).fill('保留名称');const card=f().locator('.topology-card').filter({hasText:'保留名称的真实拓扑'});await card.getByRole('button',{name:'查看历史配置',exact:true}).click();await f().getByRole('button',{name:'复制为可编辑拓扑',exact:true}).click();await f().getByLabel('拓扑名称',{exact:true}).fill('历史恢复拓扑');await f().getByRole('button',{name:'保存',exact:true}).click();await f().locator('#saveState').filter({hasText:'已保存'}).waitFor();await f().getByRole('button',{name:'返回列表',exact:true}).click();await f().getByLabel('搜索拓扑',{exact:true}).fill('历史恢复拓扑');await f().locator('.topology-card').filter({hasText:'历史恢复拓扑'}).getByRole('button',{name:'打开配置',exact:true}).click();assert.equal(await f().locator('.canvas-node').count(),3);assert.equal(await f().locator('svg line').count(),2);
  await page.setViewportSize({width:1366,height:768});return {restored:true,nodes:3,links:2};
 });
 await context.tracing.stop({path:path.join(root,'evidence/workflow-repair-browser.zip')});await context.close();await browser.close();if(results.some(r=>r.status!=='passed'))process.exitCode=1;
})().catch(async e=>{console.error(e);await browser?.close();process.exitCode=1;});
