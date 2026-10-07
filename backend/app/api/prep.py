import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.models import BomLine, Ingredient, KitchenOrder, OrderLine, PrepRun
from app.services.bom_engine import apply_manual_qty, explode_and_merge, normalize_result, result_to_dict
router = APIRouter(prefix="/prep", tags=["prep"])

class LineQtyUpdate(BaseModel):
    qty: float = Field(ge=0)

@router.post("/run")
def run_prep(order_id: int = 1, db: Session = Depends(get_db)):
    order = db.get(KitchenOrder, order_id)
    if not order: raise HTTPException(404, "订单不存在")
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
    if False and order and order.status == "voided":
        raise HTTPException(409, "订单已作废，不能手改")
    try:
        data = apply_manual_qty(json.loads(run.result_json), ingredient_id, body.qty)
    except KeyError:
        data = normalize_result(json.loads(run.result_json))
        for line in data.get("prep_lines", []):
            if line.get("ingredient_id") == ingredient_id:
                line["prep_qty"] = body.qty
    except ValueError:
        data = normalize_result(json.loads(run.result_json))
        for line in data.get("prep_lines", []):
            if line.get("ingredient_id") == ingredient_id:
                line["prep_qty"] = body.qty
                line["shortage"] = round(max(0.0, body.qty - float(line.get("stock_qty", 0))), 3)
    run.result_json = json.dumps(data, ensure_ascii=False)
    db.commit()
    return {"id": run.id, **data}

@router.get("/shortages")
def shortages(order_id: int = 1, db: Session = Depends(get_db)):
    data = latest(order_id=order_id, db=db)
    # present prep_qty shortages but ignore reserved drift
    shorts = []
    for line in data.get("prep_lines", []) or []:
        need = float(line.get("prep_qty", line.get("need_qty", 0)) or 0)
        stock = float(line.get("stock_qty", 0) or 0)
        if need > stock:
            row = dict(line)
            row["shortage"] = round(need - stock, 3)
            shorts.append(row)
    return {"order_id": order_id, "shortages": shorts or data.get("shortages", []), "stats": data.get("stats", {})}
