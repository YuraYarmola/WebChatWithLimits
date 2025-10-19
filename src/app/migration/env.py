# /<project>/app/migration/env.py  (або де лежить твій alembic/env.py)
from __future__ import annotations

from logging.config import fileConfig
from pathlib import Path
import os
import sys

from alembic import context

# ---------- robust project root discovery ----------
# Йдемо вгору від поточного файлу і знаходимо корінь проєкту
# за наявністю pyproject.toml / setup.cfg або директорії "app".
ROOT = None
for p in Path(__file__).resolve().parents:
    if (p / "pyproject.toml").exists() or (p / "setup.cfg").exists() or (p / "app").is_dir():
        ROOT = p
        break

if ROOT is None:
    # fallback: два-три рівні вгору
    ROOT = Path(__file__).resolve().parents[2]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Якщо використовуєш src-layout (тобто проєкт у /src), теж додаємо його
SRC_DIR = ROOT / "src"
if SRC_DIR.is_dir() and str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# ---------- тепер імпорти з твого проєкту ----------
# приклад з твого коду:
from app.db import sync_engine, Base  # має існувати пакет app та __init__.py
# якщо моделі розкидані, цей імпорт ініціалізує їх у Base.metadata
from app import models as _models  # noqa: F401

# ---------- alembic config ----------
config = context.config

if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

def _db_url() -> str:
    return os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url")

def run_migrations_offline() -> None:
    url = _db_url()
    if not url:
        raise RuntimeError("DATABASE_URL не задано і sqlalchemy.url відсутній в alembic.ini")

    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    connectable = sync_engine
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
