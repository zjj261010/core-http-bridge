# CORE HTTP Bridge

CORE HTTP Bridge 将 CORE Emulator 的 gRPC 接口接入 FastAPI 和浏览器管理平台。可以从页面导入镜像、配置节点模板、编排和运行拓扑，并查看接口流量、Snort 告警和运行历史。CORE 守护进程负责仿真，Bridge 负责平台配置、运行编排和记录，Docker 提供镜像节点及独立容器。 [CORE Emulator](https://github.com/coreemu/core)

## 管理页面

| 页面 | 可执行的操作 |
|---|---|
| 镜像管理 | 导入镜像或交付包，声明镜像的服务和启动模式，导入脚本，确认生效，导出配置或完整镜像包 |
| 容器管理 | 创建和管理独立容器，查看容器状态和终端 |
| 节点模板 | 分页管理 Docker 与 CORE 原生模板，按名称搜索，设置默认服务和模板修订 |
| 拓扑管理 | 使用画布添加节点、拖拽和缩放、连线、编辑接口和链路参数，配置背景流量和 Snort 监测点 |
| 数据监测 | 查看宿主和容器指标、接口吞吐、会话事件、背景流量任务及 Snort 告警 |
| 审计日志 | 查询平台操作记录和演练数据 |

## 安装与启动

### 环境要求

- 项目已在 **WSL2 Ubuntu 22.04.5 LTS x86_64、CORE 9.2.1 和 Python 3.10.12** 环境验收。Bridge 使用 CORE 安装目录中的 Python 和 gRPC SDK，当前路径为 `/opt/core/venv/bin/python`。
- `core-daemon` 默认监听 `localhost:50051`。Bridge 与守护进程共享 CORE 服务目录和请求文件目录。
- Docker 相关功能需要可用的 Docker CLI 和守护进程。启动完整 CORE 仿真和从本机节点抓包需要 Linux 网络命名空间权限。当前 WSL 由 systemd 管理这两个服务和 Bridge。
- 前端由 Bridge 直接提供静态文件。Python 依赖见 [requirements.txt](requirements.txt)，TCP/UDP 和 Snort 的可选依赖见下文。

### 启动一个实例

以下命令适用于已经安装 CORE 并配置 `core-daemon` 的 Linux 主机。在仓库目录执行。

```bash
sudo systemctl start core-daemon
# 使用镜像或容器功能时启动 Docker
sudo systemctl start docker

sudo /opt/core/venv/bin/python -m pip install -r requirements.txt

sudo env PYTHONPATH="" /opt/core/venv/bin/python -m uvicorn app:app \
  --host 0.0.0.0 --port 8080
```

当前 WSL 已配置 `core-bridge.service`，日常用 `sudo systemctl start core-bridge.service` 启动。更新后端代码后，检查导入任务和运行中的会话，再重启 Bridge 加载代码。

```bash
sudo systemctl restart core-bridge.service
curl -fsS http://127.0.0.1:8080/health
```

成功时健康接口返回 `{"status":"ok","sessions":0}`，其中 `sessions` 是 CORE 当前会话数。有会话时该数值会相应增加。浏览器打开 [管理平台](http://127.0.0.1:8080/) 或直接进入 [拓扑管理](http://127.0.0.1:8080/#topologies)。

## 从页面完成一次运行

### 第一次可以用两个原生节点

1. 打开「拓扑管理 → 新建拓扑」，填写名称。在节点库添加两个原生主机。
2. 在画布点「连线」，依次选择两个节点。选中链路可设置端点 A、端点 B 的接口地址，以及带宽、时延和丢包率等链路参数。
3. 需要服务时，点节点，在右侧「CORE 原生服务」里添加。想使用空服务列表时选择「裸节点」型号。
4. 点「保存」，再点「启动」。启动成功后进入会话视图，可以检查联通并查看节点、链路和服务配置。
5. 点「停止运行」可保留会话配置以便再次运行。在列表打开保存的拓扑可继续编辑。已结束的运行可从「运行记录」查看或复制为可编辑配置。

「保存」提交的是下次运行的配置。「启动」以当前修订创建新会话。已有会话使用启动时的配置。「删除会话」清理该会话的运行资源，保存的拓扑和运行记录保留。

新连线按点对点链路或共享的交换网络分配 IPv4，已有手填地址保留。启动确认会提示同一网络网段不一致、地址重复及同一节点的网段重叠。

拓扑文件用于在本机备份和复用，支持平台 JSON 和 CORE 原生 XML。原生 XML 以原文件格式保存、导出和启动。导入时检查引用的本机模板修订和镜像。同名拓扑可导入为副本。

### 用镜像创建 Docker 节点

1. 在「镜像管理 → 导入」选择镜像归档或完整交付包，可以上传文件或填写服务器路径。文件接收后即可关闭导入弹窗，在后台任务列表查看解包、归档校验、镜像加载和待确认阶段。失败或中断的任务可「续办」，已校验且文件未变化时会复用归档摘要。
2. 打开镜像「详情」。镜像服务表声明镜像可提供的全部服务。点服务「详情」配置启动模式，在模式里导入启动、停止和就绪检查脚本。
3. 在镜像详情点「确定」查看汇总，然后选择「仅保存，稍后生效」或「确认保存并生效」。注册使用独立修订名。确认页列出阻塞注册的活动会话及查看入口。已停止的会话可以保留。
4. 在「节点模板 → Docker 节点模板」选择镜像和已生效配置修订，勾选默认启用的服务，为每项服务选一个模式。允许空服务清单的工具镜像也可用于模板。
5. 在拓扑画布添加该模板。节点可继承模板默认值，也可单独启停服务和选择模式。

服务声明中的端口描述镜像能力。模式端口覆盖决定该模式使用的端口声明，供冲突检查和端口探测使用。程序实际监听的地址和端口由脚本、启动参数或环境变量决定，配置时应保持一致。镜像能提供哪些服务、模板默认启用哪些服务、本次节点实际启用哪些服务，分别在镜像、模板和拓扑中管理。

列表的「导出」可生成配置与脚本包，也可后台打包镜像本体和配置。导入、合并、脚本用途及兼容格式见 [镜像导入与服务配置指南](docs/platform/镜像导入指南.md)。

### 配置 CORE 原生服务和脚本

原生节点可以直接从节点库添加，也可使用「CORE 原生节点模板」。候选目录来自 CORE 守护进程中可用于原生节点的已注册服务，包括内置服务和自定义服务。

1. 在节点属性中点「添加服务」，可搜索服务名称和分组。列表中的「移除」会提示依赖这项服务的已启用服务。
2. 点「配置」调整参数或应用预设。在「配置文件 → 查看与编辑」查看 CORE 按当前接口和参数生成的脚本，也可展开查看 CORE 源模板。
3. 修改、导入或导出脚本文本。编辑后的内容作为完整文件覆盖应用到对应节点或模板。点「恢复默认」可重新使用 CORE 动态生成的文件。
4. 在服务弹窗确认，再从拓扑或模板页面保存。拓扑中的模板节点可以单独覆盖配置，也能恢复所绑定的模板默认值。

模板编辑会创建新修订。已有拓扑引用原先固定的修订，可以在节点属性中显式采用新修订。运行记录按实际用到的配置保存，便于回看。

## 背景流量与 Snort 预警

在拓扑画布添加「背景流量」任务。HTTP、TCP 和 UDP 的源、目标均可选择本机 CORE 原生节点或本机 CORE 管理的 Docker 节点。HTTP 填写业务端口和站内请求路径。TCP/UDP 由 Bridge 为任务启动 iperf3 接收端，同一接收节点上的任务使用不同的端口。目标接口使用拓扑内的私有 IPv4 地址。

选中链路后点「监测此链路」，选择本机 CORE 原生节点或 Docker 节点一侧的接口，再选择 Snort 规则集。探针以被动方式监听该接口。全 Docker 拓扑可以直接使用两项功能。Bridge 使用宿主机上的工具进入 Docker 节点的独立网络命名空间，业务镜像无需安装 iperf3 或 Snort。

在「网络任务与 Snort 告警」查看速率、任务状态和告警。背景流量可单独停止。停止拓扑会结束探针和剩余任务。Docker 节点退出或重建时，关联任务会结束并记录原因，重新启动拓扑可建立新的运行批次。已结束的批次仍可在「数据监测」回看。

启动 CORE 会话前，Bridge 根据配置检查目标接口、工具和 Snort 规则。预检未通过的任务会在本次运行记录标为失败并显示原因，其余任务和拓扑仍可启动。HTTP 背景流量分别统计成功和错误请求。

「全部运行历史」分页显示运行摘要，打开某条记录时才加载保存的配置。Snort 告警按所选运行批次查询，可按探针、时间及关键字筛选，并将筛选结果导出为 CSV。

### 宿主依赖与验收版本

| 功能 | 已验收版本 | 实际使用的能力 |
|---|---|---|
| HTTP 背景流量 | Python 3.10.12 | CORE venv 中 Bridge 的 Python 标准库 |
| TCP / UDP 背景流量 | iperf3 3.22 | 两端共用宿主二进制，Bridge 解析 `--json-stream` |
| Snort 预警 | Snort 3.13.2.0 + LibDAQ 3.0.29 | pcap 被动采集，`alert_json` 输出告警，`perf_monitor` 输出 DAQ 计数 |

iperf3 3.17 首次提供 `--json-stream`。本项目验收的是 3.22，部署可直接用 3.22。Snort 和 LibDAQ 版本变动后，先用所选规则集校验配置，再检查实际捕获包数和告警。 [iperf3 官方版本记录](https://software.es.net/iperf/news.html) · [Snort 3.13.2.0](https://github.com/snort3/snort3/releases/tag/3.13.2.0) · [LibDAQ 3.0.29](https://github.com/snort3/libdaq/releases/tag/v3.0.29)

默认工具目录是 `/opt/core-network-tools`。可用 `CORE_BRIDGE_NETWORK_ROOT` 指向其他持久目录，保持以下相对路径。Bridge 还使用宿主的 `nsenter`、`ss` 和 `timeout`。

```text
/opt/core-network-tools/
├── iperf/bin/iperf3
├── snort/bin/snort
├── snort/etc/snort/snort.lua
├── daq/lib/libdaq.so.3
└── daq/lib/daq/daq_pcap.so
```

```bash
tool_root="${CORE_BRIDGE_NETWORK_ROOT:-/opt/core-network-tools}"
/opt/core/venv/bin/python --version
"$tool_root/iperf/bin/iperf3" --version
LD_LIBRARY_PATH="$tool_root/daq/lib" "$tool_root/snort/bin/snort" -V
curl -fsS http://127.0.0.1:8080/network-activity/capabilities
```

规则文件放在 `BRIDGE_DATA_DIR/snort-rules/`，扩展名为 `.rules`。每次运行记录规则文件校验值。本机已校验的社区规则快照是 `community-20261118.rules`，SHA-256 为 `274d8ab3f891c9569bb92b92261908f753f68f4cf7d70733dba702baba5239a2`。快速检查告警链路时，可选内置「无害功能验收」规则，并在拓扑内 HTTP 流量任务中使用路径 `/?probe=BRIDGE_IDS_CHECK`。确认流量经过探针所选接口后，到数据监测查看规则命中。

## 配置和持久目录

| 环境变量或目录 | 默认值 | 用途 |
|---|---|---|
| `CORE_GRPC_ADDRESS` | `localhost:50051` | CORE gRPC 地址 |
| `BRIDGE_HOST` / `BRIDGE_PORT` | `0.0.0.0` / `8080` | 直接运行 `app.py` 时的监听地址和端口。使用 Uvicorn CLI 时按启动参数设置 |
| `BRIDGE_DATA_DIR` | `/opt/artifacts/core-drill` | 业务数据、导入附件、背景流量日志和 Snort 规则 |
| `BRIDGE_DB_PATH` | `${BRIDGE_DATA_DIR}/platform.db` | SQLite 配置、修订、运行记录、事件、指标和审计 |
| `CORE_CUSTOM_SERVICES_DIR` | `/opt/core/etc/custom_services` | CORE 服务实现和已生成的服务修订 |
| `BRIDGE_IMPORT_DIR` | `${BRIDGE_DATA_DIR}/image-imports` | 镜像导入工件 |
| `BRIDGE_TEMP_DIR` | `${BRIDGE_DATA_DIR}/tmp` | Bridge 与守护进程共享的一次性请求文件 |
| `CORE_BRIDGE_NETWORK_ROOT` | `/opt/core-network-tools` | iperf3、Snort 和 LibDAQ |
| 维护工具 | `~/.local/share/core-http-bridge/tools/` | 固定测试和浏览器依赖 |
| 验收工件 | `~/.local/share/core-http-bridge/acceptance/` | 测试日志、截图和验收数据 |
| 生产备份 | `/var/backups/core-http-bridge/` | 数据库和服务实现备份 |

业务文件保存在 WSL 持久目录。旧清单和模板在首次读取时迁移到 SQLite，并留下原文件备份。WSL 停止后，拓扑配置和历史从 SQLite 读回，仿真进程需重新启动。使用原生节点时，CORE 运行路径应足够短，以容纳 Unix socket 文件名。

默认保留历史指标 30 天、会话事件及落库的 Snort 告警 90 天。审计日志默认持续保留。

运行 `scripts/backup-platform.py --output <新备份目录>` 可创建 SQLite 一致副本，并复制服务目录和现有旧数据文件。自定义数据库路径用 `--db-path` 指定，数据库不存在或为空时命令会报错。更新 Bridge 会中断浏览器终端、事件连接以及由 Bridge 管理的背景流量和探针，建议在维护时间操作。 [部署与回滚](docs/platform/部署与回滚.md) · [WSL 持久化与维护](docs/platform/WSL持久化与维护.md)

## 离线部署准备

先准备 CORE、对应的 CORE venv、Docker 和所需的系统包。下列清单固定了 Linux x86_64、Python 3.10 环境中的 Bridge 依赖。联网准备机使用相同的 Python 版本和平台下载 wheel。仓库中的 `Dockerfile` 是历史构建示例，Bridge 的部署入口为宿主 CORE venv。

```bash
# 联网准备机
/opt/core/venv/bin/python -m pip download \
  --only-binary=:all: --dest wheels -r requirements-linux-x86_64-py310.txt

# 离线主机
sudo /opt/core/venv/bin/python -m pip install \
  --no-index --find-links wheels -r requirements-linux-x86_64-py310.txt
/opt/core/venv/bin/python scripts/offline-check.py --docker --network-tools
```

将仓库与 `wheels/` 一并放到离线环境。部署使用的镜像归档可提前准备，再通过本机的镜像管理导入。背景流量和 Snort 沿用宿主安装方式，准备已验收的工具目录、运行库和规则快照。复制工具后可用 `ldd` 检查 Snort、DAQ pcap 模块和 iperf3 的动态库，并按版本查询命令核对。自检的 `--docker` 和 `--network-tools` 可按实际使用的功能选择。 [Docker 镜像归档](https://docs.docker.com/reference/cli/docker/image/save/)

## HTTP API

Swagger UI 和 OpenAPI 给出运行实例当前已加载版本的请求参数和响应模型。页面使用的业务接口有自己的路径，CORE 会话操作主要位于 `/api`。Swagger UI 5.34.1 由 Bridge 从本机提供，离线时可直接使用 `/docs` 和 `/openapi.json`。 [Swagger UI](http://127.0.0.1:8080/docs) · [OpenAPI](http://127.0.0.1:8080/openapi.json) · [Swagger UI 上游版本](https://github.com/swagger-api/swagger-ui/releases/tag/v5.34.1)

| 用途 | 常用接口 |
|---|---|
| 健康和运行概览 | `GET /health`、`GET /topology-workspace` |
| 保存、读取和修改拓扑 | `POST /topologies`、`GET /topologies/{topology_id}`、`PUT /topologies/{topology_id}` |
| 部署和查看运行记录 | `POST /topologies/{topology_id}/deploy`、`GET /deployments` |
| 分页查看运行历史和单条快照 | `GET /deployment-history`、`GET /deployments/{deployment_id}` |
| CORE 会话启动、停止和删除 | `POST /api/sessions/{session_id}/start`、`POST /api/sessions/{session_id}/stop`、`DELETE /api/sessions/{session_id}` |
| 会话节点、链路和 CORE XML | `POST /api/sessions/{session_id}/nodes`、`POST /api/sessions/{session_id}/links`、`GET /api/sessions/{session_id}/xml`、`POST /api/xml/open` |
| WLAN 和 EMANE 配置 | `GET /api/sessions/{session_id}/wlan/{node_id}`、`GET /api/sessions/{session_id}/emane/model` |
| 镜像和导入任务 | `GET /targets`、`POST /jobs/import`、`GET /jobs/{job_id}` |
| 节点模板和原生服务 | `GET /instances`、`GET /native-services`、`GET /native-services/{name}/defaults`、`POST /native-services/{name}/preview` |
| 接口流量、事件和历史指标 | `GET /api/sessions/{session_id}/throughputs/stream`、`GET /api/sessions/{session_id}/events/stream`、`GET /metrics`、`GET /events` |
| 背景流量和 Snort 记录 | `GET /network-activity/capabilities`、`GET /deployments/{deployment_id}/network-activity` |
| 分页查询或导出 Snort 告警 | `GET /deployments/{deployment_id}/network-activity/{run_id}/alerts`、`GET /deployments/{deployment_id}/network-activity/{run_id}/alerts.csv` |
| 独立停止背景流量任务 | `POST /deployments/{deployment_id}/network-activity/{run_id}/{item_id}/stop` |

拓扑保存后使用返回的 `topology_id` 和 `revision` 进行更新或部署。若当前修订为 1，部署时直接启动可提交 `{"revision":1,"start":true}`。平台接口常用 `{"ok":true,"data":...}` 响应，CORE 桥接接口按各端点返回会话、节点或操作结果。使用相应的演练或部署标识查询历史。终端和节点移动还提供 WebSocket 接口。 [接口分类与调用说明](docs/接口文档.md)

## 开发和排查

- `app.py`、`bridge.py` 提供 HTTP、WebSocket、SSE 和 CORE gRPC 桥接。
- `topologies.py`、`native_xml.py`、`versions.py` 负责拓扑、运行记录、原生 XML 和修订绑定。
- `target_admin.py`、`image_services.py`、`jobs.py` 管理镜像、服务目录及导入任务。`native_services.py` 负责原生服务目录和脚本预览。
- `network_activity.py`、`network_http.py` 管理流量任务与探针，`platform_db.py` 和 `repository.py` 负责 SQLite，`static/` 提供管理页面。

```bash
python3 scripts/dev-tools.py paths
python3 scripts/dev-tools.py pytest tests/test_topology_workspace.py -q
```

可用 `make test` 运行仓库测试。按 [AGENTS.md](AGENTS.md) 选择必要的测试范围。真实功能验收使用新建的隔离环境，归档的验收目录保留供查阅。 [验收工具说明](tests/integration/README.md)

| 现象 | 优先检查 |
|---|---|
| `/health` 返回 503 | CORE 守护进程状态、gRPC 监听地址及 Bridge 日志 |
| 页面更新后接口找不到 | Bridge 是否加载了同一个工作目录中的新后端代码 |
| Docker 模板没有可选镜像配置 | 镜像详情是否完成最终确认，配置状态是否为「已生效」 |
| 原生节点启动失败 | 运行记录中的启动诊断、节点服务的依赖和 CORE 运行目录长度 |
| 背景流量任务失败 | 工具版本、目标接口与端口、源节点到目标的连通性及任务状态 |
| Snort 告警列表为空 | 先看探针状态和包数，再核对选中接口、流量路径和规则集 |
