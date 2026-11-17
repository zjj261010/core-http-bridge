/* One-pass browser coverage using the isolated real API; no route stubs. */
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=process.env.BRIDGE_TEST_RUN_DIR,base='http://127.0.0.1:18080';
assert(root,'BRIDGE_TEST_RUN_DIR is required');
const C=JSON.parse(fs.readFileSync(path.join(root,'browser-context.json'),'utf8'));
const results=[],focusOnly=process.env.BRIDGE_REDESIGN_FOCUS==='1',reportName=focusOnly?'image-services-browser-focused':'image-services-browser';let browser;
async function json(page,url){const response=await page.request.get(base+url);assert(response.ok(),url);const data=await response.json();return data.data??data;}
async function scenario(name,run){if(focusOnly&&!['通过实际模板表格选择服务并检查依赖','节点覆盖、恢复默认及运行历史保持独立','窄屏仍可导航，旧发布入口跳转，主导航只保留拓扑管理'].includes(name))return;const context=await browser.newContext({viewport:{width:1440,height:1050},acceptDownloads:true}),page=await context.newPage(),errors=[];page.setDefaultTimeout(12000);page.on('pageerror',error=>errors.push(error.message));page.on('dialog',dialog=>dialog.accept());let status='passed',detail;
  await context.tracing.start({screenshots:true,snapshots:true,sources:true});
  try{detail=await run(page);assert.deepEqual(errors,[]);}catch(error){status='failed';detail={message:error.message,stack:error.stack};}
  await page.screenshot({path:path.join(root,'screenshots',reportName+'-'+(results.length+1)+'.png'),fullPage:true}).catch(()=>{});
  await context.tracing.stop({path:path.join(root,'evidence',reportName+'-'+(results.length+1)+'.zip')}).catch(()=>{});
  results.push({name,status,detail,errors});fs.writeFileSync(path.join(root,reportName+'.json'),JSON.stringify(results,null,2));console.log(status+': '+name);await context.close();
}
async function openImage(page,ref=C.ref){await page.goto(base+'/static/target-admin.html');const row=page.getByRole('row').filter({hasText:ref});await row.getByRole('button',{name:'详情',exact:true}).click();await page.locator('dialog[open]').waitFor();return page.locator('dialog[open]').first();}
async function closeDirty(page,parent){await parent.getByRole('button',{name:'取消',exact:true}).click();await page.locator('dialog[open]').waitFor({state:'detached'});}

