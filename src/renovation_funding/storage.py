"""联合资金分摊服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS funding_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('clerk','fund_manager','auditor','family')),
    household_id TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS households (
    household_id TEXT PRIMARY KEY,
    head_name TEXT NOT NULL,
    household_type TEXT NOT NULL CHECK(household_type IN ('dibao','tekun','tuopin','general')),
    village TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES funding_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fund_batches (
    batch_id TEXT PRIMARY KEY,
    source TEXT NOT NULL CHECK(source IN ('central','provincial','county')),
    title TEXT NOT NULL,
    total_amount TEXT NOT NULL,
    available_amount TEXT NOT NULL,
    frozen_amount TEXT NOT NULL DEFAULT '0',
    spent_amount TEXT NOT NULL DEFAULT '0',
    carried_out TEXT NOT NULL DEFAULT '0',
    household_types_json TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    per_household_cap TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','carried','closed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES funding_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batches_validity
ON fund_batches(state, valid_to, batch_id);

CREATE TABLE IF NOT EXISTS allocation_rules (
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    source_order_json TEXT NOT NULL,
    source_caps_json TEXT NOT NULL,
    household_residual INTEGER NOT NULL CHECK(household_residual IN (0,1)),
    note TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','active','retired')),
    created_by TEXT NOT NULL REFERENCES funding_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(rule_id, version)
);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(household_id),
    household_type TEXT NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('rebuild','reinforce','repair')),
    estimated_cost TEXT NOT NULL,
    address TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'registered'
        CHECK(state IN ('registered','confirmed','completed','partially_completed','failed','cancelled')),
    rule_id TEXT,
    rule_version INTEGER,
    idempotency_key TEXT NOT NULL UNIQUE,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES funding_users(user_id),
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    settled_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_projects_household
ON projects(household_id, state);

CREATE TABLE IF NOT EXISTS allocations (
    allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    version INTEGER NOT NULL CHECK(version > 0),
    rule_id TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    input_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','superseded','settled')),
    created_by TEXT NOT NULL REFERENCES funding_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(project_id, version)
);

CREATE TABLE IF NOT EXISTS allocation_lines (
    line_id INTEGER PRIMARY KEY AUTOINCREMENT,
    allocation_id INTEGER NOT NULL REFERENCES allocations(allocation_id),
    seq INTEGER NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('central','provincial','county','household')),
    batch_id TEXT REFERENCES fund_batches(batch_id),
    amount TEXT NOT NULL,
    written_off TEXT NOT NULL DEFAULT '0',
    released TEXT NOT NULL DEFAULT '0',
    explanation TEXT NOT NULL,
    UNIQUE(allocation_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_allocation_lines
ON allocation_lines(allocation_id, seq);

CREATE TABLE IF NOT EXISTS settlements (
    settlement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL UNIQUE REFERENCES projects(project_id),
    outcome TEXT NOT NULL CHECK(outcome IN ('completed','partial','failed','cancelled')),
    accepted_amount TEXT NOT NULL,
    written_total TEXT NOT NULL,
    released_total TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES funding_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS adjustments (
    adjustment_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    base_version INTEGER NOT NULL,
    lines_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','approved','rejected')),
    proposed_by TEXT NOT NULL REFERENCES funding_users(user_id),
    proposed_at TEXT NOT NULL,
    reviewed_by TEXT REFERENCES funding_users(user_id),
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS carry_forwards (
    carry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_batch_id TEXT NOT NULL REFERENCES fund_batches(batch_id),
    to_batch_id TEXT NOT NULL REFERENCES fund_batches(batch_id),
    amount TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES funding_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS funding_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS funding_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_funding_audit_entity
ON funding_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
