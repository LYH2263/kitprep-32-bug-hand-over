import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.models import BomLine, Ingredient, KitchenOrder, OrderLine, PrepRun
from app.api.orders import hand_edit_guard
from app.services.bom_engine import apply_manual_qty, explode_and_merge, normalize_result, result_to_dict
router = APIRouter(prefix="/prep", tags=["prep"])

class LineQtyUpdate(BaseModel):
    qty: float = Field(ge=0)

@router.post("/run")
def run_prep(order_id: int = 1, db: Session = Depends(get_db)):
    order = db.get(KitchenOrder, order_id)
    if not order: raise HTTPException(404, "订单不存在")
    if not hand_edit_guard(order.status):
        raise HTTPException(409, "订单已作废，不能再改备料单")
    ols = [{"dish_id": l.dish_id, "portions": l.portions}
           for l in db.scalars(select(OrderLine).where(OrderLine.order_id == order_id)).all()]
    bom = [{"dish_id": b.dish_id, "ingredient_id": b.ingredient_id, "qty_per_portion": b.qty_per_portion}
           for b in db.scalars(select(BomLine)).all()]
    ings = {i.id: {"code": i.code, "name": i.name, "unit": i.unit, "stock_qty": i.stock_qty}
            for i in db.scalars(select(Ingredient)).all()}
    result = result_to_dict(explode_and_merge(ols, bom, ings))
    result["order"] = {"id": order.id, "code": order.code, "outlet": order.outlet, "status": order.status}
    run = PrepRun(order_id=order_id, created_at=datetime.utcnow(), result_json=json.dumps(result, ensure_ascii=False))
    db.add(run); db.commit(); db.refresh(run)
    return {"id": run.id, **result}

@router.get("/latest")
def latest(order_id: int = 1, db: Session = Depends(get_db)):
    run = db.scalars(select(PrepRun).where(PrepRun.order_id == order_id).order_by(PrepRun.id.desc())).first()
    if not run:
        return run_prep(order_id=order_id, db=db)
    data = normalize_result(json.loads(run.result_json))
    # 订单状态以库内当前值为准(快照里的可能已作废),前端禁用态/徽标才不会失真
    order = db.get(KitchenOrder, order_id)
    if order:
        data["order"] = {"id": order.id, "code": order.code, "outlet": order.outlet, "status": order.status}
    return {"id": run.id, **data}

@router.patch("/runs/{run_id}/lines/{ingredient_id}")
def edit_prep_line(run_id: int, ingredient_id: int, body: LineQtyUpdate, db: Session = Depends(get_db)):
    """Manual edit of one prep line. All checks run before any write and the
    whole updated snapshot is stored in a single commit: qty and reservation
    move together, or nothing moves at all."""
    run = db.get(PrepRun, run_id)
    if not run: raise HTTPException(404, "备料单不存在")
    latest_id = db.scalar(select(func.max(PrepRun.id)).where(PrepRun.order_id == run.order_id))
    if run.id != latest_id: raise HTTPException(409, "已存档的备料单不能手改")
    order = db.get(KitchenOrder, run.order_id)
    if not hand_edit_guard(order.status if order else None):
        raise HTTPException(409, "订单已作废，不能手改")
    snapshot = json.loads(run.result_json)
    try:
        data = apply_manual_qty(snapshot, ingredient_id, body.qty)
    except KeyError:
        # 缺行:整单不动,不留半成品
        raise HTTPException(404, "备料行不存在")
    except ValueError as exc:
        # 超需求/非法数量:占用停在改前,一个字不写
        raise HTTPException(400, str(exc))
    run.result_json = json.dumps(data, ensure_ascii=False)
    db.commit()
    return {"id": run.id, **data}

@router.get("/shortages")
def shortages(order_id: int = 1, db: Session = Depends(get_db)):
    data = latest(order_id=order_id, db=db)
    # 与备料台同一口径:实备超过库存才算缺料,数量改了缺料贴立刻跟上
    shorts = []
    for l in data.get("prep_lines", []) or []:
        prep = float(l.get("prep_qty", l.get("need_qty", 0)) or 0)
        stock = float(l.get("stock_qty", 0) or 0)
        if prep > stock:
            row = dict(l)
            row["shortage"] = round(prep - stock, 3)
            shorts.append(row)
    return {"order_id": order_id, "shortages": shorts, "stats": data.get("stats", {})}
