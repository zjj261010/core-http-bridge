# CORE HTTP Bridge

将 CORE (Common Open Research Emulator) 网络仿真器的 gRPC 接口包装为 HTTP REST API，支持 JSON/XML 拓扑导入、SSE 事件流、WebSocket 流。

## 系统要求

| 项目 | 要求 |
|:----|:----|
| 操作系统 | Ubuntu 22.04 / CentOS 9 (或支持 CORE 的 Linux 发行版) |
| Python | 3.9+ (与 CORE 要求一致) |
| CORE | 已安装并运行 `core-daemon` |

## 离线安装

### 所需包清单

在目标离线环境，需要以下 Python 包（安装到 CORE 的 venv 中，或系统 Python）：

**核心运行时（必须）**

| 包名 | 版本 | 用途 |
|:----|:----|:----|
| `fastapi` | >=0.115.0 | HTTP API 框架 |
| `uvicorn[standard]` | >=0.30.0 | ASGI 服务器 |
| `pydantic` | >=2.0.0 | 数据校验 |
| `grpcio` | >=1.60.0 | gRPC 通信（CORE 已依赖此包） |

**间接依赖（自动引入）**

| 包名 | 版本 |
|:----|:----|
| `anyio` | >=4.0.0 |
| `starlette` | >=0.40.0 |
| `click` | >=8.0.0 |
| `h11` | >=0.14.0 |
| `idna` | >=3.0.0 |
| `sniffio` | >=1.0.0 |
| `typing_extensions` | >=4.0.0 |
| `annotated_types` | >=0.6.0 |
| `pydantic_core` | >=2.0.0 |

> **注意**：`grpcio` 和 `protobuf` 通常已随 CORE 安装，无需重复下载。
> 完整的 pip 依赖列表见 [`requirements.txt`](requirements.txt)。

### 安装步骤

在有网络的机器上下载包：

```bash
# 方法1：从 PyPI 下载 wheel
pip download -d /tmp/core-bridge-deps \\
    fastapi uvicorn pydantic \\
    --platform manylinux_2_17_x86_64 \\
    --python-version 310 \\
    --only-binary=:all:

# 方法2：使用 pipenv/poetry 导出
pip freeze | grep -E "fastapi|uvicorn|pydantic" > requirements.txt
pip download -r requirements.txt -d /tmp/core-bridge-deps
```

将下载的 wheel 文件和项目目录复制到离线目标机器：

```bash
cp -r /tmp/core-bridge-deps/ /opt/core-bridge-deps/
cp -r core-http-bridge/ /opt/core-http-bridge/
```

在离线目标机器上安装到 CORE 的 Python 虚拟环境：

```bash
# 使用 CORE 的 venv
/opt/core/venv/bin/pip install --no-index \\
    --find-links /opt/core-bridge-deps/ \\
    -r /opt/core-http-bridge/requirements.txt
```

### 验证安装

```bash
/opt/core/venv/bin/python3 -c "import fastapi; print(fastapi.__version__)"
```

## 快速启动

### 前提：确保 core-daemon 正在运行

```bash
# 检查状态
systemctl status core-daemon

# 如果未运行则启动
sudo systemctl start core-daemon
# 或直接运行
sudo core-daemon
```

### 启动 HTTP Bridge

```bash
cd /opt/core-http-bridge
PYTHONPATH="" /opt/core/venv/bin/python3 -m uvicorn app:app \\
    --host 0.0.0.0 --port 8080
```

启动后访问：
- 健康检查：http://localhost:8080/health
- API 文档：http://localhost:8080/docs
- API 根：http://localhost:8080/api

### 测试连接

```bash
# 健康检查
curl http://localhost:8080/health

# 获取系统配置（查看可用服务和 EMANE 模型）
curl http://localhost:8080/api/config

# 创建新会话
curl -X POST http://localhost:8080/api/sessions

# 查看所有会话
curl http://localhost:8080/api/sessions
```

## 完整 API 接口说明

### 会话管理 Session

