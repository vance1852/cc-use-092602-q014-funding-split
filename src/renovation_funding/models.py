"""危房改造联合资金分摊的输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

GOVERNMENT_SOURCES = ("central", "provincial", "county")
SOURCES = GOVERNMENT_SOURCES + ("household",)
SOURCE_LABELS = {
    "central": "中央补助",
    "provincial": "省级配套",
    "county": "县级资金",
    "household": "家庭自筹",
}
HOUSEHOLD_TYPES = ("dibao", "tekun", "tuopin", "general")
HOUSEHOLD_TYPE_LABELS = {
    "dibao": "低保户",
    "tekun": "特困供养户",
    "tuopin": "脱贫不稳定户",
    "general": "一般户",
}
SCOPES = ("rebuild", "reinforce", "repair")
SCOPE_LABELS = {
    "rebuild": "推倒重建",
    "reinforce": "修缮加固",
    "repair": "局部修缮",
}
OUTCOMES = ("completed", "partial", "failed", "cancelled")


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def choice_list(value: object, field: str, allowed: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValidationFailed(f"{field} 必须是非空数组")
    result: list[str] = []
    for item in value:
        text = required_text(item, f"{field} 项", 32)
        if text not in allowed:
            raise ValidationFailed(f"{field} 包含不支持的取值 {text}")
        if text in result:
            raise ValidationFailed(f"{field} 不能重复")
        result.append(text)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class HouseholdInput:
    household_id: str
    head_name: str
    household_type: str
    village: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HouseholdInput":
        household_type = required_text(raw.get("household_type"), "household_type", 16)
        if household_type not in HOUSEHOLD_TYPES:
            raise ValidationFailed("household_type 必须是 dibao、tekun、tuopin 或 general")
        return cls(
            household_id=identifier(raw.get("household_id"), "household_id"),
            head_name=required_text(raw.get("head_name"), "head_name", 64),
            household_type=household_type,
            village=required_text(raw.get("village"), "village", 128),
        )


@dataclass(frozen=True, slots=True)
class FundBatchInput:
    batch_id: str
    source: str
    title: str
    total_amount: Decimal
    household_types: tuple[str, ...]
    scopes: tuple[str, ...]
    valid_from: str
    valid_to: str
    per_household_cap: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FundBatchInput":
        source = required_text(raw.get("source"), "source", 16)
        if source not in GOVERNMENT_SOURCES:
            raise ValidationFailed("source 必须是 central、provincial 或 county，家庭自筹不登记批次")
        valid_from = date_text(raw.get("valid_from"), "valid_from")
        valid_to = date_text(raw.get("valid_to"), "valid_to")
        if valid_to < valid_from:
            raise ValidationFailed("valid_to 不能早于 valid_from")
        return cls(
            batch_id=identifier(raw.get("batch_id"), "batch_id"),
            source=source,
            title=required_text(raw.get("title"), "title"),
            total_amount=decimal_value(raw.get("total_amount"), "total_amount", minimum=Decimal("0.01")),
            household_types=choice_list(raw.get("household_types"), "household_types", HOUSEHOLD_TYPES),
            scopes=choice_list(raw.get("scopes"), "scopes", SCOPES),
            valid_from=valid_from,
            valid_to=valid_to,
            per_household_cap=decimal_value(
                raw.get("per_household_cap"), "per_household_cap", minimum=Decimal("0.01")
            ),
        )


@dataclass(frozen=True, slots=True)
class RuleInput:
    rule_id: str
    source_order: tuple[str, ...]
    source_caps: Mapping[str, Decimal]
    household_residual: bool
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RuleInput":
        order = choice_list(raw.get("source_order"), "source_order", GOVERNMENT_SOURCES)
        caps_raw = raw.get("source_caps", {})
        if not isinstance(caps_raw, Mapping):
            raise ValidationFailed("source_caps 必须是对象")
        caps: dict[str, Decimal] = {}
        for key, value in caps_raw.items():
            source = required_text(key, "source_caps 键", 16)
            if source not in GOVERNMENT_SOURCES:
                raise ValidationFailed("source_caps 键必须是 central、provincial 或 county")
            caps[source] = decimal_value(
                value, f"source_caps.{source}", minimum=Decimal("0"), maximum=Decimal("100")
            )
        residual = raw.get("household_residual", True)
        if not isinstance(residual, bool):
            raise ValidationFailed("household_residual 必须是布尔值")
        note = raw.get("note")
        return cls(
            rule_id=identifier(raw.get("rule_id"), "rule_id"),
            source_order=order,
            source_caps=caps,
            household_residual=residual,
            note=required_text("联合资金分摊规则" if note is None else note, "note"),
        )


@dataclass(frozen=True, slots=True)
class ProjectInput:
    project_id: str
    household_id: str
    scope: str
    estimated_cost: Decimal
    address: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ProjectInput":
        scope = required_text(raw.get("scope"), "scope", 16)
        if scope not in SCOPES:
            raise ValidationFailed("scope 必须是 rebuild、reinforce 或 repair")
        return cls(
            project_id=identifier(raw.get("project_id"), "project_id"),
            household_id=identifier(raw.get("household_id"), "household_id"),
            scope=scope,
            estimated_cost=decimal_value(
                raw.get("estimated_cost"), "estimated_cost", minimum=Decimal("0.01")
            ),
            address=required_text(raw.get("address"), "address", 256),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class SettlementInput:
    outcome: str
    accepted_amount: Decimal
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SettlementInput":
        outcome = required_text(raw.get("outcome"), "outcome", 16)
        if outcome not in OUTCOMES:
            raise ValidationFailed("outcome 必须是 completed、partial、failed 或 cancelled")
        return cls(
            outcome=outcome,
            accepted_amount=decimal_value(
                raw.get("accepted_amount"), "accepted_amount", minimum=Decimal("0")
            ),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class AdjustmentLineInput:
    batch_id: str | None
    amount: Decimal


@dataclass(frozen=True, slots=True)
class AdjustmentInput:
    adjustment_id: str
    reason: str
    lines: tuple[AdjustmentLineInput, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AdjustmentInput":
        lines_raw = raw.get("lines")
        if not isinstance(lines_raw, (list, tuple)) or not lines_raw:
            raise ValidationFailed("lines 必须是非空数组")
        lines: list[AdjustmentLineInput] = []
        household_lines = 0
        for index, item in enumerate(lines_raw):
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"lines[{index}] 必须是对象")
            batch_raw = item.get("batch_id")
            if batch_raw is None:
                household_lines += 1
                batch_id = None
            else:
                batch_id = identifier(batch_raw, f"lines[{index}].batch_id")
            lines.append(
                AdjustmentLineInput(
                    batch_id=batch_id,
                    amount=decimal_value(
                        item.get("amount"), f"lines[{index}].amount", minimum=Decimal("0.01")
                    ),
                )
            )
        if household_lines > 1:
            raise ValidationFailed("家庭自筹行最多一行")
        return cls(
            adjustment_id=identifier(raw.get("adjustment_id"), "adjustment_id"),
            reason=required_text(raw.get("reason"), "reason"),
            lines=tuple(lines),
        )


@dataclass(frozen=True, slots=True)
class CarryForwardInput:
    new_batch_id: str
    valid_from: str
    valid_to: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CarryForwardInput":
        valid_from = date_text(raw.get("valid_from"), "valid_from")
        valid_to = date_text(raw.get("valid_to"), "valid_to")
        if valid_to < valid_from:
            raise ValidationFailed("valid_to 不能早于 valid_from")
        return cls(
            new_batch_id=identifier(raw.get("new_batch_id"), "new_batch_id"),
            valid_from=valid_from,
            valid_to=valid_to,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )
