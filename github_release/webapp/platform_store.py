"""Domain store for the multi-factory manufacturing resource coordination prototype.

This is deliberately a small, explicit SQLite domain model.  It separates:
- capacity supply and demand;
- plan generation;
- atomic reservation and two-party confirmation;
- simulated execution / acceptance / accounting;
- an append-only demo audit ledger.

The demo ledger is a hash chain stored in SQLite.  It is NOT Hyperledger Fabric
and the UI must label it accordingly.  The adapter boundary is kept in this
module so a real Fabric adapter can replace it later.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from functools import wraps

from werkzeug.security import generate_password_hash

from webapp.db import get_db
from webapp import access

SCHEMA = """
CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'factory',
    location TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capability_catalog (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    unit TEXT NOT NULL,
    category TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS user_org (
    user_id INTEGER PRIMARY KEY,
    org_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (user_id) REFERENCES users (id),
    FOREIGN KEY (org_id) REFERENCES organizations (id)
);

CREATE TABLE IF NOT EXISTS resource_supplies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL,
    site TEXT NOT NULL DEFAULT '',
    capability TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT '机时',
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    total_milli INTEGER NOT NULL,
    reserved_milli INTEGER NOT NULL DEFAULT 0,
    committed_milli INTEGER NOT NULL DEFAULT 0,
    consumed_milli INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    source TEXT NOT NULL DEFAULT 'synthetic',
    note TEXT NOT NULL DEFAULT '',
    created_by INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (org_id) REFERENCES organizations (id)
);

CREATE TABLE IF NOT EXISTS demands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL,
    site TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    capability TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT '机时',
    required_milli INTEGER NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    quality TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'OPEN',
    reference_price_cents INTEGER NOT NULL DEFAULT 12000,
    created_by INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (org_id) REFERENCES organizations (id)
);

CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    demand_id INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    algorithm_version TEXT NOT NULL DEFAULT 'manual-s1',
    status TEXT NOT NULL DEFAULT 'READY',
    snapshot_hash TEXT NOT NULL,
    allocation_json TEXT NOT NULL DEFAULT '[]',
    proposed_payment_cents INTEGER NOT NULL DEFAULT 0,
    proposed_reward_cents INTEGER NOT NULL DEFAULT 0,
    broker_surplus_cents INTEGER NOT NULL DEFAULT 0,
    settlement_status TEXT NOT NULL DEFAULT 'NOT_CHECKED',
    failure_reason TEXT,
    created_by INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (demand_id) REFERENCES demands (id)
);

CREATE TABLE IF NOT EXISTS commitments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref TEXT NOT NULL UNIQUE,
    plan_id INTEGER NOT NULL,
    demand_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'RESERVED',
    total_milli INTEGER NOT NULL,
    unit TEXT NOT NULL DEFAULT '机时',
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    buyer_org_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (plan_id) REFERENCES plans (id),
    FOREIGN KEY (demand_id) REFERENCES demands (id),
    FOREIGN KEY (buyer_org_id) REFERENCES organizations (id)
);

CREATE TABLE IF NOT EXISTS commitment_parties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id INTEGER NOT NULL,
    org_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    confirm_status TEXT NOT NULL DEFAULT 'PENDING',
    confirmed_by INTEGER,
    confirmed_at TEXT,
    UNIQUE (commitment_id, org_id),
    FOREIGN KEY (commitment_id) REFERENCES commitments (id),
    FOREIGN KEY (org_id) REFERENCES organizations (id)
);

CREATE TABLE IF NOT EXISTS commitment_allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id INTEGER NOT NULL,
    supply_id INTEGER NOT NULL,
    buyer_org_id INTEGER NOT NULL,
    supplier_org_id INTEGER NOT NULL,
    qty_milli INTEGER NOT NULL,
    unit_price_cents INTEGER NOT NULL,
    proposed_payment_cents INTEGER NOT NULL,
    proposed_reward_cents INTEGER NOT NULL,
    actual_milli INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (commitment_id) REFERENCES commitments (id),
    FOREIGN KEY (supply_id) REFERENCES resource_supplies (id),
    FOREIGN KEY (buyer_org_id) REFERENCES organizations (id),
    FOREIGN KEY (supplier_org_id) REFERENCES organizations (id)
);

CREATE TABLE IF NOT EXISTS execution_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    qty_milli INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    actor_org_id INTEGER NOT NULL,
    actor_user_id INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (commitment_id) REFERENCES commitments (id)
);

CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id INTEGER NOT NULL,
    stage TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'SIMULATED',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (commitment_id) REFERENCES commitments (id)
);

CREATE TABLE IF NOT EXISTS ledger_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    actor_org_id INTEGER,
    payload_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    tx_id TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL DEFAULT 'DEMO_HASH_CHAIN',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_supplies_org_window ON resource_supplies (org_id, window_start);
