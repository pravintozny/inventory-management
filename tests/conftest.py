import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.database import get_db, make_engine
from app.main import app
from app.models import Base


@pytest.fixture()
def engine(tmp_path):
    # File-backed SQLite so multi-threaded concurrency tests use real, separate connections.
    eng = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def Session(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def db(Session):
    s = Session()
    yield s
    s.rollback()
    s.close()


@pytest.fixture()
def client(Session):
    def _get_db():
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


class Api:
    """Tiny helper that asserts on status codes and returns JSON."""

    def __init__(self, client):
        self.c = client

    def req(self, method, url, expect=None, user=None, **kw):
        headers = {"X-User": user} if user else {}
        r = self.c.request(method, "/api/v1" + url, headers=headers, **kw)
        if expect is not None:
            assert r.status_code == expect, f"{method} {url} -> {r.status_code}: {r.text}"
        return r

    def post(self, url, json=None, expect=201, **kw):
        return self.req("POST", url, expect, json=json, **kw).json() if expect != 204 else None

    def get(self, url, expect=200, **kw):
        return self.req("GET", url, expect, **kw).json()


@pytest.fixture()
def api(client):
    return Api(client)


@pytest.fixture()
def world(api):
    """Two warehouses, a product with SKU 'W-1', a supplier that supplies it."""
    w1 = api.post("/warehouses", {"code": "WH1", "name": "Main", "capacity_units": 1000})
    w2 = api.post("/warehouses", {"code": "WH2", "name": "Secondary", "capacity_units": 500})
    p = api.post("/products", {"sku": "W-1", "name": "Widget", "category": "parts", "purchase_price": "2.00",
                               "selling_price": "5.00", "reorder_point": 10, "reorder_quantity": 40})
    sup = api.post("/suppliers", {"code": "ACME", "name": "Acme Ltd", "email": "sales@acme.test"})
    vid = p["variants"][0]["id"]
    api.req("PUT", f"/suppliers/{sup['id']}/products", 200,
            json={"variant_id": vid, "unit_price": "2.50", "min_order_qty": 5, "lead_time_days": 7,
                  "is_preferred": True})
    return {"w1": w1, "w2": w2, "product": p, "variant_id": vid, "supplier": sup,
            "w1_id": w1["id"], "w2_id": w2["id"]}


def levels(api, wid, vid):
    rows = api.get(f"/inventory?warehouse_id={wid}&variant_id={vid}")
    return {k: sum(r[k] for r in rows) for k in ("available", "reserved", "damaged", "in_transit")}
