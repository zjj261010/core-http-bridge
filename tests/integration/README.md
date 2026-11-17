# 真实功能验收

使用专用 CORE daemon、Bridge 端口及数据/服务/临时目录。禁止指向生产 8080。所有请求以 run_id / case_id / request_id 独立留证；测试仅清理本次创建的会话、容器及镜像 tag。

用户会重启 WSL。维护依赖和完整验收工件使用 `~/.local/share/core-http-bridge/`；通过 `scripts/dev-tools.py` 设置固定路径，详细说明见 `docs/platform/WSL持久化与维护.md`。已标记 `ARCHIVED` 的目录只供追溯，不能再次启动旧环境。

`real_acceptance.py --url http://127.0.0.1:18080 --run-dir <目录>` 读取 `<目录>/context.json` 的 fixture 与控制器参数，执行一次真实 API/CORE/Docker/WebSocket/SSE 验收。它不会替换产品函数。浏览器使用 `tests/e2e/real_browser.cjs`，需要 Playwright 与 Chromium，可通过 `PLAYWRIGHT_MODULE` 指向已安装包。请在一个验收批次中各运行一次；失败先保留证据并分析，不自动循环重跑。

context 的 fixture 必须包含合法 Docker 归档、服务定义及模板；不将大镜像、浏览器或依赖目录提交到仓库。EMANE 不可用时明确标记 BLOCKED。控制器仅绑定本机并使用随机令牌，提供专用环境的 daemon/Bridge 重启和退出能力。

准备示例（镜像需包含 bash、httpd；Redis 矩阵另需本机 `target-inn-base:v3`）：

```bash
CORE_BRIDGE_RUN_DIR=$(python3 scripts/dev-tools.py new-run acceptance)
python3 scripts/dev-tools.py python tests/integration/prepare_environment.py --run-dir "$CORE_BRIDGE_RUN_DIR" --start-via-core
python3 scripts/dev-tools.py python tests/integration/real_acceptance.py --url http://127.0.0.1:18080 --run-dir "$CORE_BRIDGE_RUN_DIR"
```

`--start-via-core` 显式通过本机 CORE 的 ExecuteScript 启动专用环境；不修改或重启生产 daemon。不指定时仅准备文件，可由管理员运行生成的 bootstrap.py。完成后运行 `cleanup_environment.py --run-dir <目录>`。supervisor 是生产 CORE 启动的子进程，结束后可由同一父进程 waitpid 回收；本轮具体记录见工件 `reaped.json`。

`focused_acceptance.py` 和 `focused_browser.cjs` 是已征得用户同意的定向补验，不应默认自动循环执行。`closure_acceptance.py` 覆盖 XML 绑定、两个真实实现版本并行、直连/交换机 ICMP/HTTP/Redis 与吞吐量。每份结果和原始失败证据分别保留。

单测依赖列在根目录 `requirements-dev.txt`。通过固定工具入口加载持久目录中的 pytest/httpx，未把测试依赖安装进生产虚拟环境。浏览器版本为 Playwright 1.65.1，浏览器二进制也保存在持久工具目录；历史 environment.json/基线记录保留具体版本。以上示例会实际启动验收，仅在授权批次中运行，不因路径调整自动重跑。

## 拓扑交互流程（2026-11-18）

新的真实流程入口是 `tests/e2e/topology_flow.cjs`。准备专用环境后先运行 `topology_flow_fixture.py --run-dir <目录>`，只准备 HTTP 镜像与服务目录，不创建模板、拓扑或会话；浏览器负责完成实际交互并启动资源。通过 `scripts/dev-tools.py browser --run-dir <目录> --url http://127.0.0.1:18080 tests/e2e/topology_flow.cjs` 运行。

覆盖无模板原生主机/交换机、多 Docker 和原生模板、同画布连线与接口参数、保存重开、真实 ICMP 和跨节点 HTTP、未保存活动会话另存及状态关联、历史恢复、文件往返、WLAN 删除级联、导航保护和窄屏。`BRIDGE_TOPOLOGY_FOCUS=1` 仅用于明确授权后的前三条补验，不自动执行。

CORE 的原生节点使用 Unix socket，运行路径不能过长。准备脚本将 `coretmp` 链接到当前账户 `~/.local/share/core-http-bridge/run/<短标识>/`，supervisor 使用解析后的短路径。它与验收证据一样持久保存，不使用系统临时目录。应用将原生节点运行名与用户显示名分开，避免中文、空格、斜杠和同名标签破坏底层命令及路径。

`real_browser.cjs`、`focused_browser.cjs` 和 `remaining_plan_browser.cjs` 保留旧版本的验收事实，其会话画布和 JSON 表单选择器已不适用于新的拓扑工作区，不应作为当前 UI 验收入口。当前拓扑流程以上述新用例为准；其他模块按各自最新的浏览器用例验收，不把历史用例失败当成自动重复测试的理由。