CREATE INDEX IF NOT EXISTS idx_demands_org_status ON demands (org_id, status);
CREATE INDEX IF NOT EXISTS idx_ledger_object ON ledger_events (object_type, object_id);
"""


CAPABILITY_CATALOG = (
    ('CNC-3AXIS', '三轴数控加工', '机时', '金属切削', '标准三轴 CNC 加工中心'),
    ('CNC-5AXIS', '五轴数控加工', '机时', '金属切削', '复杂曲面五轴联动加工'),
    ('INJECTION-MOLDING', '注塑成型', '模次', '塑料成型', '注塑机与模具共享'),
    ('SHEET-METAL', '钣金加工', '件', '金属成形', '激光切割、折弯与冲压'),
    ('HEAT-TREATMENT', '热处理', '炉时', '特种工艺', '淬火、回火与真空热处理'),
    ('SURFACE-TREATMENT', '表面处理', '槽时', '特种工艺', '阳极氧化、电镀与喷涂'),
    ('ASSEMBLY', '装配', '工位时', '装配', '人工与自动化工位'),
    ('CMM-INSPECTION', '三坐标检测', '件', '质量检测', '尺寸与形位公差检测'),
    ('ADDITIVE-MANUFACTURING', '增材制造', '克', '增材制造', '金属或工程塑料增材'),
)


class PlatformError(ValueError):
    """A business-rule error that should be shown to the user."""


class PermissionDenied(PlatformError):
    pass


class ConflictError(PlatformError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_milli(milli: int, unit: str = "机时") -> str:
    """Format integer thousandth-hours for display."""
    value = float(milli or 0) / 1000.0
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return f"{text or '0'} {unit}"


def fmt_money(cents: int) -> str:
    return f"¥{float(cents or 0) / 100.0:,.2f}"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def append_ledger(db, event_type: str, object_type: str, object_id: Any, actor_org_id: int | None,
                  payload: dict, created_at: str | None = None) -> dict:
    """Append an event to the demo hash chain inside the caller's transaction."""
    prev = db.execute("SELECT event_hash FROM ledger_events ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = prev["event_hash"] if prev else "GENESIS"
    stamp = created_at or now_iso()
    body = {
        "event_type": event_type,
        "object_type": object_type,
        "object_id": str(object_id),
        "actor_org_id": actor_org_id,
        "payload": payload,
        "created_at": stamp,
    }
    body_hash = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
    event_hash = hashlib.sha256((prev_hash + body_hash).encode("utf-8")).hexdigest()
    tx_id = "demo-" + event_hash[:20]
    db.execute(
        """
        INSERT INTO ledger_events
          (event_type, object_type, object_id, actor_org_id, payload_json,
           prev_hash, event_hash, tx_id, mode, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'DEMO_HASH_CHAIN', ?)
        """,
        (event_type, object_type, str(object_id), actor_org_id, _canonical(payload),
         prev_hash, event_hash, tx_id, stamp),
    )
    return {
        "event_type": event_type,
        "object_type": object_type,
        "object_id": str(object_id),
        "actor_org_id": actor_org_id,
        "event_hash": event_hash,
        "tx_id": tx_id,
        "mode": "DEMO_HASH_CHAIN",
        "created_at": stamp,
    }


def verify_ledger(db) -> tuple[bool, int | None]:
    """Recompute the demo chain. Returns (valid, first_bad_id)."""
    prev = "GENESIS"
    rows = db.execute("SELECT * FROM ledger_events ORDER BY id").fetchall()
    for row in rows:
        payload = json.loads(row["payload_json"])
        body = {
            "event_type": row["event_type"],
            "object_type": row["object_type"],
            "object_id": row["object_id"],
            "actor_org_id": row["actor_org_id"],
            "payload": payload,
            "created_at": row["created_at"],
        }
        body_hash = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
        expected = hashlib.sha256((prev + body_hash).encode("utf-8")).hexdigest()
        if expected != row["event_hash"]:
            return False, row["id"]
        prev = row["event_hash"]
    return True, None


def init_platform_schema() -> None:
    db = get_db()
    db.executescript(SCHEMA)
    db.commit()
    from webapp.paper_coordination import init_schema
    init_schema(db)


def seed_platform() -> None:
    """Seed organizations, demo accounts, memberships and one synthetic case."""
    db = get_db()
    stamp = now_iso()
    org_specs = [
        ("ORG_A", "广州精工制造有限公司", "factory", "广州"),
        ("ORG_B", "东莞共享加工中心", "factory", "东莞"),
        ("ORG_C", "佛山智能制造有限公司", "factory", "佛山"),
        ("ORG_D", "深圳精密制造有限公司", "factory", "深圳"),
        ("ORG_E", "惠州智能装备有限公司", "factory", "惠州"),
        ("ALLIANCE", "粤港澳大湾区制造资源共享联盟", "alliance", "广州"),
    ]
    for code, name, kind, location in org_specs:
        db.execute(
            "INSERT OR IGNORE INTO organizations (code, name, kind, location, created_at) VALUES (?, ?, ?, ?, ?)",
            (code, name, kind, location, stamp),
        )
    orgs = {row["code"]: row["id"] for row in db.execute("SELECT id, code FROM organizations")}
    for code, name, unit, category, note in CAPABILITY_CATALOG:
        db.execute(
            "INSERT OR IGNORE INTO capability_catalog (code, name, unit, category, note) VALUES (?, ?, ?, ?, ?)",
            (code, name, unit, category, note),
        )

    # New demo users. The existing reviewer/demo/admin users are mapped below.
    demo_password = os.environ.get("WEB_DEMO_PASSWORD", "Demo2026!")
    reviewer_password = os.environ.get("WEB_REVIEWER_PASSWORD", "Review2026!")
    new_users = [
        ("beta", "设备主管", "东莞共享加工中心", "resource_manager"),
        ("gamma", "生产主管", "佛山智能制造有限公司", "resource_manager"),
        ("delta", "设备主管", "深圳精密制造有限公司", "resource_manager"),
        ("epsilon", "生产主管", "惠州智能装备有限公司", "resource_manager"),
        ("coordinator", "平台协调员", "粤港澳大湾区制造资源共享联盟", "coordinator"),
    ]
    for username, display_name, organization, role in new_users:
        exists = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if exists is None:
            db.execute(
                """INSERT INTO users (username, password_hash, display_name, organization, role, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (username, generate_password_hash(demo_password), display_name, organization, role, stamp),
            )
        user = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        org_code = {"beta": "ORG_B", "gamma": "ORG_C", "delta": "ORG_D", "epsilon": "ORG_E", "coordinator": "ALLIANCE"}[username]
        db.execute(
            "INSERT OR IGNORE INTO user_org (user_id, org_id, role, is_primary) VALUES (?, ?, ?, 1)",
            (user["id"], orgs[org_code], role),
        )

    # Keep the primary reviewer-facing account labels aligned with the
    # multi-factory demo, including databases created by the earlier app.
    db.execute(
        "UPDATE users SET display_name = ?, organization = ? WHERE username = 'demo'",
        ("计划员", "广州精工制造有限公司"),
    )
    db.execute("UPDATE users SET display_name = ? WHERE username = 'coordinator'", ("平台协调员",))
    db.execute("UPDATE users SET display_name = ? WHERE username = 'reviewer'", ("联盟链验证员",))
    for username, display_name in (('beta','设备主管'),('gamma','生产主管'),('delta','设备主管'),('epsilon','生产主管')):
        db.execute("UPDATE users SET display_name = ? WHERE username = ?", (display_name, username))
    # The former evaluator experiment is not a public manager role anymore.
    # Keep the user row for historical attribution, but revoke its membership.
    db.execute("DELETE FROM user_org WHERE user_id IN (SELECT id FROM users WHERE username='evaluator')")
    mappings = [
        ("demo", "ORG_A", "planner"),
        ("reviewer", "ALLIANCE", "auditor"),
        ("admin", "ALLIANCE", "admin"),
    ]
    for username, org_code, role in mappings:
        user = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if user:
            db.execute(
                "INSERT OR IGNORE INTO user_org (user_id, org_id, role, is_primary) VALUES (?, ?, ?, 1)",
                (user["id"], orgs[org_code], role),
            )

    # One synthetic case. Keep it idempotent.
    count = db.execute("SELECT COUNT(*) AS n FROM resource_supplies").fetchone()["n"]
    if count == 0:
        start = (datetime.now() + timedelta(days=2)).replace(hour=8, minute=0, second=0, microsecond=0)
        end = start.replace(hour=16)
        start_s = start.isoformat(timespec="minutes")
        end_s = end.isoformat(timespec="minutes")
        beta = db.execute("SELECT id FROM users WHERE username='beta'").fetchone()["id"]
        gamma = db.execute("SELECT id FROM users WHERE username='gamma'").fetchone()["id"]
        demo = db.execute("SELECT id FROM users WHERE username='demo'").fetchone()["id"]
        db.execute(
            """INSERT INTO resource_supplies
               (org_id, site, capability, unit, window_start, window_end, total_milli,
                version, status, source, note, created_by, created_at)
               VALUES (?, ?, 'CNC-3AXIS', '机时', ?, ?, 240000, 1, 'ACTIVE', 'synthetic',
                       '合成数据：共享中心三轴加工能力池', ?, ?)""",
            (orgs["ORG_B"], "东莞共享加工中心", start_s, end_s, beta, stamp),
        )
        db.execute(
            """INSERT INTO resource_supplies
               (org_id, site, capability, unit, window_start, window_end, total_milli,
                version, status, source, note, created_by, created_at)
               VALUES (?, ?, 'CNC-3AXIS', '机时', ?, ?, 160000, 1, 'ACTIVE', 'synthetic',
                       '合成数据：共享中心三轴加工能力池', ?, ?)""",
            (orgs["ORG_C"], "佛山智能制造厂", start_s, end_s, gamma, stamp),
        )
        db.execute(
            """INSERT INTO demands
               (org_id, site, title, capability, unit, required_milli, window_start, window_end,
                quality, note, status, reference_price_cents, created_by, created_at)
               VALUES (?, ?, '新能源电池夹具加工', 'CNC-3AXIS', '机时', 3000, ?, ?,
                       'IT7 / Ra1.6', '合成数据：A 厂本地能力缺口 3 机时', 'OPEN', 12000, ?, ?)""",
            (orgs["ORG_A"], "广州精工制造厂", start_s, end_s, demo, stamp),
        )
        append_ledger(db, "SYSTEM_INIT", "platform", "seed", orgs["ALLIANCE"],
                      {"note": "synthetic platform seed", "mode": "DEMO_HASH_CHAIN"}, stamp)
    # Extra participants on the same capability class. This block is
    # idempotent and also runs for databases created before D/E existed.
    default_start = (datetime.now() + timedelta(days=2)).replace(hour=8, minute=0, second=0, microsecond=0)
    default_end = default_start.replace(hour=16)
    demand_window = db.execute("SELECT window_start, window_end FROM demands ORDER BY id LIMIT 1").fetchone()
    if demand_window:
        start_s, end_s = demand_window["window_start"], demand_window["window_end"]
    else:
        start_s, end_s = (default_start.isoformat(timespec="minutes"),
                           default_end.isoformat(timespec="minutes"))
    extra = [
        ("delta", "ORG_D", "深圳精密制造厂", 120000),
        ("epsilon", "ORG_E", "惠州智能装备厂", 80000),
    ]
    for username, org_code, site, total_milli in extra:
        user = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if user is None:
            continue
        exists = db.execute("SELECT 1 FROM resource_supplies WHERE org_id=? LIMIT 1",
                            (orgs[org_code],)).fetchone()
        if not exists:
            db.execute(
                """INSERT INTO resource_supplies
                   (org_id, site, capability, unit, window_start, window_end, total_milli,
                    version, status, source, note, created_by, created_at)
                   VALUES (?, ?, 'CNC-3AXIS', '机时', ?, ?, ?, 1, 'ACTIVE', 'synthetic',
                           '合成数据：同类标准三轴加工能力', ?, ?)""",
                (orgs[org_code], site, start_s, end_s, total_milli, user["id"], stamp),
            )
    # Additional capability classes: realistic service catalogue around the
    # CNC-3AXIS demo slice. Each org has at most one active pool per
    # capability/unit/window so the paper epoch adapter can run without
    # duplicate-org aggregation.
    extra_capabilities = [
        ('beta', 'ORG_B', '东莞共享加工中心', 'CNC-5AXIS', '机时', 120000, '五轴加工共享窗口'),
        ('gamma', 'ORG_C', '佛山智能制造厂', 'INJECTION-MOLDING', '模次', 5000000, '注塑机共享窗口'),
        ('gamma', 'ORG_C', '佛山智能制造厂', 'SHEET-METAL', '件', 1200000, '钣金加工共享窗口'),
        ('delta', 'ORG_D', '深圳精密制造厂', 'HEAT-TREATMENT', '炉时', 96000, '真空热处理共享窗口'),
        ('delta', 'ORG_D', '深圳精密制造厂', 'SURFACE-TREATMENT', '槽时', 72000, '阳极氧化共享窗口'),
        ('epsilon', 'ORG_E', '惠州智能装备厂', 'ASSEMBLY', '工位时', 240000, '装配工位共享窗口'),
        ('epsilon', 'ORG_E', '惠州智能装备厂', 'CMM-INSPECTION', '件', 480000, '三坐标检测共享窗口'),
        ('epsilon', 'ORG_E', '惠州智能装备厂', 'ADDITIVE-MANUFACTURING', '克', 60000000, '增材制造共享窗口'),
    ]
    for username, org_code, site, capability, unit, total_milli, note in extra_capabilities:
        user = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if user is None:
            continue
        exists = db.execute('''SELECT 1 FROM resource_supplies
            WHERE org_id=? AND capability=? AND unit=? AND window_start=? AND window_end=? LIMIT 1''',
            (orgs[org_code], capability, unit, start_s, end_s)).fetchone()
        if not exists:
            db.execute('''INSERT INTO resource_supplies
                (org_id, site, capability, unit, window_start, window_end, total_milli,
                 version, status, source, note, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'ACTIVE', 'synthetic-catalog', ?, ?, ?)''',
                (orgs[org_code], site, capability, unit, start_s, end_s, total_milli,
                 note, user["id"], stamp))

    extra_demands = [
        ('demo', 'ORG_A', '新能源电控壳体五轴精加工', 'CNC-5AXIS', '机时', 48000, 26000, 'IT7 / Ra0.8', '五轴曲面加工缺口'),
        ('demo', 'ORG_A', '连接器壳体注塑', 'INJECTION-MOLDING', '模次', 800000, 4500, 'CTQ 尺寸全检', '注塑产能缺口'),
        ('demo', 'ORG_A', '齿轮轴真空热处理', 'HEAT-TREATMENT', '炉时', 24000, 30000, 'HRC 58–62', '热处理外协缺口'),
        ('demo', 'ORG_A', '首件三坐标检测', 'CMM-INSPECTION', '件', 120000, 8000, 'CMM 报告', '检测能力缺口'),
    ]
    demo_user = db.execute("SELECT id FROM users WHERE username='demo'").fetchone()
    if demo_user:
        for username, org_code, title, capability, unit, required_milli, price_cents, quality, note in extra_demands:
            exists = db.execute('''SELECT 1 FROM demands
                WHERE org_id=? AND capability=? AND unit=? AND window_start=? AND window_end=? LIMIT 1''',
                (orgs[org_code], capability, unit, start_s, end_s)).fetchone()
            if not exists:
                db.execute('''INSERT INTO demands
                    (org_id, site, title, capability, unit, required_milli, window_start, window_end,
                     quality, note, status, reference_price_cents, created_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?)''',
                    (orgs[org_code], '广州精工制造厂', title, capability, unit, required_milli,
                     start_s, end_s, quality, note, price_cents, demo_user["id"], stamp))
    db.commit()


def membership_for(user_id: int):
    return get_db().execute(
        """SELECT uo.*, o.code AS org_code, o.name AS org_name, o.kind AS org_kind, o.location
           FROM user_org uo JOIN organizations o ON o.id = uo.org_id
           WHERE uo.user_id = ?""",
        (user_id,),
    ).fetchone()


def is_alliance_member(membership) -> bool:
    return bool(access.valid_member(membership) and membership['org_kind']=='alliance')


def visible_org_filter(membership):
    if not access.valid_member(membership):
        raise PermissionDenied('请使用已分配岗位的账号登录。')
    if is_alliance_member(membership):
        return None
    return int(membership["org_id"])


def get_org(org_id: int):
    return get_db().execute("SELECT * FROM organizations WHERE id = ?", (org_id,)).fetchone()


def list_supplies(membership, limit: int = 100, capability: str | None = None):
    db = get_db()
    org_id = visible_org_filter(membership)
    capability = (capability or '').strip() or None
    if org_id is None:
        sql = """SELECT s.*, o.name AS org_name FROM resource_supplies s
               JOIN organizations o ON o.id = s.org_id WHERE s.status='ACTIVE'"""
        args = []
        if capability:
            sql += " AND s.capability = ?"; args.append(capability)
        sql += " ORDER BY s.created_at DESC, s.id DESC LIMIT ?"; args.append(limit)
        return db.execute(sql, args).fetchall()
    sql = """SELECT s.*, o.name AS org_name FROM resource_supplies s
           JOIN organizations o ON o.id = s.org_id
           WHERE s.org_id = ? AND s.status='ACTIVE'"""
    args = [org_id]
    if capability:
        sql += " AND s.capability = ?"; args.append(capability)
    sql += " ORDER BY s.created_at DESC, s.id DESC LIMIT ?"; args.append(limit)
    return db.execute(sql, args).fetchall()


def list_demands(membership, limit: int = 100):
    db = get_db()
    org_id = visible_org_filter(membership)
    if org_id is None:
        return db.execute(
            """SELECT d.*, o.name AS org_name FROM demands d
               JOIN organizations o ON o.id = d.org_id
               ORDER BY d.created_at DESC, d.id DESC LIMIT ?""", (limit,)
        ).fetchall()
    return db.execute(
        """SELECT d.*, o.name AS org_name FROM demands d
           JOIN organizations o ON o.id = d.org_id
           WHERE d.org_id = ? ORDER BY d.created_at DESC, d.id DESC LIMIT ?""",
        (org_id, limit),
    ).fetchall()


def create_supply(membership, *, site, capability, window_start, window_end, hours, unit="机时", note=""):
    if not membership:
        raise PermissionDenied("请先登录企业账号。")
    if not access.can(membership, 'supply.write'):
        raise PermissionDenied("当前岗位不能发布资源供给。")
    total_milli = int(round(float(hours) * 1000))
    if total_milli <= 0:
        raise PlatformError("可共享能力必须为正数。")
    if not capability.strip():
        raise PlatformError("请填写能力类别。")
    if not window_start or not window_end or window_start >= window_end:
        raise PlatformError("共享时段不合法，结束时间必须晚于开始时间。")
    db = get_db()
    cur = db.execute(
        """INSERT INTO resource_supplies
           (org_id, site, capability, unit, window_start, window_end, total_milli,
            version, status, source, note, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'ACTIVE', 'manual', ?, ?, ?)""",
        (membership["org_id"], site or membership["org_name"], capability.strip(), unit,
         window_start, window_end, total_milli, note.strip(), membership["user_id"], now_iso()),
    )
    append_ledger(db, "RESOURCE_PUBLISHED", "resource_supply", cur.lastrowid, membership["org_id"],
                  {"org_id": membership["org_id"], "total_milli": total_milli,
                   "capability": capability.strip()})
    db.commit()
    return cur.lastrowid


def create_demand(membership, *, site, title, capability, window_start, window_end, hours,
                  unit="机时", quality="", note="", reference_price_yuan=120):
    if not membership:
        raise PermissionDenied("请先登录企业账号。")
    if not access.can(membership, 'demand.write'):
        raise PermissionDenied("当前岗位不能登记生产需求。")
    required_milli = int(round(float(hours) * 1000))
    if required_milli <= 0:
        raise PlatformError("最低需求量必须为正数。")
    if not title.strip() or not capability.strip():
        raise PlatformError("请填写需求名称和能力类别。")
    if not window_start or not window_end or window_start >= window_end:
        raise PlatformError("需求时段不合法。")
    db = get_db()
    cur = db.execute(
        """INSERT INTO demands
           (org_id, site, title, capability, unit, required_milli, window_start, window_end,
            quality, note, status, reference_price_cents, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?)""",
        (membership["org_id"], site or membership["org_name"], title.strip(), capability.strip(),
         unit, required_milli, window_start, window_end, quality.strip(), note.strip(),
         int(round(float(reference_price_yuan) * 100)), membership["user_id"], now_iso()),
    )
    append_ledger(db, "DEMAND_REGISTERED", "demand", cur.lastrowid, membership["org_id"],
                  {"org_id": membership["org_id"], "required_milli": required_milli,
                   "capability": capability.strip()})
    db.commit()
    return cur.lastrowid


def get_demand(demand_id: int):
    return get_db().execute("SELECT * FROM demands WHERE id = ?", (demand_id,)).fetchone()


def get_plan(plan_id: int):
    return get_db().execute(
        """SELECT p.*, d.title AS demand_title, d.org_id AS demand_org_id, d.site AS demand_site,
                  d.required_milli, d.window_start, d.window_end, d.unit, d.quality, d.capability
           FROM plans p JOIN demands d ON d.id = p.demand_id WHERE p.id = ?""",
        (plan_id,),
    ).fetchone()


def candidates_for_demand(demand) -> list[dict]:
    db = get_db()
    rows = db.execute(
        """SELECT * FROM resource_supplies
           WHERE capability = ? AND unit = ? AND status = 'ACTIVE' AND org_id <> ?
             AND window_start <= ? AND window_end >= ?
           ORDER BY (total_milli - reserved_milli - consumed_milli) DESC, window_start, id""",
        (demand["capability"], demand["unit"], demand["org_id"], demand["window_start"], demand["window_end"]),
    ).fetchall()
    result = []
    for row in rows:
        available = max(0, row["total_milli"] - row["reserved_milli"] - row["consumed_milli"])
        if available <= 0:
            continue
        result.append({"supply": row, "available_milli": available})
    return result


def create_manual_plan(demand_id: int, membership) -> int:
    if not access.can(membership, 'coordination.write'):
        raise PermissionDenied("只有联盟协调员可以生成协调方案。")
    demand = get_demand(demand_id)
    if demand is None:
        raise PlatformError("需求不存在。")
    if demand["status"] not in ("OPEN", "PLAN_READY"):
        raise PlatformError("当前需求状态不能生成新方案。")
    candidates = candidates_for_demand(demand)
    remaining = int(demand["required_milli"])
    allocations = []
    for candidate in candidates:
        if remaining <= 0:
            break
        supply = candidate["supply"]
        qty = min(remaining, candidate["available_milli"])
        if qty <= 0:
            continue
        unit_price = int(demand["reference_price_cents"])
        payment = round(qty * unit_price / 1000)
        allocations.append({
            "supply_id": int(supply["id"]),
            "supply_version": int(supply["version"]),
            "supplier_org_id": int(supply["org_id"]),
            "supplier_site": supply["site"],
            "qty_milli": int(qty),
            "unit_price_cents": unit_price,
            "payment_cents": int(payment),
            "reward_cents": int(payment),
        })
        remaining -= qty
    snapshot = {
        "demand_id": demand_id,
        "required_milli": int(demand["required_milli"]),
        "capability": demand["capability"],
        "candidates": [
            {"supply_id": c["supply"]["id"], "version": c["supply"]["version"],
             "available_milli": c["available_milli"], "org_id": c["supply"]["org_id"]}
            for c in candidates
        ],
    }
    snapshot_hash = hashlib.sha256(_canonical(snapshot).encode("utf-8")).hexdigest()
    db = get_db()
    if remaining > 0:
        cur = db.execute(
            """INSERT INTO plans
               (demand_id, strategy, status, snapshot_hash, allocation_json, failure_reason,
                created_by, created_at)
               VALUES (?, 'manual-s1', 'PHYSICAL_INFEASIBLE', ?, '[]', ?, ?, ?)""",
            (demand_id, snapshot_hash, "候选容量不足：仍缺 %s" % fmt_milli(remaining, demand["unit"]),
             membership["user_id"], now_iso()),
        )
        append_ledger(db, "PLAN_FAILED", "plan", cur.lastrowid, membership["org_id"],
                      {"demand_id": demand_id, "reason": "capacity", "remaining_milli": remaining})
        db.commit()
        return cur.lastrowid
    payment_total = sum(a["payment_cents"] for a in allocations)
    reward_total = sum(a["reward_cents"] for a in allocations)
    cur = db.execute(
        """INSERT INTO plans
           (demand_id, strategy, status, snapshot_hash, allocation_json,
            proposed_payment_cents, proposed_reward_cents, broker_surplus_cents,
            settlement_status, created_by, created_at)
           VALUES (?, 'manual-s1', 'READY', ?, ?, ?, ?, 0, 'PENDING', ?, ?)""",
        (demand_id, snapshot_hash, _canonical(allocations), payment_total, reward_total,
         membership["user_id"], now_iso()),
    )
    plan_id = cur.lastrowid
    db.execute("UPDATE demands SET status = 'PLAN_READY' WHERE id = ?", (demand_id,))
    append_ledger(db, "PLAN_GENERATED", "plan", plan_id, membership["org_id"],
                  {"demand_id": demand_id, "snapshot_hash": snapshot_hash,
                   "allocation_count": len(allocations), "payment_cents": payment_total,
                   "participant_org_ids": sorted({int(demand["org_id"])} |
                                                 {a["supplier_org_id"] for a in allocations})})
    db.commit()
    return plan_id


def _plan_allocations(plan) -> list[dict]:
    return json.loads(plan["allocation_json"] or "[]")


def _insert_commitment(db, plan, allocations, membership):
    """Insert one commitment inside the caller's already validated transaction."""
    plan_id = plan["id"]
    demand = db.execute("SELECT * FROM demands WHERE id=?", (plan["demand_id"],)).fetchone()
    ref = "C-" + datetime.now().strftime("%Y%m%d") + "-" + secrets.token_hex(2).upper()
    cur = db.execute(
        """INSERT INTO commitments
           (ref, plan_id, demand_id, status, total_milli, unit, window_start, window_end,
            version, buyer_org_id, created_at)
           VALUES (?, ?, ?, 'RESERVED', ?, ?, ?, ?, 1, ?, ?)""",
        (ref, plan_id, plan["demand_id"], sum(int(a["qty_milli"]) for a in allocations), demand["unit"],
         demand["window_start"], demand["window_end"], int(demand["org_id"]), now_iso()),
    )
    commitment_id = cur.lastrowid
    db.execute(
        "INSERT INTO commitment_parties (commitment_id, org_id, role) VALUES (?, ?, 'buyer')",
        (commitment_id, int(demand["org_id"])),
    )
    supplier_ids = sorted({int(a["supplier_org_id"]) for a in allocations})
    for supplier_id in supplier_ids:
        db.execute(
            "INSERT INTO commitment_parties (commitment_id, org_id, role) VALUES (?, ?, 'supplier')",
            (commitment_id, supplier_id),
        )
    for alloc in allocations:
        db.execute(
            """INSERT INTO commitment_allocations
               (commitment_id, supply_id, buyer_org_id, supplier_org_id, qty_milli,
                unit_price_cents, proposed_payment_cents, proposed_reward_cents)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (commitment_id, alloc["supply_id"], int(demand["org_id"]),
             alloc["supplier_org_id"], alloc["qty_milli"], alloc["unit_price_cents"],
             alloc["payment_cents"], alloc["reward_cents"]),
        )
    db.execute("UPDATE plans SET status = 'RESERVED' WHERE id = ?", (plan_id,))
    db.execute("UPDATE demands SET status = 'RESERVED' WHERE id = ?", (plan["demand_id"],))
    append_ledger(db, "CAPACITY_RESERVED", "commitment", commitment_id, membership["org_id"], {
        "ref": ref,
        "plan_id": plan_id,
        "allocations": allocations,
        "participant_org_ids": [int(demand["org_id"])] + supplier_ids,
    })
    db.execute("UPDATE plans SET status='SUPERSEDED' WHERE demand_id=? AND id<>? AND status='READY'",
               (plan['demand_id'],plan_id))
    return commitment_id


def reserve_plan(plan_id: int, membership) -> int:
    if not access.can(membership, 'coordination.write'):
        raise PermissionDenied('只有联盟协调员可以执行容量预留。')
    plan = get_plan(plan_id)
    if plan is None or plan['status'] != 'READY':
        raise ConflictError('当前方案不可预留。')
    if plan['coordination_run_id'] is not None:
        from webapp.paper_coordination import reserve_epoch
        return reserve_epoch(plan_id,membership)
    allocations = _plan_allocations(plan)
    if not allocations:
        raise PlatformError('方案没有可执行分配。')
    db=get_db()
    try:
        db.execute('BEGIN IMMEDIATE')
        plan=get_plan(plan_id)
        demand=get_demand(plan['demand_id'])
        if plan['status']!='READY' or demand['status'] not in ('OPEN','PLAN_READY'):
            raise ConflictError('需求或方案已被其他操作预留。')
        for alloc in allocations:
            supply=db.execute('SELECT * FROM resource_supplies WHERE id=?',(alloc['supply_id'],)).fetchone()
            if supply is None or supply['version'] != alloc['supply_version']:
                raise ConflictError('供给状态版本已变化，请重新生成方案。')
            if supply['status']!='ACTIVE' or supply['unit']!=demand['unit'] or supply['capability']!=demand['capability']:
                raise ConflictError('供给的能力、单位或状态已变化。')
            if supply['window_start']>demand['window_start'] or supply['window_end']<demand['window_end']:
                raise ConflictError('供给时间窗口不再满足需求。')
            if supply['total_milli']-supply['reserved_milli']-supply['consumed_milli']<alloc['qty_milli']:
                raise ConflictError('供给容量不足。')
            db.execute('UPDATE resource_supplies SET reserved_milli=reserved_milli+?,version=version+1 WHERE id=?',
                       (alloc['qty_milli'],alloc['supply_id']))
        commitment_id=_insert_commitment(db,plan,allocations,membership)
        db.commit()
        return commitment_id
    except ConflictError:
        db.rollback()
        db.execute("UPDATE plans SET status='EPOCH_INVALIDATED' WHERE id=? AND status='READY'",(plan_id,))
        db.commit()
        raise
    except Exception:
        db.rollback()
        raise


def list_commitments(membership, limit: int = 100):
    db = get_db()
    org_id = visible_org_filter(membership)
    if org_id is None:
        return db.execute(
            """SELECT c.*, o.name AS buyer_name FROM commitments c
               JOIN organizations o ON o.id = c.buyer_org_id
               ORDER BY c.created_at DESC, c.id DESC LIMIT ?""", (limit,)
        ).fetchall()
    return db.execute(
        """SELECT DISTINCT c.*, o.name AS buyer_name FROM commitments c
           JOIN commitment_parties p ON p.commitment_id = c.id
           JOIN organizations o ON o.id = c.buyer_org_id
           WHERE p.org_id = ? ORDER BY c.created_at DESC, c.id DESC LIMIT ?""",
        (org_id, limit),
    ).fetchall()


def get_commitment(commitment_id: int):
    return get_db().execute("SELECT * FROM commitments WHERE id = ?", (commitment_id,)).fetchone()


def commitment_parties(commitment_id: int):
    return get_db().execute(
        """SELECT p.*, o.name AS org_name, o.code AS org_code
           FROM commitment_parties p JOIN organizations o ON o.id = p.org_id
           WHERE p.commitment_id = ? ORDER BY p.id""",
        (commitment_id,),
    ).fetchall()


def commitment_allocations(commitment_id: int):
    return get_db().execute(
        """SELECT a.*, s.site AS supplier_site, o.name AS supplier_name
           FROM commitment_allocations a
           JOIN resource_supplies s ON s.id = a.supply_id
           JOIN organizations o ON o.id = a.supplier_org_id
           WHERE a.commitment_id = ? ORDER BY a.id""",
        (commitment_id,),
    ).fetchall()


def _serialized_change(function):
    """Serialize read/check/write transitions; roll back validation failures."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        db = get_db()
        try:
            db.execute('BEGIN IMMEDIATE')
            return function(*args, **kwargs)
        except Exception:
            db.rollback()
            raise
    return wrapped


@_serialized_change
def confirm_commitment(commitment_id: int, membership) -> dict:
    if not access.can(membership, 'task.confirm'):
        raise PermissionDenied('当前岗位不能确认企业共享任务。')
    db = get_db()
    commitment = get_commitment(commitment_id)
    if commitment is None:
        raise PlatformError("共享承诺不存在。")
    party = db.execute(
        "SELECT * FROM commitment_parties WHERE commitment_id = ? AND org_id = ?",
        (commitment_id, int(membership["org_id"])),
    ).fetchone()
    if party is None:
        raise PermissionDenied("当前企业不是本次共享的参与方。")
    if party['role']=='buyer' and not access.can(membership, 'task.confirm_buyer'):
        raise PermissionDenied('需求方确认需由企业计划员或企业管理员完成。')
    if commitment["status"] != "RESERVED":
        raise ConflictError("当前状态不能确认：%s" % commitment["status"])
    if party["confirm_status"] == "CONFIRMED":
        raise ConflictError("本方已经确认过该版本。")
    db.execute(
        """UPDATE commitment_parties
           SET confirm_status='CONFIRMED', confirmed_by=?, confirmed_at=?
           WHERE id=?""",
        (membership["user_id"], now_iso(), party["id"]),
    )
    parties = db.execute("SELECT * FROM commitment_parties WHERE commitment_id=?", (commitment_id,)).fetchall()
    all_confirmed = all(p["confirm_status"] == "CONFIRMED" for p in parties)
    if all_confirmed:
        allocations = commitment_allocations(commitment_id)
        for alloc in allocations:
            db.execute(
                "UPDATE resource_supplies SET committed_milli = committed_milli + ? WHERE id = ?",
                (int(alloc["qty_milli"]), int(alloc["supply_id"])),
            )
        db.execute("UPDATE commitments SET status='CONFIRMED', version=version+1 WHERE id=?", (commitment_id,))
        new_status = "CONFIRMED"
    else:
        new_status = "RESERVED"
    append_ledger(db, "PARTY_CONFIRMED", "commitment", commitment_id, int(membership["org_id"]), {
        "party_org_id": int(membership["org_id"]),
        "role": party["role"],
        "all_confirmed": all_confirmed,
        "participant_org_ids": [int(p["org_id"]) for p in parties],
    })
    if all_confirmed:
        append_ledger(db, "COMMITMENT_CONFIRMED", "commitment", commitment_id, int(membership["org_id"]), {
            "participant_org_ids": [int(p["org_id"]) for p in parties],
            "version": int(commitment["version"]) + 1,
        })
    db.commit()
    return {"status": new_status, "all_confirmed": all_confirmed}


@_serialized_change
def record_execution(commitment_id: int, membership, *, actual_hours: float, note: str = "") -> None:
    if not access.can(membership, 'task.execute'):
        raise PermissionDenied('当前岗位不能登记企业执行结果。')
    db = get_db()
    commitment = get_commitment(commitment_id)
    if commitment is None:
        raise PlatformError("共享承诺不存在。")
    supplier = db.execute(
        "SELECT * FROM commitment_parties WHERE commitment_id=? AND org_id=? AND role='supplier'",
        (commitment_id, int(membership["org_id"])),
    ).fetchone()
    if supplier is None:
        raise PermissionDenied("只有供给方可以登记执行结果。")
    if commitment["status"] != "CONFIRMED":
        raise ConflictError("只有双方确认后的承诺可以执行。")
    if db.execute("SELECT 1 FROM execution_events WHERE commitment_id=? AND actor_org_id=? AND event_type='EXECUTED'",
                  (commitment_id,membership['org_id'])).fetchone():
        raise ConflictError('本企业已登记执行结果，不能重复登记。')
    allocations = [a for a in commitment_allocations(commitment_id) if a['supplier_org_id']==membership['org_id']]
    actual_milli = int(round(float(actual_hours) * 1000))
    if actual_milli <= 0 or actual_milli > sum(a['qty_milli'] for a in allocations):
        raise PlatformError('实际用量必须大于 0 且不超过本企业承诺量。')
    remaining = actual_milli
    for alloc in allocations:
        used = min(remaining, int(alloc["qty_milli"]))
        db.execute("UPDATE commitment_allocations SET actual_milli=? WHERE id=?", (used, alloc["id"]))
        db.execute(
            """UPDATE resource_supplies
               SET reserved_milli = MAX(0, reserved_milli - ?),
                   committed_milli = MAX(0, committed_milli - ?),
                   consumed_milli = consumed_milli + ?, version = version + 1
               WHERE id=?""",
            (int(alloc["qty_milli"]), int(alloc["qty_milli"]), used, int(alloc["supply_id"])),
        )
        remaining -= used
    db.execute(
        "INSERT INTO execution_events (commitment_id, event_type, qty_milli, note, actor_org_id, actor_user_id, created_at) VALUES (?, 'EXECUTED', ?, ?, ?, ?, ?)",
        (commitment_id, actual_milli, note.strip(), int(membership["org_id"]), membership["user_id"], now_iso()),
    )
    suppliers = {p['org_id'] for p in db.execute("SELECT org_id FROM commitment_parties WHERE commitment_id=? AND role='supplier'", (commitment_id,))}
    reported = {e['actor_org_id'] for e in db.execute("SELECT actor_org_id FROM execution_events WHERE commitment_id=? AND event_type='EXECUTED'", (commitment_id,))}
    db.execute("UPDATE commitments SET status=?, version=version+1 WHERE id=?",
               ('EXECUTED' if suppliers <= reported else 'CONFIRMED',commitment_id))
    parties = db.execute("SELECT org_id FROM commitment_parties WHERE commitment_id=?", (commitment_id,)).fetchall()
    append_ledger(db, "EXECUTION_RECORDED", "commitment", commitment_id, int(membership["org_id"]), {
        "actual_milli": actual_milli,
        "participant_org_ids": [int(p["org_id"]) for p in parties],
    })
    db.commit()


@_serialized_change
def accept_commitment(commitment_id: int, membership, note: str = "") -> None:
    if not access.can(membership, 'task.accept'):
        raise PermissionDenied('当前岗位不能验收企业共享任务。')
    db = get_db()
    commitment = get_commitment(commitment_id)
    if commitment is None:
        raise PlatformError("共享承诺不存在。")
    buyer = db.execute(
        "SELECT * FROM commitment_parties WHERE commitment_id=? AND org_id=? AND role='buyer'",
        (commitment_id, int(membership["org_id"])),
    ).fetchone()
    if buyer is None:
        raise PermissionDenied("只有需求方可以验收。")
    if commitment["status"] != "EXECUTED":
        raise ConflictError("只有已执行承诺可以验收。")
    db.execute(
        "INSERT INTO execution_events (commitment_id, event_type, qty_milli, note, actor_org_id, actor_user_id, created_at) VALUES (?, 'ACCEPTED', ?, ?, ?, ?, ?)",
        (commitment_id, sum(a["actual_milli"] for a in commitment_allocations(commitment_id)), note.strip(), int(membership["org_id"]), membership["user_id"], now_iso()),
    )
    db.execute("UPDATE commitments SET status='ACCEPTED', version=version+1 WHERE id=?", (commitment_id,))
    parties = db.execute("SELECT org_id FROM commitment_parties WHERE commitment_id=?", (commitment_id,)).fetchall()
    append_ledger(db, "ACCEPTANCE_RECORDED", "commitment", commitment_id, int(membership["org_id"]), {
        "participant_org_ids": [int(p["org_id"]) for p in parties],
    })
    db.commit()


@_serialized_change
def settle_commitment(commitment_id: int, membership) -> None:
    if not access.can(membership, 'settlement.write'):
        raise PermissionDenied("只有联盟协调员可以登记结算。")
    db = get_db()
    commitment = get_commitment(commitment_id)
    if commitment is None:
        raise PlatformError("共享承诺不存在。")
    if commitment["status"] != "ACCEPTED":
        raise ConflictError("只有已验收承诺可以结算。")
    allocations = commitment_allocations(commitment_id)
    total_actual_payment = 0
    total_actual_reward = 0
    for alloc in allocations:
        ratio = (int(alloc["actual_milli"]) / int(alloc["qty_milli"])) if int(alloc["qty_milli"]) else 0
        total_actual_payment += round(int(alloc["proposed_payment_cents"]) * ratio)
        total_actual_reward += round(int(alloc["proposed_reward_cents"]) * ratio)
    db.execute(
        "INSERT INTO settlements (commitment_id, stage, amount_cents, status, detail_json, created_at) VALUES (?, 'ACTUAL', ?, 'SIMULATED', ?, ?)",
        (commitment_id, total_actual_payment,
         _canonical({"actual_payment_cents": total_actual_payment,
                     "actual_reward_cents": total_actual_reward,
                     "broker_surplus_cents": total_actual_payment - total_actual_reward,
                     "note": "记账式模拟结算，不代表真实付款"}), now_iso()),
    )
    db.execute("UPDATE commitments SET status='ACCOUNTED', version=version+1 WHERE id=?", (commitment_id,))
    parties = db.execute("SELECT org_id FROM commitment_parties WHERE commitment_id=?", (commitment_id,)).fetchall()
    append_ledger(db, "SETTLEMENT_RECORDED", "commitment", commitment_id, int(membership["org_id"]), {
        "actual_payment_cents": total_actual_payment,
        "actual_reward_cents": total_actual_reward,
        "simulated": True,
        "participant_org_ids": [int(p["org_id"]) for p in parties],
    })
    db.commit()


def commitments_for_org(org_id: int):
    return list_commitments(type("M", (), {"org_id": org_id, "org_kind": "factory", "role": "planner"})())


def ledger_events_for(membership, limit: int = 200):
    visible_org_filter(membership)
    db = get_db()
    rows = db.execute("SELECT * FROM ledger_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    if is_alliance_member(membership):
        return rows
    org_id = int(membership["org_id"])
    visible = []
    for row in rows:
        if int(row["actor_org_id"] or -1) == org_id:
            visible.append(row)
            continue
        try:
            payload = json.loads(row["payload_json"])
        except Exception:
            payload = {}
        if int(payload.get("org_id", -1) or -1) == org_id or org_id in payload.get("participant_org_ids", []):
            visible.append(row)
    return visible


def ledger_summary(membership, limit: int = 8):
    return ledger_events_for(membership, limit=limit)


def platform_stats(membership):
    db = get_db()
    org_id = visible_org_filter(membership)
    if org_id is None:
        supplies = db.execute("SELECT * FROM resource_supplies").fetchall()
        demands = db.execute("SELECT * FROM demands").fetchall()
        commitments = db.execute("SELECT * FROM commitments").fetchall()
    else:
        supplies = db.execute("SELECT * FROM resource_supplies WHERE org_id=?", (org_id,)).fetchall()
        demands = db.execute("SELECT * FROM demands WHERE org_id=?", (org_id,)).fetchall()
        commitments = db.execute(
            """SELECT DISTINCT c.* FROM commitments c
               JOIN commitment_parties p ON p.commitment_id = c.id
               WHERE p.org_id = ?""", (org_id,)
        ).fetchall()
    available = sum(max(0, s["total_milli"] - s["reserved_milli"] - s["consumed_milli"]) for s in supplies)
    open_demands = sum(1 for d in demands if d["status"] in ("OPEN", "PLAN_READY"))
    pending = sum(1 for c in commitments if c["status"] == "RESERVED")
    active = sum(1 for c in commitments if c["status"] in ("RESERVED", "CONFIRMED", "EXECUTED", "ACCEPTED"))
    completed = sum(1 for c in commitments if c["status"] in ("ACCEPTED", "ACCOUNTED"))
    available_by_unit = {}
    for item in supplies:
        if item["status"] != "ACTIVE":
            continue
        unit = item["unit"] or "机时"
        available_by_unit[unit] = available_by_unit.get(unit, 0) + max(
            0, item["total_milli"] - item["reserved_milli"] - item["consumed_milli"])
    return {
        # available_milli is retained for older callers but is not meaningful
        # across different units; new UI shows unit-separated summaries.
        "available_milli": available,
        "available_by_unit": available_by_unit,
        "available_kinds": len({(s["capability"], s["unit"]) for s in supplies if s["status"] == "ACTIVE"}),
        "open_demands": open_demands,
        "pending_confirmations": pending,
        "active_commitments": active,
        "completed_commitments": completed,
        "supply_count": sum(1 for s in supplies if s["status"] == "ACTIVE"),
        "demand_count": len(demands),
        "commitment_count": len(commitments),
    }


def list_plans(membership, limit: int = 100):
    visible_org_filter(membership)
    db = get_db()
    rows = db.execute(
        """SELECT p.*, d.title AS demand_title, d.org_id AS demand_org_id, d.required_milli,
                  d.window_start, d.window_end, d.unit
           FROM plans p JOIN demands d ON d.id = p.demand_id
           ORDER BY p.created_at DESC, p.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    if is_alliance_member(membership):
        return rows
    org_id = int(membership["org_id"])
    result = []
    for row in rows:
        if int(row["demand_org_id"]) == org_id:
            result.append(plan_view(row, membership))
            continue
        try:
            allocs = json.loads(row["allocation_json"] or "[]")
        except Exception:
            allocs = []
        if any(int(item.get("supplier_org_id", -1)) == org_id for item in allocs):
            result.append(plan_view(row, membership))
    return result


def allocation_view(allocation, buyer, commitment=False):
    """Project negotiated fields to this party's side of the transaction."""
    result = dict(allocation)
    payment = 'proposed_payment_cents' if commitment else 'payment_cents'
    reward = 'proposed_reward_cents' if commitment else 'reward_cents'
    if buyer:
        result[reward] = None
    else:
        result[payment] = None
        result['unit_price_cents'] = None
    return result


def plan_view(plan, membership):
    if not can_view_plan(plan, membership):
        raise PermissionDenied('当前企业不是该方案的参与方。')
    if is_alliance_member(membership):
        return dict(plan)
    result = dict(plan)
    buyer = plan['demand_org_id'] == membership['org_id']
    allocations = json.loads(plan['allocation_json'] or '[]')
    if not buyer:
        allocations = [a for a in allocations if a['supplier_org_id']==membership['org_id']]
        result['required_milli'] = sum(a['qty_milli'] for a in allocations)
    result['proposed_payment_cents'] = sum(a['payment_cents'] for a in allocations) if buyer else None
    result['proposed_reward_cents'] = sum(a['reward_cents'] for a in allocations) if not buyer else None
    result['broker_surplus_cents'] = None
    result['allocation_json'] = _canonical([allocation_view(a, buyer) for a in allocations])
    return result


def can_view_plan(plan, membership) -> bool:
    if plan is None or not access.valid_member(membership):
        return False
    if is_alliance_member(membership):
        return True
    org_id = int(membership["org_id"])
    if int(plan["demand_org_id"]) == org_id:
        return True
    try:
        allocs = json.loads(plan["allocation_json"] or "[]")
    except Exception:
        return False
    return any(int(item.get("supplier_org_id", -1)) == org_id for item in allocs)


def can_view_commitment(commitment_id: int, membership) -> bool:
    if not access.valid_member(membership):
        return False
    if is_alliance_member(membership):
        return True
    party = get_db().execute(
        "SELECT 1 FROM commitment_parties WHERE commitment_id=? AND org_id=?",
        (commitment_id, int(membership["org_id"])),
    ).fetchone()
    return party is not None


def export_bundle(membership) -> dict:
    """Export a reproducible JSON bundle for the current visible scope."""
    if not access.can(membership, 'trace.export'):
        raise PermissionDenied('当前岗位不能导出联盟业务与审计数据。')
    db = get_db()
    supplies = list_supplies(membership, limit=1000)
    demands = list_demands(membership, limit=1000)
    plans = list_plans(membership, limit=1000)
    commitments = list_commitments(membership, limit=1000)
    events = ledger_events_for(membership, limit=1000)
    commitment_ids = [int(item["id"]) for item in commitments]
    parties = []
    allocations = []
    executions = []
    settlements = []
    if commitment_ids:
        placeholders = ",".join("?" for _ in commitment_ids)
        parties = db.execute(
            f"SELECT * FROM commitment_parties WHERE commitment_id IN ({placeholders}) ORDER BY id",
            commitment_ids,
        ).fetchall()
        allocations = db.execute(
            f"SELECT * FROM commitment_allocations WHERE commitment_id IN ({placeholders}) ORDER BY id",
            commitment_ids,
        ).fetchall()
        executions = db.execute(
            f"SELECT * FROM execution_events WHERE commitment_id IN ({placeholders}) ORDER BY id",
            commitment_ids,
        ).fetchall()
        settlements = db.execute(
            f"SELECT * FROM settlements WHERE commitment_id IN ({placeholders}) ORDER BY id",
            commitment_ids,
        ).fetchall()

    def rows(items):
        return [dict(item) for item in items]

    return {
        "schema_version": "platform-s1-0.2",
        "generated_at": now_iso(),
        "business_data_source": "synthetic",
        "ledger_mode": "DEMO_HASH_CHAIN",
        "ledger_note": "Not Hyperledger Fabric; demo hash chain only.",
        "organization_scope": int(membership["org_id"]),
        "supplies": rows(supplies),
        "demands": rows(demands),
        "plans": rows(plans),
        "commitments": rows(commitments),
        "commitment_parties": rows(parties),
        "commitment_allocations": rows(allocations),
        "execution_events": rows(executions),
        "settlements": rows(settlements),
        "ledger_events": rows(events),
    }


def register_factory(*, company_name: str, location: str, username: str, display_name: str,
                     password: str, role: str) -> tuple[int, int]:
    """Register a new independent factory and its first user.

    This is a controlled demo registration (the route also checks an invite
    code). It does not perform email verification or alliance approval.
    """
    company_name = (company_name or "").strip()
    location = (location or "").strip()
    username = (username or "").strip().lower()
    display_name = (display_name or "").strip()
    if len(company_name) < 2 or len(company_name) > 80:
        raise PlatformError("企业名称需为 2–80 个字符。")
    if len(username) < 3 or len(username) > 30 or not all(ch.isalnum() or ch == "_" for ch in username):
        raise PlatformError("账号需为 3–30 位字母、数字或下划线。")
    if not display_name:
        raise PlatformError("请填写操作人姓名。")
    if len(password) < 8:
        raise PlatformError("密码至少 8 位。")
    if role not in ("factory_admin", "planner", "resource_manager"):
        raise PlatformError("工厂角色不合法。")
    db = get_db()
    if db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        raise PlatformError("该账号已存在。")
    stamp = now_iso()
    org_code = "ORG_" + secrets.token_hex(4).upper()
    try:
        db.execute("BEGIN IMMEDIATE")
        org_cur = db.execute(
            "INSERT INTO organizations (code, name, kind, location, created_at) VALUES (?, ?, 'factory', ?, ?)",
            (org_code, company_name, location, stamp),
        )
        org_id = org_cur.lastrowid
        user_cur = db.execute(
            """INSERT INTO users (username, password_hash, display_name, organization, role, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (username, generate_password_hash(password), display_name, company_name, role, stamp),
        )
        user_id = user_cur.lastrowid
        db.execute(
            "INSERT INTO user_org (user_id, org_id, role, is_primary) VALUES (?, ?, ?, 1)",
            (user_id, org_id, role),
        )
        append_ledger(db, "ORG_REGISTERED", "organization", org_id, org_id, {
            "org_id": org_id,
            "name": company_name,
            "location": location,
            "role": role,
            "data_source": "manual_registration",
        })
        db.commit()
        return user_id, org_id
    except Exception:
        db.rollback()
        raise
