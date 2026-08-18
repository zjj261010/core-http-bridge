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



---

## 完整 API 接口说明

### 通用约定

- **基础地址**: `http://<host>:8080`
- **路径前缀**: `/api`
- **Content-Type**: `application/json`（`POST /api/topology` 也支持 `application/xml`）
- **错误格式**: `{"error": "描述", "detail": "技术细节", "hint": "操作提示"}`

---

### 📁 Session 会话

| 方法 | 路径 | 说明 | 请求体(可选) | 状态码 | 返回值 |
|:----|:----|:----|:----|:----:|:----|
| `POST` | `/api/sessions` | 创建会话 | `{"session_id": int}` | `201` | Session对象 |
| `GET` | `/api/sessions` | 获取会话列表 | — | `200` | SessionSummary[] |
| `GET` | `/api/sessions/{id}` | 获取会话详情（含节点+链路） | — | `200` | 完整Session |
| `DELETE` | `/api/sessions/{id}` | 删除会话 | — | `200` | `{"result": bool}` |
| `GET` | `/api/sessions/{id}/check` | 检查会话是否存在 | — | `200` | `{"result": bool}` |
| `POST` | `/api/sessions/{id}/start` | 启动会话 | `{"definition": bool}` | `200` | `{"result","exceptions"}` |
| `POST` | `/api/sessions/{id}/stop` | 停止会话 | — | `200` | `{"result": bool}` |
| `POST` | `/api/sessions/{id}/alert` | 发送告警 | `{"level","source","text","node_id"}` | `200` | `{"result": bool}` |

**创建与启动会话：**
```bash
# 创建
curl -X POST http://localhost:8080/api/sessions \
  -H "Content-Type: application/json" -d '{}'
# [201] {"id": 1, "state": 1, "nodes": [], "links": [], ...}

# 启动（definition=true 仅定义，无需 root）
curl -X POST http://localhost:8080/api/sessions/1/start \
  -H "Content-Type: application/json" -d '{"definition": true}'
# [200] {"result": true, "exceptions": []}

# 获取会话详情
curl http://localhost:8080/api/sessions/1
# 返回所有节点和链路（修复了！确保使用 for n in s.nodes.values()）
```

---

### 🖥️ Node 节点

**NodeType**: `0`=DEFAULT, `4`=SWITCH, `6`=WIRELESS_LAN, `10`=EMANE, `15`=DOCKER, `17`=WIRELESS

| 方法 | 路径 | 说明 | 请求体/参数 | 状态码 | 返回值 |
|:----|:----|:----|:----|:----:|:----|
| `POST` | `/api/sessions/{sid}/nodes` | 添加节点 | `{"id","name","model","type","position","services"}` | `201` | `{"node_id": int}` |
| `GET` | `/api/sessions/{sid}/nodes/{nid}` | 获取节点详情 | — | `200` | `{"node","interfaces","links"}` |
| `PATCH` | `/api/sessions/{sid}/nodes/{nid}` | 编辑节点（图标） | `?icon=xxx` | `200` | `{"result": bool}` |
| `DELETE` | `/api/sessions/{sid}/nodes/{nid}` | 删除节点 | — | `200` | `{"result": bool}` |
| `POST` | `/api/sessions/{sid}/nodes/{nid}/move` | 移动节点位置 | `{"position":{"x","y"}}` 或 `{"geo":{...}}` | `200` | `{"result": bool}` |
| `POST` | `/api/sessions/{sid}/nodes/{nid}/command` | 在节点上执行命令 | `{"command","wait","shell"}` | `200` | `{"return_code","output"}` |
| `GET` | `/api/sessions/{sid}/nodes/{nid}/terminal` | 获取终端命令 | — | `200` | `{"terminal": "vcmd ..."}` |



---

## 实战：构建 PC—Router—PC 拓扑并验证连通性

### 网络拓扑

```
PC1 (10.0.0.1/24) ─── Router (10.0.0.2/24 + 10.0.1.1/24) ─── PC2 (10.0.1.2/24)
```

### 完整 bash 脚本

将以下内容保存为 `demo-pc-router-pc.sh` 并执行（确保 `core-daemon` 和 HTTP Bridge 已启动）：