| 方法 | 路径 | 功能 |
|:----|:----|:----|
| `GET` | `/api/sessions` | 获取所有会话摘要列表 |
| `POST` | `/api/sessions` | 创建新会话 |
| `GET` | `/api/sessions/{id}` | 获取指定会话详情 |
| `DELETE` | `/api/sessions/{id}` | 删除会话 |
| `GET` | `/api/sessions/{id}/check` | 检查会话是否存在 |
| `POST` | `/api/sessions/{id}/start` | 启动会话 |
| `POST` | `/api/sessions/{id}/stop` | 停止会话 |
| `POST` | `/api/sessions/{id}/alert` | 发送告警消息 |

**start 请求体**：

```json
{"definition": true}
```

- `definition=True` — 仅定义拓扑，不实例化（无需 root 权限）
- `definition=False` — 完整实例化（需要 root 权限创建网络命名空间）

### 节点管理 Node

| 方法 | 路径 | 功能 |
|:----|:----|:----|
| `POST` | `/api/sessions/{sid}/nodes` | 添加节点 |
| `GET` | `/api/sessions/{sid}/nodes/{nid}` | 获取节点详情（含接口和链路） |
| `PATCH` | `/api/sessions/{sid}/nodes/{nid}` | 编辑节点图标 |
| `DELETE` | `/api/sessions/{sid}/nodes/{nid}` | 删除节点 |
| `POST` | `/api/sessions/{sid}/nodes/{nid}/move` | 移动节点位置 |
| `POST` | `/api/sessions/{sid}/nodes/{nid}/command` | 在节点上执行命令 |
| `GET` | `/api/sessions/{sid}/nodes/{nid}/terminal` | 获取节点终端命令 |

**Nodetype 取值**：

| type | 含义 |
|:----:|:----|
| `0` | DEFAULT (默认路由器/主机) |
| `1` | PHYSICAL (物理接口) |
| `4` | SWITCH (交换机) |
| `5` | HUB (集线器) |
| `6` | WIRELESS_LAN (无线局域网) |
| `10` | EMANE (电磁频谱模型) |
| `15` | DOCKER (容器) |
| `17` | WIRELESS (无线节点) |
| `18` | PODMAN |

**添加节点示例**：

```bash
curl -X POST http://localhost:8080/api/sessions/1/nodes \\
  -H "Content-Type: application/json" \\
  -d '{"id": 1, "name": "router1", "model": "router",
       "type": 0, "position": {"x": 100, "y": 100},
       "services": ["zebra", "OSPFv2"]}'
```

### 链路管理 Link

| 方法 | 路径 | 功能 |
|:----|:----|:----|
| `POST` | `/api/sessions/{sid}/links` | 添加链路 |
| `PATCH` | `/api/sessions/{sid}/links` | 修改链路参数 |
| `DELETE` | `/api/sessions/{sid}/links` | 删除链路 |
| `POST` | `/api/sessions/{sid}/links/linked` | 切换链路通断 |

**LinkOptions 参数**：

| 参数 | 单位 | 含义 |
|:----|:----|:----|
| `bandwidth` | bps | 带宽 |
| `delay` | us | 延迟 |
| `loss` | % | 丢包率 |
| `jitter` | us | 抖动 |
| `dup` | % | 重复包率 |

### 拓扑导入

| 方法 | 路径 | Content-Type |
|:----|:----|:----|
| `POST` | `/api/topology` | JSON 或 XML |

**JSON 示例**：

```json
{
  "name": "my-network",
  "nodes": [
    {"id": 1, "name": "router1", "model": "router",
     "x": 100, "y": 100, "services": ["zebra", "OSPFv2"]},
    {"id": 2, "name": "pc1", "model": "PC",
     "x": 300, "y": 200}
  ],
  "wlans": [
    {"id": 3, "name": "wlan0", "x": 200, "y": 200,
     "range": "300", "bandwidth": "55000000"}
  ],
  "links": [
    {"node1_id": 1, "node2_id": 2,
     "bandwidth": 10000000, "delay": 5000, "loss": 1.5}
  ]
}
```

**XML 示例**：

```xml
<topology name="my-network">
  <node id="1" name="router1" model="router" x="100" y="100"
        services="zebra,OSPFv2"/>
  <node id="2" name="pc1" model="PC" x="300" y="200"/>
  <node id="3" name="wlan0" type="wlan" x="200" y="200"
        range="300" bandwidth="55000000"/>
  <link node1="1" node2="2" bandwidth="10000000" delay="5000"/>
</topology>
```

**Node 属性说明**：

| 属性 | 说明 | 默认值 |
|:----|:----|:----:|
| `type` | `default`/`switch`/`wlan`/`wireless`/`docker`/`emane` | `default` |
| `model` | 服务模型（`PC`/`router`/`host`/`mdr`） | `PC` |
| `services` | 逗号分隔的服务列表 | 无 |

### WLAN 配置

| 方法 | 路径 | 功能 |
|:----|:----|:----|
| `GET` | `/api/sessions/{sid}/wlan/{nid}` | 获取 WLAN 配置 |
| `POST` | `/api/sessions/{sid}/wlan` | 设置 WLAN 配置 |
| `POST` | `/api/sessions/{sid}/wlan/link` | WLAN 内节点间链路控制 |

### 移动模型

| 方法 | 路径 | 功能 |
|:----|:----|:----|
| `GET` | `/api/sessions/{sid}/mobility/{nid}` | 获取移动配置 |
| `POST` | `/api/sessions/{sid}/mobility` | 设置移动配置 |
| `POST` | `/api/sessions/{sid}/mobility/action` | 控制移动 START/PAUSE/STOP |

### 服务管理

| 方法 | 路径 | 功能 |
|:----|:----|:----|
| `GET` | `/api/sessions/{sid}/services/defaults` | 获取服务默认配置 |
| `GET` | `/api/sessions/{sid}/services/node` | 获取节点服务配置值 |
| `GET` | `/api/sessions/{sid}/services/rendered` | 获取服务渲染文件 |
| `POST` | `/api/sessions/{sid}/services/action` | 服务动作 START/STOP/RESTART/VALIDATE |
| `POST` | `/api/services/create` | 运行时创建自定义服务 |

### Wireless 节点

| 方法 | 路径 | 功能 |
|:----|:----|:----|
| `POST` | `/api/sessions/{sid}/wireless/linked` | Wireless 节点间链路控制 |
| `GET` | `/api/sessions/{sid}/wireless/{nid}` | 获取 Wireless 节点配置 |

### EMANE

| 方法 | 路径 | 功能 |
|:----|:----|:----|
| `GET` | `/api/sessions/{sid}/emane/model` | 获取 EMANE 模型配置 |
| `POST` | `/api/sessions/{sid}/emane/model` | 设置 EMANE 模型配置 |
| `GET` | `/api/sessions/{sid}/emane/channel` | 获取 EMANE 事件通道 |
| `POST` | `/api/sessions/{sid}/emane/link` | EMANE NEM 链路控制 |

### XML 导入导出

| 方法 | 路径 | 功能 |
|:----|:----|:----|
| `GET` | `/api/sessions/{sid}/xml` | 导出会话为 XML |
| `POST` | `/api/xml/open` | 从 XML 字符串导入会话 |

### 工具/全局

| 方法 | 路径 | 功能 |
|:----|:----|:----|
| `GET` | `/api/interfaces` | 获取宿主机可用网络接口 |
| `POST` | `/api/execute` | 在 CoreEmu 上下文中执行脚本 |
| `GET` | `/api/config` | 获取全局配置（可用服务+EMANE模型） |

### 流式接口

#### SSE (Server-Sent Events)

| 方法 | 路径 | 说明 |
|:----|:----|:----|
| `GET` | `/api/sessions/{sid}/events/stream` | 会话事件流 |
| `GET` | `/api/sessions/{sid}/throughputs/stream` | 吞吐量数据流（每 3s） |
| `GET` | `/api/cpu-usage/stream?delay=1` | CPU 使用率流 |

**SSE 使用示例**：

```bash
curl -N http://localhost:8080/api/sessions/1/events/stream

# 输出格式
# data: {"session_id": 1, "type": "NodeEvent"}
# data: {"session_id": 1, "type": "LinkEvent"}
```

#### WebSocket

| 路径 | 说明 |
|:----|:----|
| `/api/sessions/{sid}/move-nodes/ws` | 流式移动节点 |
| `/api/sessions/{sid}/emane-pathlosses/ws` | 流式 EMANE 路径损耗 |
| `/api/sessions/{sid}/emane-events/ws` | 流式 EMANE 事件 |

