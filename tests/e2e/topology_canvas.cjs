/* Browser regression for the topology canvas. All edits stay in a browser draft. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root = process.env.BRIDGE_TEST_RUN_DIR;
const base = process.env.BRIDGE_TEST_URL;
const topology = {
  name:'画布验证', nodes:[
    {id:1, name:'主机一', type:'default', model:'bare', x:70, y:75},
    {id:2, name:'主机二', type:'default', model:'bare', x:330, y:75}
  ], links:[]
};

async function main() {
  const browser = await chromium.launch({headless:true, executablePath:chromium.executablePath(), args:['--no-sandbox']});
  const context = await browser.newContext({viewport:{width:1600,height:1300}, acceptDownloads:true});
  const page = await context.newPage();
  page.setDefaultTimeout(8000);
  const report = {checks:[], errors:[], writes:[]};
  page.on('pageerror', error => report.errors.push(error.message));
  await context.route('**/*', route => {
    const request = route.request();
    if (new URL(request.url()).origin === new URL(base).origin && !['GET','HEAD','OPTIONS'].includes(request.method())) {
      report.writes.push(request.method() + ' ' + new URL(request.url()).pathname);
      return route.abort();
    }
    return route.continue();
  });
  const frame = page.frameLocator('#tabFrame');
  const button = name => frame.getByRole('button', {name,exact:true});
  const canvas = frame.getByRole('region', {name:'拓扑画布'});
  const zoom = frame.getByLabel('缩放比例', {exact:true});
  const node = name => button('选择节点 ' + name);
  async function drag(x, y, dx, dy) {
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x + dx, y + dy, {steps:6});
    await page.mouse.up();
  }
  async function importDraft() {
    const chooser = page.waitForEvent('filechooser');
    await button('导入').click();
    await (await chooser).setFiles({name:'canvas.json', mimeType:'application/json', buffer:Buffer.from(JSON.stringify(topology))});
    await canvas.waitFor();
    await canvas.scrollIntoViewIfNeeded();
  }
  try {
    await page.goto(base + '/#topologies');
    await importDraft();
    assert.equal(await zoom.textContent(), '100%');
    const bounds = await canvas.boundingBox(), beforePan = await node('主机一').boundingBox();
    await drag(bounds.x + 220, bounds.y + 245, 44, 38);
    const afterPan = await node('主机一').boundingBox();
    assert.equal(afterPan.x - beforePan.x, 44);
    assert.equal(afterPan.y - beforePan.y, 38);
    report.checks.push('空白处平移');

    // Zoom around a point on the first node, then drag the node in scaled coordinates.
    await page.mouse.move(afterPan.x + afterPan.width / 2, afterPan.y + afterPan.height / 2);
    await page.mouse.wheel(0, -180);
    await zoom.filter({hasText:/^(?!100%)\d+%$/}).waitFor();
    const enlarged = await node('主机一').boundingBox();
    assert(enlarged.width > afterPan.width);
    assert(Math.abs(enlarged.x + enlarged.width/2 - (afterPan.x + afterPan.width/2)) < 1);
    const scale = enlarged.width / afterPan.width;
    await drag(enlarged.x + enlarged.width/2, enlarged.y + enlarged.height/2, -70, 55);
    const moved = await node('主机一').boundingBox();
    assert(Math.abs(moved.x - enlarged.x + 70) < 2);
    assert(Math.abs(moved.y - enlarged.y - 55) < 2);
    const zoomBeforeSelection = await zoom.textContent();
    await node('主机一').click();
    assert.equal(await zoom.textContent(), zoomBeforeSelection);
    report.checks.push('滚轮锚点、缩放后节点拖动、选中后视图保留');

    await button('适应画布').click();
    await button('连线').click();
    await node('主机一').click();
    await node('主机二').click();
    await frame.getByRole('heading', {name:'链路属性',exact:true}).waitFor();
    await node('主机一').click();
    await button('链路 主机一 — 主机二').click();
    await frame.getByRole('heading', {name:'链路属性',exact:true}).waitFor();
    const downloadEvent = page.waitForEvent('download');
    await button('导出').click();
    const exported = path.join(root,'browser-draft.json');
    await (await downloadEvent).saveAs(exported);
    const draft = JSON.parse(fs.readFileSync(exported,'utf8'));
    assert.equal(draft.nodes[0].x, 70 + Math.round(-70 / scale));
    assert.equal(draft.nodes[0].y, 75 + Math.round(55 / scale));
    assert.equal(draft.nodes[1].x, 330);
    assert.equal(draft.links.length, 1);
    report.checks.push('连线、点选链路、导出布局坐标');

    await button('重置视图').click();
    assert.equal(await zoom.textContent(),'100%');
    await button('缩小').click();
    assert(parseInt(await zoom.textContent(),10) < 100);
    await button('放大').click();
    assert.equal(await zoom.textContent(),'100%');
    await canvas.press('0');
    assert.equal(await zoom.textContent(),'100%');
    await page.screenshot({path:path.join(root,'edit-canvas.png')});
    report.checks.push('工具栏缩放、适应画布与重置');

    // Read-only fixture exercises history without creating or touching a CORE session.
    await context.route('**/topology-workspace', async route => {
      const response = await route.fetch();
      const payload = await response.json();
      payload.data = {...payload.data, topologies:[], sessions:[], deployments:[{
        deployment_id:'canvas-fixture', display_name:'画布历史验证', live:false,
        status:'closed', execution_definition:{...draft, name:'画布历史验证'}
      }]};
      await route.fulfill({response,json:payload});
    });
    page.once('dialog', dialog => dialog.accept());
    await button('返回列表').click();
    await frame.getByRole('heading', {name:'拓扑列表',exact:true}).waitFor();
    await page.reload();
    await button('查看历史配置').click();
    await canvas.scrollIntoViewIfNeeded();
    const readonlyNode = await node('主机一').boundingBox();
    await drag(readonlyNode.x + readonlyNode.width/2, readonlyNode.y + readonlyNode.height/2, 28, 36);
    assert.deepEqual(await node('主机一').boundingBox(), readonlyNode);
    const historyBounds = await canvas.boundingBox();
    await drag(historyBounds.x+220, historyBounds.y+260, 25, 20);
    assert.equal((await node('主机一').boundingBox()).x - readonlyNode.x,25);
    await button('放大').click();
    assert.equal(await zoom.textContent(),'120%');
    assert.match(await frame.locator('#saveState').textContent(),/历史运行/);
    report.checks.push('只读历史视图可平移缩放，节点布局不变');
    assert.deepEqual(report.writes, []);
    assert.deepEqual(report.errors, []);
    report.status='passed';
  } catch (error) {
    report.status='failed';
    report.message=error.stack;
    await page.screenshot({path:path.join(root,'failure.png')}).catch(() => {});
    process.exitCode=1;
  } finally {
    fs.writeFileSync(path.join(root,'canvas-results.json'), JSON.stringify(report,null,2));
    console.log(JSON.stringify(report,null,2));
    await browser.close();
  }
}
main().catch(error => {console.error(error);process.exitCode=1;});
