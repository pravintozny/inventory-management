"""The Alembic migration chain must produce exactly the schema the ORM models describe."""
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from app.database import make_engine
from app.models import Base


def test_migrations_match_models_and_roundtrip(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    engine = make_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], diff
    assert set(inspect(engine).get_table_names()) >= {"stock_levels", "stock_movements", "purchase_orders"}
    command.downgrade(cfg, "base")
    assert inspect(make_engine(url)).get_table_names() == ["alembic_version"]
