/* Read-only deployment smoke: browser-local unsaved draft, no save/start/mutating HTTP. */
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE);
const root=process.env.BRIDGE_TEST_RUN_DIR,base=process.env.BRIDGE_TEST_URL;
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:chromium.executablePath(),args:['--no-sandbox']});
 const context=await browser.newContext({viewport:{width:1366,height:900}}),page=await context.newPage();page.setDefaultTimeout(15000);
 const f=()=>page.frameLocator('#tabFrame'),answers=[],handled=new Map(),report={dialogs:[],mutations:[],errors:[]};
 page.on('request',r=>{if(!['GET','HEAD','OPTIONS'].includes(r.method()))report.mutations.push(r.method()+' '+r.url());});
 page.on('pageerror',e=>report.errors.push(e.message));
 page.on('dialog',d=>{const accept=answers.shift();report.dialogs.push({type:d.type(),message:d.message(),accept});handled.set(d,accept?d.accept():d.dismiss());});
 async function answer(run,accept){answers.push(accept);const event=page.waitForEvent('dialog');await run();const d=await event;await handled.get(d);}
 try{
  await page.goto(base+'/#topologies');await f().getByRole('button',{name:'新建拓扑',exact:true}).click();await f().getByLabel('拓扑名称',{exact:true}).fill('未保存的导航验证');await f().getByRole('button',{name:'添加原生主机',exact:true}).click();
  await answer(()=>page.locator('.side-tab[data-tab="images"]').click(),false);
  assert.equal(await f().getByLabel('拓扑名称',{exact:true}).inputValue(),'未保存的导航验证');assert(page.url().endsWith('#topologies'));
  await page.screenshot({path:path.join(root,'screenshots/topology-navigation-cancel.png'),fullPage:true});
  await answer(()=>page.locator('.side-tab[data-tab="images"]').click(),true);await f().getByRole('heading',{name:'镜像管理',exact:true}).waitFor();
  await page.locator('.side-tab[data-tab="topologies"]').click();await f().getByRole('heading',{name:'拓扑列表',exact:true}).waitFor();assert.equal(await f().locator('.topology-card').filter({hasText:'未保存的导航验证'}).count(),0);
  assert.equal(report.dialogs.length,2);assert.deepEqual(report.errors,[]);assert.deepEqual(report.mutations,[]);report.status='passed';
 }catch(e){report.status='failed';report.message=e.message;process.exitCode=1;}
 finally{fs.writeFileSync(path.join(root,'platform-navigation-smoke.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));await context.close();await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
