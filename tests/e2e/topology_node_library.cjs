/* Exercise aligned workspace panels with long libraries and node properties. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE);

const root = process.env.BRIDGE_TEST_RUN_DIR;
const base = process.env.BRIDGE_TEST_URL;
const origin = new URL(base).origin;
const templates = [
  ...Array.from({length:40},(_,i)=>({name:`Docker 验收 ${i+1}`,runtime_type:'docker',version:1,image_tag:'fixture:v1'})),
  ...Array.from({length:32},(_,i)=>({name:`原生验收 ${i+1}`,runtime_type:'netns',version:1})),
];
const services = Array.from({length:32},(_,i)=>({id:'service-'+i,name:'验收服务 '+(i+1),
  modes:[{id:'default',name:'常规启动',default:true}]}));

async function main() {
  const browser = await chromium.launch({headless:true,executablePath:chromium.executablePath(),args:['--no-sandbox']});
  const context = await browser.newContext({viewport:{width:1600,height:1040}});
  const page = await context.newPage();
  page.setDefaultTimeout(9500);
  const report = {writes:[],errors:[],templates:templates.length};
  page.on('pageerror',error=>report.errors.push(error.message));
  await context.route('**/*',route => {
    const request=route.request(), url=new URL(request.url());
    if (url.origin!==origin) return route.continue();
    if (url.pathname==='/image-services/node/validate' && request.method()==='POST') {
      return route.fulfill({json:{ok:true,data:{errors:[],warnings:[],order:[]}}});
    }
    if (request.method()!=='GET') {
      report.writes.push(request.method()+' '+url.pathname);
      return route.abort();
    }
    if (url.pathname==='/instances') return route.fulfill({json:{ok:true,data:templates}});
    const match=url.pathname.match(/^\/instances\/([^/]+)\/service-options$/);
    if (match) {
      const template=templates.find(item=>item.name===decodeURIComponent(match[1]));
      return route.fulfill({json:{ok:true,data:{template,catalog:{services},selection:{}}}});
    }
    return route.continue();
  });

  try {
    await page.goto(base+'/#topologies');
    const frame=page.frameLocator('#tabFrame');
    const library=frame.getByRole('complementary',{name:'节点库'});
    const search=library.getByRole('searchbox',{name:'搜索节点库'});
    const toggle=text=>library.getByRole('button',{name:text,exact:true});
    const docker=library.getByRole('region',{name:'Docker 模板列表'});
    const native=library.getByRole('region',{name:'原生模板列表'});
    const quick=name=>library.getByRole('button',{name,exact:true});
    const zoom=frame.getByLabel('缩放比例',{exact:true});
    const panels=frame.locator('.topology-workspace > .topology-panel');
    const aligned=async()=>{
      const dimensions=await panels.evaluateAll(items=>items.map(item=>{
        const {top,left,height}=item.getBoundingClientRect();
        return {top,left,height};
      }));
      assert.equal(dimensions.length,3);
      const [library, canvas, inspector]=dimensions;
      assert(canvas.height>0 && dimensions.every(panel=>panel.height===canvas.height && panel.top===canvas.top),JSON.stringify(dimensions));
      assert(library.left<canvas.left && canvas.left<inspector.left);
      return canvas.height;
    };
    await frame.getByRole('button',{name:'新建拓扑',exact:true}).click();
    await toggle('Docker 模板（40）').waitFor();
    await toggle('原生模板（32）').waitFor();
    const desktopPanel=await aligned();
    const viewport=await docker.evaluate(node=>({height:node.clientHeight,content:node.scrollHeight}));
    assert(viewport.height<=190 && viewport.content>viewport.height);
    await docker.hover({position:{x:48,y:48}});
    const outside=await docker.evaluate(()=>document.scrollingElement.scrollTop);
    const searchY=(await search.boundingBox()).y;
    await page.mouse.wheel(0,530);
    const child=page.frames().find(candidate=>candidate.url().includes('/static/topologies.html'));
    await child.waitForFunction(()=>document.querySelector('[aria-label="Docker 模板列表"]').scrollTop>100);
    assert.equal(await docker.evaluate(()=>document.scrollingElement.scrollTop),outside);
    assert.equal((await search.boundingBox()).y,searchY);
    assert.equal(await native.evaluate(node=>node.scrollTop),0);
    assert.equal(await zoom.textContent(),'100%');

    await toggle('原生模板（32）').click();
    await native.waitFor({state:'hidden'});
    const chosen=quick('添加模板 Docker 验收 18');
    await chosen.scrollIntoViewIfNeeded();
    const before=await docker.evaluate(node=>node.scrollTop);
    assert(before>0);
    await chosen.click();
    await frame.getByRole('button',{name:'选择节点 Docker 验收 18 1',exact:true}).waitFor();
    const properties=frame.getByRole('region',{name:'属性内容'});
    await properties.getByRole('cell',{name:'验收服务 32',exact:true}).waitFor();
    assert.equal(await aligned(),desktopPanel);
    assert(await properties.evaluate(node=>node.scrollHeight>node.clientHeight));
    await properties.hover({position:{x:8,y:65}});
    const inspectorTop=await properties.evaluate(()=>document.scrollingElement.scrollTop);
    await page.mouse.wheel(0,470);
    await child.waitForFunction(()=>document.querySelector('[aria-label="属性内容"]').scrollTop>100);
    assert.equal(await properties.evaluate(()=>document.scrollingElement.scrollTop),inspectorTop);
    assert.equal(await aligned(),desktopPanel);
    await frame.getByRole('button',{name:'放大',exact:true}).click();
    assert.equal(await zoom.textContent(),'120%');
    await child.waitForFunction(expected=>Math.abs(document.querySelector('[aria-label="Docker 模板列表"]').scrollTop-expected)<2,before);
    await native.waitFor({state:'hidden'});
    await search.fill('原生验收 32');
    await toggle('原生模板（1）').waitFor();
    await quick('添加模板 原生验收 32').waitFor();
    await toggle('Docker 模板（0）').waitFor();
    await quick('添加原生主机').waitFor();
    await quick('添加交换机').waitFor();
    await search.fill('');
    await toggle('原生模板（32）').waitFor();
    await native.waitFor({state:'hidden'});
    await child.waitForFunction(expected=>Math.abs(document.querySelector('[aria-label="Docker 模板列表"]').scrollTop-expected)<2,before);
    await page.screenshot({path:path.join(root,'screenshots','node-library-desktop.png')});

    await page.setViewportSize({width:1366,height:768});
    await toggle('原生模板（32）').click();
    const laptop=await native.evaluate(node=>({height:node.clientHeight,content:node.scrollHeight}));
    assert(laptop.height>0 && laptop.height<=190 && laptop.content>laptop.height);
    const laptopPanel=await aligned();
    await quick('添加交换机').click();
    await frame.getByRole('button',{name:'选择节点 交换机 2',exact:true}).waitFor();
    assert(await child.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    assert.deepEqual(report.writes,[]);
    assert.deepEqual(report.errors,[]);
    Object.assign(report,{status:'passed',desktopPanel,laptopPanel,desktopListHeight:viewport.height,laptopListHeight:laptop.height,
      preservedScroll:before,folding:true,search:true,quickAdd:true,inspectorScroll:true});
    await page.screenshot({path:path.join(root,'screenshots','node-library-laptop.png')});
  } catch (error) {
    Object.assign(report,{status:'failed',error:error.stack});
    process.exitCode=1;
  } finally {
    fs.writeFileSync(path.join(root,'evidence','node-library.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report,null,2));
    await browser.close();
  }
}

main().catch(error=>{console.error(error);process.exitCode=1;});