```bash
#!/bin/bash
BASE="http://localhost:8080/api"

# 1. 清理残留会话
for sid in $(curl -s $BASE/sessions | python3 -c "import sys,json; [print(s['id']) for s in json.load(sys.stdin)]" 2>/dev/null); do
    curl -s -X DELETE "$BASE/sessions/$sid" > /dev/null
done

# 2. 创建会话
SID=$(curl -s -X POST "$BASE/sessions" -H "Content-Type: application/json" -d '{}' | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "Session ID: $SID"

# 3. 添加节点：PC1(id=1), Router(id=2), PC2(id=3)
curl -s -X POST "$BASE/sessions/$SID/nodes" -H "Content-Type: application/json" \
  -d '{"id":1,"name":"pc1","model":"PC","type":0,"position":{"x":100,"y":100}}'
curl -s -X POST "$BASE/sessions/$SID/nodes" -H "Content-Type: application/json" \
  -d '{"id":2,"name":"router","model":"router","type":0,"position":{"x":200,"y":100}}'
curl -s -X POST "$BASE/sessions/$SID/nodes" -H "Content-Type: application/json" \
  -d '{"id":3,"name":"pc2","model":"PC","type":0,"position":{"x":300,"y":100}}'

# 4. 添加链路：PC1<->Router<->PC2
curl -s -X POST "$BASE/sessions/$SID/links" -H "Content-Type: application/json" \
  -d '{"node1_id":1,"node2_id":2,"iface1":{"id":0},"iface2":{"id":0}}'
curl -s -X POST "$BASE/sessions/$SID/links" -H "Content-Type: application/json" \
  -d '{"node1_id":2,"node2_id":3,"iface1":{"id":1},"iface2":{"id":0}}'

# 5. 启动会话（实例化，需要 root）
curl -s -X POST "$BASE/sessions/$SID/start" -H "Content-Type: application/json" \
  -d '{"definition":false}'

# 6. 配置 IP 地址
## PC1: eth0 = 10.0.0.1/24
curl -s -X POST "$BASE/sessions/$SID/nodes/1/command" -H "Content-Type: application/json" \
  -d '{"command":"ip addr add 10.0.0.1/24 dev eth0","wait":true,"shell":true}'
curl -s -X POST "$BASE/sessions/$SID/nodes/1/command" -H "Content-Type: application/json" \
  -d '{"command":"ip link set eth0 up","wait":true,"shell":true}'

## Router: eth0=10.0.0.2/24, eth1=10.0.1.1/24
curl -s -X POST "$BASE/sessions/$SID/nodes/2/command" -H "Content-Type: application/json" \
  -d '{"command":"ip addr add 10.0.0.2/24 dev eth0","wait":true,"shell":true}'
curl -s -X POST "$BASE/sessions/$SID/nodes/2/command" -H "Content-Type: application/json" \
  -d '{"command":"ip addr add 10.0.1.1/24 dev eth1","wait":true,"shell":true}'
curl -s -X POST "$BASE/sessions/$SID/nodes/2/command" -H "Content-Type: application/json" \
  -d '{"command":"ip link set eth0 up","wait":true,"shell":true}'
curl -s -X POST "$BASE/sessions/$SID/nodes/2/command" -H "Content-Type: application/json" \
  -d '{"command":"ip link set eth1 up","wait":true,"shell":true}'

## PC2: eth0 = 10.0.1.2/24
curl -s -X POST "$BASE/sessions/$SID/nodes/3/command" -H "Content-Type: application/json" \
  -d '{"command":"ip addr add 10.0.1.2/24 dev eth0","wait":true,"shell":true}'
curl -s -X POST "$BASE/sessions/$SID/nodes/3/command" -H "Content-Type: application/json" \
  -d '{"command":"ip link set eth0 up","wait":true,"shell":true}'

# 7. 启用路由转发
curl -s -X POST "$BASE/sessions/$SID/nodes/2/command" -H "Content-Type: application/json" \
  -d '{"command":"sysctl -w net.ipv4.ip_forward=1","wait":true,"shell":true}'

# 8. 添加默认路由
curl -s -X POST "$BASE/sessions/$SID/nodes/1/command" -H "Content-Type: application/json" \
  -d '{"command":"ip route add default via 10.0.0.2","wait":true,"shell":true}'
curl -s -X POST "$BASE/sessions/$SID/nodes/3/command" -H "Content-Type: application/json" \
  -d '{"command":"ip route add default via 10.0.1.1","wait":true,"shell":true}'

# 9. 测试连通性
echo ""
echo "=== Ping Test: PC1 -> PC2 (10.0.1.2) ==="
curl -s -X POST "$BASE/sessions/$SID/nodes/1/command" -H "Content-Type: application/json" \
  -d '{"command":"ping -c 3 10.0.1.2","wait":true,"shell":true}' | python3 -c "
import sys,json
j=json.load(sys.stdin)
for line in j['output'].split('\n'):
    if 'ttl=' in line.lower() or 'transmitted' in line.lower():
        print(f'  {line}')
"

echo ""
echo "=== Ping Test: PC2 -> PC1 (10.0.0.1) ==="
curl -s -X POST "$BASE/sessions/$SID/nodes/3/command" -H "Content-Type: application/json" \
  -d '{"command":"ping -c 3 10.0.0.1","wait":true,"shell":true}' | python3 -c "
import sys,json
j=json.load(sys.stdin)
for line in j['output'].split('\n'):
    if 'ttl=' in line.lower() or 'transmitted' in line.lower():
        print(f'  {line}')
"

echo ""
echo "=== Routing Table: Router ==="
curl -s -X POST "$BASE/sessions/$SID/nodes/2/command" -H "Content-Type: application/json" \
  -d '{"command":"ip route","wait":true,"shell":true}' | python3 -c "import sys,json;print(json.load(sys.stdin)['output'])"

echo ""
echo "=== Interfaces: Router ==="
curl -s -X POST "$BASE/sessions/$SID/nodes/2/command" -H "Content-Type: application/json" \
  -d '{"command":"ip addr","wait":true,"shell":true}' | python3 -c "import sys,json;print(json.load(sys.stdin)['output'][:300])"

# 10. 清理（可选）
# curl -s -X DELETE "$BASE/sessions/$SID"
```

