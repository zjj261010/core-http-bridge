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
from settings import settings
import database

DB_PATH = str(settings.db_path)
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
    return database.connect(DB_PATH)


def init() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        c = _conn()
        c.executescript(_SCHEMA)
        if 'identity' not in {r[1] for r in c.execute('PRAGMA table_info(drills)')}:
            c.execute('ALTER TABLE drills ADD COLUMN identity TEXT')
        if 'drill_id' not in {r[1] for r in c.execute('PRAGMA table_info(audit)')}:
            c.execute('ALTER TABLE audit ADD COLUMN drill_id TEXT')
        _migrate_constraints(c)
        c.commit()
        c.close()
        _initialized = True
        logger.info("platform db ready: %s", DB_PATH)


def _migrate_constraints(c):
    c.execute('CREATE TABLE IF NOT EXISTS platform_migration_issues(id INTEGER PRIMARY KEY,entity TEXT,detail TEXT)')
    for r in c.execute("SELECT * FROM drills WHERE status='active' AND rowid NOT IN (SELECT MAX(rowid) FROM drills WHERE status='active' GROUP BY core_sid)").fetchall():
        c.execute('INSERT INTO platform_migration_issues(entity,detail) VALUES(?,?)',('duplicate-active-drill',json.dumps(dict(r))))
        c.execute("UPDATE drills SET status='interrupted',closed_at=? WHERE drill_id=?",(time.strftime('%Y-%m-%d %H:%M:%S'),r['drill_id']))
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_drill_per_sid ON drills(core_sid) WHERE status='active'")
    columns={r[1] for r in c.execute('PRAGMA table_info(deploy_refs)')}
    if 'instance_version' not in columns: c.execute('ALTER TABLE deploy_refs ADD COLUMN instance_version INTEGER')
    # Preserve ambiguous legacy rows in diagnostics before removing duplicate live references.
    bad=c.execute("SELECT * FROM deploy_refs WHERE id NOT IN (SELECT MAX(id) FROM deploy_refs GROUP BY core_sid,node_id) OR drill_id NOT IN (SELECT drill_id FROM drills WHERE status='active')").fetchall()
    for r in bad:
        c.execute('INSERT INTO platform_migration_issues(entity,detail) VALUES(?,?)',('legacy-deploy-ref',json.dumps(dict(r))))
        c.execute('DELETE FROM deploy_refs WHERE id=?',(r['id'],))
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS one_ref_per_live_node ON deploy_refs(core_sid,node_id)')
    c.execute('CREATE TABLE IF NOT EXISTS revisions(kind TEXT,name TEXT,version INTEGER,body TEXT NOT NULL,created_at TEXT,PRIMARY KEY(kind,name,version))')
    checks="""SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM drills WHERE drill_id=NEW.drill_id AND core_sid=NEW.core_sid AND status='active') THEN RAISE(ABORT,'reference requires active drill') END;
SELECT CASE WHEN NEW.instance_version IS NULL OR NOT EXISTS(SELECT 1 FROM revisions WHERE kind='template' AND name=NEW.instance_name AND version=NEW.instance_version) THEN RAISE(ABORT,'reference requires published template version') END;"""
    for operation in ('INSERT','UPDATE'):
        c.execute(f"CREATE TRIGGER IF NOT EXISTS deploy_ref_{operation.lower()}_valid BEFORE {operation} ON deploy_refs BEGIN {checks} END")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _new_drill_id() -> str:
    return f"d-{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(8).hex()}"


# ═══════════════════════════════════════════════
# 演练身份（drills）
# ═══════════════════════════════════════════════

def issue_drill(core_sid: int, name: str | None = None, identity: str | None = None) -> str:
    """签发演练身份。会话创建时调用; 旁路创建/同号新会话由 ensure_drill 惰性调用"""
    import repository as repo
    with repo.transaction() as c:
        drill_id=_new_drill_id()
        c.execute("INSERT INTO drills(drill_id,core_sid,name,created_at,identity,status) VALUES(?,?,?,?,?,'active')",(drill_id,core_sid,name,_now(),identity))
    return drill_id


def get_active_drill(core_sid:int) -> str | None:
    init()
    with database.connection(DB_PATH) as c:
        row=c.execute("SELECT drill_id FROM drills WHERE core_sid=? AND status='active'",(core_sid,)).fetchone()
    return row['drill_id'] if row else None


