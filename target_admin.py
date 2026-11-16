"""
靶标包管理路由（Tab1 镜像管理 + Tab2 独立容器管理）
设计文档：/opt/artifacts/core-drill/管理平台改造设计文档.md / 镜像管理模块设计文档.md
路线 B：基座镜像不自启服务，CORE 声明启动；本模块管理交付物（镜像/服务定义/清单）和独立容器

响应统一为 {"ok": bool, "data": {...}} / {"ok": False, "error": {"code", "message"}}（设计 §3.2）
"""
import asyncio
import json
import logging
import os
import pty
import signal
import subprocess

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

logger = logging.getLogger("core.target_admin")

router = APIRouter()

# ── 路径常量 ──
MANIFEST_STORE = "/opt/artifacts/core-drill/manifests.json"
CUSTOM_SERVICES_DIR = "/opt/core/etc/custom_services"
IMAGE_IMPORT_DIR = "/opt/artifacts/core-drill/image-imports"
INSTANCE_STORE = "/opt/artifacts/core-drill/instances.json"
TOPOLOGY_STORE = "/opt/artifacts/core-drill/topologies.json"
AUDIT_LOG = "/opt/artifacts/core-drill/audit.jsonl"
STANDALONE_PREFIX = "standalone-"

os.makedirs(IMAGE_IMPORT_DIR, exist_ok=True)


# ═══════════════════════════════════════════════
# 内部工具
# ═══════════════════════════════════════════════

