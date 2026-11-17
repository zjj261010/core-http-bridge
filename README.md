# CORE HTTP Bridge

> WSL 重启后的持久化与固定维护入口见 [WSL持久化与维护](docs/platform/WSL持久化与维护.md)。测试依赖、浏览器与完整验收资料保存在用户持久目录，不依赖临时目录。

> 管理平台已于 2026-11-17 更新：镜像详情采用服务/启动模式两级弹窗，配置通过表格和文件导入导出。旧版本发布入口已合并。当前操作见 [镜像导入指南](docs/platform/镜像导入指南.md)，实施范围与验收见 [二次改造实施与验收记录](docs/platform/二次改造实施与验收记录.md)。

将 CORE (Common Open Research Emulator) 网络仿真器的 gRPC 接口包装为 HTTP REST API。
支持 JSON/XML 拓扑一键导入、SSE 事件流、WebSocket 交互式终端。

## 系统要求

| 项目 | 要求 |
|:----|:----|
| 操作系统 | Ubuntu 22.04 / CentOS 9 |
| Python | 3.9+（与 CORE 一致） |
| CORE | 已安装并运行 `core-daemon` |
| root 权限 | **仅完整实例化时需要**（`definition=false`） |

## 离线安装

### 依赖清单

| 包 | 版本 | 用途 | 备注 |
|:----|:----:|:----|:----|
| `fastapi` | >=0.115.0 | HTTP API 框架 | |
| `uvicorn[standard]` | >=0.30.0 | ASGI 服务器 | 含 WebSocket、hot-reload、httptools |
| `pydantic` | >=2.0.0 | 请求/响应数据校验 | |
| `grpcio` | >=1.60.0 | gRPC 通信 | CORE 已安装，无需额外下载 |

**`uvicorn[standard]` 额外依赖（已包含在 wheels 中）：**
`websockets`, `httptools`, `watchfiles`, `uvloop`, `python-dotenv`, `pyyaml`

完整依赖列表见 [`requirements.txt`](requirements.txt)。

### 安装步骤

```bash
# 解压离线包
tar xzf core-http-bridge-offline-bundle-v1.1.0.tar.gz
cd core-http-bridge

# 安装依赖到 CORE 的 venv
/opt/core/venv/bin/pip install --no-index \
  --find-links wheels \
  -r requirements.txt

# 启动
PYTHONPATH="" /opt/core/venv/bin/python3 \
  -m uvicorn app:app --host 0.0.0.0 --port 8080
```

## 快速启动

```bash
# 1. 确保 core-daemon 在运行
sudo systemctl start core-daemon

# 2. 启动 HTTP Bridge
cd /opt/core-http-bridge
PYTHONPATH="" /opt/core/venv/bin/python3 -m uvicorn app:app \
  --host 0.0.0.0 --port 8080

# 3. 验证
curl http://localhost:8080/health
# {"status":"ok","sessions":0}
```

---

# 完整 API 接口说明

## 通用约定

| 项目 | 说明 |
|:----|:----|
| **基础地址** | `http://<host>:8080` |
| **API 前缀** | `/api`（所有路径以此为前缀） |
| **Content-Type** | `application/json`（除 `/api/topology` 也支持 `application/xml`） |
| **会话状态** | `1`=DEFINITION, `2`=CONFIGURATION, `3`=INSTANTIATION, `4`=RUNTIME |
| **错误响应格式** | `{\"error\", \"detail\", \"hint\"}` |

---

## 1. 会话管理 — Session

### `POST /api/sessions` — 创建会话

| 参数 | 位置 | 类型 | 必填 | 默认值 | 说明 |
|:----|:----:|:----:|:----:|:----:|:----|
| `session_id` | body | `int \| null` | 否 | `null` | 指定会话 ID（自动生成） |

**限制：**
- `session_id` 如果指定，必须是正整数且不与现有会话重复

**返回（201）：**

```json
{
  "id": 1,
  "state": 1,
  "nodes": [],
  "links": [],
  "dir": "/tmp/pycore.1",
  "user": "",
  "file": null
}
```

**示例：**
```bash
curl -X POST http://localhost:8080/api/sessions \
  -H "Content-Type: application/json" -d '{}'
# [201] {"id": 1, "state": 1, "nodes": [], "links": [], ...}
```

---

### `GET /api/sessions` — 获取会话列表

无参数。

**返回（200）：**
```json
[{"id": 1, "state": 1, "nodes": 0, "file": null, "dir": "/tmp/pycore.1"}]
```

**示例：**
```bash
curl http://localhost:8080/api/sessions
# [200] [{"id":1, "state":1, "nodes":0, ...}]
```

---

### `GET /api/sessions/{session_id}` — 获取会话详情

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |

**返回（200）：** 完整的会话对象，包含 `nodes[]` 和 `links[]` 字段（已修复——确保迭代 `nodes.values()` 而非 `nodes.keys()`）。

**示例：**
```bash
curl http://localhost:8080/api/sessions/1
# [200] {"id":1, "state":1, "nodes":[...], "links":[...], "dir":"/tmp/pycore.1", ...}
```

---

### `DELETE /api/sessions/{session_id}` — 删除会话

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 要删除的会话 ID |

**返回（200）：** `{"result": true}`

---

### `GET /api/sessions/{session_id}/check` — 检查会话是否存在

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |

**返回（200）：**
```json
{"result": true}   // 存在
{"result": false}  // 不存在
```

---

### `POST /api/sessions/{session_id}/start` — 启动会话

| 参数 | 位置 | 类型 | 必填 | 默认值 | 说明 |
|:----|:----:|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | — | 会话 ID |
| `definition` | body | `bool` | 否 | `false` | `true`=仅定义（无需root）`false`=完整实例化（需root） |

**返回（200）：**
```json
{"result": true, "exceptions": []}
```

> ⚠️ `definition=false` 需要 **root 权限** 才能创建网络命名空间和 veth 对。

**示例：**
```bash
# 仅定义（无需 root）
curl -X POST http://localhost:8080/api/sessions/1/start \
  -H "Content-Type: application/json" -d '{"definition": true}'
# [200] {"result": true, "exceptions": []}

# 完整实例化（需要 root）
curl -X POST http://localhost:8080/api/sessions/1/start \
  -H "Content-Type: application/json" -d '{"definition": false}'
```

---

### `POST /api/sessions/{session_id}/stop` — 停止会话

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |

**行为：** 销毁运行时资源（网络命名空间、veth 对），但**保留拓扑定义**（节点和链路不会丢失）。停止后可通过 `start(definition=true)` 查看拓扑，或 `start(definition=false)` 重新实例化。

**返回（200）：** `{"result": true}`

---

### `POST /api/sessions/{session_id}/alert` — 发送告警

| 参数 | 位置 | 类型 | 必填 | 允许值 | 说明 |
|:----|:----:|:----:|:----:|:----|:----|
| `session_id` | path | `int` | ✅ | — | 会话 ID |
| `level` | body | `string` | ✅ | `DEFAULT` `FATAL` `ERROR` `WARNING` `NOTICE` | 告警级别 |
| `source` | body | `string` | ✅ | 任意字符串 | 告警来源 |
| `text` | body | `string` | ✅ | 任意字符串 | 告警内容 |
| `node_id` | body | `int \| null` | 否 | — | 关联的节点 ID |

**返回（200）：** `{"result": true}`

**示例：**
```bash
curl -X POST http://localhost:8080/api/sessions/1/alert \
  -H "Content-Type: application/json" \
  -d '{"level":"WARNING","source":"test","text":"链路断开","node_id":1}'
# [200] {"result": true}
```

---

## 2. 节点管理 — Node

### `POST /api/sessions/{session_id}/nodes` — 添加节点

| 参数 | 位置 | 类型 | 必填 | 默认值 | 说明 |
|:----|:----:|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | — | 会话 ID |
| `id` | body | `int` | ✅ | — | 节点 ID（自定义，正整数） |
| `name` | body | `string` | ✅ | — | 节点名称 |
| `model` | body | `string` | 否 | `"PC"` | 服务模型：`PC` / `router` / `host` / `mdr` |
| `type` | body | `int` | 否 | `0` | **[节点类型表]** |
| `position` | body | `object` | 否 | — | `{"x": float, "y": float}` |
| `services` | body | `string[]` | 否 | — | 服务列表，如 `["zebra","OSPFv2"]` |
| `icon` | body | `string` | 否 | — | 图标路径 |
| `image` | body | `string` | 否 | — | Docker/镜像名 |
| `server` | body | `string` | 否 | — | 分布式服务器名称 |
| `geo` | body | `object` | 否 | — | `{"lat": float, "lon": float, "alt": float}` |

**节点类型表：**

| type | 含义 | type | 含义 |
|:---:|:----|:---:|:----|
| `0` | DEFAULT（路由器/主机） | `6` | WIRELESS_LAN（无线局域网） |
| `1` | PHYSICAL（物理接口） | `10` | EMANE（射频模型） |
| `4` | SWITCH（交换机） | `15` | DOCKER（容器） |
| `5` | HUB（集线器） | `17` | WIRELESS（无线节点） |

**限制：**
- `name` 在同一会话内不必唯一（但不建议重复）
- 节点类型 `DOCKER` / `PODMAN` 需要宿主机有对应运行时
- 节点必须先被添加到会话，才能创建链路

**返回（201）：** `{"node_id": int}`

**示例：**
```bash
# 添加路由器
curl -X POST http://localhost:8080/api/sessions/1/nodes \
  -H "Content-Type: application/json" \
  -d '{"id":1,"name":"r1","model":"router","type":0,
       "position":{"x":100,"y":100},
       "services":["zebra","OSPFv2"]}'
# [201] {"node_id": 1}

# 添加 PC
curl -X POST http://localhost:8080/api/sessions/1/nodes \
  -H "Content-Type: application/json" \
  -d '{"id":2,"name":"pc1","model":"PC","type":0,
       "position":{"x":300,"y":200}}'
# [201] {"node_id": 2}

# 添加交换机
curl -X POST http://localhost:8080/api/sessions/1/nodes \
  -H "Content-Type: application/json" \
  -d '{"id":3,"name":"sw1","type":4,"position":{"x":200,"y":150}}'
# [201] {"node_id": 3}
```

---

### `GET /api/sessions/{session_id}/nodes/{node_id}` — 获取节点详情

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `node_id` | path | `int` | ✅ | 节点 ID |

**返回（200）：**
```json
{
  "node": {"id":1,"name":"r1","type":0,"model":"router",
           "position":{"x":100,"y":100},"services":["zebra","OSPFv2"]},
  "interfaces": [{"id":0,"name":"eth0","ip4":"10.0.0.1","ip4_mask":24}],
  "links": [{"node1_id":1,"node2_id":2,"type":1}]
}
```

---

### `PATCH /api/sessions/{session_id}/nodes/{node_id}` — 编辑节点（图标）

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `node_id` | path | `int` | ✅ | 节点 ID |
| `icon` | query | `string` | 否 | 图标路径，如 `router.png` |

**返回（200）：** `{"result": true}`

**示例：**
```bash
curl -X PATCH "http://localhost:8080/api/sessions/1/nodes/1?icon=router.png"
# [200] {"result": true}
```

---

### `DELETE /api/sessions/{session_id}/nodes/{node_id}` — 删除节点

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `node_id` | path | `int` | ✅ | 节点 ID |

**返回（200）：** `{"result": true}`

---

### `POST /api/sessions/{session_id}/nodes/{node_id}/move` — 移动节点

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `node_id` | path | `int` | ✅ | 节点 ID |
| `position` | body | `object` | 二选一 | `{"x": float, "y": float}` |
| `geo` | body | `object` | 二选一 | `{"lat": float, "lon": float, "alt": float}` |

**限制：** `position` 和 `geo` 不能同时省略，也不能同时提供。

**返回（200）：** `{"result": true}`

**示例：**
```bash
curl -X POST http://localhost:8080/api/sessions/1/nodes/1/move \
  -H "Content-Type: application/json" \
  -d '{"position":{"x":200,"y":200}}'
# [200] {"result": true}
```

---

### `POST /api/sessions/{session_id}/nodes/{node_id}/command` — 执行命令

| 参数 | 位置 | 类型 | 必填 | 默认值 | 说明 |
|:----|:----:|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | — | 会话 ID |
| `node_id` | path | `int` | ✅ | — | 节点 ID |
| `command` | body | `string` | ✅ | — | 要执行的命令内容 |
| `wait` | body | `bool` | 否 | `true` | 是否等待命令完成 |
| `shell` | body | `bool` | 否 | `false` | 是否通过 shell 执行 |

**限制：**
- ⚠️ 需要会话已**完整实例化**（`definition=false`），仅定义状态（`definition=true`）下不可用
- `command` 最大长度 4096 字符

**返回（200）：**
```json
{"return_code": 0, "output": "命令执行的输出内容..."}
```

**示例：**
```bash
# 查看接口信息（需要 root + 实例化）
curl -X POST http://localhost:8080/api/sessions/1/nodes/1/command \
  -H "Content-Type: application/json" \
  -d '{"command":"ip addr","wait":true,"shell":true}'
# [200] {"return_code":0, "output":"1: lo: <LOOPBACK,UP,LOWER_UP>...\\n2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP>..."}

# Ping 测试
curl -X POST http://localhost:8080/api/sessions/1/nodes/1/command \
  -H "Content-Type: application/json" \
  -d '{"command":"ping -c 3 10.0.0.2","wait":true,"shell":true}'
# [200] {"return_code":0, "output":"64 bytes from 10.0.0.2: icmp_seq=1 ttl=64 time=0.123 ms\\n..."}
```

---

### `GET /api/sessions/{session_id}/nodes/{node_id}/terminal` — 获取终端命令

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `node_id` | path | `int` | ✅ | 节点 ID |

**返回（200）：**
```json
{"terminal": "vcmd -c /tmp/pycore.1/n1 -- /bin/bash"}
```

终端命令可在 **core-daemon 所在宿主机** 上直接运行，打开交互式 bash。

---

### `ws /api/sessions/{session_id}/nodes/{node_id}/terminal/ws` — WebSocket 交互式终端

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `node_id` | path | `int` | ✅ | 节点 ID |

**协议：** 建立 WebSocket 后，后端通过 `vcmd -q -i -c <channel> -- /bin/bash` 启动交互式 bash，双向转发数据。

**前端用法：**
```javascript
const ws = new WebSocket("ws://localhost:8080/api/sessions/1/nodes/1/terminal/ws");

// 收到终端输出
ws.onmessage = (e) => console.log(e.data);

// 发送命令
ws.send("ip addr\r\n");
ws.send("ping 10.0.0.2\r\n");

// 中断运行中的命令（Ctrl+C）
ws.send("\x03");

// 退出（Ctrl+D）
ws.send("\x04");
```

**限制：**
- 需要会话已实例化（`definition=false`，root 权限）
- 关闭 WebSocket 或 Ctrl+D 退出后，进程自动清理

**示例（命令行测试）：**
```bash
# 使用 wscat
npm install -g wscat
wscat -c "ws://localhost:8080/api/sessions/1/nodes/1/terminal/ws"

# 连接后直接输入命令
ip addr
ping 10.0.0.2
^C
```

---

## 3. 链路管理 — Link

### `POST /api/sessions/{session_id}/links` — 添加链路

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `node1_id` | body | `int` | ✅ | 节点 A ID |
| `node2_id` | body | `int` | ✅ | 节点 B ID |
| `iface1` | body | `object \| null` | 否 | 节点 A 的接口配置 |
| `iface2` | body | `object \| null` | 否 | 节点 B 的接口配置 |
| `options` | body | `object \| null` | 否 | 链路参数（QoS） |

**`iface1`/`iface2` 接口配置：**

| 子字段 | 类型 | 必填 | 默认值 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `id` | `int` | ✅ | — | 接口序号，从 0 开始 |
| `name` | `string` | 否 | `"eth<id>"` | 接口名 |
| `ip4` | `string` | 否 | — | IPv4 地址，如 `"10.0.0.1"` |
| `ip4_mask` | `int` | 否 | — | 掩码位数，如 `24` |
| `ip6` | `string` | 否 | — | IPv6 地址 |
| `ip6_mask` | `int` | 否 | — | IPv6 掩码 |
| `mac` | `string` | 否 | 自动生成 | MAC 地址 |

**`options` 链路参数（QoS）：**

| 子字段 | 类型 | 单位 | 说明 |
|:----|:----:|:----:|:----|
| `bandwidth` | `int` | bps | 带宽限制 |
| `delay` | `int` | µs | 延迟 |
| `loss` | `float` | % | 丢包率 |
| `jitter` | `int` | µs | 抖动 |
| `dup` | `int` | % | 重复包率 |
| `mer` | `int` | — | 误码率 |
| `burst` | `int` | — | 突发大小 |

**限制：**
- 两个节点需属于同一会话
- `iface1.id` 对节点 A 必须唯一
- `iface1.ip4` 和 `iface1.ip4_mask` 同时出现时 mask 默认 `24`
- 不提供 `iface1`/`iface2` 时由系统自动分配

**返回（201）：**
```json
{
  "result": true,
  "iface1": {"id":0, "name":"eth0", "ip4":"10.0.0.1", "ip4_mask":24, ...},
  "iface2": {"id":0, "name":"eth0", "ip4":"10.0.0.2", "ip4_mask":24, ...}
}
```

**示例：**
```bash
# 基本链路（无 IP，由系统自动分配）
curl -X POST http://localhost:8080/api/sessions/1/links \
  -H "Content-Type: application/json" \
  -d '{"node1_id":1,"node2_id":2,"iface1":{"id":0},"iface2":{"id":0}}'
# [201] {"result":true, "iface1":{"id":0,"name":"veth1.0.1",...}, "iface2":{...}}

# 带自定义 IP 和 QoS 的链路
curl -X POST http://localhost:8080/api/sessions/1/links \
  -H "Content-Type: application/json" \
  -d '{"node1_id":1,"node2_id":2,
       "iface1":{"id":0,"ip4":"10.0.0.1","ip4_mask":24},
       "iface2":{"id":0,"ip4":"10.0.0.2","ip4_mask":24},
       "options":{"bandwidth":100000000,"delay":5000,"loss":1.5}}'
# [201] {"result":true, "iface1":{"id":0,"ip4":"10.0.0.1",...}, "iface2":{"id":0,"ip4":"10.0.0.2",...}}
```

---

### `PATCH /api/sessions/{session_id}/links` — 修改链路参数

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `node1_id` | body | `int` | ✅ | 节点 A ID |
| `node2_id` | body | `int` | ✅ | 节点 B ID |
| `iface1` | body | `object \| null` | 否 | 接口配置（只传 id 即可） |
| `iface2` | body | `object \| null` | 否 | 接口配置 |
| `options` | body | `object \| null` | 否 | 需要修改的链路参数 |

**返回（200）：** `{"result": true}`

**用途：修改链路 QoS 参数（bandwidth/delay/loss/jitter/dup 等）。**

> ✅ **改接口 IP（与 CORE-GUI 行为对齐）：**
> - **会话未运行**（定义/停止态）：请求体中携带 `iface1`/`iface2` 的 `ip4/ip4_mask` → 桥后端**自动删链重建**（保留原接口 id/MAC 与合并后的 QoS），一次调用即可完成「改 IP + 改 QoS」。返回 `{"action": "replace_ip"}`。
> - **运行中**（RUNTIME/DATACOLLECT）：请求改 IP → **HTTP 409 拒绝**（CORE 协议 `EditLinkRequest` 无 ip4 字段，运行中接口 IP 被锁定）。需先 `POST /api/sessions/{id}/stop` 停止会话再改；或运行中直接删链（`DELETE /links`）后以新 IP 重建（`POST /links`）。运行中仅 QoS 可改。
> - options 为**合并式更新**：以链路当前 QoS 为基准，只覆盖请求体里传入的字段，未传入的参数保持原值（不会清零）。

**示例：**
```bash
# 只改 delay（bandwidth 等其他参数保持原值）
curl -X PATCH http://localhost:8080/api/sessions/1/links \
  -H "Content-Type: application/json" \
  -d '{"node1_id":1,"node2_id":2,"options":{"delay":1000}}'
# [200] {"result": true}

# 未运行：改接口1 IP → 后端自动删链重建，保留 QoS delay/bandwidth
curl -X PATCH http://localhost:8080/api/sessions/1/links \
  -H "Content-Type: application/json" \
  -d '{"node1_id":1,"node2_id":2,"iface1":{"ip4":"10.9.9.9","ip4_mask":24}}'
# [200] {"result": true, "action": "replace_ip", "state": "DEFINITION"}

# 运行中改 IP → 409 拒绝
curl -X PATCH http://localhost:8080/api/sessions/1/links \
  -H "Content-Type: application/json" \
  -d '{"node1_id":1,"node2_id":2,"iface1":{"ip4":"10.9.9.99","ip4_mask":24}}'
# [409] {"error":"会话运行中(RUNTIME)不可在线修改接口 IP", "detail":"…", "hint":"先停止会话，再改 IP；QoS 可直接改"}
```

> 查询链路当前接口 IP / QoS：`GET /api/sessions/{id}` 的 `links[]`，每条的 `iface1/iface2`（`id/ip4/ip4_mask`）与 `options`（QoS）。

---

### `DELETE /api/sessions/{session_id}/links` — 删除链路

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `node1_id` | body | `int` | ✅ | 节点 A ID |
| `node2_id` | body | `int` | ✅ | 节点 B ID |
| `iface1` | body | `object \| null` | 否 | `{"id": int}` |
| `iface2` | body | `object \| null` | 否 | `{"id": int}` |

> ⚠️ 部分 HTTP 客户端（如 `fetch`）不支持 DELETE 带 body。可用 `POST /api/sessions/{sid}/links/delete` 作为替代（需自行添加该端点）。

**返回（200）：** `{"result": true}`

---

### `POST /api/sessions/{session_id}/links/linked` — 切换链路通断

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `node1_id` | body | `int` | ✅ | 节点 A ID |
| `node2_id` | body | `int` | ✅ | 节点 B ID |
| `iface1_id` | body | `int` | ✅ | 节点 A 的接口 ID |
| `iface2_id` | body | `int` | ✅ | 节点 B 的接口 ID |
| `linked` | body | `bool` | ✅ | `true`=接通，`false`=断开 |

**行为：** 模拟网线插拔（断开时丢包 100%，接通时恢复正常），**不删除链路定义**。

**返回（200）：** `{"result": true}`

**示例：**
```bash
# 断开
curl -X POST http://localhost:8080/api/sessions/1/links/linked \
  -H "Content-Type: application/json" \
  -d '{"node1_id":1,"node2_id":2,"iface1_id":0,"iface2_id":0,"linked":false}'
# [200] {"result": true}

# 接通
curl -X POST http://localhost:8080/api/sessions/1/links/linked \
  -H "Content-Type: application/json" \
  -d '{"node1_id":1,"node2_id":2,"iface1_id":0,"iface2_id":0,"linked":true}'
# [200] {"result": true}
```

---

## 4. WLAN 配置 — WLAN

### `GET /api/sessions/{session_id}/wlan/{node_id}` — 获取 WLAN 配置

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `node_id` | path | `int` | ✅ | WLAN 节点 ID |

**返回（200）：** 配置选项字典
```json
{
  "range": {"label":"无线范围","value":"275","type":7,...},
  "bandwidth": {"label":"带宽(bps)","value":"54000000","type":7,...},
  "delay": {"label":"传输延迟(usec)","value":"5000","type":7,...},
  "jitter": {"label":"抖动(usec)","value":"0","type":7,...},
  "error": {"label":"丢包率(%)","value":"0","type":9,...}
}
```

### `POST /api/sessions/{session_id}/wlan` — 设置 WLAN 配置

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `wlan_id` | body | `int` | ✅ | WLAN 节点 ID |
| `config` | body | `object` | ✅ | 配置键值对 |

**可配置项：** `range`(像素), `bandwidth`(bps), `delay`(µs), `jitter`(µs), `error`(%), `promiscuous`(0/1)

**示例：**
```bash
curl -X POST http://localhost:8080/api/sessions/1/wlan \
  -H "Content-Type: application/json" \
  -d '{"wlan_id":4,"config":{"range":"300","bandwidth":"55000000"}}'
# [200] {"result": true}
```

---

## 5. 移动模型 — Mobility

### `GET /api/sessions/{session_id}/mobility/{node_id}` — 获取移动配置

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `node_id` | path | `int` | ✅ | 节点 ID |

**返回（200）：** ns-2 移动脚本配置选项（`file`, `script_start`, `refresh_ms`, `loop` 等）

### `POST /api/sessions/{session_id}/mobility` — 设置移动配置

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `node_id` | body | `int` | ✅ | 节点 ID |
| `config` | body | `object` | ✅ | 配置键值对 |

**示例：**
```bash
curl -X POST http://localhost:8080/api/sessions/1/mobility \
  -H "Content-Type: application/json" \
  -d '{"node_id":1,"config":{"file":"/tmp/test.ns2","refresh_ms":"100"}}'
# [200] {"result": true}
```