### 执行方式

```bash
chmod +x demo-pc-router-pc.sh
# 直接运行（bridge 和 core-daemon 需已启动）
bash demo-pc-router-pc.sh
```

### 预期输出

```
Session ID: 1
=== Ping Test: PC1 -> PC2 (10.0.1.2) ===
  64 bytes from 10.0.1.2: icmp_seq=1 ttl=63 time=0.169 ms
  64 bytes from 10.0.1.2: icmp_seq=2 ttl=63 time=0.196 ms
  64 bytes from 10.0.1.2: icmp_seq=3 ttl=63 time=0.206 ms
  3 packets transmitted, 3 received, 0% packet loss

=== Ping Test: PC2 -> PC1 (10.0.0.1) ===
  64 bytes from 10.0.0.1: icmp_seq=1 ttl=63 time=0.136 ms
  64 bytes from 10.0.0.1: icmp_seq=2 ttl=63 time=0.063 ms
  64 bytes from 10.0.0.1: icmp_seq=3 ttl=63 time=0.065 ms
  3 packets transmitted, 3 received, 0% packet loss

=== Routing Table: Router ===
10.0.0.0/24 dev eth0 proto kernel scope link src 10.0.0.2
10.0.1.0/24 dev eth1 proto kernel scope link src 10.0.1.1

=== Interfaces: Router ===
2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500
    inet 10.0.0.2/24 scope global eth0
3: eth1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500
    inet 10.0.1.1/24 scope global eth1
```

### 关键说明

| 步骤 | 说明 |
|:----|:----|
| `definition=false` 启动 | **需要 root 权限**，因为 CORE 会创建网络命名空间和 veth 对 |
| `ip addr add ... dev eth0` | 在节点的网络命名空间中配置 IP |
| `ip route add default via ...` | 添加默认路由指向路由器 |
| `sysctl -w net.ipv4.ip_forward=1` | 在路由器上启用 IP 转发，否则不会路由跨网段流量 |
| `ping -c 3` | `ttl=63` 说明经过了 1 跳路由器（初始 64，减 1） |

### NodeCommand 接口说明

```bash
POST /api/sessions/{sid}/nodes/{nid}/command
Content-Type: application/json

{
  "command": "要执行的命令",    # 必填
  "wait": true,                 # 是否等待执行完成（获取输出）
  "shell": true                 # 是否通过 shell 执行
}
```

