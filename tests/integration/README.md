# 真实功能验收

使用专用 CORE daemon、Bridge 端口及数据/服务/临时目录。禁止指向生产 8080。所有请求以 run_id / case_id / request_id 独立留证；测试仅清理本次创建的会话、容器及镜像 tag。

`real_acceptance.py --url http://127.0.0.1:18080 --run-dir <目录>` 读取 `<目录>/context.json` 的 fixture 与控制器参数，执行一次真实 API/CORE/Docker/WebSocket/SSE 验收。它不会替换产品函数。浏览器使用 `tests/e2e/real_browser.cjs`，需要 Playwright 与 Chromium，可通过 `PLAYWRIGHT_MODULE` 指向已安装包。请在一个验收批次中各运行一次；失败先保留证据并分析，不自动循环重跑。

context 的 fixture 必须包含合法 Docker 归档、服务定义及模板；不将大镜像、浏览器或依赖目录提交到仓库。EMANE 不可用时明确标记 BLOCKED。控制器仅绑定本机并使用随机令牌，提供专用环境的 daemon/Bridge 重启和退出能力。

准备示例（镜像需包含 bash、httpd；Redis 矩阵另需本机 `target-inn-base:v3`）：

```bash
/opt/core/venv/bin/python tests/integration/prepare_environment.py --run-dir /tmp/bridge-acceptance-unique --start-via-core
/opt/core/venv/bin/python tests/integration/real_acceptance.py --url http://127.0.0.1:18080 --run-dir /tmp/bridge-acceptance-unique
```

`--start-via-core` 显式通过本机 CORE 的 ExecuteScript 启动专用环境；不修改或重启生产 daemon。不指定时仅准备文件，可由管理员运行生成的 bootstrap.py。完成后运行 `cleanup_environment.py --run-dir <目录>`。supervisor 是生产 CORE 启动的子进程，结束后可由同一父进程 waitpid 回收；本轮具体记录见工件 `reaped.json`。

`focused_acceptance.py` 和 `focused_browser.cjs` 是已征得用户同意的定向补验，不应默认自动循环执行。`closure_acceptance.py` 覆盖 XML 绑定、两个真实实现版本并行、直连/交换机 ICMP/HTTP/Redis 与吞吐量。每份结果和原始失败证据分别保留。

单测依赖列在根目录 `requirements-dev.txt`。本轮通过独立 PYTHONPATH 加载 pytest/httpx，未把测试依赖安装进生产虚拟环境。浏览器版本为 Playwright 1.65.1，使用独立浏览器缓存；本轮工件 environment.json/旧基线环境记录保留具体版本。
