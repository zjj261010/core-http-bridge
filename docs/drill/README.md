# CORE 漏洞演练体系 —— 归档资产清单

> 归档位置：/opt/artifacts/core-drill/（持久目录，/tmp 清理不影响）
> 归档时间：2026-11-12　环境：CORE 9.2.1 + Docker + 自定义服务（路线 B）

## 目录结构

| 目录/文件 | 内容 |
|:--|:--|
| `演练执行手册.md` | **主文档**：手工执行命令全集（部署/检查/攻击链/加固切换/复盘/清理/排障） |
| `演练想定.md` | 演练场景设计（拓扑/编址/攻击链/修复建议） |
| `deploy/` | 4 个部署脚本（路线 A 混合版 → 路线 B 服务声明版 → 双模式参数化版） |
| `attack/` | 攻击链脚本（盲测十阶段、工具化模拟、分步攻击） |
| `logs/` | 实跑记录（盲测输出） |
| `targets/` | 全部靶标镜像 Dockerfile 与模式脚本（构建源） |
| `core-services/` | CORE 自定义服务定义包（drill_services.py + templates/） |

## 运行时位置（与归档的对应关系，需保持同步）

| 归档文件 | 运行时位置 | 说明 |
|:--|:--|:--|
| `core-services/drill_services.py` + `templates/` | `/opt/core/etc/custom_services/drill/` | daemon 加载源，改后需重启 core-daemon |
| `targets/Dockerfile.*` | 构建源，产物为本机 docker 镜像 | 镜像改后需重新部署 |

## 镜像清单（运行时依赖）

| 镜像 | 角色 |
|:--|:--|
| `target-attacker-kali:v1` | 攻击机（Kali + CSLE 工具清单 + nmap CVE 指纹 + SecLists，无弱口令） |
| `target-fw:v1` | 边界防火墙（iptables 策略开机自启） |
| `target-dmz-base:v3` / `target-inn-base:v3` | 路线 B 基座（软件装好不启动，服务由 CORE 声明） |
| 基料：`kalilinux/kali-last-release`、`httpd:2.4.49`、`nginx:stable`、`python:3.10-slim` | 构建上游 |

## 快速使用

```bash
# 部署（vuln = 红方定向；normal = 基线对照）
python3 /opt/artifacts/core-drill/deploy/core_run_v3b.py vuln

# 盲测攻击链（不知漏洞版）
bash /opt/artifacts/core-drill/attack/blind_drill.sh

# 加固切换（蓝方）
python3 - <<'PY'
from core.api.grpc import client
from core.api.grpc.wrappers import ServiceAction
c = client.CoreGrpcClient(); c.connect()
c.service_action(1, 12, "svc-httpd-41773-vuln", ServiceAction.STOP)
c.service_action(1, 12, "svc-httpd-41773-normal", ServiceAction.START)
PY

# 验证加固效果（同 PoC 应 403）
docker exec attacker curl -s -o /dev/null -w '%{http_code}\n' --path-as-is \
  -d 'echo;id' "http://10.0.2.20/cgi-bin/.%2e/.%2e/.%2e/.%2e/bin/sh"
```

## 架构要点（路线 B 定稿）

1. **基座镜像不自启服务**（软件装好 + `WORKDIR /` + iproute2；CORE 的 tail 覆盖天然保活）
2. **模式 = CORE 服务声明**（svc-<软件>-<CVE>-vuln/normal 成对注册、同软件互斥、缺省安全）
3. **双向锚点验证**：normal 态利用必须失败、vuln 态利用必须成功
4. **已知 CORE 缺口与规避**（详见手册 §9）：executables 宿主校验对容器节点不适用（服务定义已去 executables）、模板必须在 templates/ 子目录、docker exec 后台 daemon 必须重定向输出、默认路由宿主侧补