**常用命令参考**：

| 命令 | 用途 |
|:----|:----|
| `ip addr` | 查看 IP 地址和接口状态 |
| `ip link set eth0 up` | 启用接口 |
| `ip addr add 10.0.0.1/24 dev eth0` | 配置 IP 地址 |
| `ip route` | 查看路由表 |
| `ip route add default via 10.0.0.2` | 添加默认路由 |
| `ip route add 10.0.0.0/24 via 10.0.0.1` | 添加静态路由 |
| `ping -c 3 <IP>` | 测试连通性 |
| `ping -c 3 -I eth0 <IP>` | 指定源接口 ping |
| `traceroute <IP>` | 追踪路由路径 |
| `sysctl -w net.ipv4.ip_forward=1` | 启用 IP 转发（路由器） |
| `iptables -L` | 查看防火墙规则 |
| `ip neigh` | 查看 ARP 表 |
| `ethtool eth0` | 查看接口信息 |
| `tc qdisc show dev eth0` | 查看流量控制（链路延迟/丢包） |
| `iperf -s` | 启动 iperf 服务端（带宽测试） |
| `iperf -c <server_ip>` | iperf 客户端测试 |

> **注意**：命令在节点的网络命名空间中执行。会话未实例化时（`definition=true`）节点没有网络命名空间，命令会失败。需要先 `start(definition=false)` 实例化会话（需 root 权限）。
**添加路由器：**
```bash
curl -X POST http://localhost:8080/api/sessions/1/nodes \
  -H "Content-Type: application/json" \
  -d '{"id":1,"name":"r1","model":"router","type":0,
       "position":{"x":100,"y":100},
       "services":["zebra","OSPFv2"]}'
# [201] {"node_id": 1}
```

**获取节点详情（含接口和链路）：**
```bash
curl http://localhost:8080/api/sessions/1/nodes/1
# [200] {"node":{"id":1,"name":"r1",...}, "interfaces":[...], "links":[...]}
```

---

### 🔗 Link 链路

**LinkOptions**: `bandwidth`(bps), `delay`(µs), `loss`(%), `jitter`(µs), `dup`(%)

| 方法 | 路径 | 说明 | 请求体 | 状态码 | 返回值 |
|:----|:----|:----|:----|:----:|:----|
| `POST` | `/api/sessions/{sid}/links` | 添加链路 | `{"node1_id","node2_id","iface1","iface2","options"}` | `201` | `{"result","iface1","iface2"}` |
| `PATCH` | `/api/sessions/{sid}/links` | 修改链路参数 | `{"node1_id","node2_id","iface1","iface2","options"}` | `200` | `{"result": bool}` |
| `DELETE` | `/api/sessions/{sid}/links` | 删除链路 | `{"node1_id","node2_id","iface1","iface2"}` | `200` | `{"result": bool}` |
| `POST` | `/api/sessions/{sid}/links/linked` | 切换链路通断 | `{"node1_id","node2_id","iface1_id","iface2_id","linked"}` | `200` | `{"result": bool}` |

**添加链路（含带宽/延迟限制）：**
```bash
curl -X POST http://localhost:8080/api/sessions/1/links \
  -H "Content-Type: application/json" \
  -d '{"node1_id":1,"node2_id":2,
       "iface1":{"id":0},"iface2":{"id":0},
       "options":{"bandwidth":100000000,"delay":5000,"loss":1.5}}'
# [201] {"result":true, "iface1":{"id":0,"name":"eth0",...}, "iface2":{...}}
```

**拔插网线（Linked 接口）：**
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

### 📡 WLAN / 🏃 Mobility / 🛠️ Service

| 方法 | 路径 | 说明 |
|:----|:----|:----|
| `GET` | `/api/sessions/{sid}/wlan/{nid}` | 获取WLAN无线配置 |
| `POST` | `/api/sessions/{sid}/wlan` | 设置WLAN配置 `{"wlan_id","config":{...}}` |
| `GET` | `/api/sessions/{sid}/mobility/{nid}` | 获取移动模型配置 |
| `POST` | `/api/sessions/{sid}/mobility` | 设置移动配置 `{"node_id","config":{...}}` |
| `POST` | `/api/sessions/{sid}/mobility/action` | 移动控制 `{"node_id","action":"START\|PAUSE\|STOP"}` |
| `GET` | `/api/sessions/{sid}/services/defaults` | 服务默认配置 |
| `GET` | `/api/sessions/{sid}/services/node` | 节点服务配置 |
| `GET` | `/api/sessions/{sid}/services/rendered` | 服务渲染文件 |
| `POST` | `/api/sessions/{sid}/services/action` | 服务动作 `{"node_id","service","action":...}` |
| `POST` | `/api/services/create` | 创建自定义服务 |

