# WSL 持久化与维护

用户会重启 WSL，后续仍需使用的内容统一放在持久目录。仓库 `AGENTS.md` 已记录此约定，未来维护不能只把依赖和验收资料放在临时目录。

## 当前存放位置

| 内容 | 持久目录 |
| --- | --- |
| 项目代码、操作文档、关键验收摘要 | `/opt/core-http-bridge` |
| 业务数据库、导入内容和脚本附件 | `/opt/artifacts/core-drill` |
| CORE 服务实现 | `/opt/core/etc/custom_services` |
| 生产备份 | `/var/backups/core-http-bridge` |
| Git 自动认证 | 当前账户 `~/.config/git/core-http-bridge.credentials`，权限 600 |
| Python 测试依赖 | `~/.local/share/core-http-bridge/tools/python-deps` |
| Playwright、私有运行库、中文字体及 fontconfig | `~/.local/share/core-http-bridge/tools/browser` |
| Chromium / Headless Shell / FFmpeg | `~/.local/share/core-http-bridge/tools/playwright-browsers` |
| 完整验收资料 | `~/.local/share/core-http-bridge/acceptance` |

以上路径位于 WSL 的持久文件系统。目录迁移不改变业务配置、不重启服务，也不会把维护依赖安装进生产 Python 环境。

## 已保留的资料

本轮现存维护工具、浏览器和完整验收目录已从原临时/缓存位置迁移。迁移使用同一文件系统中的重命名，保留文件 inode、内容与权限；映射记录为 `~/.local/share/core-http-bridge/migration.json`。

- 二次改造完整工件：`~/.local/share/core-http-bridge/acceptance/20261117-image-services`。
- 设计原型检查：`~/.local/share/core-http-bridge/acceptance/20261117-prototype-review`。
- 首轮与补验 XML 报告已放入上述二次改造目录。
- 关键摘要和预览仍在仓库 `docs/platform/验收记录/20261117-image-services/`，可随 Git 保留。

已完成验收目录包含 `ARCHIVED` 标记，仅作历史存档。原始日志和生成的脚本保留当时路径以便追溯；不要重启旧 bootstrap、控制器或其他旧进程脚本。重新验收必须准备新的目录和隔离环境。

更早、在本次迁移前已经清理的临时原件无法迁回。相关文档标为历史路径，现有 Git 中的实施摘要继续保留，不冒充原始证据包仍然存在。

## 固定工具入口

只查看路径，不运行测试：

```sh
python3 scripts/dev-tools.py paths
```

单元测试由固定依赖启动，默认把 JUnit 报告保存到新的持久验收目录：

```sh
make test
# 或只运行用户授权的指定用例
python3 scripts/dev-tools.py pytest tests/test_image_services.py::test_staging_and_confirmation_preserve_optimistic_revision -q
```

以上命令仅为使用示例，本次路径迁移没有再次运行这些测试。继续遵守 AGENTS.md，不因工具迁移反复重跑已通过的用例。

准备新的真实验收目录并运行脚本：

```sh
CORE_BRIDGE_RUN_DIR=$(python3 scripts/dev-tools.py new-run image-services)
python3 scripts/dev-tools.py python tests/integration/prepare_environment.py --run-dir "$CORE_BRIDGE_RUN_DIR" --start-via-core
python3 scripts/dev-tools.py python tests/integration/image_services_acceptance.py --run-dir "$CORE_BRIDGE_RUN_DIR"
python3 scripts/dev-tools.py browser --run-dir "$CORE_BRIDGE_RUN_DIR" tests/e2e/image_services_browser.cjs
python3 scripts/dev-tools.py python tests/integration/cleanup_environment.py --run-dir "$CORE_BRIDGE_RUN_DIR"
```

这些命令会实际运行验收，仅在用户授权的测试批次中执行；禁止指向生产 8080。`new-run` 只输出新目录路径，由准备脚本创建目录。工具入口拒绝把验收目录放进临时区域，也拒绝重启带 `ARCHIVED` 标记的旧环境。

入口自动设置 Python 依赖、Playwright 包、浏览器、运行库和中文字体路径，用户不需要再填写临时 `PYTHONPATH`、`PLAYWRIGHT_MODULE` 等变量。Node.js 的持久路径额外记录在 `tools/runtime.json`，重开终端后 PATH 没包含 Node 时仍可使用。

维护根目录可通过 `CORE_BRIDGE_MAINTENANCE_DIR` 指向其他持久位置，Python 可通过 `CORE_BRIDGE_PYTHON` 指定；不要指向临时目录。

## 重建维护依赖

当前依赖已经迁移，不需要重新下载。以后确实需要重建时，安装到相同持久位置：

```sh
CORE_BRIDGE_TOOLS_DIR="$HOME/.local/share/core-http-bridge/tools"
/opt/core/venv/bin/python -m pip install --target "$CORE_BRIDGE_TOOLS_DIR/python-deps" pytest==9.2.0 httpx==0.28.1
npm install --prefix "$CORE_BRIDGE_TOOLS_DIR/browser" playwright@1.65.1
PLAYWRIGHT_BROWSERS_PATH="$CORE_BRIDGE_TOOLS_DIR/playwright-browsers" "$CORE_BRIDGE_TOOLS_DIR/browser/node_modules/.bin/playwright" install chromium
```

私有浏览器运行库和字体的安装包也保存在 `tools/browser/debs`，展开内容位于 `tools/browser/runtime`。当前 `fonts.conf` 指向新的持久目录，Playwright 的安装关联也已更新。重建这些文件时沿用同一位置，不覆盖系统库或业务环境。

## WSL 启动与运行时文件

当前 `/etc/wsl.conf` 已设置 `systemd=true`，Docker、CORE daemon、Bridge 和网络规则服务均已启用。WSL 启动后由 systemd 启动这些服务，不依赖测试目录或一次性 shell 环境。本次未实际重启 WSL，也未通过重启中断业务来验证。

持久保存文件不等于自动恢复运行中的 CORE 会话；会话进程、终端和连接在 WSL 停止后需要重新建立。

一次性请求文件、容器内可重新投递的运行脚本和测试可丢弃的中间文件仍可使用临时空间。配置、原始脚本、数据库、报告和恢复资料都保存在上表的持久位置。

## 本次核对与远端更新

本次只完成一次迁移核对：入口 Python 语法、持久路径存在性、pytest/httpx 的实际导入位置、Playwright 模块与浏览器二进制位置、动态运行库解析均正常；7 项迁移记录的新位置存在且旧位置不再作为入口。`make -n test` 确认固定入口，没有执行测试套件或浏览器交互用例。

期间按用户要求快进拉取远端 `d322fac`，包含预检修正与外部调用接口文档，保留本地持久化调整。为加载远端后端改动，在无 CORE 会话和活动任务时仅重载 Bridge；这与目录迁移无关。CORE PID 仍为 195、重启次数未变。操作记录保存在维护根目录 `operations/20261117-pull-d322fac/reload.json`。未实际重启 WSL 验证开机过程。

### CORE 原生节点的短运行路径

验收目录包含时间和任务名，不能直接作为原生节点的套接字路径：Linux Unix socket 路径有长度限制。新的准备脚本将验收目录下的 `coretmp` 链接到 `~/.local/share/core-http-bridge/run/<短标识>/`，supervisor 使用解析后的真实路径。两处路径均持久保存；历史运行目录仍受 `ARCHIVED` 约束，不能因为套接字目录还在就重启旧环境。
