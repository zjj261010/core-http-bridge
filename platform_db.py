"""
平台数据库层（SQLite, 设计 §7.2 数据留存 + drill_id 演练身份）

背景: CORE 会话 id 可回收（daemon 重启计数器归零/会话删除后计数器只增但重启归零）,
裸 session_id 当数据主键会让两场演练的数据混桶。解法: 平台签发 drill_id（日期+短随机,
全局唯一）作为数据主键, CORE 会话号降级为普通属性。

表:
- drills:  演练身份登记（创建签发 / 删除 closed / 会话异常消失 interrupted / 同号新会话开新行）
- audit:   审计（平台操作 + 节点终端命令）, JSONL 同步双写一个版本期
- events:  CORE 会话事件（拓扑层动作时间线持久化, 采样线程独立订阅, 与前端 SSE 互不影响）
- metrics: 监测指标（仅 RUNTIME 会话期间 5s 一拍, docker 型节点）

约束: 离线部署（标准库 sqlite3 零新依赖）; 单写者多线程（每次操作独立连接, WAL 模式）。
"""
import json
import logging
import os
import sqlite3
import subprocess
import threading
import time

DB_PATH = "/opt/artifacts/core-drill/platform.db"
logger = logging.getLogger("core.platform_db")

# 保留策略（天）：audit 永久（量小, 追溯凭证）; metrics/events 按演练价值设限。
# 0 = 不清理。周期清理挂在采样线程（每小时一次）
METRICS_RETENTION_DAYS = 30
EVENTS_RETENTION_DAYS = 90
AUDIT_RETENTION_DAYS = 0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS drills(
  drill_id   TEXT PRIMARY KEY,
  core_sid   INTEGER,
  name       TEXT,
  created_at TEXT,
  closed_at  TEXT,
  status     TEXT DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS audit(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT,
  action     TEXT,
  session_id INTEGER,
  node_id    INTEGER,
  target     TEXT,
  detail     TEXT
);
CREATE TABLE IF NOT EXISTS events(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT,
  drill_id   TEXT,
  core_sid   INTEGER,
  type       TEXT,
  action     TEXT,
  detail     TEXT
);
CREATE TABLE IF NOT EXISTS metrics(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT,
  drill_id   TEXT,
  core_sid   INTEGER,
  node_id    INTEGER,
  name       TEXT,
  cpu        TEXT,
  mem_usage  TEXT,
  mem_pct    TEXT,
  net_io     TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_action  ON audit(action);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit(session_id);
CREATE INDEX IF NOT EXISTS idx_events_drill  ON events(drill_id, ts);
CREATE INDEX IF NOT EXISTS idx_metrics_drill ON metrics(drill_id, ts);
CREATE TABLE IF NOT EXISTS deploy_refs(
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  drill_id      TEXT,
  core_sid      INTEGER,
  node_id       INTEGER,
  instance_name TEXT,
  created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_deploy_refs_instance ON deploy_refs(instance_name);
"""

_init_lock = threading.Lock()
_initialized = False


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        c = _conn()
        c.executescript(_SCHEMA)
        c.commit()
        c.close()
        _initialized = True
        logger.info("platform db ready: %s", DB_PATH)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _new_drill_id() -> str:
    return f"d-{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}"


# ═══════════════════════════════════════════════
# 演练身份（drills）
# ═══════════════════════════════════════════════

def issue_drill(core_sid: int, name: str | None = None) -> str:
    """签发演练身份。会话创建时调用; 旁路创建/同号新会话由 ensure_drill 惰性调用"""
    init()
    drill_id = _new_drill_id()
    c = _conn()
    c.execute("INSERT INTO drills(drill_id, core_sid, name, created_at, status) VALUES (?,?,?,?, 'active')",
              (drill_id, core_sid, name, _now()))
    c.commit()
    c.close()
    logger.info("drill issued: %s (core_sid=%s)", drill_id, core_sid)
    return drill_id


def get_active_drill(core_sid: int) -> str | None:
    init()
    c = _conn()
    row = c.execute(
        "SELECT drill_id FROM drills WHERE core_sid=? AND status='active' ORDER BY created_at DESC LIMIT 1",
        (core_sid,)).fetchone()
    c.close()
    return row["drill_id"] if row else None


def ensure_drill(core_sid: int) -> str:
    """惰性登记: 有 active 用 active（stop→restart 不换身份）; 没有则签发新身份
    （覆盖: 旁路创建的会话 / daemon 重启后同号新会话——旧行已 interrupted, 新行新身份）"""
    d = get_active_drill(core_sid)
    if d:
        return d
    return issue_drill(core_sid)


def close_drill(core_sid: int, status: str = "closed") -> None:
    """关闭演练身份。delete → closed; reconciler 发现会话消失 → interrupted"""
    init()
    c = _conn()
    c.execute("UPDATE drills SET status=?, closed_at=? WHERE core_sid=? AND status='active'",
              (status, _now(), core_sid))
    c.commit()
    c.close()


# ═══════════════════════════════════════════════
# 审计（audit）
# ═══════════════════════════════════════════════

def write_audit(action: str, **kw) -> None:
    """审计入库: session_id/node_id 提取成列, 对象名进 target, 其余进 detail(JSON)"""
    init()
    session_id = kw.pop("session_id", None)
    node_id = kw.pop("node_id", None)
    target = None
    for k in ("target", "name", "ref", "path", "package", "node"):
        if kw.get(k) is not None:
            target = kw.pop(k)
            break
    c = _conn()
    c.execute("INSERT INTO audit(ts, action, session_id, node_id, target, detail) VALUES (?,?,?,?,?,?)",
              (_now(), action, session_id, node_id,
               str(target) if target is not None else None,
               json.dumps(kw, ensure_ascii=False, default=str)))
    c.commit()
    c.close()


def query_audit(action: str | None = None, session_id: int | None = None,
                node_id: int | None = None, limit: int = 100) -> list[dict]:
    init()
    sql = "SELECT ts, action, session_id, node_id, target, detail FROM audit WHERE 1=1"
    args: list = []
    if action:
        sql += " AND action=?"
        args.append(action)
    if session_id is not None:
        sql += " AND session_id=?"
        args.append(session_id)
    if node_id is not None:
        sql += " AND node_id=?"
        args.append(node_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    c = _conn()
    rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    c.close()
    for r in rows:
        try:
            r["detail"] = json.loads(r["detail"]) if r["detail"] else {}
        except json.JSONDecodeError:
            pass
    return rows


# ═══════════════════════════════════════════════
# CORE 事件（events）
# ═══════════════════════════════════════════════

def event_to_dict(event) -> dict:
    """wrappers.Event → 结构化详情（node/link/alert/session）"""
    d: dict = {"session_id": getattr(event, "session_id", None)}
    if getattr(event, "source", None):
        d["source"] = event.source
    if getattr(event, "node_event", None):
        ne = event.node_event
        n = ne.node
        d["type"] = "node"
        d["action"] = getattr(ne.message_type, "name", str(ne.message_type)).lower()
        d["node"] = {"id": n.id, "name": n.name,
                     "type": getattr(getattr(n, "type", None), "value", None)}
    elif getattr(event, "link_event", None):
        le = event.link_event
        l = le.link
        d["type"] = "link"
        d["action"] = getattr(le.message_type, "name", str(le.message_type)).lower()
        d["link"] = {"node1_id": l.node1_id, "node2_id": l.node2_id}
    elif getattr(event, "alert_event", None):
        ae = event.alert_event
        d["type"] = "alert"
        d["node_id"] = ae.node_id
        d["level"] = getattr(ae.level, "name", str(ae.level)).lower()
        d["source"] = ae.source
        d["text"] = (ae.text or "")[:300]
    elif getattr(event, "session_event", None):
        se = event.session_event
        d["type"] = "session"
        d["name"] = se.name
        d["data"] = (se.data or "")[:200]
    else:
        d["type"] = "unknown"
    return d


def record_event(core_sid: int, detail: dict) -> None:
    init()
    drill_id = ensure_drill(core_sid)
    c = _conn()
    c.execute("INSERT INTO events(ts, drill_id, core_sid, type, action, detail) VALUES (?,?,?,?,?,?)",
              (_now(), drill_id, core_sid, detail.get("type"), detail.get("action"),
               json.dumps(detail, ensure_ascii=False, default=str)))
    c.commit()
    c.close()


def query_events(drill_id: str | None = None, limit: int = 200) -> list[dict]:
    init()
    sql = "SELECT ts, drill_id, core_sid, type, action, detail FROM events WHERE 1=1"
    args: list = []
    if drill_id:
        sql += " AND drill_id=?"
        args.append(drill_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    c = _conn()
    rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    c.close()
    for r in rows:
        try:
            r["detail"] = json.loads(r["detail"]) if r["detail"] else {}
        except json.JSONDecodeError:
            pass
    return rows


# ═══════════════════════════════════════════════
# 部署引用（deploy_refs）: 实例锁定扳机（设计 §5.2 方向1）
# 部署(带 instance 建节点) → 自动登记; 会话删除/异常消失 → 自动解除
# ═══════════════════════════════════════════════

def register_deploy_ref(drill_id: str, core_sid: int, node_id: int, instance_name: str) -> None:
    init()
    c = _conn()
    c.execute("INSERT INTO deploy_refs(drill_id, core_sid, node_id, instance_name, created_at) VALUES (?,?,?,?,?)",
              (drill_id, core_sid, node_id, instance_name, _now()))
    c.commit()
    c.close()


def release_deploy_refs(core_sid: int | None = None, drill_id: str | None = None) -> int:
    """解除引用（会话删除时按 core_sid; daemon 异常时按 drill_id）"""
    init()
    c = _conn()
    if core_sid is not None:
        cur = c.execute("DELETE FROM deploy_refs WHERE core_sid=?", (core_sid,))
    else:
        cur = c.execute("DELETE FROM deploy_refs WHERE drill_id=?", (drill_id,))
    n = cur.rowcount
    c.commit()
    c.close()
    return n


def release_deploy_ref_node(core_sid: int, node_id: int) -> int:
    """节点级解除：会话内删除单个节点时释放它的引用（同实例还有其它节点则继续锁定）"""
    init()
    c = _conn()
    cur = c.execute("DELETE FROM deploy_refs WHERE core_sid=? AND node_id=?", (core_sid, node_id))
    n = cur.rowcount
    c.commit()
    c.close()
    return n


def query_deploy_refs(instance_name: str) -> list[dict]:
    init()
    c = _conn()
    rows = [dict(r) for r in c.execute(
        "SELECT drill_id, core_sid, node_id FROM deploy_refs WHERE instance_name=?", (instance_name,)).fetchall()]
    c.close()
    return rows


# ═══════════════════════════════════════════════
# 监测指标（metrics）
# ═══════════════════════════════════════════════

def insert_metrics(drill_id: str, core_sid: int, rows: list[dict]) -> None:
    if not rows:
        return
    init()
    c = _conn()
    c.executemany(
        "INSERT INTO metrics(ts, drill_id, core_sid, node_id, name, cpu, mem_usage, mem_pct, net_io) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [(_now(), drill_id, core_sid, r["node_id"], r["name"],
          r["cpu"], r["mem_usage"], r["mem_pct"], r["net_io"]) for r in rows])
    c.commit()
    c.close()


def query_metrics(drill_id: str | None = None, node_id: int | None = None, limit: int = 500) -> list[dict]:
    init()
    sql = "SELECT ts, drill_id, core_sid, node_id, name, cpu, mem_usage, mem_pct, net_io FROM metrics WHERE 1=1"
    args: list = []
    if drill_id:
        sql += " AND drill_id=?"
        args.append(drill_id)
    if node_id is not None:
        sql += " AND node_id=?"
        args.append(node_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    c = _conn()
    rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    c.close()
    return rows


def query_drills(limit: int = 200) -> list[dict]:
    init()
    c = _conn()
    rows = [dict(r) for r in c.execute(
        "SELECT drill_id, core_sid, name, created_at, closed_at, status FROM drills "
        "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()]
    c.close()
    return rows


# ═══════════════════════════════════════════════
# 保留策略（自动周期清理 + 手动清除）
# ═══════════════════════════════════════════════

def cleanup_old(metrics_days: int = METRICS_RETENTION_DAYS,
                events_days: int = EVENTS_RETENTION_DAYS,
                audit_days: int = AUDIT_RETENTION_DAYS) -> dict:
    """按 ts 删除超期数据（ts 为 'YYYY-MM-DD HH:MM:SS', 字符串比较即时间序）"""
    init()
    deleted = {}
    c = _conn()
    for table, days in (("metrics", metrics_days), ("events", events_days), ("audit", audit_days)):
        if not days:
            continue
        cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))
        cur = c.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
        deleted[table] = cur.rowcount
    c.commit()
    c.close()
    if any(deleted.values()):
        logger.info("retention cleanup: %s", deleted)
    return deleted


def purge_drill_data(drill_id: str) -> dict:
    """手动清除某演练的 metrics/events（drills 行保留作历史索引; audit 不动）"""
    init()
    c = _conn()
    m = c.execute("DELETE FROM metrics WHERE drill_id=?", (drill_id,)).rowcount
    e = c.execute("DELETE FROM events WHERE drill_id=?", (drill_id,)).rowcount
    c.commit()
    c.close()
    logger.info("purge drill data: %s metrics=%s events=%s", drill_id, m, e)
    return {"metrics": m, "events": e}


# ═══════════════════════════════════════════════
# 采样线程: reconciler（drill 生命周期）+ 事件订阅 + 指标采样
# ═══════════════════════════════════════════════

_sampler_started = False
_event_subs: dict = {}  # core_sid -> gRPC events stream
_last_cleanup: float = 0.0


def _maybe_cleanup() -> None:
    """每小时跑一次保留策略清理"""
    global _last_cleanup
    if time.time() - _last_cleanup < 3600:
        return
    _last_cleanup = time.time()
    try:
        cleanup_old()
    except Exception:
        logger.exception("retention cleanup failed")


def subscribe_events(core_sid: int) -> None:
    """会话事件订阅（幂等）。创建会话时同步调用——避免等 5s tick 导致头几秒动作漏录"""
    if core_sid in _event_subs:
        return
    try:
        import app as _app
        b = getattr(_app, "bridge", None)
        if b is None:
            return

        def handler(event, _sid=core_sid):
            try:
                record_event(_sid, event_to_dict(event))
            except Exception:
                pass

        _event_subs[core_sid] = b.events(core_sid, handler=handler)
        logger.info("events subscribed: core_sid=%s", core_sid)
    except Exception as e:
        logger.warning("events subscribe failed sid=%s: %s", core_sid, e)


def unsubscribe_events(core_sid: int) -> None:
    stream = _event_subs.pop(core_sid, None)
    if stream is not None:
        try:
            stream.cancel()
        except Exception:
            pass


def start_sampler() -> None:
    global _sampler_started
    if _sampler_started:
        return
    _sampler_started = True
    threading.Thread(target=_sampler_loop, name="platform-db-sampler", daemon=True).start()


def _sampler_loop() -> None:
    while True:
        try:
            _tick()
            _maybe_cleanup()
        except Exception:
            logger.exception("sampler tick failed")
        time.sleep(5)


def _tick() -> None:
    import app as _app
    b = getattr(_app, "bridge", None)
    if b is None:
        return
    sessions = {}
    try:
        sessions = {s.id: s for s in b.get_sessions()}
    except Exception as e:
        logger.warning("get_sessions failed: %s", e)
        return

    # 1) reconciler: 会话消失的 active drill → interrupted（daemon 重启/被杀）
    c = _conn()
    actives = c.execute("SELECT drill_id, core_sid FROM drills WHERE status='active'").fetchall()
    c.close()
    for r in actives:
        if r["core_sid"] not in sessions:
            close_drill(r["core_sid"], status="interrupted")
            release_deploy_refs(core_sid=r["core_sid"])  # 会话消失 → 部署引用解除(实例解锁)

    # 2) 旁路创建的会话补登记
    for sid in sessions:
        if get_active_drill(sid) is None:
            issue_drill(sid)

    # 3) 事件订阅兜底（创建时已同步订阅; 这里只补漏, 数据只从这里落库）
    for sid in sessions:
        subscribe_events(sid)
    for sid in list(_event_subs):
        if sid not in sessions:
            unsubscribe_events(sid)

    # 4) 指标采样（仅 RUNTIME/DATACOLLECT 会话的 docker 型节点）
    runtime = {sid: s for sid, s in sessions.items()
               if getattr(s.state, "value", s.state) in (4, 5)}
    if not runtime:
        return
    stats = _docker_stats_map()
    if not stats:
        return
    for sid, s in runtime.items():
        try:
            sess = b.get_session(sid)
        except Exception:
            continue
        nodes = sess.nodes.values() if isinstance(sess.nodes, dict) else sess.nodes
        rows = []
        for n in nodes:
            if getattr(n.type, "value", n.type) != 15:
                continue
            stx = stats.get(n.name)
            if not stx:
                continue
            rows.append({"node_id": n.id, "name": n.name, **stx})
        if rows:
            insert_metrics(ensure_drill(sid), sid, rows)


def _docker_stats_map() -> dict:
    """docker stats 一次性快照: {容器名: {cpu, mem_usage, mem_pct, net_io}}"""
    try:
        r = subprocess.run(
            "docker stats --no-stream --format '{{.Name}}\\t{{.CPUPerc}}\\t{{.MemUsage}}\\t{{.MemPerc}}\\t{{.NetIO}}'",
            shell=True, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return {}
    out = {}
    for ln in (r.stdout or "").splitlines():
        p = ln.split("\t")
        if len(p) >= 5:
            out[p[0]] = {"cpu": p[1], "mem_usage": p[2], "mem_pct": p[3], "net_io": p[4]}
    return out