---



### 📦 拓扑导入（JSON + XML）

#### `POST /api/topology` — 导入网络拓扑

通过 `Content-Type` 自动识别格式（`application/json` 或 `application/xml`）。

**链路支持自定义接口 IP：** 在 JSON 的 `links` 或 XML 的 `<link>` 中指定：
- `iface1_ip4` / `iface1_ip4_mask` — 节点1 的接口 IP
- `iface2_ip4` / `iface2_ip4_mask` — 节点2 的接口 IP

```bash
# JSON（含自定义IP）
curl -X POST http://localhost:8080/api/topology \
  -H "Content-Type: application/json" \
  -d '{
    "name":"demo-net",
    "nodes":[
      {"id":1,"name":"r1","model":"router","x":100,"y":100,"services":["zebra","OSPFv2"]},
      {"id":2,"name":"pc1","model":"PC","x":300,"y":200}
    ],
    "links":[
      {"node1_id":1,"node2_id":2,
       "iface1_ip4":"10.0.0.1","iface1_ip4_mask":24,
       "iface2_ip4":"10.0.0.2","iface2_ip4_mask":24,
       "bandwidth":100000000,"delay":5000}
    ],
    "wlans":[
      {"id":3,"name":"wlan0","x":200,"y":200,"range":"300"}
    ]
  }'
```
```json
// [201] Response:
{"session_id":1, "name":"demo-net", "nodes":3, "links":1}
```

```bash
# XML（含自定义IP）
curl -X POST http://localhost:8080/api/topology \
  -H "Content-Type: application/xml" \
  -d '<topology name="demo-net">
    <node id="1" name="r1" model="router" x="100" y="100" services="zebra,OSPFv2"/>
    <node id="2" name="pc1" model="PC" x="300" y="200"/>
    <node id="3" name="wlan0" type="wlan" x="200" y="200" range="300"/>
    <link node1="1" node2="2"
          iface1_ip4="10.0.0.1" iface1_ip4_mask="24"
          iface2_ip4="10.0.0.2" iface2_ip4_mask="24"
          bandwidth="100000000" delay="5000"/>
  </topology>'
```
### 🗂️ XML / 🛠️ 工具 / 📡 SSE / 🔌 WebSocket

| 方法 | 路径 | 说明 |
|:----|:----|:----|
| `GET` | `/api/sessions/{sid}/xml` | 导出会话为XML |
| `POST` | `/api/xml/open` | 从XML字符串导入会话 |
| `GET` | `/api/interfaces` | 宿主机网络接口 |
| `GET` | `/api/config` | 全局系统配置（服务+EMANE模型列表） |
| `GET` | `/api/sessions/{sid}/events/stream` | SSE 会话事件流 |
| `GET` | `/api/sessions/{sid}/throughputs/stream` | SSE 吞吐量流 |
| `GET` | `/api/cpu-usage/stream?delay=N` | SSE CPU使用率流 |
| `WS` | `/api/sessions/{sid}/move-nodes/ws` | WebSocket 流式移动节点 |
| `WS` | `/api/sessions/{sid}/emane-pathlosses/ws` | WebSocket EMANE路径损耗 |
| `WS` | `/api/sessions/{sid}/emane-events/ws` | WebSocket EMANE事件 |

**SSE 使用：**
```bash
curl -N http://localhost:8080/api/sessions/1/events/stream
# data: {"session_id":1, "type":"NodeEvent"}
# data: {"session_id":1, "type":"LinkEvent"}
```

**WebSocket 使用：**
```javascript
const ws = new WebSocket("ws://localhost:8080/api/sessions/1/move-nodes/ws");
ws.send(JSON.stringify({node_id:1, position:{x:200, y:200}}));
ws.send(JSON.stringify({stop: true}));
```





### 🔗 Link 链路管理

