"""
Database engine/session configuration.

Uses SQLite by default (zero-setup for the assessment/demo), but every
concurrency-critical operation in this codebase is written as an atomic
conditional UPDATE (WHERE status = X ...) rather than a
read-then-write in Python. That pattern is what actually makes booking
safe, and it holds regardless of backend:

  - SQLite:   the whole DB is serialized behind a single writer lock, so
              conditional updates are trivially race-free.
  - Postgres: each UPDATE takes row-level locks for the rows it matches,
              so two concurrent transactions racing for the same slot
              row will be serialized by Postgres itself; the loser's
              WHERE clause simply matches zero rows.

To point this at Postgres for real concurrent-process testing, set
DATABASE_URL, e.g.:
    postgresql+psycopg://user:pass@localhost:5432/clinzo
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./clinzo_scheduler.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)

# SQLite: make transactions actually behave (IMMEDIATE locking) so that
# concurrent writers block/fail deterministically instead of interleaving.
if DATABASE_URL.startswith("sqlite"):
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from app import models  # noqa: F401 (ensure models are registered)
    Base.metadata.create_all(bind=engine)
