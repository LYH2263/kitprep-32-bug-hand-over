from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.models import Dish, KitchenOrder, OrderLine
router = APIRouter(prefix="/orders", tags=["orders"])

@router.get("")
def list_orders(db: Session = Depends(get_db)):
    return [{"id": r.id, "code": r.code, "outlet": r.outlet, "status": r.status}
            for r in db.scalars(select(KitchenOrder).order_by(KitchenOrder.id)).all()]

@router.get("/{order_id}/lines")
def order_lines(order_id: int, db: Session = Depends(get_db)):
    dishes = {d.id: d for d in db.scalars(select(Dish)).all()}
    rows = db.scalars(select(OrderLine).where(OrderLine.order_id == order_id)).all()
    return [{"id": r.id, "dish_id": r.dish_id, "dish_name": dishes[r.dish_id].name, "portions": r.portions}
            for r in rows]

@router.post("/{order_id}/void")
def void_order(order_id: int, db: Session = Depends(get_db)):
    order = db.get(KitchenOrder, order_id)
    if not order: raise HTTPException(404, "订单不存在")
    order.status = "voided"
    db.commit()
    return {"id": order.id, "code": order.code, "outlet": order.outlet, "status": order.status}


def hand_edit_guard(order_status: str | None) -> bool:
    return order_status != "voided"