_identity_lock = threading.RLock()


class IdentityUnconfirmed(RuntimeError):
    def __init__(self, sid, message):
        self.sid=sid
        super().__init__('会话归属待确认: '+str(message))


def ensure_drill(core_sid:int, identity:str | None=None) -> str:
    import app,core_adapter,repository as repo
    try:
        if identity is None: identity=core_adapter.identities(app.bridge).get(str(core_sid))
        if not identity: raise ValueError('无法取得 CORE 会话实体标识')
    except Exception as e:
        repo.put('identity_diagnostics',str(core_sid),{'status':'pending','message':str(e),'updated_at':_now()})
        raise IdentityUnconfirmed(core_sid,str(e)) from e
    with repo.transaction() as c:
        row=c.execute("SELECT drill_id,identity FROM drills WHERE core_sid=? AND status='active'",(core_sid,)).fetchone()
        repo.remove('identity_diagnostics',str(core_sid))
        if row and row['identity']==identity: return row['drill_id']
        if row:
            unsubscribe_events(core_sid)
            import topologies
            topologies.state(core_sid,'interrupted')
            close_drill(core_sid,'interrupted')
            release_deploy_refs(core_sid=core_sid)
        return issue_drill(core_sid,identity=identity)


def close_drill(core_sid:int,status:str='closed') -> None:
    init()
    with database.connection(DB_PATH) as c:
        c.execute("UPDATE drills SET status=?,closed_at=? WHERE core_sid=? AND status='active'",(status,_now(),core_sid))


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
    drill_id=kw.pop('drill_id',None) or (get_active_drill(session_id) if session_id is not None else None)
    with database.connection(DB_PATH) as c:
        c.execute('INSERT INTO audit(ts,action,session_id,node_id,target,detail,drill_id) VALUES(?,?,?,?,?,?,?)',(_now(),action,session_id,node_id,str(target) if target is not None else None,json.dumps(kw,ensure_ascii=False,default=str),drill_id))


def query_audit(action: str | None = None, session_id: int | None = None,
                node_id: int | None = None, limit: int = 100, drill_id: str | None = None) -> list[dict]:
    init()
    sql = "SELECT ts, action, session_id, node_id, target, detail, drill_id FROM audit WHERE 1=1"
    args: list = []
    if drill_id:
        sql += " AND drill_id=?"
        args.append(drill_id)
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


def record_event(core_sid: int, detail: dict, drill_id: str | None = None) -> None:
    init()
    drill_id = drill_id or ensure_drill(core_sid)
    c = _conn()
    c.execute("INSERT INTO events(ts, drill_id, core_sid, type, action, detail) VALUES (?,?,?,?,?,?)",
              (_now(), drill_id, core_sid, detail.get("type"), detail.get("action"),
               json.dumps(detail, ensure_ascii=False, default=str)))
    c.commit()
    c.close()


def query_events(drill_id=None,limit=200,core_sid=None,before_id=None,after_id=None):
    init()
    if core_sid is not None: drill_id=ensure_drill(core_sid)
    sql='SELECT id,ts,drill_id,core_sid,type,action,detail FROM events WHERE 1=1';args=[]
    if drill_id: sql+=' AND drill_id=?';args.append(drill_id)
    if before_id is not None: sql+=' AND id<=?';args.append(before_id)
    if after_id is not None: sql+=' AND id>?';args.append(after_id)
    sql+=' ORDER BY id '+('ASC' if after_id is not None else 'DESC')+' LIMIT ?';args.append(max(1,min(int(limit),2000)))
    with database.connection(DB_PATH) as c: rows=[dict(r) for r in c.execute(sql,args)]
    for row in rows:
        try: row['detail']=json.loads(row['detail']) if row['detail'] else {}
        except ValueError: row['detail']={}
    return rows


def event_cursor(drill_id):
    init()
    with database.connection(DB_PATH) as c:
        return c.execute('SELECT COALESCE(MAX(id),0) FROM events WHERE drill_id=?',(drill_id,)).fetchone()[0]


# ═══════════════════════════════════════════════
# 部署引用（deploy_refs）: 实例锁定扳机（设计 §5.2 方向1）
# 部署(带 instance 建节点) → 自动登记; 会话删除/异常消失 → 自动解除
# ═══════════════════════════════════════════════

