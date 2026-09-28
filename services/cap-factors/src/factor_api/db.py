"""Postgres access for cap-factors.

factor-db sits on the cap-factors internal network only. It is unreachable from
the product by design -- that is the Compose equivalent of namespace isolation,
and it makes the ownership boundary enforceable rather than decorative.
"""

from __future__ import annotations

import logging
import os
import time

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

_pool: ConnectionPool | None = None


def dsn() -> str:
    return os.environ.get(
        "FACTOR_DB_URL",
        "postgresql://factors:factors@factor-db:5432/factors",
    )


def init_pool(retries: int = 30, delay: float = 1.0) -> ConnectionPool:
    """Open the pool, waiting for Postgres to accept connections.

    Cold start budget for the whole stack is 90s; this loop is why a slow
    Postgres init does not turn into a crash-loop.
    """
    global _pool
    if _pool is not None:
        return _pool

    last: Exception | None = None
    for attempt in range(retries):
        try:
            pool = ConnectionPool(
                dsn(), min_size=1, max_size=8, kwargs={"row_factory": dict_row}
            )
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            _pool = pool
            log.info("factor-db pool ready after %d attempt(s)", attempt + 1)
            return pool
        except Exception as exc:  # noqa: BLE001 - startup retry is intentional
            last = exc
            time.sleep(delay)

    raise RuntimeError(f"factor-db unreachable after {retries} attempts: {last}")


def pool() -> ConnectionPool:
    if _pool is None:
        return init_pool()
    return _pool


def dataset_version() -> str:
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT dataset_version FROM dataset_meta ORDER BY published_at DESC LIMIT 1"
        ).fetchone()
    return row["dataset_version"] if row else "unknown"


def region_exists(code: str) -> bool:
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM regions WHERE code = %s", (code,)
        ).fetchone()
    return row is not None


def get_factor(region: str, activity: str, year: int) -> dict | None:
    with pool().connection() as conn:
        return conn.execute(
            """
            SELECT region_code, activity, year, factor, unit, source, dataset_version
            FROM emission_factors
            WHERE region_code = %s AND activity = %s AND year = %s
            """,
            (region, activity, year),
        ).fetchone()


def get_factors_bulk(region: str, activities: list[str], year: int) -> list[dict]:
    if not activities:
        return []
    with pool().connection() as conn:
        return conn.execute(
            """
            SELECT region_code, activity, year, factor, unit, source, dataset_version
            FROM emission_factors
            WHERE region_code = %s AND year = %s AND activity = ANY(%s)
            ORDER BY activity
            """,
            (region, year, activities),
        ).fetchall()


def list_regions() -> list[dict]:
    with pool().connection() as conn:
        return conn.execute(
            """
            SELECT code, name, country, avg_annual_tco2e, avg_source
            FROM regions ORDER BY country, name
            """
        ).fetchall()


def ping() -> str:
    try:
        with pool().connection() as conn:
            conn.execute("SELECT 1")
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}"
