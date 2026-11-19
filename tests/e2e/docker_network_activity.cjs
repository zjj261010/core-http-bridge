/* Configure and run an all-Docker topology through the real Bridge and CORE. */
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE);

const directory = process.env.BRIDGE_TEST_RUN_DIR;
const base = process.env.BRIDGE_TEST_URL;
assert(directory && new URL(base).origin === 'http://127.0.0.1:18080', '仅在隔离 Bridge 运行');
const fixture = JSON.parse(fs.readFileSync(path.join(directory, 'topology-fixture.json'), 'utf8'));
const artifact = path.join(directory, 'evidence', 'docker-network-browser.json');

async function main() {
  const report = {};
  const browser = await chromium.launch({headless:true, executablePath:chromium.executablePath(), args:['--no-sandbox']});
  const context = await browser.newContext({viewport:{width:1600,height:1050}});
  const page = await context.newPage();
  const errors = [];
  page.setDefaultTimeout(15000);
  page.on('dialog', dialog => dialog.accept());
  page.on('pageerror', error => errors.push(error.message));
  const button = (name, root = page) => root.getByRole('button', {name,exact:true});
  const label = (name, root = page) => root.getByLabel(name, {exact:true});
  const dialog = name => page.getByRole('dialog', {name,exact:true});
  const activity = dialog('网络任务与 Snort 告警');
  const row = name => activity.getByRole('row').filter({hasText:name});

  async function api(method, route, data) {
    const response = await context.request.fetch(base + route,{method,data,timeout:55000});
    const body = await response.json();
    assert(response.ok() && body.ok !== false, JSON.stringify(body));
    return body.data ?? body;
  }

  async function addTraffic(name, protocol, port, duration, httpPath) {
    await button('新增背景流量',dialog('背景流量')).click();
    const form = dialog('新增背景流量');
    await label('名称',form).fill(name);
    await label('流量协议',form).selectOption(protocol);
    await label('源节点',form).selectOption('1');
    await label('目标节点',form).selectOption('2');
    assert.equal(await label('目标接口',form).inputValue(), '1,2');
    await label('目标端口',form).fill(String(port));
    await label('启动延迟（秒）',form).fill('2');
    await label('持续时间（秒）',form).fill(String(duration));
    if (httpPath) await label('请求路径',form).fill(httpPath);
    await button('确定',form).click();
    await form.waitFor({state:'hidden'});
  }

  try {
    const template = '验收 Docker Web';
    const sender = 'Docker 发送端', receiver = 'Docker 接收端';
    const longRequest = '/?acceptance=' + fixture.prefix;
    await api('POST','/instances',{
      name:template, runtime_type:'docker', image_tag:fixture.ref, target_name:fixture.catalog.target_name,
      target_version:fixture.catalog.applied_target_version, services:[], service_selection:{http:'default'},
    });
    await page.goto(base + '/static/topologies.html');
    await button('新建拓扑').click();
    await label('拓扑名称').fill('全 Docker 网络功能验收');
    for (const name of [sender,receiver]) {
      await button('添加模板 ' + template).click();
      await label('节点名称').fill(name);
    }
    await button('连线').click();
    await button('选择节点 ' + sender).click();
    await button('选择节点 ' + receiver).click();
    await button('监测此链路').click();
    const probe = dialog('新增Snort 监测点');
    await label('监测节点',probe).selectOption('2');
    await label('Snort 规则集',probe).selectOption('verification');
    await button('确定',probe).click();
    await button('关闭',dialog('Snort 监测点')).click();

    await button('背景流量').click();
    await addTraffic('HTTP 验证','http',8080,4,'/?probe=BRIDGE_IDS_CHECK');
    await addTraffic('TCP 验证','tcp',5201,3);
    await addTraffic('UDP 验证','udp',5202,3);
    await addTraffic('持续 HTTP','http',8080,90,longRequest);
    await button('关闭',dialog('背景流量')).click();

    const response = page.waitForResponse(res => /\/topologies\/[^/]+\/deploy$/.test(res.url()),{timeout:65000});
    await button('启动').click();
    const launched = await response;
    assert(launched.ok(), await launched.text());
    const {data:deployment} = await launched.json();
    report.deployment = deployment.deployment_id;
    report.sid = deployment.core_sid;
    assert.deepEqual(deployment.network_activity_start.failed_items, []);
    assert.deepEqual(deployment.execution_definition.nodes.map(node => node.type), ['docker','docker']);
    const recordUrl = '/deployments/' + report.deployment + '/network-activity';
    const names = Object.values(deployment.bindings).map(binding => binding.runtime_name);
    assert(names.every(name => /^chb-[a-f\d]+-[12]$/.test(name)));

    await button('停止运行').waitFor();
    await button('网络任务与 Snort 告警').click();
    for (const name of ['HTTP 验证','TCP 验证','UDP 验证']) {
      await row(name).getByText('已完成',{exact:true}).waitFor({timeout:26000});
    }
    await activity.getByText('Bridge IDS verification',{exact:true}).first().waitFor();
    const first = (await api('GET',recordUrl + '/batches')).run;
    report.firstRun = first.run_id;
    const items = Object.fromEntries(Object.values(first.items).map(item => [item.name,item]));
    assert(items['HTTP 验证'].metrics.successes > 0);
    assert(items['TCP 验证'].metrics.bits_per_second > 0);
    assert(items['UDP 验证'].metrics.bits_per_second > 0);
    const captured = Object.values(first.items).find(item => item.kind === 'probe');
    assert(captured.metrics.packets > 0 && captured.metrics.alerts > 0);
    report.metrics = Object.fromEntries(Object.values(first.items).map(item => [item.name,item.metrics]));
    await page.screenshot({path:path.join(directory,'screenshots','docker-network-activity.png')});

    await button('停止任务',row('持续 HTTP')).click();
    await row('持续 HTTP').getByText('已停止',{exact:true}).waitFor();
    await button('关闭',activity).click();
    await button('停止运行').click();
    await button('再次运行').waitFor({timeout:30000});
    const stopped = (await api('GET',recordUrl + '/batches')).run;
    assert.equal(stopped.items[captured.id].status,'stopped');
    const containers = execFileSync('docker',['ps','-a','--format','{{.Names}}'],{encoding:'utf8'}).split('\n');
    assert(names.every(name => !containers.includes(name)));
    const processes = execFileSync('ps',['-eo','args='],{encoding:'utf8'}).split('\n');
    const remains = processes.filter(command => command.includes(first.run_id + '/') ||
      (command.includes('network_http.py') && command.includes(longRequest)));
    assert.deepEqual(remains,[]);
    report.stopped = {containersRemoved:true, workersRemaining:remains.length};

    await button('再次运行').click();
    await button('停止运行').waitFor({timeout:60000});
    const workspace = await api('GET','/topology-workspace?summary=true');
    const live = workspace.deployments.find(run => run.core_sid === report.sid && run.live);
    assert(live);
    const restartedUrl = '/deployments/' + live.deployment_id + '/network-activity';
    const second = (await api('GET',restartedUrl + '/batches')).run;
    assert.notEqual(first.run_id,second.run_id);
    report.secondRun = second.run_id;
    const current = await api('GET','/deployments/' + live.deployment_id);
    const destination = current.bindings['2'].runtime_name;
    assert(/^chb-[a-f\d]+-2$/.test(destination));
    await button('网络任务与 Snort 告警').click();
    await row('持续 HTTP').getByText('运行中',{exact:true}).waitFor();
    execFileSync('docker',['stop','--time','1',destination],{timeout:12000});
    await row('持续 HTTP').getByText(/Docker 节点已退出或重建/).waitFor({timeout:20000});
    assert.match(await row('持续 HTTP').innerText(), /失败/);
    const interrupted = (await api('GET',restartedUrl + '/batches')).run;
    const failedProbe = interrupted.items[captured.id];
    assert.equal(failedProbe.status,'failed');
    assert.match(failedProbe.error,/已退出或重建/);
    report.externalStop = {probe:failedProbe.status,error:failedProbe.error};
    assert.deepEqual(errors,[]);
    report.status = 'passed';
  } catch (error) {
    report.status = 'failed';
    report.error = error.stack;
    process.exitCode = 1;
  } finally {
    await browser.close();
    fs.writeFileSync(artifact,JSON.stringify(report,null,2));
    console.log(JSON.stringify(report,null,2));
  }
}
main().catch(error => {console.error(error);process.exitCode = 1;});