def register_deploy_ref(drill_id,core_sid,node_id,instance_name,instance_version=None):
    import repository as repo
    if instance_version is None:
        import topologies
        instance_version=topologies.bindings(core_sid).get(str(node_id),{}).get('instance_version')
    if instance_version is None:
        raise ValueError('部署引用必须明确模板版本')
    with repo.transaction() as c:
        c.execute("INSERT INTO deploy_refs(drill_id,core_sid,node_id,instance_name,created_at,instance_version) VALUES(?,?,?,?,?,?) ON CONFLICT(core_sid,node_id) DO UPDATE SET drill_id=excluded.drill_id,instance_name=excluded.instance_name,instance_version=excluded.instance_version,created_at=excluded.created_at",(drill_id,core_sid,node_id,instance_name,_now(),instance_version))


def release_deploy_refs(core_sid=None,drill_id=None):
    init()
    with database.connection(DB_PATH) as c:
        if core_sid is not None: cursor=c.execute('DELETE FROM deploy_refs WHERE core_sid=?',(core_sid,))
        else: cursor=c.execute('DELETE FROM deploy_refs WHERE drill_id=?',(drill_id,))
        return cursor.rowcount


def release_deploy_ref_node(core_sid,node_id):
    init()
    with database.connection(DB_PATH) as c:
        return c.execute('DELETE FROM deploy_refs WHERE core_sid=? AND node_id=?',(core_sid,node_id)).rowcount


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
_subscription_lock=threading.RLock()
_subscription_retry={}
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


def _subscribe_events(core_sid: int) -> None:
    """会话事件订阅（幂等）。创建会话时同步调用——避免等 5s tick 导致头几秒动作漏录"""
    old = _event_subs.get(core_sid)
    if old is not None:
        if not hasattr(old,'is_active') or old.is_active(): return
        unsubscribe_events(core_sid)
    try:
        import app as _app
        b = getattr(_app, "bridge", None)
        if b is None:
            return

        drill_id = ensure_drill(core_sid)
        def handler(event, _sid=core_sid, _drill=drill_id):
            try:
                record_event(_sid, event_to_dict(event),_drill)
            except Exception:
                pass

        import core_adapter
        _event_subs[core_sid] = core_adapter.events(b,core_sid,handler)
        logger.info("events subscribed: core_sid=%s", core_sid)
    except Exception as e:
        logger.warning("events subscribe failed sid=%s: %s", core_sid, e)
        raise


def subscribe_events(core_sid):
    with _subscription_lock:
        previous=_event_subs.get(core_sid)
        if previous is not None and previous.is_active(): return True
        retry=_subscription_retry.get(core_sid,{'at':0,'delay':.25})
        if time.monotonic()<retry['at']: return False
        try:
            _subscribe_events(core_sid)
            if core_sid not in _event_subs: return False
            _subscription_retry.pop(core_sid,None)
            return True
        except Exception:
            _subscription_retry[core_sid]={'at':time.monotonic()+retry['delay'],'delay':min(5,retry['delay']*2)}
            return False


def unsubscribe_events(core_sid: int) -> None:
    _subscription_retry.pop(core_sid,None)
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

    import core_adapter
    tokens=core_adapter.identities(b)
    # 1) reconciler: 会话消失的 active drill → interrupted（daemon 重启/被杀）
    c = _conn()
    actives = c.execute("SELECT drill_id, core_sid FROM drills WHERE status='active'").fetchall()
    c.close()
    for r in actives:
        if r["core_sid"] not in sessions:
            import topologies
            topologies.state(r["core_sid"],"interrupted")
            close_drill(r["core_sid"], status="interrupted")
            release_deploy_refs(core_sid=r["core_sid"])  # 会话消失 → 部署引用解除(实例解锁)

    # 2) 旁路创建的会话补登记
    for sid in sessions:
        ensure_drill(sid,tokens.get(str(sid)))

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
            from target_admin import _container_net_bytes, _fmt_bytes
            nb=_container_net_bytes(n.name)
            stx['net_io']=f'RX {_fmt_bytes(nb[0])} / TX {_fmt_bytes(nb[1])}' if nb is not None else None
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
