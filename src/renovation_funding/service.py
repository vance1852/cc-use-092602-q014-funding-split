"""危房改造联合资金分摊的应用服务。

覆盖资金批次登记、分摊规则版本、项目确认冻结、按实际验收量核销、
取消与失败释放、到期批次结转、双人复核人工调整，以及按角色裁剪的
授权明细视图。所有写操作都在单个事务内完成并记录哈希链审计事件。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    SOURCE_LABELS,
    AdjustmentInput,
    CarryForwardInput,
    FundBatchInput,
    HouseholdInput,
    ProjectInput,
    RuleInput,
    SettlementInput,
    identifier,
)
from .planning import (
    BatchSpec,
    InsufficientFunds,
    RuleSpec,
    canonical_json,
    compute_allocation,
    decimal_text,
    digest,
    distribute_writeoff,
    quantize_money,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "clerk": {"household.write", "project.write", "project.read"},
    "fund_manager": {
        "batch.write", "batch.read", "rule.write", "rule.read",
        "project.read", "project.confirm", "project.settle",
        "adjustment.propose", "adjustment.review", "carryforward.write",
        "allocation.read",
    },
    "auditor": {"audit.read", "batch.read", "rule.read", "project.read", "allocation.read"},
    "family": {"own.read"},
}

OUTCOME_STATES = {
    "completed": "completed",
    "partial": "partially_completed",
    "failed": "failed",
    "cancelled": "cancelled",
}

FULL_DETAIL_ROLES = {"fund_manager", "auditor"}


class FundingService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _today(self) -> date:
        return self.clock.now().date()

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM funding_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM funding_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO funding_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def _idempotent_replay(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM funding_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if stored is None:
            return None
        if stored["request_sha256"] != request_digest:
            raise Conflict("相同编号对应不同请求内容")
        return json.loads(stored["response_json"])

    def _store_idempotent(
        self, scope: str, key: str, request_digest: str, response: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO funding_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (scope, key, request_digest, canonical_json(response), self._now()),
        )

    # ------------------------------------------------------------------
    # 用户与家庭户
    # ------------------------------------------------------------------

    def create_user(
        self, user_id: str, display_name: str, role: str, household_id: str | None = None
    ) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        if role == "family" and not household_id:
            raise ValidationFailed("家庭角色必须绑定 household_id")
        if role != "family" and household_id is not None:
            raise ValidationFailed("只有家庭角色可以绑定 household_id")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO funding_users(user_id,display_name,role,household_id,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, household_id, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def register_household(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "household.write")
        household = HouseholdInput.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO households(household_id,head_name,household_type,village,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        household.household_id,
                        household.head_name,
                        household.household_type,
                        household.village,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "household", household.household_id, "household.registered", actor_id,
                    {"household_type": household.household_type, "village": household.village},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("家庭户编号已经存在") from exc
        return {"household_id": household.household_id, "household_type": household.household_type}

    # ------------------------------------------------------------------
    # 资金批次
    # ------------------------------------------------------------------

    def register_batch(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "batch.write")
        batch = FundBatchInput.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO fund_batches(batch_id,source,title,total_amount,available_amount,"
                    "household_types_json,scopes_json,valid_from,valid_to,per_household_cap,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        batch.batch_id,
                        batch.source,
                        batch.title,
                        decimal_text(batch.total_amount),
                        decimal_text(batch.total_amount),
                        canonical_json(list(batch.household_types)),
                        canonical_json(list(batch.scopes)),
                        batch.valid_from,
                        batch.valid_to,
                        decimal_text(batch.per_household_cap),
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "batch", batch.batch_id, "batch.registered", actor_id,
                    {"source": batch.source, "total_amount": decimal_text(batch.total_amount)},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("资金批次编号已经存在") from exc
        return self.get_batch(actor_id, batch.batch_id)

    def _batch_row(self, batch_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM fund_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFound("资金批次不存在")
        return row

    @staticmethod
    def _batch_view(row: sqlite3.Row) -> dict[str, Any]:
        money = lambda key: decimal_text(quantize_money(Decimal(row[key])))  # noqa: E731
        return {
            "batch_id": row["batch_id"],
            "source": row["source"],
            "source_label": SOURCE_LABELS[row["source"]],
            "title": row["title"],
            "total_amount": money("total_amount"),
            "available_amount": money("available_amount"),
            "frozen_amount": money("frozen_amount"),
            "spent_amount": money("spent_amount"),
            "carried_out": money("carried_out"),
            "household_types": json.loads(row["household_types_json"]),
            "scopes": json.loads(row["scopes_json"]),
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "per_household_cap": money("per_household_cap"),
            "state": row["state"],
            "revision": row["revision"],
        }

    def get_batch(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        self._require(actor_id, "batch.read")
        return self._batch_view(self._batch_row(batch_id))

    def list_batches(self, actor_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "batch.read")
        rows = self.connection.execute(
            "SELECT * FROM fund_batches ORDER BY valid_to, batch_id"
        ).fetchall()
        return [self._batch_view(row) for row in rows]

    @staticmethod
    def _batch_spec(row: sqlite3.Row) -> BatchSpec:
        return BatchSpec(
            batch_id=row["batch_id"],
            source=row["source"],
            available=Decimal(row["available_amount"]),
            per_household_cap=Decimal(row["per_household_cap"]),
            household_types=frozenset(json.loads(row["household_types_json"])),
            scopes=frozenset(json.loads(row["scopes_json"])),
            valid_from=date.fromisoformat(row["valid_from"]),
            valid_to=date.fromisoformat(row["valid_to"]),
        )

    def _update_batch(
        self,
        row: sqlite3.Row,
        *,
        available: Decimal,
        frozen: Decimal,
        spent: Decimal,
        carried_out: Decimal,
    ) -> None:
        if min(available, frozen, spent, carried_out) < 0:
            raise Conflict("资金批次余额不能为负数")
        cursor = self.connection.execute(
            "UPDATE fund_batches SET available_amount=?,frozen_amount=?,spent_amount=?,"
            "carried_out=?,revision=revision+1 WHERE batch_id=? AND revision=?",
            (
                decimal_text(quantize_money(available)),
                decimal_text(quantize_money(frozen)),
                decimal_text(quantize_money(spent)),
                decimal_text(quantize_money(carried_out)),
                row["batch_id"],
                row["revision"],
            ),
        )
        if cursor.rowcount != 1:
            raise Conflict("资金批次版本冲突")

    # ------------------------------------------------------------------
    # 分摊规则（版本化，修订不回写已确认项目）
    # ------------------------------------------------------------------

    def create_rule(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "rule.write")
        rule = RuleInput.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self._insert_rule(rule, version=1, actor_id=actor_id)
                self._audit("rule", rule.rule_id, "rule.created", actor_id, {"version": 1})
        except sqlite3.IntegrityError as exc:
            raise Conflict("分摊规则已经存在，请使用修订生成新版本") from exc
        return self.get_rule(actor_id, rule.rule_id, version=1)

    def _insert_rule(self, rule: RuleInput, *, version: int, actor_id: str) -> None:
        self.connection.execute(
            "INSERT INTO allocation_rules(rule_id,version,source_order_json,source_caps_json,"
            "household_residual,note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                rule.rule_id,
                version,
                canonical_json(list(rule.source_order)),
                canonical_json({key: decimal_text(value) for key, value in rule.source_caps.items()}),
                1 if rule.household_residual else 0,
                rule.note,
                actor_id,
                self._now(),
            ),
        )

    def _rule_row(self, rule_id: str, version: int | None = None) -> sqlite3.Row:
        if version is None:
            row = self.connection.execute(
                "SELECT * FROM allocation_rules WHERE rule_id=? ORDER BY version DESC LIMIT 1",
                (rule_id,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM allocation_rules WHERE rule_id=? AND version=?",
                (rule_id, version),
            ).fetchone()
        if row is None:
            raise NotFound("分摊规则不存在")
        return row

    @staticmethod
    def _rule_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "rule_id": row["rule_id"],
            "version": row["version"],
            "source_order": json.loads(row["source_order_json"]),
            "source_caps": json.loads(row["source_caps_json"]),
            "household_residual": bool(row["household_residual"]),
            "note": row["note"],
            "state": row["state"],
        }

    @staticmethod
    def _rule_spec(row: sqlite3.Row) -> RuleSpec:
        return RuleSpec(
            rule_id=row["rule_id"],
            version=row["version"],
            source_order=tuple(json.loads(row["source_order_json"])),
            source_caps={
                key: Decimal(value)
                for key, value in json.loads(row["source_caps_json"]).items()
            },
            household_residual=bool(row["household_residual"]),
        )

    def get_rule(self, actor_id: str, rule_id: str, version: int | None = None) -> dict[str, Any]:
        self._require(actor_id, "rule.read")
        return self._rule_view(self._rule_row(rule_id, version))

    def activate_rule(self, actor_id: str, rule_id: str, version: int) -> dict[str, Any]:
        self._require(actor_id, "rule.write")
        with transaction(self.connection, immediate=True):
            row = self._rule_row(rule_id, version)
            if row["state"] != "draft":
                raise InvalidState("只有草稿版本可以启用")
            self.connection.execute(
                "UPDATE allocation_rules SET state='retired' WHERE rule_id=? AND state='active'",
                (rule_id,),
            )
            self.connection.execute(
                "UPDATE allocation_rules SET state='active' WHERE rule_id=? AND version=?",
                (rule_id, version),
            )
            self._audit("rule", rule_id, "rule.activated", actor_id, {"version": version})
        return self.get_rule(actor_id, rule_id, version)

    def revise_rule(self, actor_id: str, rule_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """基于当前启用版本生成新的草稿版本；已确认项目仍引用旧版本，不回写。"""
        self._require(actor_id, "rule.write")
        active = self.connection.execute(
            "SELECT * FROM allocation_rules WHERE rule_id=? AND state='active' "
            "ORDER BY version DESC LIMIT 1",
            (rule_id,),
        ).fetchone()
        if active is None:
            raise InvalidState("只有存在已启用版本的规则可以修订")
        merged: dict[str, Any] = {
            "rule_id": rule_id,
            "source_order": json.loads(active["source_order_json"]),
            "source_caps": json.loads(active["source_caps_json"]),
            "household_residual": bool(active["household_residual"]),
            "note": active["note"],
        }
        for key in ("source_order", "source_caps", "household_residual", "note"):
            if key in raw and raw[key] is not None:
                merged[key] = raw[key]
        rule = RuleInput.from_dict(merged)
        latest = self._rule_row(rule_id)
        version = latest["version"] + 1
        with transaction(self.connection, immediate=True):
            self._insert_rule(rule, version=version, actor_id=actor_id)
            self._audit(
                "rule", rule_id, "rule.revised", actor_id,
                {"version": version, "base_version": active["version"]},
            )
        return self.get_rule(actor_id, rule_id, version)

    # ------------------------------------------------------------------
    # 改造项目登记与确认
    # ------------------------------------------------------------------

    def register_project(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "project.write")
        project = ProjectInput.from_dict(raw)
        request_digest = digest({"action": "register_project", "request": dict(raw)})
        replay = self._idempotent_replay("project", project.idempotency_key, request_digest)
        if replay is not None:
            return replay
        household = self.connection.execute(
            "SELECT * FROM households WHERE household_id=?", (project.household_id,)
        ).fetchone()
        if household is None:
            raise NotFound("家庭户不存在")
        response = {
            "project_id": project.project_id,
            "household_id": project.household_id,
            "state": "registered",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO projects(project_id,household_id,household_type,scope,estimated_cost,"
                    "address,idempotency_key,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        project.project_id,
                        project.household_id,
                        household["household_type"],
                        project.scope,
                        decimal_text(quantize_money(project.estimated_cost)),
                        project.address,
                        project.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self._store_idempotent("project", project.idempotency_key, request_digest, response)
                self._audit(
                    "project", project.project_id, "project.registered", actor_id,
                    {"household_id": project.household_id, "scope": project.scope},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("项目编号或幂等键冲突") from exc
        return response

    def _project_row(self, project_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise NotFound("改造项目不存在")
        return row

    def _active_allocation(self, project_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM allocations WHERE project_id=? AND state='active' "
            "ORDER BY version DESC LIMIT 1",
            (project_id,),
        ).fetchone()

    def _allocation_lines(self, allocation_id: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM allocation_lines WHERE allocation_id=? ORDER BY seq",
            (allocation_id,),
        ).fetchall()

    def confirm_project(self, actor_id: str, project_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """确认项目：按当前启用规则版本计算分摊并冻结各批次额度。"""
        self._require(actor_id, "project.confirm")
        rule_id = identifier(raw.get("rule_id"), "rule_id")
        key = identifier(raw.get("idempotency_key"), "idempotency_key")
        request_digest = digest(
            {"action": "confirm_project", "project_id": project_id, "request": dict(raw)}
        )
        replay = self._idempotent_replay("confirm", key, request_digest)
        if replay is not None:
            return replay
        with transaction(self.connection, immediate=True):
            project = self._project_row(project_id)
            if project["state"] != "registered":
                raise InvalidState("项目已确认或已办结，不能重复确认")
            rule_row = self.connection.execute(
                "SELECT * FROM allocation_rules WHERE rule_id=? AND state='active' "
                "ORDER BY version DESC LIMIT 1",
                (rule_id,),
            ).fetchone()
            if rule_row is None:
                raise InvalidState("分摊规则没有已启用版本")
            as_of = self._today()
            batch_rows = self.connection.execute(
                "SELECT * FROM fund_batches WHERE state='active' ORDER BY valid_to, batch_id"
            ).fetchall()
            specs = [self._batch_spec(row) for row in batch_rows]
            try:
                lines = compute_allocation(
                    estimated_cost=Decimal(project["estimated_cost"]),
                    household_type=project["household_type"],
                    scope=project["scope"],
                    as_of=as_of,
                    rule=self._rule_spec(rule_row),
                    batches=specs,
                )
            except InsufficientFunds as exc:
                raise InvalidState(str(exc)) from exc
            input_sha256 = digest(
                {
                    "project": {
                        "project_id": project["project_id"],
                        "household_type": project["household_type"],
                        "scope": project["scope"],
                        "estimated_cost": project["estimated_cost"],
                    },
                    "rule": self._rule_view(rule_row),
                    "batches": [self._batch_view(row) for row in batch_rows],
                    "as_of": as_of.isoformat(),
                }
            )
            cursor = self.connection.execute(
                "INSERT INTO allocations(project_id,version,rule_id,rule_version,input_sha256,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    project_id,
                    1,
                    rule_row["rule_id"],
                    rule_row["version"],
                    input_sha256,
                    actor_id,
                    self._now(),
                ),
            )
            allocation_id = int(cursor.lastrowid)
            batch_map = {row["batch_id"]: row for row in batch_rows}
            response_lines: list[dict[str, Any]] = []
            frozen_total = Decimal("0")
            household_commitment = Decimal("0")
            for line in lines:
                self.connection.execute(
                    "INSERT INTO allocation_lines(allocation_id,seq,source,batch_id,amount,explanation) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        allocation_id,
                        line.seq,
                        line.source,
                        line.batch_id,
                        decimal_text(line.amount),
                        line.explanation,
                    ),
                )
                if line.batch_id is None:
                    household_commitment += line.amount
                else:
                    batch_row = batch_map[line.batch_id]
                    available = Decimal(batch_row["available_amount"])
                    if available < line.amount:
                        raise Conflict(f"批次 {line.batch_id} 可用余额不足")
                    self._update_batch(
                        batch_row,
                        available=available - line.amount,
                        frozen=Decimal(batch_row["frozen_amount"]) + line.amount,
                        spent=Decimal(batch_row["spent_amount"]),
                        carried_out=Decimal(batch_row["carried_out"]),
                    )
                    batch_map[line.batch_id] = self._batch_row(line.batch_id)
                    frozen_total += line.amount
                response_lines.append(
                    {
                        "seq": line.seq,
                        "source": line.source,
                        "source_label": SOURCE_LABELS[line.source],
                        "batch_id": line.batch_id,
                        "amount": decimal_text(line.amount),
                        "explanation": line.explanation,
                    }
                )
            updated = self.connection.execute(
                "UPDATE projects SET state='confirmed',rule_id=?,rule_version=?,confirmed_at=?,"
                "revision=revision+1 WHERE project_id=? AND state='registered'",
                (rule_row["rule_id"], rule_row["version"], self._now(), project_id),
            )
            if updated.rowcount != 1:
                raise InvalidState("项目状态已变化，不能确认")
            response = {
                "project_id": project_id,
                "state": "confirmed",
                "allocation_id": allocation_id,
                "version": 1,
                "rule_id": rule_row["rule_id"],
                "rule_version": rule_row["version"],
                "input_sha256": input_sha256,
                "total_frozen": decimal_text(quantize_money(frozen_total)),
                "household_commitment": decimal_text(quantize_money(household_commitment)),
                "lines": response_lines,
            }
            self._store_idempotent("confirm", key, request_digest, response)
            self._audit(
                "project", project_id, "project.confirmed", actor_id,
                {
                    "allocation_id": allocation_id,
                    "rule_id": rule_row["rule_id"],
                    "rule_version": rule_row["version"],
                    "input_sha256": input_sha256,
                },
            )
        return response

    # ------------------------------------------------------------------
    # 核销：完工按实际验收量核销，取消与失败释放，部分完成核销加释放
    # ------------------------------------------------------------------

    def settle_project(self, actor_id: str, project_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "project.settle")
        request = SettlementInput.from_dict(raw)
        request_digest = digest(
            {"action": "settle_project", "project_id": project_id, "request": dict(raw)}
        )
        replay = self._idempotent_replay("settlement", request.idempotency_key, request_digest)
        if replay is not None:
            return replay
        accepted = quantize_money(request.accepted_amount)
        with transaction(self.connection, immediate=True):
            project = self._project_row(project_id)
            if project["state"] != "confirmed":
                raise InvalidState("项目未确认或已办结，不能核销")
            allocation = self._active_allocation(project_id)
            if allocation is None:
                raise InvalidState("项目没有有效分摊记录")
            lines = self._allocation_lines(allocation["allocation_id"])
            remaining_frozen = [
                (
                    line["seq"],
                    Decimal(line["amount"]) - Decimal(line["written_off"]) - Decimal(line["released"]),
                )
                for line in lines
            ]
            frozen_total = sum((frozen for _, frozen in remaining_frozen), Decimal("0"))
            if request.outcome == "completed" and accepted < frozen_total:
                raise ValidationFailed("完工核销的验收金额不能低于冻结总额，部分验收请使用 partial")
            if request.outcome == "partial" and not Decimal("0") < accepted < frozen_total:
                raise ValidationFailed("部分完成的验收金额必须大于零且低于冻结总额")
            if request.outcome in {"failed", "cancelled"} and accepted != Decimal("0"):
                raise ValidationFailed("取消或失败的项目验收金额必须为零")
            writeoff, household_extra = distribute_writeoff(remaining_frozen, accepted)
            changes = {item["seq"]: item for item in writeoff}
            line_views: list[dict[str, Any]] = []
            written_total = Decimal("0")
            released_total = Decimal("0")
            for line in lines:
                change = changes[line["seq"]]
                write = Decimal(str(change["write_off"]))
                release = Decimal(str(change["release"]))
                self.connection.execute(
                    "UPDATE allocation_lines SET written_off=?,released=? WHERE line_id=?",
                    (
                        decimal_text(quantize_money(Decimal(line["written_off"]) + write)),
                        decimal_text(quantize_money(Decimal(line["released"]) + release)),
                        line["line_id"],
                    ),
                )
                if line["batch_id"] is not None:
                    batch_row = self._batch_row(line["batch_id"])
                    self._update_batch(
                        batch_row,
                        available=Decimal(batch_row["available_amount"]) + release,
                        frozen=Decimal(batch_row["frozen_amount"]) - write - release,
                        spent=Decimal(batch_row["spent_amount"]) + write,
                        carried_out=Decimal(batch_row["carried_out"]),
                    )
                written_total += write
                released_total += release
                line_views.append(
                    {
                        "seq": line["seq"],
                        "source": line["source"],
                        "source_label": SOURCE_LABELS[line["source"]],
                        "batch_id": line["batch_id"],
                        "write_off": decimal_text(quantize_money(write)),
                        "release": decimal_text(quantize_money(release)),
                    }
                )
            self.connection.execute(
                "UPDATE allocations SET state='settled' WHERE allocation_id=?",
                (allocation["allocation_id"],),
            )
            new_state = OUTCOME_STATES[request.outcome]
            self.connection.execute(
                "UPDATE projects SET state=?,settled_at=?,revision=revision+1 WHERE project_id=?",
                (new_state, self._now(), project_id),
            )
            response = {
                "project_id": project_id,
                "state": new_state,
                "outcome": request.outcome,
                "accepted_amount": decimal_text(accepted),
                "written_total": decimal_text(quantize_money(written_total)),
                "released_total": decimal_text(quantize_money(released_total)),
                "household_extra": decimal_text(quantize_money(household_extra)),
                "lines": line_views,
            }
            self.connection.execute(
                "INSERT INTO settlements(project_id,outcome,accepted_amount,written_total,"
                "released_total,idempotency_key,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    project_id,
                    request.outcome,
                    decimal_text(accepted),
                    response["written_total"],
                    response["released_total"],
                    request.idempotency_key,
                    canonical_json(response),
                    actor_id,
                    self._now(),
                ),
            )
            self._store_idempotent(
                "settlement", request.idempotency_key, request_digest, response
            )
            self._audit(
                "project", project_id, "project.settled", actor_id,
                {
                    "outcome": request.outcome,
                    "accepted_amount": decimal_text(accepted),
                    "written_total": response["written_total"],
                    "released_total": response["released_total"],
                },
            )
        return response

    # ------------------------------------------------------------------
    # 人工调整：双人复核，批准后形成新的分摊版本
    # ------------------------------------------------------------------

    def propose_adjustment(
        self, actor_id: str, project_id: str, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require(actor_id, "adjustment.propose")
        proposal = AdjustmentInput.from_dict(raw)
        with transaction(self.connection, immediate=True):
            project = self._project_row(project_id)
            if project["state"] != "confirmed":
                raise InvalidState("只有已确认未办结的项目可以人工调整")
            allocation = self._active_allocation(project_id)
            if allocation is None:
                raise InvalidState("项目没有有效分摊记录")
            lines = self._allocation_lines(allocation["allocation_id"])
            current_total = sum((Decimal(line["amount"]) for line in lines), Decimal("0"))
            proposed_total = sum((line.amount for line in proposal.lines), Decimal("0"))
            if quantize_money(proposed_total) != quantize_money(current_total):
                raise ValidationFailed("调整前后分摊总额必须一致")
            per_batch: dict[str, Decimal] = {}
            for line in proposal.lines:
                if line.batch_id is None:
                    continue
                per_batch[line.batch_id] = per_batch.get(line.batch_id, Decimal("0")) + line.amount
            for batch_id, amount in per_batch.items():
                batch_row = self._batch_row(batch_id)
                if batch_row["state"] != "active":
                    raise ValidationFailed(f"批次 {batch_id} 不可用")
                if project["household_type"] not in json.loads(batch_row["household_types_json"]):
                    raise ValidationFailed(f"批次 {batch_id} 不适用户别 {project['household_type']}")
                if project["scope"] not in json.loads(batch_row["scopes_json"]):
                    raise ValidationFailed(f"批次 {batch_id} 不适用改造范围 {project['scope']}")
                valid_from = date.fromisoformat(batch_row["valid_from"])
                valid_to = date.fromisoformat(batch_row["valid_to"])
                if not valid_from <= self._today() <= valid_to:
                    raise ValidationFailed(f"批次 {batch_id} 不在有效期内")
                if amount > Decimal(batch_row["per_household_cap"]):
                    raise ValidationFailed(f"批次 {batch_id} 合计调整金额超过每户封顶")
            lines_json = canonical_json(
                [
                    {"batch_id": line.batch_id, "amount": decimal_text(quantize_money(line.amount))}
                    for line in proposal.lines
                ]
            )
            try:
                self.connection.execute(
                    "INSERT INTO adjustments(adjustment_id,project_id,base_version,lines_json,reason,"
                    "proposed_by,proposed_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        proposal.adjustment_id,
                        project_id,
                        allocation["version"],
                        lines_json,
                        proposal.reason,
                        actor_id,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("调整编号已经存在") from exc
            self._audit(
                "adjustment", proposal.adjustment_id, "adjustment.proposed", actor_id,
                {"project_id": project_id, "base_version": allocation["version"]},
            )
        return {
            "adjustment_id": proposal.adjustment_id,
            "project_id": project_id,
            "base_version": allocation["version"],
            "state": "pending",
        }

    def review_adjustment(
        self, actor_id: str, adjustment_id: str, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require(actor_id, "adjustment.review")
        decision = raw.get("decision")
        if decision not in {"approve", "reject"}:
            raise ValidationFailed("decision 必须是 approve 或 reject")
        with transaction(self.connection, immediate=True):
            adjustment = self.connection.execute(
                "SELECT * FROM adjustments WHERE adjustment_id=?", (adjustment_id,)
            ).fetchone()
            if adjustment is None:
                raise NotFound("调整提案不存在")
            if adjustment["state"] != "pending":
                raise InvalidState("调整提案已复核")
            if adjustment["proposed_by"] == actor_id:
                raise Forbidden("人工调整须双人复核，提案人不能复核自己的提案")
            project_id = adjustment["project_id"]
            if decision == "reject":
                self.connection.execute(
                    "UPDATE adjustments SET state='rejected',reviewed_by=?,reviewed_at=? "
                    "WHERE adjustment_id=?",
                    (actor_id, self._now(), adjustment_id),
                )
                self._audit(
                    "adjustment", adjustment_id, "adjustment.reviewed", actor_id,
                    {"decision": "reject"},
                )
                return {"adjustment_id": adjustment_id, "state": "rejected"}
            project = self._project_row(project_id)
            if project["state"] != "confirmed":
                raise InvalidState("项目状态已变化，无法应用调整")
            allocation = self._active_allocation(project_id)
            if allocation is None or allocation["version"] != adjustment["base_version"]:
                raise InvalidState("分摊版本已变化，调整需重新提案")
            # 先释放旧版本全部剩余冻结，再按提案冻结新版本，同一事务内完成。
            old_lines = self._allocation_lines(allocation["allocation_id"])
            for line in old_lines:
                remaining = (
                    Decimal(line["amount"])
                    - Decimal(line["written_off"])
                    - Decimal(line["released"])
                )
                if remaining <= 0:
                    continue
                if line["batch_id"] is not None:
                    batch_row = self._batch_row(line["batch_id"])
                    self._update_batch(
                        batch_row,
                        available=Decimal(batch_row["available_amount"]) + remaining,
                        frozen=Decimal(batch_row["frozen_amount"]) - remaining,
                        spent=Decimal(batch_row["spent_amount"]),
                        carried_out=Decimal(batch_row["carried_out"]),
                    )
                self.connection.execute(
                    "UPDATE allocation_lines SET released=? WHERE line_id=?",
                    (
                        decimal_text(
                            quantize_money(Decimal(line["released"]) + remaining)
                        ),
                        line["line_id"],
                    ),
                )
            self.connection.execute(
                "UPDATE allocations SET state='superseded' WHERE allocation_id=?",
                (allocation["allocation_id"],),
            )
            proposed = json.loads(adjustment["lines_json"])
            new_version = allocation["version"] + 1
            input_sha256 = digest(
                {
                    "adjustment_id": adjustment_id,
                    "base_version": adjustment["base_version"],
                    "lines": proposed,
                }
            )
            cursor = self.connection.execute(
                "INSERT INTO allocations(project_id,version,rule_id,rule_version,input_sha256,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    project_id,
                    new_version,
                    allocation["rule_id"],
                    allocation["rule_version"],
                    input_sha256,
                    actor_id,
                    self._now(),
                ),
            )
            new_allocation_id = int(cursor.lastrowid)
            for seq, line in enumerate(proposed, start=1):
                batch_id = line["batch_id"]
                amount = Decimal(line["amount"])
                if batch_id is None:
                    source = "household"
                else:
                    batch_row = self._batch_row(batch_id)
                    source = batch_row["source"]
                    available = Decimal(batch_row["available_amount"])
                    if available < amount:
                        raise Conflict(f"批次 {batch_id} 可用余额不足，无法完成调整")
                    self._update_batch(
                        batch_row,
                        available=available - amount,
                        frozen=Decimal(batch_row["frozen_amount"]) + amount,
                        spent=Decimal(batch_row["spent_amount"]),
                        carried_out=Decimal(batch_row["carried_out"]),
                    )
                explanation = (
                    f"人工调整 {adjustment_id}（{adjustment['reason']}）："
                    f"{adjustment['proposed_by']} 提案、{actor_id} 复核，"
                    f"双人复核形成第 {new_version} 版分摊"
                )
                self.connection.execute(
                    "INSERT INTO allocation_lines(allocation_id,seq,source,batch_id,amount,explanation) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        new_allocation_id,
                        seq,
                        source,
                        batch_id,
                        decimal_text(quantize_money(amount)),
                        explanation,
                    ),
                )
            self.connection.execute(
                "UPDATE adjustments SET state='approved',reviewed_by=?,reviewed_at=? "
                "WHERE adjustment_id=?",
                (actor_id, self._now(), adjustment_id),
            )
            self.connection.execute(
                "UPDATE projects SET revision=revision+1 WHERE project_id=?", (project_id,)
            )
            self._audit(
                "adjustment", adjustment_id, "adjustment.reviewed", actor_id,
                {"decision": "approve", "allocation_version": new_version},
            )
        return {
            "adjustment_id": adjustment_id,
            "state": "approved",
            "allocation_version": new_version,
        }

    # ------------------------------------------------------------------
    # 到期批次结转
    # ------------------------------------------------------------------

    def carry_forward_batch(
        self, actor_id: str, from_batch_id: str, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require(actor_id, "carryforward.write")
        request = CarryForwardInput.from_dict(raw)
        request_digest = digest(
            {"action": "carry_forward", "from_batch_id": from_batch_id, "request": dict(raw)}
        )
        replay = self._idempotent_replay("carryforward", request.idempotency_key, request_digest)
        if replay is not None:
            return replay
        try:
            with transaction(self.connection, immediate=True):
                source = self._batch_row(from_batch_id)
                if source["state"] != "active":
                    raise InvalidState("资金批次已结转或关闭")
                if date.fromisoformat(source["valid_to"]) >= self._today():
                    raise InvalidState("资金批次尚未到期，不能结转")
                amount = Decimal(source["available_amount"])
                if amount <= 0:
                    raise InvalidState("资金批次没有可结转余额")
                self.connection.execute(
                    "INSERT INTO fund_batches(batch_id,source,title,total_amount,available_amount,"
                    "household_types_json,scopes_json,valid_from,valid_to,per_household_cap,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        request.new_batch_id,
                        source["source"],
                        f"{source['title']}（结转）",
                        decimal_text(quantize_money(amount)),
                        decimal_text(quantize_money(amount)),
                        source["household_types_json"],
                        source["scopes_json"],
                        request.valid_from,
                        request.valid_to,
                        source["per_household_cap"],
                        actor_id,
                        self._now(),
                    ),
                )
                self._update_batch(
                    source,
                    available=Decimal("0"),
                    frozen=Decimal(source["frozen_amount"]),
                    spent=Decimal(source["spent_amount"]),
                    carried_out=Decimal(source["carried_out"]) + amount,
                )
                self.connection.execute(
                    "UPDATE fund_batches SET state='carried' WHERE batch_id=?",
                    (from_batch_id,),
                )
                cursor = self.connection.execute(
                    "INSERT INTO carry_forwards(from_batch_id,to_batch_id,amount,idempotency_key,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        from_batch_id,
                        request.new_batch_id,
                        decimal_text(quantize_money(amount)),
                        request.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                response = {
                    "carry_id": int(cursor.lastrowid),
                    "from_batch_id": from_batch_id,
                    "to_batch_id": request.new_batch_id,
                    "amount": decimal_text(quantize_money(amount)),
                }
                self._store_idempotent(
                    "carryforward", request.idempotency_key, request_digest, response
                )
                self._audit(
                    "batch", from_batch_id, "batch.carryforward", actor_id,
                    {"to_batch_id": request.new_batch_id, "amount": response["amount"]},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("新批次编号或幂等键冲突") from exc
        return response

    # ------------------------------------------------------------------
    # 授权明细视图与审计链
    # ------------------------------------------------------------------

    def _project_access(self, actor_id: str, project_id: str) -> tuple[sqlite3.Row, bool]:
        user = self._user(actor_id)
        project = self._project_row(project_id)
        if user["role"] == "family":
            if not user["household_id"] or user["household_id"] != project["household_id"]:
                raise Forbidden("家庭角色只能查看本户授权明细")
            return project, False
        self._require(actor_id, "project.read")
        return project, user["role"] in FULL_DETAIL_ROLES

    @staticmethod
    def _line_view(line: sqlite3.Row, *, full: bool) -> dict[str, Any]:
        view = {
            "seq": line["seq"],
            "source": line["source"],
            "source_label": SOURCE_LABELS[line["source"]],
            "amount": line["amount"],
            "written_off": line["written_off"],
            "released": line["released"],
            "explanation": line["explanation"],
        }
        if full:
            view["batch_id"] = line["batch_id"]
        return view

    def _allocation_view(
        self, allocation: sqlite3.Row, *, full: bool
    ) -> dict[str, Any]:
        lines = self._allocation_lines(allocation["allocation_id"])
        view = {
            "allocation_id": allocation["allocation_id"],
            "version": allocation["version"],
            "state": allocation["state"],
            "rule_id": allocation["rule_id"],
            "rule_version": allocation["rule_version"],
            "lines": [self._line_view(line, full=full) for line in lines],
        }
        if full:
            view["input_sha256"] = allocation["input_sha256"]
        return view

    def get_project(self, actor_id: str, project_id: str) -> dict[str, Any]:
        project, full = self._project_access(actor_id, project_id)
        view: dict[str, Any] = {
            "project_id": project["project_id"],
            "household_id": project["household_id"],
            "household_type": project["household_type"],
            "scope": project["scope"],
            "estimated_cost": project["estimated_cost"],
            "address": project["address"],
            "state": project["state"],
            "revision": project["revision"],
            "rule_id": project["rule_id"],
            "rule_version": project["rule_version"],
            "confirmed_at": project["confirmed_at"],
            "settled_at": project["settled_at"],
        }
        allocation = self.connection.execute(
            "SELECT * FROM allocations WHERE project_id=? ORDER BY version DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if allocation is not None:
            view["allocation"] = self._allocation_view(allocation, full=full)
        settlement = self.connection.execute(
            "SELECT * FROM settlements WHERE project_id=?", (project_id,)
        ).fetchone()
        if settlement is not None:
            view["settlement"] = {
                "outcome": settlement["outcome"],
                "accepted_amount": settlement["accepted_amount"],
                "written_total": settlement["written_total"],
                "released_total": settlement["released_total"],
            }
        return view

    def get_allocation(self, actor_id: str, project_id: str) -> dict[str, Any]:
        _, full = self._project_access(actor_id, project_id)
        allocation = self.connection.execute(
            "SELECT * FROM allocations WHERE project_id=? ORDER BY version DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if allocation is None:
            raise NotFound("项目尚未确认，没有分摊记录")
        return self._allocation_view(allocation, full=full)

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM funding_audit_events ORDER BY event_id"
        ).fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
