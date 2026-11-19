/* Import rules from the probe dialog, then observe a real alert in isolated CORE. */
const assert = require('node:assert/strict');
const {createHash} = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE);

const directory = process.env.BRIDGE_TEST_RUN_DIR;
const base = process.env.BRIDGE_TEST_URL;
assert(directory && new URL(base).origin === 'http://127.0.0.1:18080', '仅在隔离 Bridge 运行');
const fixture = JSON.parse(fs.readFileSync(path.join(directory, 'topology-fixture.json'), 'utf8'));
const ruleName = '流量验收.rules';
const message = 'Bridge imported rule verification';
const rule = `alert tcp any any -> any any (msg:"${message}"; content:"BRIDGE_RULE_IMPORT"; sid:1000091; rev:1;)\n`;
const digest = createHash('sha256').update(rule).digest('hex');

async function main() {
  const report = {};
  const browser = await chromium.launch({headless:true, executablePath:chromium.executablePath(), args:['--no-sandbox']});
  const context = await browser.newContext({viewport:{width:1520,height:1024}});
  const page = await context.newPage();
  const errors = [];
  page.setDefaultTimeout(15000);
  page.on('dialog', dialog => dialog.accept());
  page.on('pageerror', error => errors.push(error.message));
  const button = (name, root = page) => root.getByRole('button', {name,exact:true});
  const label = (name, root = page) => root.getByLabel(name, {exact:true});
  const dialog = name => page.getByRole('dialog', {name,exact:true});
  const probes = dialog('Snort 监测点');

  async function api(method, route, data) {
    const response = await context.request.fetch(base + route,{method,data,timeout:55000});
    const body = await response.json();
    assert(response.ok() && body.ok !== false, JSON.stringify(body));
    return body.data ?? body;
  }

  async function upload(form, name, content, status) {
    const chooser = page.waitForEvent('filechooser');
    await button('导入规则',form).click();
    await (await chooser).setFiles({name,mimeType:'text/plain',buffer:Buffer.from(content)});
    const outcome = form.getByRole('status').filter({hasText:status});
    await outcome.waitFor();
    return outcome.innerText();
  }

  try {
    const template = '验收 Docker Web';
    const sender = '发送节点', receiver = '监测节点';
    await api('POST','/instances',{
      name:template,runtime_type:'docker',image_tag:fixture.ref,target_name:fixture.catalog.target_name,
      target_version:fixture.catalog.applied_target_version,services:[],service_selection:{http:'default'},
    });
    await page.goto(base + '/static/topologies.html');
    await button('新建拓扑').click();
    await label('拓扑名称').fill('Snort 规则导入验收');
    for (const name of [sender,receiver]) {
      await button('添加模板 ' + template).click();
      await label('节点名称').fill(name);
    }
    await button('连线').click();
    await button('选择节点 ' + sender).click();
    await button('选择节点 ' + receiver).click();
    await button('监测此链路').click();
    const newProbe = dialog('新增Snort 监测点');
    await label('名称',newProbe).fill('自定义规则探针');
    await label('监测节点',newProbe).selectOption('2');
    const initialRule = await label('Snort 规则集',newProbe).inputValue();
    const invalid = 'alert tcp any any -> any any (fixture_invalid_option:1; sid:1000091; rev:1;)\n';
    report.rejected = await upload(newProbe,'无效.rules',invalid,/无法导入/);
    assert.match(report.rejected,/fixture_invalid_option/);
    assert.equal(await label('Snort 规则集',newProbe).inputValue(),initialRule);
    await upload(newProbe,ruleName,rule,/Snort 校验通过/);
    const original = await label('Snort 规则集',newProbe).inputValue();
    assert.notEqual(original,initialRule);
    assert.equal(await label('名称',newProbe).inputValue(),'自定义规则探针');
    assert.equal(await label('监测节点',newProbe).inputValue(),'2');
    assert.equal(await label('监测接口',newProbe).inputValue(),'1,2');
    await page.screenshot({path:path.join(directory,'screenshots','snort-rule-import.png')});
    await button('确定',newProbe).click();
    await button('关闭',probes).click();

    await button('Snort 监测点').click();
    await button('编辑',probes).click();
    const editProbe = dialog('编辑Snort 监测点');
    await upload(editProbe,'另一个名字.rules',rule,/已选中已有规则集/);
    assert.equal(await label('Snort 规则集',editProbe).inputValue(),original);
    await upload(editProbe,ruleName,rule.replace('rev:1','rev:2'),/ · v2/);
    const nextVersion = await label('Snort 规则集',editProbe).inputValue();
    assert.notEqual(nextVersion,original);
    await button('取消',editProbe).click();
    await editProbe.waitFor({state:'hidden'});
    await button('编辑',probes).click();
    assert.equal(await label('Snort 规则集',editProbe).inputValue(),original);
    assert.equal(await label('Snort 规则集',editProbe).locator('option[value="' + nextVersion + '"]').innerText(),ruleName + ' · v2');
    await button('关闭',editProbe).click();
    await button('关闭',probes).click();
    report.versions = {original,nextVersion};

    await button('背景流量').click();
    await button('新增背景流量',dialog('背景流量')).click();
    const traffic = dialog('新增背景流量');
    await label('名称',traffic).fill('HTTP 验收流量');
    await label('源节点',traffic).selectOption('1');
    await label('目标节点',traffic).selectOption('2');
    await label('目标端口',traffic).fill('8080');
    await label('请求路径',traffic).fill('/?probe=BRIDGE_RULE_IMPORT');
    await label('启动延迟（秒）',traffic).fill('2');
    await label('持续时间（秒）',traffic).fill('8');
    await button('确定',traffic).click();
    await button('关闭',dialog('背景流量')).click();
    const saved = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/topologies');
    await button('保存').click();
    assert((await saved).ok());
    const launched = page.waitForResponse(response => /\/topologies\/[^/]+\/deploy$/.test(response.url()),{timeout:65000});
    await button('启动').click();
    const response = await launched;
    assert(response.ok(),await response.text());
    const {data:deployment} = await response.json();
    report.deployment = deployment.deployment_id;
    assert.deepEqual(deployment.network_activity_start.failed_items,[]);
    assert.deepEqual(deployment.execution_definition.nodes.map(node => node.type),['docker','docker']);
    const activity = dialog('网络任务与 Snort 告警');
    await button('网络任务与 Snort 告警').click();
    await activity.getByText(message,{exact:true}).first().waitFor({timeout:30000});
    await activity.getByText(ruleName,{exact:true}).waitFor();
    const recordUrl = '/deployments/' + report.deployment + '/network-activity';
    const {run} = await api('GET',recordUrl + '/batches');
    const probe = Object.values(run.items).find(item => item.kind === 'probe');
    assert.equal(probe.ruleset,original);
    assert.equal(probe.ruleset_name,ruleName);
    assert.equal(probe.rules_sha256,digest);
    const alerts = (await api('GET',recordUrl + '/' + run.run_id + '/alerts')).items;
    const alert = alerts.find(item => item.detail.msg === message);
    assert.equal(alert.detail.ruleset_name,ruleName);
    assert.equal(alert.detail.rules_sha256,digest);
    report.run = run.run_id;
    report.alert = alert.detail;
    assert.deepEqual(errors,[]);
    await page.screenshot({path:path.join(directory,'screenshots','snort-imported-rule-alert.png')});
    report.status = 'passed';
  } catch (error) {
    report.status = 'failed';
    report.error = error.stack;
    await page.screenshot({path:path.join(directory,'screenshots','snort-rule-import-failure.png')}).catch(() => {});
    process.exitCode = 1;
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(directory,'evidence','snort-rule-import.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report,null,2));
  }
}

main().catch(error => {console.error(error);process.exitCode = 1;});
