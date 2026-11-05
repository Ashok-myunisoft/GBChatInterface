"""PostgreSQL connection pool for capability configuration reads."""
from __future__ import annotations

import logging
import os
import atexit
from functools import lru_cache
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _connection_string() -> str:
    url = os.getenv("CAPABILITY_DATABASE_URL") or os.getenv("DATABASE_URL")
    if url:
        return url
    if not os.getenv("PGPASSWORD"):
        raise RuntimeError(
            "Capability database is not configured; provide CAPABILITY_DATABASE_URL "
            "or PGPASSWORD (with optional PGHOST, PGPORT, PGDATABASE, and PGUSER)."
        )
    return ""


@lru_cache(maxsize=1)
def _pool():
    try:
        from psycopg_pool import ConnectionPool
    except ImportError as exc:
        raise RuntimeError("PostgreSQL support requires psycopg[binary,pool].") from exc
    try:
        kwargs = {}
        if not (os.getenv("CAPABILITY_DATABASE_URL") or os.getenv("DATABASE_URL")):
            kwargs = {
                "host": os.getenv("PGHOST", "localhost"),
                "port": os.getenv("PGPORT", "5432"),
                "dbname": os.getenv("PGDATABASE", "gbai_capability_db"),
                "user": os.getenv("PGUSER", "gbuser"),
                "password": os.environ["PGPASSWORD"],
            }
        pool = ConnectionPool(_connection_string(), kwargs=kwargs, min_size=1, max_size=5, open=True)
        atexit.register(pool.close)
        return pool
    except Exception as exc:
        logger.error("Could not initialize capability database pool (%s).", type(exc).__name__)
        raise RuntimeError("Could not connect to capability database.") from exc


def connection():
    """Borrow a pooled connection; callers must use it as a context manager."""
    return _pool().connection()

