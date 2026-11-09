"""SQLite persistence and demo-account seeding for the web prototype."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app, g
from werkzeug.security import generate_password_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    organization TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'reviewer',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    resource_type TEXT NOT NULL DEFAULT '',
    unit_label TEXT NOT NULL DEFAULT '单位',
    status TEXT NOT NULL DEFAULT 'completed',
    is_sample INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    summary_json TEXT NOT NULL DEFAULT '{}',
    chart_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE INDEX IF NOT EXISTS idx_experiments_user_created
    ON experiments (user_id, created_at DESC);
"""

DEMO_ACCOUNTS = (
    {
        "username": "reviewer",
        "password_env": "WEB_REVIEWER_PASSWORD",
        "default_password": "Review2026!",
        "display_name": "联盟链验证员",
        "organization": "同行评议演示",
        "role": "reviewer",
    },
    {
        "username": "demo",
        "password_env": "WEB_DEMO_PASSWORD",
        "default_password": "Demo2026!",
        "display_name": "计划员",
        "organization": "广州精工制造有限公司",
        "role": "enterprise",
    },
    {
        "username": "admin",
        "password_env": "WEB_ADMIN_PASSWORD",
        "default_password": "Admin2026!",
        "display_name": "系统管理员",
        "organization": "BidShare Lab",
        "role": "admin",
    },
)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
            timeout=15,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")
    return g.db


def close_db(_error=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = get_db()
    db.executescript(SCHEMA)
    db.commit()


def seed_demo_users() -> None:
    """Create reviewer/demo/admin accounts once; env vars can override secrets."""
    db = get_db()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for account in DEMO_ACCOUNTS:
        password = os.environ.get(account["password_env"], account["default_password"])
        row = db.execute(
            "SELECT id FROM users WHERE username = ?", (account["username"],)
        ).fetchone()
        if row is None:
            db.execute(
                """
                INSERT INTO users
                    (username, password_hash, display_name, organization, role, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    account["username"],
                    generate_password_hash(password),
                    account["display_name"],
                    account["organization"],
                    account["role"],
                    now,
                ),
            )
        elif os.environ.get(account["password_env"]):
            # Explicit deployment secrets take effect on restart.
            db.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (generate_password_hash(password), account["username"]),
            )
    db.commit()


def get_user_by_id(user_id: int):
    return get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_username(username: str):
    return get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def init_app(app) -> None:
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    app.teardown_appcontext(close_db)


def insert_experiment(*, user_id, config, result, charts, status="completed", is_sample=False,
                      error_message=None):
    db = get_db()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    summary = result.get("summary", {}) if result else {}
    cursor = db.execute(
        """
        INSERT INTO experiments
            (user_id, name, resource_type, unit_label, status, is_sample,
             config_json, result_json, summary_json, chart_json, error_message,
             duration_ms, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            config.get("name", "未命名实验"),
            config.get("resource_type", ""),
            config.get("unit_label", "单位"),
            status,
            1 if is_sample else 0,
            _json_dumps(config),
            _json_dumps(result or {}),
            _json_dumps(summary),
            _json_dumps(charts or {}),
            error_message,
            int(summary.get("duration_ms", 0)),
            now,
        ),
    )
    db.commit()
    return cursor.lastrowid


def update_experiment_result(experiment_id, result, charts, status="completed", error_message=None):
    db = get_db()
    summary = result.get("summary", {}) if result else {}
    db.execute(
        """
        UPDATE experiments
           SET result_json = ?, summary_json = ?, chart_json = ?, status = ?,
               error_message = ?, duration_ms = ?
         WHERE id = ?
        """,
        (
            _json_dumps(result or {}),
            _json_dumps(summary),
            _json_dumps(charts or {}),
            status,
            error_message,
            int(summary.get("duration_ms", 0)),
            experiment_id,
        ),
    )
    db.commit()


def _json_dumps(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
