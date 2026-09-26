"""确定且可解释的联合资金分摊计算。

所有函数都是纯函数：相同输入必然得到相同输出，每一行分摊都带
约束解释（每户封顶、批次可用余额或剩余需求哪一个生效），方便
向家庭和审计说明每一分钱来自哪里。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Sequence


ZERO = Decimal("0")
CENT = Decimal("0.01")


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class BatchSlice:
    """参与分摊的批次快照：available 在确认时是可用余额，核销时是已冻结额。"""

    batch_id: str
    source: str
    cap_cny: Decimal
    available_cny: Decimal
    priority: int
    valid_from: str


def _ordered(batches: Iterable[BatchSlice], source: str) -> list[BatchSlice]:
    candidates = [batch for batch in batches if batch.source == source]
    return sorted(candidates, key=lambda batch: (batch.priority, batch.valid_from, batch.batch_id))


def allocate_cost(
    total_cny: Decimal,
    steps: Sequence[str],
    batches: Iterable[BatchSlice],
) -> dict[str, object]:
    """按规则步骤顺序注水分摊，剩余部分由家庭自筹承担。"""
    demand = quantize_money(total_cny)
    if demand <= ZERO:
        raise ValueError("分摊总额必须大于零")
    remaining = demand
    lines: list[dict[str, object]] = []
    for source in steps:
        for batch in _ordered(batches, source):
            if remaining <= ZERO:
                break
            cap = quantize_money(batch.cap_cny)
            available = quantize_money(batch.available_cny)
            amount = quantize_money(min(remaining, cap, available))
            if amount <= ZERO:
                continue
            if amount == remaining:
                binding = "demand"
            elif cap <= available:
                binding = "per_household_cap"
            else:
                binding = "batch_available"
            lines.append({
                "batch_id": batch.batch_id,
                "source": batch.source,
                "amount_cny": decimal_text(amount),
                "demand_before_cny": decimal_text(remaining),
                "per_household_cap_cny": decimal_text(cap),
                "available_before_cny": decimal_text(available),
                "binding_constraint": binding,
            })
            remaining = quantize_money(remaining - amount)
    public_total = quantize_money(demand - remaining)
    return {
        "total_cny": decimal_text(demand),
        "public_total_cny": decimal_text(public_total),
        "household_share_cny": decimal_text(remaining),
        "lines": lines,
    }


def settle_cost(
    actual_cny: Decimal,
    steps: Sequence[str],
    frozen_lines: Sequence[dict[str, object]],
) -> dict[str, object]:
    """按实际验收造价核销：沿用规则顺序，单批次核销不超过其冻结额。"""
    actual = quantize_money(actual_cny)
    if actual < ZERO:
        raise ValueError("实际验收造价不能为负数")
    slices = [
        BatchSlice(
            batch_id=str(line["batch_id"]),
            source=str(line["source"]),
            cap_cny=Decimal(str(line["per_household_cap_cny"])),
            available_cny=Decimal(str(line["amount_cny"])),
            priority=index,
            valid_from="",
        )
        for index, line in enumerate(frozen_lines)
    ]
    if actual == ZERO:
        final = {"total_cny": "0.00", "public_total_cny": "0.00", "household_share_cny": "0.00", "lines": []}
    else:
        final = allocate_cost(actual, steps, slices)
    final_by_batch = {str(line["batch_id"]): Decimal(str(line["amount_cny"])) for line in final["lines"]}
    write_offs: list[dict[str, object]] = []
    releases: list[dict[str, object]] = []
    for line in frozen_lines:
        batch_id = str(line["batch_id"])
        frozen = Decimal(str(line["amount_cny"]))
        written = final_by_batch.get(batch_id, ZERO)
        remainder = quantize_money(frozen - written)
        write_offs.append({
            "batch_id": batch_id,
            "source": str(line["source"]),
            "frozen_cny": decimal_text(quantize_money(frozen)),
            "written_off_cny": decimal_text(quantize_money(written)),
        })
        if remainder > ZERO:
            releases.append({
                "batch_id": batch_id,
                "source": str(line["source"]),
                "remainder_cny": decimal_text(remainder),
            })
    return {
        "actual_cny": decimal_text(actual),
        "public_total_cny": final["public_total_cny"],
        "household_share_cny": final["household_share_cny"],
        "lines": final["lines"],
        "write_offs": write_offs,
        "remainders": releases,
        "remainder_total_cny": decimal_text(quantize_money(
            sum((Decimal(str(item["remainder_cny"])) for item in releases), ZERO)
        )),
    }