**LinkOptions**: `bandwidth`(bps), `delay`(µs), `loss`(%), `jitter`(µs), `dup`(%)

**接口配置（iface1 / iface2）：** 支持以下字段：
| 字段 | 类型 | 说明 |
|:----|:----|:----|
| `id` | int | 接口序号（必填，从 0 开始） |
| `name` | str | 接口名（默认 eth<id>） |
| `ip4` | str | IPv4 地址，如 `"10.0.0.1"` |
| `ip4_mask` | int | IPv4 掩码位数，如 `24` |
| `mac` | str | MAC 地址（自动生成） |

| 方法 | 路径 | 说明 | 请求体 | 状态码 | 返回值 |
|:----|:----|:----|:----|:----:|:----|
| `POST` | `/api/sessions/{sid}/links` | 添加链路 | `{"node1_id","node2_id","iface1","iface2","options"}` | `201` | `{"result","iface1","iface2"}` |
| `PATCH` | `/api/sessions/{sid}/links` | 修改链路参数 | `{"node1_id","node2_id","iface1","iface2","options"}` | `200` | `{"result": bool}` |
| `DELETE` | `/api/sessions/{sid}/links` | 删除链路 | `{"node1_id","node2_id","iface1","iface2"}` | `200` | `{"result": bool}` |
| `POST` | `/api/sessions/{sid}/links/linked` | 切换通断 | `{"node1_id","node2_id","iface1_id","iface2_id","linked"}` | `200` | `{"result": bool}` |

**添加链路（指定 IP + 链路参数）：**
```bash
curl -X POST http://localhost:8080/api/sessions/1/links \
  -H "Content-Type: application/json" \
  -d '{"node1_id":1,"node2_id":2,
       "iface1":{"id":0,"ip4":"10.0.0.1","ip4_mask":24},
       "iface2":{"id":0,"ip4":"10.0.0.2","ip4_mask":24},
       "options":{"bandwidth":100000000,"delay":5000,"loss":1.5}}'
```
```json
// [201] Response:
{"result":true, "iface1":{"id":0,"name":"eth0","ip4":"10.0.0.1","ip4_mask":24,...},
 "iface2":{"id":0,"name":"eth0","ip4":"10.0.0.2","ip4_mask":24,...}}
```

**拔插网线（Linked 接口）：**
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
### 🌐 拓扑导入（JSON + XML）

#### `POST /api/topology` — 一键导入完整拓扑

支持 `Content-Type: application/json` 和 `application/xml` 两种格式。

**链路支持自定义IP：** 在 JSON 链路或 XML `<link>` 中指定 `iface1_ip4`/`iface1_ip4_mask`/`iface2_ip4`/`iface2_ip4_mask` 来设置接口 IP。

**JSON 格式：**
```json
{
  "name": "拓扑名",
  "nodes": [
    {"id": 1, "name": "r1", "model": "router", "x": 100, "y": 100,
     "services": ["zebra", "OSPFv2"]},
    {"id": 2, "name": "pc1", "model": "PC", "x": 300, "y": 200}
  ],
  "links": [
    {"node1_id": 1, "node2_id": 2,
     "iface1_ip4": "10.0.0.1", "iface1_ip4_mask": 24,
     "iface2_ip4": "10.0.0.2", "iface2_ip4_mask": 24,
     "bandwidth": 100000000, "delay": 5000}
  ],
  "wlans": [
    {"id": 3, "name": "wlan0", "x": 200, "y": 200,
     "range": "300", "bandwidth": "55000000"}
  ]
}
```

**XML 格式（含自定义IP）：**
```xml
<topology name="拓扑名">
  <node id="1" name="r1" model="router" x="100" y="100" services="zebra,OSPFv2"/>
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
  -H "Content-Type: application/json" \
  -d '{"name":"demo","nodes":[{"id":1,"name":"r1","x":100,"y":100},{"id":2,"name":"pc1","x":300,"y":200}],
       "links":[{"node1_id":1,"node2_id":2,"iface1_ip4":"10.0.0.1","iface1_ip4_mask":24,
                 "iface2_ip4":"10.0.0.2","iface2_ip4_mask":24}]}'
```
```json
// [201] Response:
{"session_id": 3, "name": "demo", "nodes": 2, "links": 1}
```

