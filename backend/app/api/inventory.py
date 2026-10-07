import json

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.models import Ingredient, KitchenOrder, PrepRun
from app.services.bom_engine import normalize_result
router = APIRouter(prefix="/inventory", tags=["inventory"])

def _reservations_by_ingredient(db: Session) -> dict[int, float]:
    """占用口径:只统计未作废订单的最新备料单,行上占用以最新快照为准。
    作废单与更早存档单都不占库存。"""
    reserved: dict[int, float] = {}
    active_orders = select(KitchenOrder.id).where(KitchenOrder.status != "voided")
    latest_ids = (
        select(func.max(PrepRun.id))
        .where(PrepRun.order_id.in_(active_orders))
        .group_by(PrepRun.order_id)
    )
    for run in db.scalars(select(PrepRun).where(PrepRun.id.in_(latest_ids))).all():
        data = normalize_result(json.loads(run.result_json))
        for line in data.get("prep_lines", []):
            iid = int(line["ingredient_id"])
            reserved[iid] = reserved.get(iid, 0.0) + float(line.get("reserved_qty", 0) or 0.0)
    return reserved

@router.get("")
def list_inventory(db: Session = Depends(get_db)):
    reserved = _reservations_by_ingredient(db)
    rows = []
    for r in db.scalars(select(Ingredient).order_by(Ingredient.id)).all():
        used = round(reserved.get(r.id, 0.0), 3)
        # 可再用 = 当前库存 - 未作废最新备料单占用;手改保存后占用与这里同成同败
        available = round(r.stock_qty - used, 3)
        rows.append({"id": r.id, "code": r.code, "name": r.name, "unit": r.unit,
                     "stock_qty": r.stock_qty, "reserved_qty": used, "available_qty": available})
    return rows
