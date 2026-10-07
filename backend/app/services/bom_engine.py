"""Central kitchen BOM explode: order lines × BOM qty, merge ingredients, shortage = need - stock."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

ROUND = 3


@dataclass
class NeedLine:
    ingredient_id: int
    ingredient_code: str
    ingredient_name: str
    unit: str
    need_qty: float
    prep_qty: float
    reserved_qty: float
    stock_qty: float
    shortage: float


def explode_and_merge(
    order_lines: list[dict],
    bom_lines: list[dict],
    ingredients: dict[int, dict],
) -> list[NeedLine]:
    """order_lines: dish_id, portions; bom_lines: dish_id, ingredient_id, qty_per_portion."""
    need: dict[int, float] = {}
    for ol in order_lines:
        for bl in bom_lines:
            if bl["dish_id"] != ol["dish_id"]:
                continue
            need[bl["ingredient_id"]] = need.get(bl["ingredient_id"], 0.0) + ol["portions"] * bl["qty_per_portion"]
    lines: list[NeedLine] = []
    for iid, qty in sorted(need.items()):
        ing = ingredients[iid]
        stock = round(float(ing.get("stock_qty", 0)), ROUND)
        need_qty = round(qty, ROUND)
        shortage = round(max(0.0, need_qty - stock), ROUND)
        lines.append(NeedLine(
            ingredient_id=iid,
            ingredient_code=ing["code"],
            ingredient_name=ing["name"],
            unit=ing.get("unit", ""),
            need_qty=need_qty,
            prep_qty=need_qty,
            reserved_qty=round(min(need_qty, stock), ROUND),
            stock_qty=stock,
            shortage=shortage,
        ))
    return lines


def _recalc(lines: list[dict]) -> dict:
    """Rebuild the derived blocks (shortages + stats) from prep_lines."""
    shortages = [l for l in lines if l["shortage"] > 0]
    return {
        "prep_lines": lines,
        "shortages": shortages,
        "stats": {
            "ingredient_count": len(lines),
            "shortage_count": len(shortages),
            "total_shortage_qty": round(sum(l["shortage"] for l in shortages), ROUND),
        },
    }


def result_to_dict(lines: list[NeedLine]) -> dict:
    return _recalc([asdict(l) for l in lines])


def normalize_result(result: dict) -> dict:
    """Backfill prep_qty/reserved_qty on snapshots saved before manual edit existed.

    Read-time only: returns a new dict, never mutates the stored snapshot.
    """
    lines: list[dict] = []
    for l in result.get("prep_lines", []):
        line = dict(l)
        line["ingredient_id"] = int(line["ingredient_id"])
        need = round(float(line.get("need_qty", 0)), ROUND)
        stock = round(float(line.get("stock_qty", 0)), ROUND)
        prep = round(float(line.get("prep_qty", need)), ROUND)
        line["need_qty"] = need
        line["stock_qty"] = stock
        line["prep_qty"] = prep
        line["reserved_qty"] = round(float(line.get("reserved_qty", min(prep, stock))), ROUND)
        line["shortage"] = round(float(line.get("shortage", max(0.0, prep - stock))), ROUND)
        lines.append(line)
    return {**result, **_recalc(lines)}


def apply_manual_qty(result: dict, ingredient_id: int, qty: float) -> dict:
    """Apply a manual qty to one prep line of the latest run.

    Validates everything before changing anything: raises KeyError if the line
    is absent, ValueError if qty is negative/non-finite or exceeds the line's
    current demand (need_qty). On success returns a new result dict where the
    line's qty, reservation, shortage and the run-level stats move together;
    on failure the caller's snapshot is left untouched.
    """
    data = normalize_result(result)
    target = next((l for l in data["prep_lines"] if l["ingredient_id"] == ingredient_id), None)
    if target is None:
        raise KeyError(ingredient_id)
    if not math.isfinite(qty) or qty < 0:
        raise ValueError("数量必须是不小于 0 的数字")
    qty = round(qty, ROUND)
    if qty > target["need_qty"]:
        raise ValueError(f"超过该行当前需求 {target['need_qty']}")
    new_lines: list[dict] = []
    for l in data["prep_lines"]:
        line = dict(l)
        if line["ingredient_id"] == ingredient_id:
            stock = float(line.get("stock_qty", 0))
            line["prep_qty"] = qty
            line["reserved_qty"] = round(min(qty, stock), ROUND)
            line["shortage"] = round(max(0.0, qty - stock), ROUND)
        new_lines.append(line)
    return {**data, **_recalc(new_lines)}