**WebSocket 使用示例**：

```javascript
const ws = new WebSocket("ws://localhost:8080/api/sessions/1/move-nodes/ws");
ws.send(JSON.stringify({
    "node_id": 1, "position": {"x": 200, "y": 150}
}));
ws.send(JSON.stringify({"stop": true}));
```

## 错误处理

所有错误响应统一格式：

```json
{
  "error": "人类可读的错误描述",
  "detail": "技术细节（可选）",
  "hint": "可操作的建议（可选）"
}
```

### 常见错误

| 状态码 | 错误消息 | 原因 | 解决方法 |
|:----:|:----|:----|:----|
| `400` | `invalid service action` | action 参数值无效 | 使用 START/STOP/RESTART/VALIDATE |
| `400` | `invalid alert level` | level 参数值无效 | 使用 DEFAULT/FATAL/ERROR/WARNING/NOTICE |
| `400` | `invalid JSON` | JSON 格式错误 | 检查 JSON 语法 |
| `400` | `invalid XML` | XML 格式错误 | 检查 XML 语法 |
| `404` | `session X not found` | session_id 不存在 | 先 POST /api/sessions 创建 |
| `404` | `unknown node id X` | node_id 不存在或不属于会话 | 检查节点是否已添加 |
| `404` | `node X does not have interface Y` | iface_id 不存在 | 检查接口 ID |
| `404` | `X is not using BasicRangeModel` | WLAN 未就绪 | 需要先启动会话 |
| `415` | `unsupported media type` | 不支持的 Content-Type | 使用 application/json 或 application/xml |
| `422` | `Field required` | 请求体缺少必填字段 | 检查 API 文档 |
| `422` | `Input should be a valid integer` | 字段类型错误 | 检查字段类型 |
| `500` | `internal server error` | 服务器内部错误 | 查看服务器日志 |
| `503` | `bridge unavailable` | core-daemon 未运行 | `systemctl start core-daemon` |
| `501` | (gRPC UNIMPLEMENTED) | EMANE 未安装 | 安装 EMANE 或忽略 |

## 典型使用流程

```bash
# 1. 确保 core-daemon 在运行
sudo core-daemon &

# 2. 启动 HTTP Bridge
cd /opt/core-http-bridge
PYTHONPATH="" /opt/core/venv/bin/python3 -m uvicorn app:app &
# 服务监听 http://localhost:8080

# 3. 创建并配置拓扑
# 方式 A：通过拓扑导入一键生成
curl -X POST http://localhost:8080/api/topology \\
  -H "Content-Type: application/json" \\
  -d '{
    "name": "demo",
    "nodes": [
      {"id": 1, "name": "r1", "model": "router", "x": 100, "y": 100},
      {"id": 2, "name": "pc1", "model": "PC", "x": 300, "y": 200}
    ],
    "links": [
      {"node1_id": 1, "node2_id": 2, "bandwidth": 10000000}
    ]
  }'

# 方式 B：分步创建
SID=$(curl -s -X POST http://localhost:8080/api/sessions | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -X POST http://localhost:8080/api/sessions/$SID/start \\
  -H "Content-Type: application/json" -d '{"definition": true}'
curl -X POST http://localhost:8080/api/sessions/$SID/nodes \\
  -H "Content-Type: application/json" \\
  -d '{"id": 1, "name": "r1", "model": "router", "position": {"x": 100, "y": 100}}'

# 4. 启动会话（实例化，需要 root）
curl -X POST http://localhost:8080/api/sessions/$SID/start \\
  -H "Content-Type: application/json" -d '{"definition": false}'

# 5. 在节点上执行命令
curl -X POST http://localhost:8080/api/sessions/$SID/nodes/1/command \\
  -H "Content-Type: application/json" \\
  -d '{"command": "ip addr", "wait": true, "shell": true}'

# 6. 清理
curl -X DELETE http://localhost:8080/api/sessions/$SID
```

## 完整示例：创建带路由的网络