### `POST /api/sessions/{session_id}/mobility/action` — 控制移动

| 参数 | 位置 | 类型 | 必填 | 允许值 | 说明 |
|:----|:----:|:----:|:----:|:----|:----|
| `session_id` | path | `int` | ✅ | — | 会话 ID |
| `node_id` | body | `int` | ✅ | — | 节点 ID |
| `action` | body | `string` | ✅ | `START` `PAUSE` `STOP` | 移动控制指令 |

**示例：**
```bash
curl -X POST http://localhost:8080/api/sessions/1/mobility/action \
  -H "Content-Type: application/json" \
  -d '{"node_id":1,"action":"START"}'
# [200] {"result": true}
```

---

## 6. 服务管理 — Service

### `GET /api/sessions/{session_id}/services/defaults` — 获取服务默认配置

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `node_id` | query | `int` | ✅ | 节点 ID |
| `name` | query | `string` | ✅ | 服务名称，如 `DefaultRoute` |

**返回（200）：**
```json
{
  "templates": {"file.sh": "模板内容..."},
  "config": {},
  "modes": [{"name":"模式名","config":{...}}]
}
```

### `GET /api/sessions/{session_id}/services/node` — 获取节点服务配置

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `node_id` | query | `int` | ✅ | 节点 ID |
| `name` | query | `string` | ✅ | 服务名称 |

**返回（200）：** `{"config": {...}}`

### `GET /api/sessions/{session_id}/services/rendered` — 获取服务渲染文件

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `node_id` | query | `int` | ✅ | 节点 ID |
| `name` | query | `string` | ✅ | 服务名称 |

**返回（200）：** `{"rendered": {"文件名": "渲染后的文件内容..."}}`

### `POST /api/sessions/{session_id}/services/action` — 服务动作

| 参数 | 位置 | 类型 | 必填 | 允许值 | 说明 |
|:----|:----:|:----:|:----:|:----|:----|
| `session_id` | path | `int` | ✅ | — | 会话 ID |
| `node_id` | body | `int` | ✅ | — | 节点 ID |
| `service` | body | `string` | ✅ | — | 服务名称 |
| `action` | body | `string` | ✅ | `START` `STOP` `RESTART` `VALIDATE` | 动作类型 |

**示例：**
```bash
curl -X POST http://localhost:8080/api/sessions/1/services/action \
  -H "Content-Type: application/json" \
  -d '{"node_id":1,"service":"DefaultRoute","action":"VALIDATE"}'
# [200] {"result": true}
```

### `POST /api/services/create` — 创建自定义服务

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `name` | body | `string` | ✅ | 服务名称 |
| `group` | body | `string` | 否 | 分组 |
| `startup` | body | `string[]` | 否 | 启动命令列表 |
| `validate` | body | `string[]` | 否 | 验证命令列表 |
| `shutdown` | body | `string[]` | 否 | 关闭命令列表 |
| `directories` | body | `string[]` | 否 | 需要创建的目录 |
| `files` | body | `string[]` | 否 | 需要生成的文件 |
| `templates` | body | `dict` | 否 | 文件模板 |
| `recreate` | body | `bool` | 否 | true=覆盖已存在的服务 |

**返回（200）：** `{"result": true}`

**示例：**
```bash
curl -X POST http://localhost:8080/api/services/create \
  -H "Content-Type: application/json" \
  -d '{"name":"my_service","group":"Custom",
       "startup":["echo hello"],"shutdown":["echo bye"]}'
# [200] {"result": true}
```

---

## 7. 无线节点 — Wireless

### `POST /api/sessions/{session_id}/wireless/linked` — 控制无线链路

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `wireless_id` | body | `int` | ✅ | Wireless 节点 ID |
| `node1_id` | body | `int` | ✅ | 节点 A ID |
| `node2_id` | body | `int` | ✅ | 节点 B ID |
| `linked` | body | `bool` | ✅ | `true`=连接，`false`=断开 |

**限制：** 两个节点必须先通过 `AddLink` 连接到 Wireless 节点（非直接相连），再通过此接口控制互通。

**返回（200）：** `{"result": true}`

### `GET /api/sessions/{session_id}/wireless/{node_id}` — 获取无线配置

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `node_id` | path | `int` | ✅ | Wireless 节点 ID |

**返回（200）：** 配置选项字典（`bandwidth`, `delay`, `loss`, `jitter`, `range`, `movement` 等）

---

## 8. EMANE 射频模型

### `GET /api/sessions/{session_id}/emane/model` — 获取 EMANE 模型配置

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `node_id` | query | `int` | ✅ | 节点 ID |
| `model` | query | `string` | ✅ | 模型名（`rfpipe`, `ieee80211abg`, `tdma` 等） |
| `iface_id` | query | `int` | 否 | 接口 ID（-1 表示节点级配置） |

**返回：** 若无 EMANE 环境返回 `500`（"emane model does not exist"），有环境返回 `200` + 配置选项。