def _load_manifests() -> dict:
    if os.path.exists(MANIFEST_STORE):
        try:
            with open(MANIFEST_STORE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("manifest store corrupt: %s", e)
    return {}


def _save_manifests(data: dict):
    os.makedirs(os.path.dirname(MANIFEST_STORE), exist_ok=True)
    with open(MANIFEST_STORE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _load_store(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("store corrupt %s: %s", path, e)
    return {}


def _save_store(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _docker(cmd: str, timeout: int = 120) -> tuple[str, int]:
    r = subprocess.run(f"docker {cmd}", shell=True, capture_output=True, text=True, timeout=timeout)
    out = r.stdout.strip()
    if r.returncode != 0 and r.stderr.strip():
        out = out or r.stderr.strip()
    return out, r.returncode


def _container_net_bytes(name: str) -> tuple[int, int] | None:
    """容器内 eth* 接口累计 rx/tx 字节。
    docker stats 的 NetIO 对 --net=none 容器（CORE 方式）恒为 0B——它只统计 docker 自建网络,
    后注入的 veth 接口须直接读容器 netns 的 /sys/class/net 计数器"""
    rx_out, _ = _docker(f"exec {name} bash -c 'cat /sys/class/net/eth[0-9]/statistics/rx_bytes 2>/dev/null'")
    tx_out, _ = _docker(f"exec {name} bash -c 'cat /sys/class/net/eth[0-9]/statistics/tx_bytes 2>/dev/null'")
    try:
        rx = sum(int(x) for x in rx_out.split() if x.strip().isdigit())
        tx = sum(int(x) for x in tx_out.split() if x.strip().isdigit())
        return rx, tx
    except ValueError:
        return None


def _fmt_bytes(n: float) -> str:
    for unit, div in (("TB", 2**40), ("GB", 2**30), ("MB", 2**20), ("KB", 2**10)):
        if n >= div:
            return f"{n/div:.1f}{unit}"
    return f"{n:.0f}B"


def _err(code: str, message: str, status: int = 400, **extra):
    body = {"ok": False, "error": {"code": code, "message": message}}
    if extra:
        body["error"].update(extra)
    return JSONResponse(body, status_code=status)


def _audit(action: str, **kw):
    """审计留痕（设计 §8.1）：主写 platform.db（audit 表, 可按会话/节点/动作过滤）,
    JSONL 双写一个版本期（历史兼容）; bridge.log 文本同步"""
    logger.info("AUDIT %s %s", action, kw)
    try:
        import platform_db
        platform_db.write_audit(action, **kw)
    except Exception as e:
        logger.warning("audit db write failed: %s", e)
    try:
        import time as _t
        entry = {"ts": _t.strftime("%Y-%m-%d %H:%M:%S"), "action": action, **kw}
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as e:
        logger.warning("audit jsonl write failed: %s", e)


def _manifest_index() -> dict:
    """镜像 ref -> manifest（按 manifest.image.name/tag 索引）"""
    idx = {}
    for m in _load_manifests().values():
        if not isinstance(m, dict):
            continue
        img = m.get("image") or {}
        nm, tg = img.get("name"), img.get("tag")
        if nm:
            idx[f"{nm}:{tg or 'latest'}"] = m
    return idx


_svc_cache = {"loaded": False, "names": set(), "registry": {}}


def _service_registry() -> dict:
    """服务注册表（name -> 类对象）：内置 + custom_services——带缓存"""
    if _svc_cache["loaded"]:
        return _svc_cache["registry"]
    names, registry = set(), {}
    try:
        from core.services.manager import ServiceManager
        mgr = ServiceManager()
        mgr.load_locals()
        if os.path.isdir(CUSTOM_SERVICES_DIR):
            mgr.load(os.path.abspath(CUSTOM_SERVICES_DIR))
        registry = dict(mgr.services)
        names = set(registry.keys())
    except Exception as e:
        logger.warning("service registry load failed: %s", e)
    # 兜底: 内置名硬保底（ServiceManager 异常时 DefaultRoute 等不至于误报缺失）
    names.update({"DefaultRoute", "SSH", "NAT", "IPForward"})
    _svc_cache["names"] = names
    _svc_cache["registry"] = registry
    _svc_cache["loaded"] = True
    return registry


def _registered_service_names() -> set:
    """CORE 认识的服务名全集（内置 27 个 + custom_services 注册的）——带缓存"""
    return set(_service_registry().keys())


def _svc_dir_exists(svc_name: str) -> bool:
    """服务定义是否就位（设计 §4.1 services 件）：
    ① CORE 认识的服务名（内置 + custom_services 注册，前提⑤⁺ 基础服务条目算就位）
    ② 兜底扫描 custom_services .py 内容"""
    if svc_name in _registered_service_names():
        return True
    if not os.path.isdir(CUSTOM_SERVICES_DIR):
        return False
    needle = f'"{svc_name}"'
    for root, _dirs, files in os.walk(CUSTOM_SERVICES_DIR):
        for fn in files:
            if fn.endswith(".py"):
                try:
                    with open(os.path.join(root, fn), encoding="utf-8", errors="ignore") as f:
                        if needle in f.read():
                            return True
                except OSError:
                    continue
    return False


def _pieces(ref: str, mindex: dict) -> dict:
    """三件完整性（设计 §4.1）：镜像(docker 在列) / 服务定义(落地 custom_services) / 清单(已编目)"""
    repo = ref.rsplit(":", 1)[0]
    man = mindex.get(ref) or mindex.get(repo)
    svcs_ok = True
    svc_list = []
    for s in (man or {}).get("services") or []:
        svc_list.append({"name": s.get("name"), "mode": s.get("mode")})
        if not _svc_dir_exists(s.get("name", "")):
            svcs_ok = False
    has_svcs = bool(svc_list)
    return {
        "image": True,
        "services": has_svcs and svcs_ok,
        "manifest": bool(man),
        "complete": bool(man) and has_svcs and svcs_ok,
        "manifest_data": man,
        "service_pairs": svc_list,
    }


def _enrich_pairs(pairs: list) -> list:
    """服务对附加实现层信息（设计 §8.2 服务表格数据源）：
    startup/validate 来自服务注册表；owner = 落位包（None=CORE 内置或未落位）"""
    reg = _service_registry()
    out = []
    for p in pairs:
        p = dict(p)
        cls = reg.get(p.get("name"))
        if cls:
            p["impl"] = {"startup": list(cls.startup or []),
                         "validate": list(getattr(cls, "validate", []) or []),
                         "files": list(cls.files or [])}
            p["owner"] = _service_name_owner(p.get("name")) or "CORE 内置"
        else:
            p["impl"] = None
            p["owner"] = "CORE 内置" if not str(p.get("name", "")).startswith("svc-") else None
        out.append(p)
    return out


# ═══════════════════════════════════════════════
# Tab1 镜像管理

# ═══════════════════════════════════════════════
# Tab1 镜像管理
# ═══════════════════════════════════════════════

@router.get("/targets")
async def targets_list():
    """靶标镜像列表（按 image ID 去重别名，含三件完整性 + 编目状态）"""
    manifests = _load_manifests()
    mindex = _manifest_index()
    out, _code = _docker("images --format '{{json .}}'", 30)
    by_id = {}
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except json.JSONDecodeError:
            continue
        repo, tag = o.get("Repository") or "", o.get("Tag") or ""
        if not repo or repo == "<none>" or tag == "<none>":
            continue
        ref = f"{repo}:{tag}"
        iid = o.get("ID") or ref
        if iid in by_id:
            by_id[iid]["aliases"].append(ref)
            continue
        pc = _pieces(ref, mindex)
        by_id[iid] = {
            "ref": ref,
            "size": o.get("Size", ""),
            "import_time": " ".join(str(o.get("CreatedAt", "")).split()[:2]) or None,
            "aliases": [],
            "pieces": {k: pc[k] for k in ("image", "services", "manifest", "complete")},
            "has_manifest": pc["manifest"],
        }
    # 规则（用户定稿）: 镜像必须导入（列表行存在的前提）; 服务定义/清单可缺, 进详情补录。
    # 纯 manifest 的孤儿条目不进列表（镜像未导入则整个靶标条目不存在）。
    images = sorted(by_id.values(), key=lambda i: i["import_time"] or "", reverse=True)
    return JSONResponse({"ok": True, "data": {"images": images, "manifests": manifests}})


@router.post("/targets/import/image")
async def targets_import_image(body: dict):
    """导入镜像 tar（docker load）。body: {path} 服务器路径"""
    path = (body.get("path") or "").strip()
    if not path or not os.path.exists(path):
        return _err("FILE_NOT_FOUND", f"文件不存在: {path}")
    out, code = _docker(f"load -i '{path}'", 600)
    _audit("image.import", path=path, ok=code == 0)
    if code != 0:
        return _err("LOAD_FAILED", out or "docker load 失败", status=500)
    return JSONResponse({"ok": True, "data": {"output": out}})


@router.post("/targets/import/bundle")
async def targets_import_bundle(body: dict):
    """整包导入（设计 §8.3 导入向导的落地）: 一次目录, 三件独立落位, 归属一目了然。
    目录约定（包名=目录名）:
      <dir>/image.tar        ① 基座镜像（可缺——外源镜像另行导入）
      <dir>/services/        ② 服务定义（*.py + templates/, 整树拷贝到 custom_services/<包名>/）
      <dir>/manifest.json    ③ 配方清单（可缺——详情抽屉补录）"""
    import shutil
    dir_path = (body.get("dir") or "").strip().rstrip("/")
    if not dir_path or not os.path.isdir(dir_path):
        return _err("DIR_NOT_FOUND", f"目录不存在: {dir_path}")
    package = os.path.basename(dir_path)
    result = {"package": package, "pieces": {}, "warnings": [], "hint": None}

    # ① 基座镜像
    tar = os.path.join(dir_path, "image.tar")
    if os.path.exists(tar):
        out, code = _docker(f"load -i '{tar}'", 600)
        result["pieces"]["image"] = {"ok": code == 0, "detail": (out or "已导入")[:300]}
    else:
        # 镜像必须导入（用户定稿）: 缺 image.tar 的包不完整 → 判失败
        result["pieces"]["image"] = {"ok": False, "detail": "缺 image.tar——镜像必须导入，补齐后重新整包导入"}

    # ② 服务定义（整树拷贝, CORE ServiceManager 本就扫描目录内全部 .py）
    svc_src = os.path.join(dir_path, "services")
    py_files = [f for f in (os.listdir(svc_src) if os.path.isdir(svc_src) else []) if f.endswith(".py")]
    if py_files:
        dst = os.path.join(CUSTOM_SERVICES_DIR, package)
        shutil.copytree(svc_src, dst, dirs_exist_ok=True)
        open(os.path.join(dst, "__init__.py"), "w").close()
        result["pieces"]["services"] = {"ok": True, "detail": f"落位 custom_services/{package}/（{len(py_files)} 个 .py, 重启 core-daemon 生效）"}
        result["hint"] = "需重启 core-daemon 使服务注册生效（维护窗口）"
    else:
        result["pieces"]["services"] = {"ok": None, "detail": "缺 services/*.py——服务无法声明启动, 可后续单独导入"}

    # ③ 配方清单
    man_path = os.path.join(dir_path, "manifest.json")
    if os.path.exists(man_path):
        try:
            with open(man_path) as f:
                man = json.load(f)
            name = (man.get("name") or package).strip()
            man["name"] = name
            store = _load_manifests()
            store[name] = man
            _save_manifests(store)
            result["pieces"]["manifest"] = {"ok": True, "detail": f"已编目: {name}"}
            if name != package:
                result["warnings"].append(f"manifest.name({name}) 与包名({package})不一致——建议统一归属")
            if not (man.get("image") or {}).get("name"):
                result["warnings"].append("manifest 缺 image.name/tag 绑定——三件完整性无法关联到镜像")
        except json.JSONDecodeError as e:
            result["pieces"]["manifest"] = {"ok": False, "detail": f"manifest.json 解析失败: {e}"}
    else:
        result["pieces"]["manifest"] = {"ok": None, "detail": "缺 manifest.json——可在镜像详情抽屉补录"}

    _audit("bundle.import", package=package, dir=dir_path,
           image=result["pieces"]["image"]["ok"], services=result["pieces"]["services"]["ok"],
           manifest=result["pieces"]["manifest"]["ok"])
    return JSONResponse({"ok": True, "data": result})


@router.post("/targets/import/pieces")
async def targets_import_pieces(body: dict):
    """分开导入三件（导入对话框的"分开导入"模式）。
    body: {image_path?, package?, py?, templates?, manifest?} 至少提供一项; 未提供的件标记缺件"""
    result = {"pieces": {}, "warnings": [], "hint": None}
    provided = 0

    # 规则（用户定稿）: 镜像必须导入; 服务定义/清单可缺
    image_path = (body.get("image_path") or "").strip()
    if not image_path:
        return _err("IMAGE_REQUIRED", "镜像必须导入——分开导入也必须提供镜像 tar 路径")
    provided += 1
    if not os.path.exists(image_path):
        result["pieces"]["image"] = {"ok": False, "detail": f"文件不存在: {image_path}"}
    else:
        out, code = _docker(f"load -i '{image_path}'", 600)
        result["pieces"]["image"] = {"ok": code == 0, "detail": (out or "已导入")[:300]}

    package = (body.get("package") or "").strip().replace("/", "_").replace("..", "_")
    py = body.get("py") or ""
    manifest = body.get("manifest")
    manifest_name = ""
    if isinstance(manifest, dict) and manifest:
        manifest_name = (manifest.get("name") or package).strip() or package
    if package and py:
        provided += 1
        if manifest_name and manifest_name != package:
            return _err("NAME_MISMATCH", f"package({package}) 与 manifest.name({manifest_name}) 必须一致（归属锚点唯一）")
        # 前缀重写（B1）→ 服务名冲突拦截（B2）→ 落位
        py_rw = _rewrite_py_names(py, manifest_name or package)
        names = _extract_service_names(py_rw)
        conflicts = _svc_name_conflicts(package, names)
        if conflicts:
            det = ", ".join(f"{x['service']}(已在包 {x['owner_package']})" for x in conflicts)
            return _err("SERVICE_DUPLICATE", f"跨包同名服务冲突: {det}", status=409, conflicts=conflicts)
        if isinstance(manifest, dict):
            _rewrite_manifest_names(manifest, manifest_name or package)
        svc_dir = os.path.join(CUSTOM_SERVICES_DIR, package)
        os.makedirs(os.path.join(svc_dir, "templates"), exist_ok=True)
        with open(os.path.join(svc_dir, "service.py"), "w") as f:
            f.write(py_rw)
        for fn, fc in (body.get("templates") or {}).items():
            with open(os.path.join(svc_dir, "templates", os.path.basename(fn)), "w") as f:
                f.write(fc)
        open(os.path.join(svc_dir, "__init__.py"), "w").close()
        n_t = len(body.get("templates") or {})
        result["pieces"]["services"] = {"ok": True,
                                        "detail": f"落位 custom_services/{package}/（service.py + {n_t} 个模板, 重启 core-daemon 生效, 服务名已按清单前缀重写）"}
        result["hint"] = "需重启 core-daemon 使服务注册生效（维护窗口）"
    elif package or py:
        result["pieces"]["services"] = {"ok": False, "detail": "包名与 .py 内容必须同时提供"}
    else:
        result["pieces"]["services"] = {"ok": None, "detail": "未提供——可在详情抽屉补录"}

    manifest = body.get("manifest")
    if isinstance(manifest, dict) and manifest:
        provided += 1
        name = (manifest.get("name") or "").strip()
        if not name:
            result["pieces"]["manifest"] = {"ok": False, "detail": "manifest.name 必填"}
        else:
            store = _load_manifests()
            store[name] = manifest
            _save_manifests(store)
            result["pieces"]["manifest"] = {"ok": True, "detail": f"已编目: {name}"}
            if not (manifest.get("image") or {}).get("name"):
                result["warnings"].append("manifest 缺 image.name/tag 绑定——无法关联到镜像")
    else:
        result["pieces"]["manifest"] = {"ok": None, "detail": "未提供——可在详情抽屉补录"}

    if not provided:
        return _err("EMPTY_IMPORT", "至少提供服务定义或 manifest 之一（镜像 tar 为必填）")
    _audit("pieces.import", **{k: v["ok"] for k, v in result["pieces"].items()})
    return JSONResponse({"ok": True, "data": result})


@router.post("/targets/import/services")
async def targets_import_services(body: dict):
    """导入服务定义（.py + templates/）。body: {py, templates: {文件名: 内容}, package}"""
    py_content = body.get("py", "")
    templates = body.get("templates", {})  # {filename: content}
    if not py_content:
        return _err("EMPTY_PY", "缺少 .py 内容")
    pkg = (body.get("package") or "imported").strip().replace("/", "_").replace("..", "_")
    svc_dir = os.path.join(CUSTOM_SERVICES_DIR, pkg)
    os.makedirs(os.path.join(svc_dir, "templates"), exist_ok=True)
    with open(os.path.join(svc_dir, "service.py"), "w") as f:
        f.write(py_content)
    for fn, fc in templates.items():
        safe = os.path.basename(fn)
        with open(os.path.join(svc_dir, "templates", safe), "w") as f:
            f.write(fc)
    with open(os.path.join(svc_dir, "__init__.py"), "w") as f:
        f.write("")
    _audit("services.import", package=pkg, files=len(templates) + 1)
    return JSONResponse({"ok": True, "data": {"dir": svc_dir, "files": len(templates) + 1},
                         "hint": "需重启 core-daemon 使服务注册生效（维护窗口）"})


@router.post("/targets/import/manifest")
async def targets_import_manifest(body: dict):
    """导入/覆盖配方清单。body: {manifest: {...}}，manifest.name 必填"""
    manifest = body.get("manifest") or {}
    name = (manifest.get("name") or "").strip()
    if not name:
        return _err("NO_NAME", "manifest 缺少 name")
    store = _load_manifests()
    store[name] = manifest
    _save_manifests(store)
    _audit("manifest.save", name=name)
    return JSONResponse({"ok": True, "data": {"name": name}})


@router.get("/targets/manifest/{name}")
async def targets_get_manifest(name: str):
    store = _load_manifests()
    if name not in store:
        return _err("NOT_FOUND", f"未编目: {name}", status=404)
    return JSONResponse({"ok": True, "data": store[name]})


@router.post("/targets/manifest/{name}")
async def targets_edit_manifest(name: str, body: dict):
    """手动补录/编辑清单（详情抽屉 JSON 表单保存入口）"""
    manifest = body.get("manifest") if isinstance(body.get("manifest"), dict) else body
    manifest = dict(manifest)
    manifest.setdefault("name", name)
    store = _load_manifests()
    store[name] = manifest
    _save_manifests(store)
    _audit("manifest.edit", name=name)
    return JSONResponse({"ok": True, "data": manifest})


@router.post("/targets/image/detail")
async def targets_image_detail(body: dict):
    """靶标详情（设计 §8.2）：三件完整性 + manifest + docker inspect 摘要。body: {ref}"""
    ref = (body.get("ref") or "").strip()
    if not ref:
        return _err("NO_REF", "缺少 ref")
    mindex = _manifest_index()
    pc = _pieces(ref, mindex)
    out, code = _docker("inspect --format '{{json .}}' " + ref, 30)
    if code != 0:
        # 镜像必须导入（用户定稿）: 镜像不存在则条目不存在, 无"缺镜像可补"状态
        return _err("NOT_FOUND", f"镜像不存在: {ref}", status=404)
    info = {}
    try:
        raw = json.loads(out.splitlines()[0])
        cfg = raw.get("Config") or {}
        info = {
            "id": (raw.get("Id") or "").split(":")[-1][:12],
            "created": raw.get("Created"),
            "os_arch": f'{raw.get("Os", "?")}/{raw.get("Architecture", "?")}',
            "workdir": cfg.get("WorkingDir") or "/",
            "cmd": cfg.get("Cmd"),
            "entrypoint": cfg.get("Entrypoint"),
            "ports": sorted((cfg.get("ExposedPorts") or {}).keys()),
            "env_count": len(cfg.get("Env") or []),
            "labels": cfg.get("Labels") or {},
            "repo_tags": raw.get("RepoTags") or [],
        }
    except (json.JSONDecodeError, IndexError):
        return _err("INSPECT_FAILED", "docker inspect 输出解析失败", status=500)
    return JSONResponse({"ok": True, "data": {"ref": ref, "pieces": pc, "manifest": pc["manifest_data"],
                                              "service_pairs": _enrich_pairs(pc["service_pairs"]), "inspect": info}})


@router.post("/targets/image/delete")
async def targets_image_delete(body: dict):
    """删除镜像（先占用检查，设计 §3.1 IMAGE_CONFLICT）。body: {ref}"""
    ref = (body.get("ref") or "").strip()
    if not ref:
        return _err("NO_REF", "缺少 ref")
    out, _c = _docker("ps -a --filter ancestor='" + ref + "' --format '{{.Names}}'", 30)
    used = [x for x in out.splitlines() if x.strip()]
    if used:
        return _err("IMAGE_CONFLICT", f"镜像被容器引用，删除被拒: {', '.join(used[:5])}", status=409,
                    containers=used)
    out, code = _docker(f"rmi -f '{ref}'", 120)
    _audit("image.delete", ref=ref, ok=code == 0)
    if code != 0:
        return _err("RMI_FAILED", out or "docker rmi 失败", status=500)
    return JSONResponse({"ok": True, "data": {"ref": ref, "output": out}})


# ═══════════════════════════════════════════════
# Tab2 独立容器管理
# ═══════════════════════════════════════════════

def _ensure_standalone(name: str):
    """命名隔离硬规则（设计 §4.2）：Tab2 只允许操作 standalone- 前缀容器，
    物理上看不到/动不到 CORE 拓扑容器（防止误删正在运行的演练）。
    返回错误响应（拒绝）或 None（放行）"""
    if not name.startswith(STANDALONE_PREFIX):
        return _err("NOT_STANDALONE", f"仅允许操作 {STANDALONE_PREFIX}* 独立容器，{name} 不是独立容器", status=403)
    return None


@router.get("/containers/standalone")
async def standalone_containers_list():
    """独立容器列表（standalone- 前缀，普通隔离，验证用；CORE 拓扑容器不在此列）"""
    out, _code = _docker("ps -a --format '{{.Names}}\\t{{.Image}}\\t{{.Status}}' --filter name=standalone-", 30)
    items = []
    for ln in out.splitlines():
        parts = ln.split("\t")
        if len(parts) >= 3:
            items.append({"name": parts[0], "image": parts[1], "status": parts[2]})
    return JSONResponse({"ok": True, "data": items})


@router.post("/containers/standalone/run")
async def standalone_container_run(body: dict):
    """独立启动容器（普通隔离，验证用；不接 CORE 网络）。
    body: {image, name?, ports?: ["8080:80"], command?}
    command: 可选保活/初始命令——路线 B 镜像无 CMD, 独立验证需带 `tail -f /dev/null` 否则立即退出"""
    image = (body.get("image") or "").strip()
    if not image:
        return _err("NO_IMAGE", "缺少 image")
    name = (body.get("name") or f"standalone-{image.split('/')[-1].split(':')[0]}").strip()
    if not name.startswith(STANDALONE_PREFIX):
        name = STANDALONE_PREFIX + name
    ports = body.get("ports") or []
    port_args = " ".join(f"-p {p}" for p in ports)
    cmd_args = ""
    command = (body.get("command") or "").strip()
    if command:
        cmd_args = " " + command
    dup, _c = _docker("ps -a --format '{{.Names}}' --filter name=^" + name + "$", 30)
    if dup.strip():
        return _err("NAME_CONFLICT", f"容器名已存在: {name}（先删除或改名）", status=409)
    out, code = _docker(f"run -d --name {name} {port_args} {image}{cmd_args}", 120)
    _audit("container.run", name=name, image=image, ok=code == 0)
    if code != 0:
        return _err("RUN_FAILED", out, status=500)
    # 秒退检测: 无常驻进程的镜像（如 kali 官方镜像）不填保活命令会立即 Exited
    hint = None
    import time as _t
    _t.sleep(1.2)
    st, _c = _docker("inspect --format '{{.State.Running}} {{.State.ExitCode}}' " + name, 15)
    if st.startswith("false"):
        hint = ("容器已立即退出（Exited）——该镜像没有常驻进程。"
                "请删除后重新创建，并在「启动命令」填 tail -f /dev/null 保活")
    data = {"name": name, "image": image}
    if hint:
        data["hint"] = hint
    return JSONResponse({"ok": True, "data": data, **({"hint": hint} if hint else {})})


@router.post("/containers/standalone/{name}/stop")
async def standalone_container_stop(name: str):
    denied = _ensure_standalone(name)
    if denied:
        return denied
    out, code = _docker(f"stop {name}", 60)
    _audit("container.stop", name=name, ok=code == 0)
    return JSONResponse({"ok": code == 0, "data": {"name": name, "output": out}})


@router.post("/containers/standalone/{name}/start")
async def standalone_container_start(name: str):
    denied = _ensure_standalone(name)
    if denied:
        return denied
    out, code = _docker(f"start {name}", 60)
    _audit("container.start", name=name, ok=code == 0)
    return JSONResponse({"ok": code == 0, "data": {"name": name, "output": out}})


@router.delete("/containers/standalone/{name}")
async def standalone_container_delete(name: str):
    denied = _ensure_standalone(name)
    if denied:
        return denied
    out, code = _docker(f"rm -f {name}", 60)
    _audit("container.delete", name=name, ok=code == 0)
    return JSONResponse({"ok": code == 0, "data": {"name": name, "output": out}})


@router.post("/containers/standalone/{name}/exec")
async def standalone_container_exec(name: str, body: dict):
    """容器内执行命令（审计留痕）。body: {cmd}"""
    denied = _ensure_standalone(name)
    if denied:
        return denied
    cmd = (body.get("cmd") or "").strip()
    if not cmd:
        return _err("NO_CMD", "缺少 cmd")
    out, code = _docker(f"exec {name} {cmd}", 60)
    _audit("container.exec", name=name, cmd=cmd)
    return JSONResponse({"ok": code == 0, "data": {"output": out, "return_code": code}})


# ═══════════════════════════════════════════════
# Tab3 节点实例管理（设计 §5）
# 实例 = 模板：拓扑绑定实例名（引用），改实例所有引用拓扑下次部署生效；
# 被引用锁定（locked）；同软件 normal/vuln 互斥、跨软件可组合
# ═══════════════════════════════════════════════

def _available_services() -> dict:
    """可声明服务总表（清单驱动声明 §⑤ 前提）：
    drill = 自定义演练服务（custom_services 注册，成对 normal/vuln）
    system = CORE 内置基础服务（SSH/DefaultRoute/NAT…，netns 型常用）"""
    system = []
    try:
        from core.services.manager import ServiceManager
        mgr = ServiceManager()
        mgr.load_locals()
        if os.path.isdir(CUSTOM_SERVICES_DIR):
            mgr.load(os.path.abspath(CUSTOM_SERVICES_DIR))
        system = [{"name": n, "source": "drill" if n.startswith("svc-") else "system"}
                  for n in sorted(mgr.services.keys())]
    except Exception as e:
        logger.warning("service manager unavailable: %s", e)
    # 软件归属：优先 manifest（software 权威），其次 svc-<软件>-<CVE>-<mode> 命名解析
    manifest_of = {}
    for m in _load_manifests().values():
        if isinstance(m, dict):
            for s in m.get("services") or []:
                manifest_of[s.get("name")] = m.get("software", "")
    out_drill = []
    for s in system:
        if s["source"] != "drill":
            continue
        name = s["name"]
        mode = name.rsplit("-", 1)[-1] if name.rsplit("-", 1)[-1] in ("normal", "vuln") else None
        software = manifest_of.get(name) or (name.split("-")[1] if name.startswith("svc-") and len(name.split("-")) > 2 else name)
        out_drill.append({"name": name, "mode": mode, "software": software})
    out_system = [s["name"] for s in system if s["source"] == "system"]
    return {"drill": out_drill, "system": out_system}


def _mutex_conflict(services: list, available: dict) -> dict | None:
    """同软件 normal/vuln 互斥校验（设计 §5.3）；跨软件可组合。
    返回冲突对描述或 None"""
    by_sw: dict = {}
    for s in services:
        sw = next((d["software"] for d in available["drill"] if d["name"] == s.get("name")), None)
        if sw is None:
            continue
        by_sw.setdefault(sw, []).append(s.get("mode"))
    for sw, modes in by_sw.items():
        if "normal" in modes and "vuln" in modes:
            return {"software": sw,
                    "detail": f"软件 {sw} 的 normal 与 vuln 声明互斥（二选一）"}
    return None


def _validate_instance(inst: dict, available: dict) -> JSONResponse | None:
    """实例创建/更新公共校验。返回错误响应或 None。
    服务来源过滤（清单驱动声明 ⑤⁺ + Tab3 分组规则）:
    - netns 实例: 仅 system 基础服务（宿主 rootfs 可用）; drill 演练服务是容器靶标专属 → 拒绝
    - docker 实例: 镜像清单（manifest.services）内的条目 + CORE 内置服务；
      声明清单外的 drill 服务 → SERVICE_NOT_IN_MANIFEST（声明时拦截错配, 比预检更前置）"""
    name = (inst.get("name") or "").strip()
    if not name:
        return _err("NO_NAME", "实例缺少 name")
    rt = inst.get("runtime_type")
    if rt not in ("docker", "netns"):
        return _err("RUNTIME_INVALID", "runtime_type 仅允许 docker | netns")
    img = (inst.get("image_tag") or "").strip()
    if rt == "docker" and not img:
        return _err("IMAGE_REQUIRED", "docker 型实例必须绑定 image_tag")
    if rt == "netns" and img:
        return _err("NETNS_NO_IMAGE", "netns 型共享宿主 rootfs，不绑定镜像")
    drill_names = {d["name"] for d in available["drill"]}
    system_names = set(available["system"])
    # docker 实例的清单白名单 = 该镜像 manifest.services 声明的服务
    manifest_ok = None
    if rt == "docker" and img:
        mindex = _manifest_index()
        man = mindex.get(img) or mindex.get(img.rsplit(":", 1)[0])
        if man:
            manifest_ok = {s.get("name") for s in man.get("services") or []}
    for s in inst.get("services") or []:
        sn = s.get("name", "")
        if sn not in drill_names and sn not in system_names:
            return _err("SERVICE_UNKNOWN", f"服务未注册: {sn}（清单驱动声明，请从可用服务中选择）",
                        service=sn)
        if sn not in drill_names and s.get("mode"):
            return _err("MODE_INVALID", f"系统服务 {sn} 无 normal/vuln 之分", service=sn)
        if rt == "netns" and sn in drill_names:
            return _err("NETNS_DRILL_DENIED",
                        f"netns 节点不可声明演练服务 {sn}（演练服务为容器靶标专属；netns 仅基础服务）",
                        service=sn)
        if rt == "docker" and sn in drill_names and manifest_ok is not None and sn not in manifest_ok:
            return _err("SERVICE_NOT_IN_MANIFEST",
                        f"服务 {sn} 不在该镜像的清单声明中（清单如实声明原则 ⑤⁺——先编目再声明）",
                        service=sn)
    conflict = _mutex_conflict(inst.get("services") or [], available)
    if conflict:
        return _err("SERVICES_MUTEX", conflict["detail"], status=409, conflict=conflict)
    return None


def _instance_refs(name: str, topologies: dict) -> list:
    """实例的全部引用方 = 活跃部署引用（platform.db, 自动登记）+ 想定登记表（topologies.json, 手动）"""
    import platform_db
    refs = [f"{r['drill_id']}（会话{r['core_sid']}）" for r in platform_db.query_deploy_refs(name)]
    # topologies.json 值必须为 dict; 跳过注释/损坏条目
    refs += [t for t, td in topologies.items()
             if isinstance(td, dict) and name in (td.get("instances") or [])]
    return sorted(set(refs))


@router.get("/instances")
async def instances_list():
    """节点实例列表（含引用/锁定状态，设计 §5.2）"""
    instances = _load_store(INSTANCE_STORE)
    topologies = _load_store(TOPOLOGY_STORE)
    out = []
    for name, inst in instances.items():
        refs = _instance_refs(name, topologies)
        out.append({**inst, "name": name, "locked": bool(refs), "referenced_by": refs})
    return JSONResponse({"ok": True, "data": out})


@router.get("/instances/available-services")
async def instances_available_services():
    """可声明服务总表（drill 成对 + system 基础），实例编辑器的服务选择来源"""
    return JSONResponse({"ok": True, "data": _available_services()})


@router.post("/instances")
async def instances_create(body: dict):
    """创建节点实例。body: {name, runtime_type, image_tag?, services?, ports?, cve?, flags?}"""
    inst = dict(body)
    name = (inst.get("name") or "").strip()
    available = _available_services()
    err = _validate_instance(inst, available)
    if err:
        return err
    instances = _load_store(INSTANCE_STORE)
    if name in instances:
        return _err("INSTANCE_DUPLICATE", f"实例已存在: {name}", status=409)
    instances[name] = {k: v for k, v in inst.items() if k != "name"}
    _save_store(INSTANCE_STORE, instances)
    _audit("instance.create", name=name, runtime_type=inst.get("runtime_type"))
    return JSONResponse({"ok": True, "data": {"name": name}})


@router.post("/instances/{name}")
async def instances_update(name: str, body: dict):
    """更新实例（模板式：引用它的拓扑下次部署生效）。被引用锁定 → 409（设计 §5.2）"""
    instances = _load_store(INSTANCE_STORE)
    if name not in instances:
        return _err("NOT_FOUND", f"实例不存在: {name}", status=404)
    topologies = _load_store(TOPOLOGY_STORE)
    refs = _instance_refs(name, topologies)
    if refs:
        return _err("INSTANCE_LOCKED", f"被拓扑引用，禁止修改: {', '.join(refs)}（先解除引用，或复制实例）",
                    status=409, referenced_by=refs)
    inst = dict(body)
    inst["name"] = name  # 实例名不可改（引用锚点）
    err = _validate_instance(inst, _available_services())
    if err:
        return err
    instances[name] = {k: v for k, v in inst.items() if k != "name"}
    _save_store(INSTANCE_STORE, instances)
    _audit("instance.update", name=name)
    return JSONResponse({"ok": True, "data": {"name": name}})


@router.post("/instances/{name}/clone")
async def instances_clone(name: str, body: dict):
    """复制实例（被引用锁定的出路之二，设计 §5.2）。body: {name?: 新名}"""
    instances = _load_store(INSTANCE_STORE)
    if name not in instances:
        return _err("NOT_FOUND", f"实例不存在: {name}", status=404)
    base = name + "-copy"
    new = (body.get("name") or "").strip()
    if not new:
        i = 1
        new = base
        while new in instances:
            i += 1
            new = f"{base}{i}"
    if new in instances:
        return _err("INSTANCE_DUPLICATE", f"实例已存在: {new}", status=409)
    instances[new] = dict(instances[name])
    _save_store(INSTANCE_STORE, instances)
    _audit("instance.clone", name=name, new=new)
    return JSONResponse({"ok": True, "data": {"name": new}})


@router.delete("/instances/{name}")
async def instances_delete(name: str):
    """删除实例（被引用锁定 → 409）"""
    instances = _load_store(INSTANCE_STORE)
    if name not in instances:
        return _err("NOT_FOUND", f"实例不存在: {name}", status=404)
    topologies = _load_store(TOPOLOGY_STORE)
    refs = _instance_refs(name, topologies)
    if refs:
        return _err("INSTANCE_LOCKED", f"被拓扑引用，禁止删除: {', '.join(refs)}",
                    status=409, referenced_by=refs)
    del instances[name]
    _save_store(INSTANCE_STORE, instances)
    _audit("instance.delete", name=name)
    return JSONResponse({"ok": True, "data": {"name": name}})


@router.websocket("/ws/containers/standalone/{name}/terminal")
async def standalone_terminal_ws(websocket: WebSocket, name: str):
    """独立容器交互式终端。docker exec -it 挂真实 PTY，双向转发 WebSocket。

    用法（与 CORE 节点终端一致）:
      const ws = new WebSocket("ws://host:8080/ws/containers/standalone/standalone-dmz/terminal");
      ws.onmessage = (e) => console.log(e.data);   // 终端输出
      ws.send("ls /\n");                            // 发送命令
      ws.send("\\x03");                              // Ctrl+C
    """
    # 命名隔离：非 standalone 容器拒绝进入终端（设计 §4.2）
    if not name.startswith(STANDALONE_PREFIX):
        await websocket.accept()
        await websocket.send_json({"error": f"仅允许进入 {STANDALONE_PREFIX}* 独立容器终端"})
        await websocket.close(code=1008)
        return

    await websocket.accept()
    master_fd = None
    process = None
    loop = asyncio.get_running_loop()
    try:
        # 同 app.py 节点终端: docker exec 需要真实 PTY（-t 校验 + readline 行编辑），
        # 用 pty.openpty() 生成对端，exec 进程挂 slave，后端读写 master
        master_fd, slave_fd = pty.openpty()
        cmd = f"docker exec -i -t {name} env TERM=xterm bash"
        logger.info("standalone_terminal_ws: %s", cmd)
        process = await asyncio.create_subprocess_shell(
            cmd, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, start_new_session=True)
        os.close(slave_fd)
        os.set_blocking(master_fd, False)

        read_queue: asyncio.Queue = asyncio.Queue()

        def _on_readable():
            try:
                data = os.read(master_fd, 4096)
            except BlockingIOError:
                return
            except OSError:
                read_queue.put_nowait(None)
                return
            read_queue.put_nowait(data if data else None)

        loop.add_reader(master_fd, _on_readable)

        async def pipe_to_ws():
            try:
                while True:
                    data = await read_queue.get()
                    if data is None:
                        break
                    await websocket.send_text(data.decode(errors="replace"))
            except (WebSocketDisconnect, ConnectionResetError):
                pass
            except Exception:
                logger.exception("standalone terminal stdout pipe error")

        reader_task = asyncio.create_task(pipe_to_ws())
        try:
            while True:
                raw = await websocket.receive_text()
                if process.returncode is not None:
                    break
                if raw in ("\x03", "\x04"):
                    os.write(master_fd, raw.encode())
                else:
                    os.write(master_fd, raw.encode())
        except WebSocketDisconnect:
            pass
        finally:
            reader_task.cancel()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):
                pass
            try:
                loop.remove_reader(master_fd)
            except Exception:
                pass
            # exec + bash 整进程组 SIGKILL（同 vcmd 教训: 单杀父进程留孤儿）
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
            try:
                await process.wait()
            except Exception:
                pass
            try:
                os.close(master_fd)
            except Exception:
                pass
    except Exception as e:
        logger.exception("standalone_terminal_ws error")
        try:
            await websocket.send_json({"error": str(e)[:200]})
            await websocket.close(code=1011)
        except Exception:
            pass


# ═══════════════════════════════════════════════
# Tab5 数据监测（设计 §7）
# 三分组: CORE 会话容器 / 独立容器(不深监测) / 其他容器(列表可见不深监测)
# 宿主载荷: /proc 直读; CORE SSE 三流 bridge 已有, 前端直连透传
# ═══════════════════════════════════════════════

@router.get("/monitor/host")
async def monitor_host():
    """宿主载荷：CPU ticks(前端两次采样算 %) + loadavg + 内存 + 磁盘"""
    def _disk(path: str) -> dict | None:
        try:
            st = os.statvfs(path)
            total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
            return {"path": path, "total_gb": round(total / 2**30, 1),
                    "used_gb": round((total - free) / 2**30, 1),
                    "used_pct": round((total - free) / total * 100, 1) if total else 0}
        except OSError:
            return None

    cpu_ticks = {}
    try:
        with open("/proc/stat") as f:
            for ln in f:
                if ln.startswith("cpu "):
                    cpu_ticks["total"] = [int(x) for x in ln.split()[1:8]]
                    break
    except OSError:
        pass
    mem = {}
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for ln in f:
                parts = ln.split(":")
                if len(parts) == 2:
                    info[parts[0]] = int(parts[1].strip().split()[0])  # kB
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        mem = {"total_gb": round(total / 2**20, 2), "used_gb": round((total - avail) / 2**20, 2),
               "used_pct": round((total - avail) / total * 100, 1) if total else 0}
    except (OSError, ValueError):
        pass
    return JSONResponse({"ok": True, "data": {
        "cpu_ticks": cpu_ticks,
        "loadavg": [round(x, 2) for x in os.getloadavg()],
        "mem": mem,
        "disks": [d for d in (_disk("/"), _disk("/opt")) if d],
    }})


@router.get("/monitor/containers")
async def monitor_containers():
    """容器三分组视图（设计 §7.2）。CORE 会话容器按会话节点名匹配（DockerNode 容器名=节点名）"""
    # 全量容器列表
    out, _c = _docker("ps -a --format '{{.Names}}\\t{{.Image}}\\t{{.Status}}'", 30)
    containers = []
    for ln in out.splitlines():
        parts = ln.split("\t")
        if len(parts) >= 3:
            containers.append({"name": parts[0], "image": parts[1], "status": parts[2]})

    # 运行中容器 stats（独立容器按设计只列不深监测, 但 stats 一次拿到无妨——分组决定展示深度）
    stats = {}
    sout, _c = _docker("stats --no-stream --format '{{.Name}}\\t{{.CPUPerc}}\\t{{.MemUsage}}\\t{{.MemPerc}}\\t{{.NetIO}}'", 30)
    for ln in sout.splitlines():
        p = ln.split("\t")
        if len(p) >= 5:
            stats[p[0]] = {"cpu": p[1], "mem_usage": p[2], "mem_pct": p[3], "net_io": p[4]}

    # CORE 会话容器名集合（RUNTIME/DATACOLLECT 会话的 docker 型节点名）
    # 注意 wrappers 的 SessionState/NodeType 是普通 Enum, 与 int 比较恒 False, 统一取 .value
    core_names = set()
    core_drills: dict = {}  # 容器名 -> ["d-xxx（会话N）", ...]（多会话归属标注, 设计 §7.2）
    try:
        import platform_db
        import app as _app
        b = getattr(_app, "bridge", None)
        if b is not None:
            for s in b.get_sessions():
                st = getattr(s.state, "value", s.state)
                if st in (4, 5):
                    sess = b.get_session(s.id)
                    drill_id = platform_db.ensure_drill(s.id)
                    label = f"{drill_id}（会话{s.id}）"
                    # sess.nodes 是 {node_id: Node} 字典
                    for n in (sess.nodes.values() if isinstance(sess.nodes, dict) else sess.nodes):
                        nt = getattr(n.type, "value", n.type)
                        if nt == 15 or (getattr(n, "image", None) or ""):
                            core_names.add(n.name)
                            core_drills.setdefault(n.name, []).append(label)
    except Exception as e:
        logger.warning("core session lookup failed: %s", e)

    groups = {"core": [], "standalone": [], "others": []}
    for c in containers:
        c = dict(c)
        c["stats"] = stats.get(c["name"]) if c["status"].startswith("Up") else None
        if c["name"].startswith(STANDALONE_PREFIX):
            groups["standalone"].append(c)
        elif c["name"] in core_names:
            c["drills"] = sorted(set(core_drills.get(c["name"], [])))
            # docker stats NetIO 对 --net=none 容器恒为 0B → core 组改读容器内接口计数器（真实累计收发）
            if c["stats"] is not None:
                nb = _container_net_bytes(c["name"])
                if nb:
                    c["stats"]["net_io"] = f"RX {_fmt_bytes(nb[0])} / TX {_fmt_bytes(nb[1])}"
            groups["core"].append(c)
        else:
            groups["others"].append(c)
    return JSONResponse({"ok": True, "data": {"groups": groups}})


@router.get("/audit")
async def audit_list(action: str | None = None, session_id: int | None = None,
                     node_id: int | None = None, limit: int = 100):
    """审计查询（platform.db audit 表）：动作/会话/节点任意组合过滤"""
    import platform_db
    entries = platform_db.query_audit(action=action, session_id=session_id,
                                      node_id=node_id, limit=limit)
    # 兼容旧前端字段: ok/fail 从 detail 提取
    for e in entries:
        if isinstance(e.get("detail"), dict) and "ok" in e["detail"]:
            e["ok"] = e["detail"]["ok"]
    return JSONResponse({"ok": True, "data": {"total": len(entries), "entries": entries}})


@router.get("/drills")
async def drills_list(limit: int = 200):
    """演练身份列表（drill_id 为数据主键; CORE 会话号可回收, 仅作展示）"""
    import platform_db
    return JSONResponse({"ok": True, "data": platform_db.query_drills(limit=limit)})


@router.delete("/drills/{drill_id}/data")
async def drills_purge_data(drill_id: str):
    """手动清除某演练的 metrics/events（drills 行与 audit 保留; 自动保留策略见 platform_db 配置）"""
    import platform_db
    return JSONResponse({"ok": True, "data": platform_db.purge_drill_data(drill_id)})


@router.get("/metrics")
async def metrics_query(drill_id: str | None = None, node_id: int | None = None, limit: int = 500):
    """历史监测指标查询（采样线程仅 RUNTIME 会话期间 5s 一拍落库）"""
    import platform_db
    rows = platform_db.query_metrics(drill_id=drill_id, node_id=node_id, limit=limit)
    return JSONResponse({"ok": True, "data": {"total": len(rows), "entries": rows}})


@router.get("/events")
async def events_query(drill_id: str | None = None, limit: int = 200):
    """持久化会话事件查询（动作时间线复盘; 实时视图走 SSE /events/stream）"""
    import platform_db
    rows = platform_db.query_events(drill_id=drill_id, limit=limit)
    return JSONResponse({"ok": True, "data": {"total": len(rows), "entries": rows}})


# ═══════════════════════════════════════════════
# A1 服务/端口就绪度预检（设计 §8.1 环境与声明一致性检查）
# 原则: 一切检查项来自"声明"——节点声明的 CORE 服务（validate 命令）+ 编目清单端口。
# 平台不窥探镜像内容; 外源镜像自动降级到其声明范围（L1 存活 + L2 服务）。
# ═══════════════════════════════════════════════

def _service_validate_map() -> dict:
    """服务名 -> validate 命令列表（CORE 内置 + custom_services 统一加载）"""
    from core.services.manager import ServiceManager
    mgr = ServiceManager()
    mgr.load_locals()
    if os.path.isdir(CUSTOM_SERVICES_DIR):
        mgr.load(os.path.abspath(CUSTOM_SERVICES_DIR))
    return {name: list(getattr(cls, "validate", []) or []) for name, cls in mgr.services.items()}


def _manifest_ports_map() -> dict:
    """镜像 ref -> 编目端口列表（L3 仅编目镜像有）。ports 兼容 image.ports 与顶层两种写法"""
    out = {}
    for m in _load_manifests().values():
        if not isinstance(m, dict):
            continue
        img = m.get("image") or {}
        nm = img.get("name")
        if nm:
            ports = [str(p) for p in (img.get("ports") or m.get("ports") or [])]
            out[f"{nm}:{img.get('tag') or 'latest'}"] = ports
    return out


@router.post("/precheck")
async def precheck(session_id: int, wait: int = 8):
    """环境与声明一致性预检（RUNTIME 后、红方开打前）。
    L1 容器存活 / L2 声明服务的 validate 命令 / L3 编目端口探测。
    wait: 失败项宽限重试窗口（秒）——刚实例化时服务进程可能尚未完成端口绑定（慢机器实测踩过）。
    不回答漏洞是否可利用（无验证门禁原则, 设计 §1.3）。"""
    import platform_db
    import time as _t
    import app as _app
    b = getattr(_app, "bridge", None)
    if b is None:
        return _err("BRIDGE_UNAVAILABLE", "bridge 未连接", status=503)
    try:
        sess = b.get_session(session_id)
    except Exception as e:
        return _err("SESSION_NOT_FOUND", f"会话不存在: {e}", status=404)
    st = getattr(sess.state, "value", sess.state)
    if st not in (4, 5):
        return _err("NOT_RUNNING", f"会话未运行（state={st}）——节点实例化后预检才有意义", status=409)
    wait = max(0, min(int(wait), 60))

    drill_id = platform_db.ensure_drill(session_id)
    validate_map = _service_validate_map()
    ports_map = _manifest_ports_map()

    # L1: 容器状态（DockerNode 容器名=节点名）
    out, _c = _docker("ps --format '{{.Names}}\\t{{.Status}}'", 30)
    up_map = {}
    for ln in out.splitlines():
        p = ln.split("\t")
        if len(p) >= 2:
            up_map[p[0]] = p[1]

    nodes = sess.nodes.values() if isinstance(sess.nodes, dict) else sess.nodes
    report_nodes = []
    ready = True

    def _grace(fn, *args):
        """失败时在 wait 窗口内重试（刚实例化的服务可能尚未完成端口绑定）"""
        deadline = _t.time() + wait
        attempts = 0
        while True:
            ok, item = fn(*args)
            attempts += 1
            if ok or _t.time() >= deadline:
                return ok, item, attempts
            _t.sleep(1.5)

    for n in nodes:
        nt = getattr(n.type, "value", n.type)
        entry = {"node_id": n.id, "name": n.name,
                 "runtime": "docker" if nt == 15 else "netns",
                 "container": None, "services": [], "ports": [], "notes": []}
        if nt == 15:
            status = up_map.get(n.name)
            entry["container"] = status
            if not status:
                entry["notes"].append("容器未运行")
                ready = False
        else:
            entry["notes"].append("netns 节点（无容器实体, 仅校验服务）")

        # L2: 声明服务的 validate 命令（宽限重试）
        for svc in sorted(n.services or []):
            vcmds = validate_map.get(svc)
            if vcmds is None:
                entry["services"].append({"name": svc, "validate": None, "result": "unknown",
                                          "detail": "服务定义未注册（bridge 侧 ServiceManager 加载不到）"})
                ready = False
                continue
            if not vcmds:
                entry["services"].append({"name": svc, "validate": [], "result": "no-validate",
                                          "detail": "服务定义无校验命令, 跳过"})
                continue
            results = []
            ok_all = True
            for vcmd in vcmds:
                def run_validate(v=vcmd):
                    try:
                        rc, output = b.node_command(session_id, n.id, v)
                    except Exception as e:
                        return False, {"cmd": v, "result": "error", "detail": str(e)[:120]}
                    return rc == 0, {"cmd": v, "result": "ok" if rc == 0 else "missing",
                                     "detail": (output or "")[:120]}
                ok, item, _att = _grace(run_validate)
                results.append(item)
                if not ok:
                    ok_all = False
            entry["services"].append({"name": svc, "validate": vcmds,
                                      "result": "ok" if ok_all else "missing",
                                      "detail": results})
            if not ok_all:
                ready = False

        # L3: 编目端口探测（镜像有 manifest 才有；宽限重试）
        img = getattr(n, "image", None) or ""
        ports = ports_map.get(img) or ports_map.get(img.rsplit(":", 1)[0]) or []
        if not ports and (getattr(n, "image", None) or ""):
            entry["notes"].append("镜像未编目（无端口声明）: 仅完成存活/服务检查")
        for port in ports:
            probe = f'bash -c "echo > /dev/tcp/127.0.0.1/{port}"'

            def run_port(pp=port, pr=probe):
                try:
                    rc, output = b.node_command(session_id, n.id, pr)
                except Exception as e:
                    return False, {"port": pp, "result": "error", "detail": str(e)[:120]}
                return rc == 0, {"port": pp, "result": "open" if rc == 0 else "closed",
                                 "detail": (output or "")[:80]}
            ok, item, _att = _grace(run_port)
            entry["ports"].append(item)
            if not ok:
                ready = False
        report_nodes.append(entry)

    platform_db.write_audit("precheck", session_id=session_id, drill_id=drill_id,
                            verdict="READY" if ready else "NOT_READY")
    return JSONResponse({"ok": True, "data": {
        "drill_id": drill_id, "session_id": session_id,
        "verdict": "READY" if ready else "NOT_READY",
        "nodes": report_nodes}})


# ═══════════════════════════════════════════════
# 导入事务模型（P-导入1）：流式上传 + 草稿读取 + 重写注入 + 重名拦截
# 流程: 导入(镜像立即 load + 草稿载入弹窗) → 详情编辑 → 确定注册(写 custom_services/清单)
# ═══════════════════════════════════════════════

def _sanitize_rel(rel: str) -> str:
    """包内相对路径消毒: 逐段 basename; 含 .. / 空段 → 直接拒绝（不静默改写）"""
    parts = []
    for seg in rel.strip().strip("/").split("/"):
        seg = os.path.basename(seg.strip())
        if not seg or seg in (".", "..") or ".." in seg:
            raise ValueError(f"路径段非法: {seg!r}")
        parts.append(seg)
    if not parts:
        raise ValueError("rel_path 为空")
    rel2 = "/".join(parts)
    base = os.path.realpath(IMAGE_IMPORT_DIR)
    full = os.path.realpath(os.path.join(base, rel2))
    if full != base and not full.startswith(base + os.sep):
        raise ValueError("路径逃逸")
    return rel2


@router.post("/targets/import/upload")
async def targets_import_upload(request: Request, rel_path: str = ""):
    """页面直传（raw body 流式, 零新依赖）。query rel_path = 包内相对路径
    （如 web-tiny-bundle/image.tar 或 web-tiny-bundle/services/service.py）。
    落位 IMAGE_IMPORT_DIR/<rel_path>；上限 20GB；前端 fetch(url, {method:'POST', body: file})"""
    try:
        rel = _sanitize_rel(rel_path)
    except ValueError as e:
        return _err("BAD_PATH", str(e))
    target = os.path.join(os.path.realpath(IMAGE_IMPORT_DIR), rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    size = 0
    limit = 20 * 1024**3
    try:
        with open(target, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    f.close()
                    os.remove(target)
                    return _err("TOO_LARGE", f"上传超过 {limit // 2**30}GB 上限，已中止", status=413)
                f.write(chunk)
    except Exception as e:
        try:
            os.remove(target)
        except OSError:
            pass
        return _err("UPLOAD_FAILED", str(e)[:200], status=500)
    _audit("upload", rel_path=rel, size=size, ok=True)
    return JSONResponse({"ok": True, "data": {"path": target, "rel_path": rel, "size": size}})


@router.get("/targets/import/bundle/draft")
async def bundle_draft(dir: str = ""):
    """路径模式草稿读取：包目录已在服务器上时，读出 services/*.py 内容 + manifest.json
    供详情弹窗载入编辑（确认后走 bundle 导入落位）"""
    dir_path = (dir or "").strip().rstrip("/")
    if not dir_path or not os.path.isdir(dir_path):
        return _err("DIR_NOT_FOUND", f"目录不存在: {dir_path}")
    package = os.path.basename(dir_path)
    svc_dir = os.path.join(dir_path, "services")
    py_files = sorted(f for f in (os.listdir(svc_dir) if os.path.isdir(svc_dir) else []) if f.endswith(".py"))
    tpl_dir = os.path.join(svc_dir, "templates")
    tpl_files = sorted(f for f in (os.listdir(tpl_dir) if os.path.isdir(tpl_dir) else []) if not f.startswith("."))
    py_content = ""
    if py_files:
        try:
            with open(os.path.join(svc_dir, py_files[0]), encoding="utf-8", errors="ignore") as f:
                py_content = f.read()
        except OSError:
            pass
    manifest = None
    man_path = os.path.join(dir_path, "manifest.json")
    if os.path.exists(man_path):
        try:
            with open(man_path, encoding="utf-8") as f:
                manifest = json.load(f)
        except json.JSONDecodeError:
            manifest = None
    return JSONResponse({"ok": True, "data": {
        "package": package, "dir": dir_path,
        "draft": {"py": py_content, "manifest": manifest},
        "inventory": {"py": py_files, "templates": tpl_files,
                      "has_image_tar": os.path.exists(os.path.join(dir_path, "image.tar"))}}})


@router.post("/targets/import/bundle/confirm")
async def bundle_confirm(body: dict):
    """确认注册（草稿 → 落位）：dir 包目录必须已在服务器上（上传或路径模式均先落盘）。
    与 /targets/import/bundle 的差异 = 不读盘判断，直接执行落位（前端草稿已确认过内容）。"""
    dir_path = (body.get("dir") or "").strip().rstrip("/")
    if not dir_path or not os.path.isdir(dir_path):
        return _err("DIR_NOT_FOUND", f"目录不存在: {dir_path}")
    package = os.path.basename(dir_path)
    result = {"package": package, "pieces": {}, "warnings": [], "hint": None}

    # ① 镜像（必须件）
    tar = os.path.join(dir_path, "image.tar")
    if os.path.exists(tar):
        out, code = _docker(f"load -i '{tar}'", 600)
        result["pieces"]["image"] = {"ok": code == 0, "detail": (out or "已导入")[:300]}
    else:
        result["pieces"]["image"] = {"ok": False, "detail": "缺 image.tar——镜像必须导入"}

    # ② 服务定义（整树拷贝）+ 重写注入 + 跨包重名拦截
    svc_src = os.path.join(dir_path, "services")
    py_files = [f for f in (os.listdir(svc_src) if os.path.isdir(svc_src) else []) if f.endswith(".py")]
    manifest = body.get("manifest") or None
    manifest_name = ""
    if isinstance(manifest, dict) and manifest:
        manifest_name = (manifest.get("name") or package).strip()

    if py_files:
        # 前缀重写（B1）→ 跨包重名拦截（B2）→ 全部通过才落位
        rewritten = {}
        for fn in py_files:
            with open(os.path.join(svc_src, fn), encoding="utf-8", errors="ignore") as f:
                rewritten[fn] = _rewrite_py_names(f.read(), manifest_name or package)
        all_names = sum((_extract_service_names(t) for t in rewritten.values()), [])
        conflicts = _svc_name_conflicts(package, all_names)
        if conflicts:
            det = ", ".join(f"{c['service']}(已在包 {c['owner_package']})" for c in conflicts)
            return _err("SERVICE_DUPLICATE", f"跨包同名服务冲突（CORE 重名时先扫描者静默胜出，禁止跨包重名）: {det}",
                        status=409, conflicts=conflicts)
        import shutil
        dst = os.path.join(CUSTOM_SERVICES_DIR, package)
        os.makedirs(dst, exist_ok=True)
        shutil.copytree(svc_src, dst, dirs_exist_ok=True)
        for fn, txt in rewritten.items():
            with open(os.path.join(dst, fn), "w") as f:
                f.write(txt)
        open(os.path.join(dst, "__init__.py"), "w").close()
        result["pieces"]["services"] = {"ok": True, "detail": f"落位 custom_services/{package}/（{len(py_files)} 个 .py，服务名已按清单前缀重写）"}
        result["hint"] = "需重启 core-daemon 使服务注册生效（维护窗口）"
    else:
        result["pieces"]["services"] = {"ok": None, "detail": "缺 services/*.py——可在详情补录"}

    # ③ manifest（services 名与 .py 重写同步）
    if isinstance(manifest, dict) and manifest:
        name = (manifest.get("name") or package).strip()
        manifest["name"] = name
        _rewrite_manifest_names(manifest, name)
        store = _load_manifests()
        store[name] = manifest
        _save_manifests(store)
        result["pieces"]["manifest"] = {"ok": True, "detail": f"已编目: {name}"}
    else:
        result["pieces"]["manifest"] = {"ok": None, "detail": "缺 manifest——可在详情补录"}

    _audit("bundle.confirm", package=package, dir=dir_path,
           image=result["pieces"]["image"]["ok"], services=result["pieces"]["services"]["ok"],
           manifest=result["pieces"]["manifest"]["ok"])
    return JSONResponse({"ok": True, "data": result})


# ═══ 服务名前缀重写 + 跨包重名拦截（B1+B2 定稿）═══

def _sanitize_manifest_prefix(name: str) -> str:
    """清单名 → 服务名前缀片段（小写化, 非法字符转 -）"""
    import re as _re
    return _re.sub(r"[^a-z0-9-]+", "-", (name or "").lower()).strip("-") or "pkg"


def _prefixed_service_name(name: str, manifest_name: str) -> str:
    """name → svc-<清单名>-<原名去svc->；已带本清单前缀则原样（重复导入幂等）"""
    mn = _sanitize_manifest_prefix(manifest_name)
    if name.startswith(f"svc-{mn}-"):
        return name
    base = name[4:] if name.startswith("svc-") else name
    return f"svc-{mn}-{base}"


def _rewrite_py_names(py_text: str, manifest_name: str) -> str:
    """重写 .py 内全部服务注册名（name: str = "xxx" → 加清单前缀）"""
    import re as _re
    mn = _sanitize_manifest_prefix(manifest_name)
    if not mn:
        return py_text

    def _sub(m):
        return f'name: str = "{_prefixed_service_name(m.group(1), mn)}"'
    return _re.sub(r'name:\s*str\s*=\s*"([^"]+)"', _sub, py_text)


def _rewrite_manifest_names(manifest: dict, manifest_name: str) -> dict:
    for s in manifest.get("services") or []:
        s["name"] = _prefixed_service_name(s.get("name", ""), manifest_name)
    return manifest


def _svc_name_conflicts(package: str, names: list) -> list:
    """服务名冲突（重名时 CORE 先扫描者静默胜出——必须在导入时拦截, 实测 2026-11-15）：
    跨包同名（owner≠package）→ 归属混乱；同包内重复定义（owner=package）→ 打包错误。都拒绝"""
    seen_in_submission = set()
    conflicts = []
    for name in names:
        if name in seen_in_submission:
            conflicts.append({"service": name, "owner_package": package, "reason": "本次提交内重复定义"})
        seen_in_submission.add(name)
        owner = _service_name_owner(name)
        if owner and owner != package:
            conflicts.append({"service": name, "owner_package": owner, "reason": "已被其它包定义"})
    return conflicts


def _extract_service_names(py_text: str) -> list:
    import re as _re
    return _re.findall(r'name:\s*str\s*=\s*"([^"]+)"', py_text or "")


def _service_name_owner(name: str) -> str | None:
    """服务名当前定义在哪个包目录（custom_services/<包>/*.py）"""
    if not os.path.isdir(CUSTOM_SERVICES_DIR):
        return None
    needle = f'name: str = "{name}"'
    for pkg in sorted(os.listdir(CUSTOM_SERVICES_DIR)):
        pkg_dir = os.path.join(CUSTOM_SERVICES_DIR, pkg)
        if not os.path.isdir(pkg_dir):
            continue
        for root, _dirs, files in os.walk(pkg_dir):
            for fn in sorted(files):
                if fn.endswith(".py"):
                    try:
                        with open(os.path.join(root, fn), encoding="utf-8", errors="ignore") as f:
                            if needle in f.read():
                                return pkg
                    except OSError:
                        continue
    return None


@router.post("/targets/import/bundle/upload")
async def bundle_upload(request: Request, name: str = ""):
    """整体导入-本机上传：上传整包压缩包（.tar.gz，内含 image.tar/services/manifest.json），
    服务端解包落位 IMAGE_IMPORT_DIR/<包名>/ 并返回清单草稿。确认注册走 /targets/import/bundle/confirm"""
    import re as _re
    pkg = _re.sub(r"[^a-zA-Z0-9_-]+", "-", (name or "").strip()).strip("-")
    if not pkg:
        return _err("BAD_NAME", "包名非法（仅允许字母数字-_）")
    target_dir = os.path.join(os.path.realpath(IMAGE_IMPORT_DIR), pkg)
    os.makedirs(target_dir, exist_ok=True)
    archive = os.path.join(target_dir, "bundle.tar.gz")
    size = 0
    limit = 20 * 1024**3
    try:
        with open(archive, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    f.close()
                    os.remove(archive)
                    return _err("TOO_LARGE", f"超过 {limit // 2**30}GB 上限", status=413)
                f.write(chunk)
    except Exception as e:
        return _err("UPLOAD_FAILED", str(e)[:200], status=500)
    # 解包（--no-same-owner; GNU tar 拒绝绝对路径成员; 解包后校验无逃逸）
    rc = subprocess.run(f"tar -xzf '{archive}' -C '{target_dir}' --no-same-owner 2>&1",
                        shell=True, capture_output=True, text=True)
    if rc.returncode != 0:
        os.remove(archive)
        return _err("EXTRACT_FAILED", (rc.stdout + rc.stderr)[:300], status=400)
    # 归一化: 若解包出唯一顶层目录则上移一层
    entries = [e for e in os.listdir(target_dir) if e != "bundle.tar.gz"]
    if len(entries) == 1 and os.path.isdir(os.path.join(target_dir, entries[0])):
        inner = os.path.join(target_dir, entries[0])
        for e in os.listdir(inner):
            os.rename(os.path.join(inner, e), os.path.join(target_dir, e))
        os.rmdir(inner)
    # 逃逸校验
    for root, _dirs, files in os.walk(target_dir):
        for fn in files:
            fp = os.path.realpath(os.path.join(root, fn))
            if not fp.startswith(os.path.realpath(target_dir)):
                return _err("EXTRACT_UNSAFE", "压缩包含越界路径，已中止", status=400)
    os.remove(archive)
    inv = {
        "has_image_tar": os.path.exists(os.path.join(target_dir, "image.tar")),
        "py": sorted(f for f in os.listdir(os.path.join(target_dir, "services"))
                     if f.endswith(".py")) if os.path.isdir(os.path.join(target_dir, "services")) else [],
        "has_manifest": os.path.exists(os.path.join(target_dir, "manifest.json")),
    }
    _audit("bundle.upload", package=pkg, size=size, ok=True)
    return JSONResponse({"ok": True, "data": {"package": pkg, "dir": target_dir, "inventory": inv}})
