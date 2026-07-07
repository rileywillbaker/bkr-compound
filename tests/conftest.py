import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sentinel.db import models  # noqa: F401 - register tables
from sentinel.db.base import Base


@pytest.fixture()
def db() -> Session:
    """In-memory SQLite session with all tables created.

    check_same_thread=False + StaticPool let FastAPI's TestClient (which runs
    requests in a worker thread) share the single in-memory connection.
    Postgres-specific behaviors (hypertables, ON CONFLICT upserts in ingest)
    are exercised against the real database during docker verification, not
    here.
    """
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