```bash
# 创建会话
SID=$(curl -s -X POST http://localhost:8080/api/sessions \\
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

# 添加路由器（带 OSPF 服务）
curl -X POST http://localhost:8080/api/sessions/$SID/nodes \\
  -H "Content-Type: application/json" \\
  -d '{"id": 1, "name": "r-core", "model": "router",
       "type": 0, "position": {"x": 200, "y": 100},
       "services": ["zebra", "OSPFv2"]}'

# 添加两个主机
curl -X POST http://localhost:8080/api/sessions/$SID/nodes \\
  -H "Content-Type: application/json" \\
  -d '{"id": 2, "name": "pc-left", "model": "PC",
       "type": 0, "position": {"x": 100, "y": 200}}'

curl -X POST http://localhost:8080/api/sessions/$SID/nodes \\
  -H "Content-Type: application/json" \\
  -d '{"id": 3, "name": "pc-right", "model": "PC",
       "type": 0, "position": {"x": 300, "y": 200}}'

# 添加链路（含带宽限制）
curl -X POST http://localhost:8080/api/sessions/$SID/links \\
  -H "Content-Type: application/json" \\
  -d '{"node1_id": 1, "node2_id": 2,
       "iface1": {"id": 0}, "iface2": {"id": 0},
       "options": {"bandwidth": 100000000, "delay": 1000}}'

curl -X POST http://localhost:8080/api/sessions/$SID/links \\
  -H "Content-Type: application/json" \\
  -d '{"node1_id": 1, "node2_id": 3,
       "iface1": {"id": 1}, "iface2": {"id": 0},
       "options": {"bandwidth": 100000000, "delay": 1000}}'

# 启动会话（需要 root）
curl -X POST http://localhost:8080/api/sessions/$SID/start \\
  -H "Content-Type: application/json" -d '{"definition": false}'

# 查看节点状态
curl http://localhost:8080/api/sessions/$SID/nodes/1
```

## 可能出现的问题

### 1. core-daemon 未运行

**现象**：`/health` 返回 `503`，或调用 `/api/*` 返回 gRPC 错误。

**解决**：
```bash
sudo systemctl start core-daemon
# 或
sudo core-daemon -d
# 或前台运行（调试用）
sudo core-daemon
```

### 2. Python 环境问题

**现象**：启动时提示 `ModuleNotFoundError: No module named 'fastapi'`

**解决**：确保使用 CORE 的 venv 运行：
```bash
PYTHONPATH="" /opt/core/venv/bin/python3 -m uvicorn app:app
```

### 3. 端口冲突

**现象**：`Address already in use`。

**解决**：
```bash
# 更改端口
PYTHONPATH="" /opt/core/venv/bin/python3 -m uvicorn app:app --host 0.0.0.0 --port 8081

# 或查找占用进程
lsof -i :8080
kill <PID>
```

### 4. gRPC 连接失败

**现象**：请求返回 `503 Unavailable`，detail 为 `gRPC status: UNAVAILABLE`。

**原因**：core-daemon 默认只监听 `localhost:50051`。

**解决**：
```bash
# 确认 core-daemon 正在监听
ss -tlnp | grep 50051

# 检查 core-daemon 配置
cat /etc/core/core.conf | grep port
```

### 5. 启动会话需要 root 权限

**现象**：`start_session(definition=false)` 失败。

**原因**：完整实例化需要 root 权限创建网络命名空间和 veth 对。

**解决**：
- 使用 `"definition": true` 仅定义拓扑（不实例化）
- 以 root 运行 HTTP Bridge，或让 core-daemon 使用 `--grpc` 的认证机制

### 6. EMANE 相关接口不可用

**现象**：EMANE 端点返回 `501 Unimplemented`。

**原因**：环境中未安装 EMANE。

**解决方法**：忽略 EMANE 接口，仅使用其他功能。

### 7. 拓扑导入后节点查询不到

**现象**：`POST /api/topology` 返回成功，但 `GET /nodes/1` 返回 404。

**原因**：拓扑导入返回后 session 可能已清理或节点 ID 被重置。

**解决方法**：使用返回的 `session_id` 立即查询，不要先调用 `start_session`。

### 8. DELETE 请求带 body 在部分客户端中失败

**现象**：某些 HTTP 客户端（如浏览器 `fetch`）不允许 DELETE 带 body。

**解决方法**：对 DELETE 链路操作，可以先 POST 自定义端点，或确保使用的是 httpx/requests 这类支持 DELETE body 的客户端。