### `POST /api/sessions/{session_id}/emane/model` — 设置 EMANE 模型配置

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `node_id` | body | `int` | ✅ | 节点 ID |
| `model` | body | `string` | ✅ | 模型名 |
| `config` | body | `dict` | ✅ | 配置键值对 |
| `iface_id` | body | `int \| null` | 否 | 接口 ID |

### `GET /api/sessions/{session_id}/emane/channel` — 获取事件通道

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `nem_id` | query | `int` | ✅ | NEM ID |

**返回（200）：** `{"group": "224.0.0.1", "port": 50000, "device": "eth0"}`

### `POST /api/sessions/{session_id}/emane/link` — EMANE 链路控制

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |
| `nem1` | body | `int` | ✅ | NEM A |
| `nem2` | body | `int` | ✅ | NEM B |
| `linked` | body | `bool` | ✅ | 连接/断开 |

---

## 9. XML 导入导出

### `GET /api/sessions/{session_id}/xml` — 导出会话为 XML

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |

**返回（200）：** `{"data": "<?xml version='1.0'?>...", "session_id": int}`

### `POST /api/xml/open` — 从 XML 字符串导入会话

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `data` | body | `string` | ✅ | CORE XML 格式的场景描述 |
| `start` | body | `bool` | 否 | `true`=立即实例化，`false`=仅定义 |

**返回（200）：** `{"result": true, "session_id": int}`

---

## 10. 拓扑导入 — Topology（JSON + XML）

### `POST /api/topology` — 一键导入完整拓扑

`Content-Type: application/json` 或 `application/xml`

**顶层字段：**

| 参数 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----|
| `name` | `string` | 否 | 拓扑名称 |
| `nodes` | `TopologyNode[]` | ✅ | 节点列表 |
| `links` | `TopologyLink[]` | 否 | 链路列表 |
| `wlans` | `TopologyWlan[]` | 否 | WLAN 节点列表 |

**`TopologyNode`：**

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `id` | `int` | ✅ | — | 节点 ID |
| `name` | `string` | ✅ | — | 节点名称 |
| `model` | `string` | 否 | `"PC"` | `PC` / `router` / `host` / `mdr` |
| `type` | `string` | 否 | `"default"` | `default` / `switch` / `wlan` / `wireless` / `docker` |
| `x` | `float` | 否 | `200` | X 坐标 |
| `y` | `float` | 否 | `200` | Y 坐标 |
| `services` | `string[]` | 否 | — | 服务列表 |
| `icon` | `string` | 否 | — | 图标路径 |

**`TopologyLink`（支持自定义接口 IP）：**

| 字段 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----|
| `node1_id` | `int` | ✅ | 节点 A ID |
| `node2_id` | `int` | ✅ | 节点 B ID |
| `iface1_ip4` | `string` | 否 | 节点 A 的接口 IP |
| `iface1_ip4_mask` | `int` | 否 | 节点 A 的接口掩码（默认 24） |
| `iface2_ip4` | `string` | 否 | 节点 B 的接口 IP |
| `iface2_ip4_mask` | `int` | 否 | 节点 B 的接口掩码 |
| `bandwidth` | `int` | 否 | 带宽（bps） |
| `delay` | `int` | 否 | 延迟（µs） |
| `loss` | `float` | 否 | 丢包率（%） |
| `jitter` | `int` | 否 | 抖动（µs） |
| `dup` | `int` | 否 | 重复包率（%） |

**`TopologyWlan`：**

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `id` | `int` | ✅ | — | 节点 ID |
| `name` | `string` | ✅ | — | 节点名称 |
| `x` | `float` | 否 | `200` | X 坐标 |
| `y` | `float` | 否 | `200` | Y 坐标 |
| `range` | `string` | 否 | `"275"` | 无线范围（像素） |
| `bandwidth` | `string` | 否 | `"54000000"` | 带宽（bps） |
| `delay` | `string` | 否 | `"5000"` | 延迟（µs） |
| `jitter` | `string` | 否 | `"0"` | 抖动（µs） |
| `error` | `string` | 否 | `"0"` | 丢包率（%） |

**返回（201）：**
```json
{"session_id": 1, "name": "拓扑名", "nodes": 3, "links": 1}
```

**JSON 格式示例（含自定义 IP）：**
```json
{
  "name": "demo-network",
  "nodes": [
    {"id": 1, "name": "r1", "model": "router", "x": 100, "y": 100,
     "services": ["zebra", "OSPFv2"]},
    {"id": 2, "name": "pc1", "model": "PC", "x": 300, "y": 200}
  ],
  "wlans": [
    {"id": 3, "name": "wlan0", "x": 200, "y": 200,
     "range": "300", "bandwidth": "55000000"}
  ],
  "links": [
    {"node1_id": 1, "node2_id": 2,
     "iface1_ip4": "10.0.0.1", "iface1_ip4_mask": 24,
     "iface2_ip4": "10.0.0.2", "iface2_ip4_mask": 24,
     "bandwidth": 100000000, "delay": 5000}
  ]
}
```

```bash
curl -X POST http://localhost:8080/api/topology \
  -H "Content-Type: application/json" -d @上述JSON
# [201] {"session_id":1, "name":"demo-network", "nodes":3, "links":1}
```