(async()=>{
  browser=await chromium.launch({headless:true,executablePath:chromium.executablePath(),args:['--no-sandbox']});
  await scenario('真实脚本上传、两级暂存、取消及焦点返回',async page=>{
    const before=await json(page,'/image-services/detail?ref='+encodeURIComponent(C.ref)),parent=await openImage(page);
    assert.equal(await parent.locator('textarea').count(),0);
    const service=parent.getByRole('row').filter({hasText:'HTTP 服务'});await service.getByRole('button',{name:'详情',exact:true}).click();
    assert.equal(await page.locator('dialog[open]').count(),2);const child=page.locator('dialog[open]').last();
    await child.getByRole('button',{name:'新增模式',exact:true}).click();await child.getByLabel('模式名称',{exact:true}).fill('浏览器脚本模式');
    await child.getByLabel('选择脚本文件',{exact:true}).setInputFiles({name:'browser-start.sh',mimeType:'text/plain',buffer:Buffer.from('#!/bin/sh\nprintf browser\n')});
    await child.getByRole('cell',{name:'browser-start.sh',exact:true}).waitFor();
    await child.getByRole('button',{name:'确定并暂存回镜像详情',exact:true}).click();
    assert.equal(await page.locator('dialog[open]').count(),1);
    assert.equal(await page.evaluate(()=>document.activeElement.dataset.returnId),'http');
    assert.equal((await json(page,'/image-services/detail?ref='+encodeURIComponent(C.ref))).revision,before.revision);
    await closeDirty(page,parent);
    const after=await json(page,'/image-services/detail?ref='+encodeURIComponent(C.ref));assert.deepEqual(after.services,before.services);
    return {revision:after.revision,filesUploaded:true,stagingCancelled:true};
  });
  await scenario('真实清单导入冲突预览、配置附件导出和删除声明提醒',async page=>{
    const parent=await openImage(page),downloadEvent=page.waitForEvent('download');
    await parent.getByRole('button',{name:'导出当前配置与脚本',exact:true}).click();const download=await downloadEvent;const file=path.join(root,'fixtures','browser-config.zip');await download.saveAs(file);assert(fs.statSync(file).size>0);
    const chooserEvent=page.waitForEvent('filechooser');await parent.getByRole('button',{name:'导入清单',exact:true}).click();await (await chooserEvent).setFiles(file);
    await parent.getByRole('heading',{name:'导入冲突预览',exact:true}).waitFor();assert.equal(await page.locator('dialog[open]').count(),1);
    const preview=parent.locator('section').filter({has:page.getByRole('heading',{name:'导入冲突预览',exact:true})}).last();assert((await preview.getByRole('combobox').count())>0);
    await preview.getByRole('button',{name:'取消预览',exact:true}).click();
    const row=parent.getByRole('row').filter({hasText:'代理服务'});await row.getByRole('button',{name:'移除声明',exact:true}).click();await parent.getByRole('heading',{name:'移除 代理服务 的声明？',exact:true}).waitFor();assert((await parent.innerText()).includes('不卸载软件'));
    await parent.getByRole('button',{name:'保留',exact:true}).click();await parent.getByRole('button',{name:'取消',exact:true}).click();return {zipBytes:fs.statSync(file).size};
  });
  await scenario('旧服务模式可合并，父弹窗取消不改变旧导入内容',async page=>{
    const baseline=await json(page,'/image-services/detail?ref='+encodeURIComponent(C.legacy_ref)),parent=await openImage(page,C.legacy_ref);
    await parent.getByRole('button',{name:/预览导入 /}).first().click();await parent.getByRole('button',{name:'合入本次编辑',exact:true}).click();
    const serviceRows=parent.locator('section').filter({has:page.getByRole('heading',{name:'服务清单',exact:true})}).first().getByRole('button',{name:'详情',exact:true});await serviceRows.first().click();
    const child=page.locator('dialog[open]').last();await child.getByRole('heading',{name:'将旧模式合并到此服务',exact:true}).waitFor();await child.getByRole('button',{name:'暂存移入',exact:true}).click();await child.getByRole('button',{name:'确定并暂存回镜像详情',exact:true}).click();
    assert.equal(await parent.getByRole('button',{name:'详情',exact:true}).count(),1);await closeDirty(page,parent);
    const after=await json(page,'/image-services/detail?ref='+encodeURIComponent(C.legacy_ref));assert.equal(after.revision,baseline.revision);assert.deepEqual(after.services,baseline.services);return {sourceModes:baseline.imports[0].catalog.services.length};
  });
  await scenario('通过实际模板表格选择服务并检查依赖',async page=>{
    await page.goto(base+'/static/instances.html');await page.getByRole('button',{name:'新建模板',exact:true}).click();const dialog=page.locator('dialog[open]');
    await dialog.getByLabel('模板名称',{exact:true}).fill('browser-'+C.template);
    await dialog.getByLabel('镜像',{exact:true}).selectOption(C.ref);
    const http=dialog.getByRole('row').filter({hasText:'HTTP 服务'});await http.getByRole('checkbox').check();await dialog.getByText(/缺少依赖/).waitFor();
    await dialog.getByRole('row').filter({hasText:'初始化'}).getByRole('checkbox').check();await dialog.getByText(/^启用组合检查通过/).waitFor();
    await dialog.getByRole('button',{name:'保存模板',exact:true}).click();await page.locator('dialog[open]').waitFor({state:'detached'});
    const records=await json(page,'/instances'),created=records.find(t=>t.name==='browser-'+C.template);assert(created);assert.equal(created.service_selection.http,'http-8081');assert.equal(created.service_selection.prepare,'prepare-default');return {template:created.name,revision:created.version};
  });
  await scenario('节点覆盖、恢复默认及运行历史保持独立',async page=>{
    const original=await json(page,'/topologies/'+C.topology_id);await page.goto(base+'/static/topologies.html');await page.getByRole('button',{name:original.name,exact:true}).click();await page.getByLabel('配置来源',{exact:true}).selectOption('custom');
    const http=page.getByRole('row').filter({hasText:'HTTP 服务'});await http.getByRole('checkbox').uncheck();await page.getByRole('button',{name:'保存',exact:true}).click();
    await page.getByText('拓扑已保存。',{exact:true}).waitFor();let saved=await json(page,'/topologies/'+C.topology_id);
    assert.equal(saved.definition.nodes[0].service_overrides.http,null);assert.equal(saved.definition.nodes[1].service_overrides.http,'http-8082');
    await page.getByRole('button',{name:'恢复模板默认',exact:true}).click();
    await Promise.all([page.waitForResponse(response=>response.url().endsWith('/topologies')&&response.request().method()==='GET'),page.getByRole('button',{name:'保存',exact:true}).click()]);saved=await json(page,'/topologies/'+C.topology_id);
    assert(!saved.definition.nodes[0].service_overrides);assert.equal(saved.definition.nodes[1].service_overrides.http,'http-8082');assert(saved.definition.extension.preserved);
    const deployment=(await json(page,'/deployments')).find(d=>d.topology_id===C.topology_id);assert.equal(deployment.execution_definition.nodes.find(n=>n.id===1).effective_services.find(s=>s.service_id==='http').mode_id,'http-8081');return {revision:saved.revision};
  });
  await scenario('未完整的服务可以保存待完善，确认按钮保持禁用',async page=>{
    const parent=await openImage(page);await parent.getByRole('button',{name:'新增服务',exact:true}).click();await parent.getByLabel('服务名称',{exact:true}).fill('浏览器待完善服务');await parent.getByRole('button',{name:'暂存此服务',exact:true}).click();await parent.getByRole('button',{name:'确定',exact:true}).click();
    await parent.getByText(/尚未配置启动模式/).waitFor();assert(await parent.getByRole('button',{name:'确认保存并生效',exact:true}).isDisabled());await parent.getByRole('button',{name:'仅保存，稍后生效',exact:true}).click();await parent.getByRole('heading',{name:'基本信息',exact:true}).waitFor();
    const saved=await json(page,'/image-services/detail?ref='+encodeURIComponent(C.ref));assert.equal(saved.state,'incomplete');assert(saved.applied_target_version);return {revision:saved.revision,state:saved.state};
  });
  await scenario('窄屏仍可导航，旧发布入口跳转，主导航只保留拓扑管理',async page=>{
    await page.setViewportSize({width:390,height:844});await page.goto(base+'/static/target-admin.html');await page.getByRole('button',{name:'导入',exact:true}).waitFor();await page.getByRole('link',{name:'下载完整镜像包',exact:true}).first().waitFor();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
    assert.equal(await page.getByRole('link',{name:'拓扑管理',exact:true}).count(),1);assert.equal(await page.getByRole('link',{name:'版本发布',exact:true}).count(),0);
    await page.goto(base+'/static/publications.html');await page.waitForURL(/target-admin\.html/);await page.goto(base+'/#topologies');await page.locator('#tabFrame').waitFor({state:'visible'});
    assert.equal(await page.locator('.side-tab').filter({hasText:'拓扑管理'}).count(),1);assert(!(await page.locator('#sideNav').innerText()).includes('版本草稿'));return {width:390};
  });
  await browser.close();if(results.some(item=>item.status==='failed'))process.exitCode=1;
})().catch(async error=>{console.error(error);await browser?.close();process.exitCode=1;});
