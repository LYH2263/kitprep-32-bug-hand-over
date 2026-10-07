import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.models import BomLine, Dish, Ingredient, KitchenOrder, OrderLine, PrepRun
from app.services.bom_engine import apply_manual_qty, normalize_result


@pytest.fixture()
def env():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    dish = Dish(code="D-HS", name="红烧肉套餐", portion_unit="份")
    ing = Ingredient(code="I-PR", name="五花肉", unit="kg", stock_qty=8.0)
    db.add_all([dish, ing])
    db.flush()
    db.add(BomLine(dish_id=dish.id, ingredient_id=ing.id, qty_per_portion=0.25))
    order = KitchenOrder(code="KO-1", outlet="城西门店", status="open")
    db.add(order)
    db.flush()
    db.add(OrderLine(order_id=order.id, dish_id=dish.id, portions=40))  # need = 10kg, stock 8
    db.commit()
    ids = {"order_id": order.id, "ingredient_id": ing.id}
    db.close()

    def override_get_db():
        s = TestingSessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app), ids, TestingSessionLocal
    finally:
        app.dependency_overrides.clear()


def _line(data, ingredient_id):
    return next(l for l in data["prep_lines"] if l["ingredient_id"] == ingredient_id)


def _stored_run(SessionLocal, run_id):
    db = SessionLocal()
    try:
        return db.get(PrepRun, run_id)
    finally:
        db.close()


# ---------- pure function ----------

def test_apply_manual_qty_moves_qty_and_reservation_together():
    result = normalize_result({"prep_lines": [
        {"ingredient_id": 1, "ingredient_code": "I-PR", "ingredient_name": "五花肉",
         "unit": "kg", "need_qty": 10.0, "stock_qty": 8.0},
    ]})
    out = apply_manual_qty(result, 1, 6.0)
    line = out["prep_lines"][0]
    assert line["need_qty"] == 10.0          # 需求上限不变
    assert line["prep_qty"] == 6.0           # 数量变了
    assert line["reserved_qty"] == 6.0       # 占用跟着变
    assert line["shortage"] == 0.0           # 缺料跟着重算
    assert out["stats"]["shortage_count"] == 0
    assert out["shortages"] == []


def test_apply_manual_qty_over_demand_raises_and_mutates_nothing():
    result = normalize_result({"prep_lines": [
        {"ingredient_id": 1, "ingredient_code": "I-PR", "ingredient_name": "五花肉",
         "unit": "kg", "need_qty": 10.0, "prep_qty": 10.0, "reserved_qty": 8.0,
         "stock_qty": 8.0, "shortage": 2.0},
    ]})
    before = json.dumps(result, sort_keys=True)
    with pytest.raises(ValueError):
        apply_manual_qty(result, 1, 10.5)
    assert json.dumps(result, sort_keys=True) == before  # 占用原样挂着,一个字没动


def test_apply_manual_qty_missing_line_raises():
    result = normalize_result({"prep_lines": [
        {"ingredient_id": 1, "ingredient_code": "I-PR", "ingredient_name": "五花肉",
         "unit": "kg", "need_qty": 10.0, "stock_qty": 8.0},
    ]})
    with pytest.raises(KeyError):
        apply_manual_qty(result, 999, 1.0)


def test_apply_manual_qty_rejects_negative_and_nan():
    result = normalize_result({"prep_lines": [
        {"ingredient_id": 1, "ingredient_code": "I-PR", "ingredient_name": "五花肉",
         "unit": "kg", "need_qty": 10.0, "stock_qty": 8.0},
    ]})
    with pytest.raises(ValueError):
        apply_manual_qty(result, 1, -1)
    with pytest.raises(ValueError):
        apply_manual_qty(result, 1, float("nan"))


# ---------- API ----------

def test_edit_line_updates_whole_run_atomically(env):
    client, ids, _ = env
    run = client.post(f"/api/prep/run?order_id={ids['order_id']}").json()
    res = client.patch(f"/api/prep/runs/{run['id']}/lines/{ids['ingredient_id']}", json={"qty": 6})
    assert res.status_code == 200
    line = _line(res.json(), ids["ingredient_id"])
    assert (line["need_qty"], line["prep_qty"], line["reserved_qty"], line["shortage"]) == (10.0, 6.0, 6.0, 0.0)
    assert res.json()["stats"]["total_shortage_qty"] == 0.0
    latest = client.get(f"/api/prep/latest?order_id={ids['order_id']}").json()
    assert _line(latest, ids["ingredient_id"])["prep_qty"] == 6.0


def test_edit_above_demand_fails_and_reservation_stays(env):
    client, ids, SessionLocal = env
    run = client.post(f"/api/prep/run?order_id={ids['order_id']}").json()
    before = _stored_run(SessionLocal, run["id"]).result_json
    res = client.patch(f"/api/prep/runs/{run['id']}/lines/{ids['ingredient_id']}", json={"qty": 10.5})
    assert res.status_code == 400
    assert _stored_run(SessionLocal, run["id"]).result_json == before  # 整单回退,占用没动
    line = _line(client.get(f"/api/prep/latest?order_id={ids['order_id']}").json(), ids["ingredient_id"])
    assert (line["prep_qty"], line["reserved_qty"], line["shortage"]) == (10.0, 8.0, 2.0)


def test_edit_missing_line_writes_nothing(env):
    client, ids, SessionLocal = env
    run = client.post(f"/api/prep/run?order_id={ids['order_id']}").json()
    before = _stored_run(SessionLocal, run["id"]).result_json
    res = client.patch(f"/api/prep/runs/{run['id']}/lines/999", json={"qty": 1})
    assert res.status_code == 404
    assert _stored_run(SessionLocal, run["id"]).result_json == before  # 不写一半


def test_voided_order_cannot_be_edited(env):
    client, ids, SessionLocal = env
    run = client.post(f"/api/prep/run?order_id={ids['order_id']}").json()
    assert client.post(f"/api/orders/{ids['order_id']}/void").json()["status"] == "voided"
    before = _stored_run(SessionLocal, run["id"]).result_json
    res = client.patch(f"/api/prep/runs/{run['id']}/lines/{ids['ingredient_id']}", json={"qty": 5})
    assert res.status_code == 409
    assert _stored_run(SessionLocal, run["id"]).result_json == before


def test_archived_run_is_not_rewritten_by_manual_edit(env):
    client, ids, SessionLocal = env
    old = client.post(f"/api/prep/run?order_id={ids['order_id']}").json()
    new = client.post(f"/api/prep/run?order_id={ids['order_id']}").json()  # old 从此存档
    before = _stored_run(SessionLocal, old["id"]).result_json
    res = client.patch(f"/api/prep/runs/{old['id']}/lines/{ids['ingredient_id']}", json={"qty": 5})
    assert res.status_code == 409
    assert _stored_run(SessionLocal, old["id"]).result_json == before  # 存档不被改字
    ok = client.patch(f"/api/prep/runs/{new['id']}/lines/{ids['ingredient_id']}", json={"qty": 5})
    assert ok.status_code == 200
    assert _line(ok.json(), ids["ingredient_id"])["prep_qty"] == 5.0
