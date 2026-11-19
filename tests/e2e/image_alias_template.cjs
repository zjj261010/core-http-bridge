/* Verify one service catalog through two local labels and edit a separate native template. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE);

const directory = process.env.BRIDGE_TEST_RUN_DIR;
const base = process.env.BRIDGE_TEST_URL;
assert(directory && new URL(base).origin === 'http://127.0.0.1:18080', '仅在隔离 Bridge 运行');
const fixture = JSON.parse(fs.readFileSync(path.join(directory,'topology-fixture.json'),'utf8'));
const contextData = JSON.parse(fs.readFileSync(path.join(directory,'context.json'),'utf8'));
const primary = fixture.ref, alias = contextData.old_image;
assert.notEqual(primary,alias);

async function main() {
  const browser = await chromium.launch({headless:true,executablePath:chromium.executablePath(),args:['--no-sandbox']});
  const page = await browser.newPage({viewport:{width:1480,height:1050}});
  const report = {}, errors = [];
  page.on('pageerror',error => errors.push(error.message));
  page.on('dialog',dialog => dialog.accept());
  page.setDefaultTimeout(15000);
  const button = (name, root = page) => root.getByRole('button',{name,exact:true});
  const label = (name, root = page) => root.getByLabel(name,{exact:true});
  const dialog = name => page.getByRole('dialog',{name,exact:true});
  const row = name => page.getByRole('row').filter({has:page.getByRole('cell',{name,exact:true})});
  async function get(route) {
    const response = await page.request.get(base+route);
    assert(response.ok(),route);
    return (await response.json()).data;
  }
  async function createDocker(ref,name) {
    await page.goto(base+'/static/instances.html');
    await button('新建 Docker 模板').click();
    const modal = dialog('新建 Docker 模板');
    await label('模板名称',modal).fill(name);
    await label('镜像',modal).selectOption(ref);
    await modal.getByRole('row').filter({hasText:'HTTP 服务'}).getByRole('checkbox').check();
    await button('保存模板',modal).click();
    await modal.waitFor({state:'hidden'});
    await row(name).waitFor();
  }

  try {
    const inventory = (await get('/targets')).images;
    const digest = inventory.find(item => item.ref===primary).image_digest;
    assert(digest && digest===inventory.find(item => item.ref===alias).image_digest);
    const firstName = '已有主标签模板', aliasName = '别名模板', native = '原生模板更新验收';
    await createDocker(primary,firstName);
    await page.goto(base+'/static/target-admin.html');
    await button('详情',row(alias)).click();
    const detail = dialog('镜像详情');
    await detail.getByRole('row').filter({hasText:'配置状态'}).getByText('已生效',{exact:true}).waitFor();
    await button('确定',detail).click();
    await detail.getByRole('checkbox').check();
    await button('确认保存并生效',detail).click();
    await detail.getByText(/^已生效。/).waitFor();
    await button('关闭',detail).click();
    await createDocker(alias,aliasName);

    await page.getByRole('tab',{name:'CORE 原生节点模板',exact:true}).click();
    await button('新建原生模板').click();
    const create = dialog('新建原生模板');
    await label('模板名称',create).fill(native);
    await button('保存模板',create).click();
    await create.waitFor({state:'hidden'});
    await button('编辑',row(native)).click();
    const edit = dialog('编辑模板');
    await label('说明',edit).fill('原生模板更新成功');
    await button('保存模板',edit).click();
    await edit.waitFor({state:'hidden'});
    await button('复制',row(native)).click();
    await button('删除',row(native+'-copy')).click();
    await row(native+'-copy').waitFor({state:'hidden'});

    const templates = await get('/instances');
    const first = templates.find(item => item.name===firstName);
    const second = templates.find(item => item.name===aliasName);
    assert.deepEqual([first.image_tag,second.image_tag],[primary,alias]);
    assert.equal(first.target_name,second.target_name);
    assert.equal(first.target_version,second.target_version);
    assert.equal(first.version,1);
    const updated = templates.find(item => item.name===native);
    assert.equal(updated.notes,'原生模板更新成功');
    assert.equal(updated.version,2);
    assert.deepEqual(errors,[]);
    Object.assign(report,{status:'passed',primary,alias,digest,targetVersion:first.target_version,nativeVersion:updated.version});
    await page.screenshot({path:path.join(directory,'screenshots','image-alias-template.png')});
  } catch (error) {
    Object.assign(report,{status:'failed',error:error.stack,errors});
    process.exitCode = 1;
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(directory,'evidence','image-alias-template.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report,null,2));
  }
}

main().catch(error => {console.error(error);process.exitCode=1;});
