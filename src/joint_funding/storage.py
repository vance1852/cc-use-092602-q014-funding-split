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
    role TEXT NOT NULL CHECK(role IN ('clerk','fund_manager','household','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fund_batches (
    batch_id TEXT PRIMARY KEY,
    source TEXT NOT NULL CHECK(source IN ('central','provincial','county')),
    name TEXT NOT NULL,
    total_cny TEXT NOT NULL,
    frozen_cny TEXT NOT NULL DEFAULT '0.00',
    used_cny TEXT NOT NULL DEFAULT '0.00',
    household_types_json TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    per_household_cap_cny TEXT NOT NULL,
    priority INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','closed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES funding_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batches_source
ON fund_batches(source, priority, valid_from);

CREATE TABLE IF NOT EXISTS allocation_rules (
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','superseded')),
    created_by TEXT NOT NULL REFERENCES funding_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(rule_id, version)
);

CREATE TABLE IF NOT EXISTS renovation_projects (
    project_id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL,
    household_type TEXT NOT NULL,
    scope TEXT NOT NULL,
    estimated_cost_cny TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft'
        CHECK(state IN ('draft','confirmed','completed','completed_partial','cancelled','failed')),
    rule_id TEXT,
    rule_version INTEGER,
    active_allocation_version INTEGER,
    failure_reason TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES funding_users(user_id),
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS project_allocations (
    project_id TEXT NOT NULL REFERENCES renovation_projects(project_id),
    version INTEGER NOT NULL CHECK(version > 0),
    kind TEXT NOT NULL CHECK(kind IN ('rule','manual')),
    rule_id TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    lines_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    public_total_cny TEXT NOT NULL,
    household_share_cny TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','superseded')),
    proposed_by TEXT NOT NULL REFERENCES funding_users(user_id),
    approved_by TEXT REFERENCES funding_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, version)
);

CREATE TABLE IF NOT EXISTS adjustments (
    adjustment_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES renovation_projects(project_id),
    lines_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','approved','rejected')),
    proposed_by TEXT NOT NULL REFERENCES funding_users(user_id),
    decided_by TEXT REFERENCES funding_users(user_id),
    decided_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settlements (
    settlement_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES renovation_projects(project_id),
    request_sha256 TEXT NOT NULL,
    actual_cost_cny TEXT NOT NULL,
    remainder_policy TEXT NOT NULL CHECK(remainder_policy IN ('release','carry_forward')),
    outcome TEXT NOT NULL CHECK(outcome IN ('completed','completed_partial')),
    lines_json TEXT NOT NULL,
    remainders_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES funding_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_settlements_project
ON settlements(project_id, created_at);

CREATE TABLE IF NOT EXISTS fund_transfers (
    transfer_id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_batch_id TEXT NOT NULL REFERENCES fund_batches(batch_id),
    to_batch_id TEXT NOT NULL REFERENCES fund_batches(batch_id),
    amount_cny TEXT NOT NULL,
    reason TEXT NOT NULL,
    project_id TEXT,
    settlement_id TEXT,
    created_by TEXT NOT NULL REFERENCES funding_users(user_id),
    created_at TEXT NOT NULL
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