**XML 格式示例：**
```xml
<?xml version="1.0"?>
<topology name="demo-network">
  <node id="1" name="r1" model="router" x="100" y="100"
        services="zebra,OSPFv2"/>
  <node id="2" name="pc1" model="PC" x="300" y="200"/>
  <node id="3" name="wlan0" type="wlan" x="200" y="200" range="300"/>
  <link node1="1" node2="2"
        iface1_ip4="10.0.0.1" iface1_ip4_mask="24"
        iface2_ip4="10.0.0.2" iface2_ip4_mask="24"
        bandwidth="100000000" delay="5000"/>
</topology>
```

```bash
curl -X POST http://localhost:8080/api/topology \
  -H "Content-Type: application/xml" -d @上述XML
# [201] {"session_id":2, "name":"demo-network", "nodes":3, "links":1}
```

---

## 11. 工具接口 — Utility & Global

### `GET /api/interfaces` — 获取宿主机网络接口

无参数。

**返回（200）：**
```json
{"interfaces": ["eth0", "ens3", "lo"]}
```

### `GET /api/config` — 获取系统配置

无参数。

**返回（200）：**
```json
{
  "services": [{"name":"FRRBabel","group":"FRR",...}, ...],
  "emane_models": ["rfpipe","ieee80211abg","tdma","bypass"]
}
```

---

## 12. SSE 流式接口

### `GET /api/sessions/{session_id}/events/stream` — 会话事件流

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |

**格式：** `text/event-stream`
```bash
curl -N http://localhost:8080/api/sessions/1/events/stream
# data: {"session_id":1, "type":"NodeEvent"}
# data: {"session_id":1, "type":"LinkEvent"}
# ...
```

### `GET /api/sessions/{session_id}/throughputs/stream` — 吞吐量流

| 参数 | 位置 | 类型 | 必填 | 说明 |
|:----|:----:|:----:|:----:|:----|
| `session_id` | path | `int` | ✅ | 会话 ID |

每 3 秒推送一次吞吐量数据 + 心跳。

### `GET /api/cpu-usage/stream` — CPU 使用率流

| 参数 | 位置 | 类型 | 必填 | 默认值 | 说明 |
|:----|:----:|:----:|:----:|:----:|:----|
| `delay` | query | `int` | 否 | `1` | 推送间隔（秒） |

---

## 13. WebSocket 流式接口

| 路径 | 说明 |
|:----|:----|
| `ws://host:8080/api/sessions/{sid}/nodes/{nid}/terminal/ws` | **交互式终端**（见 Node 章节） |
| `ws://host:8080/api/sessions/{sid}/move-nodes/ws` | 流式移动节点 |
| `ws://host:8080/api/sessions/{sid}/emane-pathlosses/ws` | EMANE 路径损耗 |
| `ws://host:8080/api/sessions/{sid}/emane-events/ws` | EMANE 事件（Location/CommEffect） |

---

## 14. 错误码速查

| 状态码 | 含义 | 常见原因 |
|:----:|:----|:----|
| `200` | 成功 | — |
| `201` | 创建成功 | `POST .../sessions`, `POST .../nodes`, `POST .../links`, `POST .../topology` |
| `400` | 请求参数错误 | 无效的 action 值、缺少字段、JSON/XML 格式错误 |
| `404` | 资源不存在 | session_id / node_id / iface_id 不存在 |
| `409` | 资源冲突 | 重复创建 |
| `415` | 不支持的 Content-Type | 调用 `/api/topology` 用了非 JSON/XML 的 Content-Type |
| `422` | 参数校验失败 | Pydantic 校验失败，如类型不对、缺少必填字段 |
| `500` | 服务器内部错误 | gRPC 调用异常、Python 异常 |
| `501` | 未实现 | EMANE 未安装时调用 EMANE 相关接口 |
| `503` | 服务不可用 | core-daemon 未运行或 bridge 未连接 |

---

## 15. 常见问题

### core-daemon 未运行
**现象：** `/health` 返回 `503`，API 请求返回 gRPC `UNAVAILABLE`。
**解决：** `sudo systemctl start core-daemon`

### Python 环境问题
**现象：** `ModuleNotFoundError: No module named 'fastapi'`
**解决：** 使用 CORE venv 运行：`PYTHONPATH="" /opt/core/venv/bin/python3 -m uvicorn app:app`

### 启动会话需要 root
**现象：** `start(definition=false)` 返回 `500`
**解决：** 使用 `{"definition": true}` 仅定义拓扑，或使用 root 权限运行 HTTP Bridge

### Stop 后拓扑丢失
**v1.1.0 已修复：** stop 会保留节点和链路的定义，可再次 start。

### GET /sessions/{id} 在添加节点后报错
**v1.1.0 已修复：** `_session_to_dict` 中 `for n in s.nodes` 改为 `for n in s.nodes.values()`。

### DELETE 请求带 body
部分客户端（`fetch`）不支持 DELETE body。建议用 `POST` 替代或使用支持 DELETE body 的客户端（curl、httpx）。

---

*文档版本: v1.1.1 — 最后更新: 2026-08-18*