from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row

from app.config import settings


def get_connection():
    return psycopg.connect(settings.database_url, row_factory=dict_row)



def fetch_all(query: str, params: tuple[Any, ...] = ()) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()


def fetch_one(query: str, params: tuple[Any, ...] = ()) -> dict | None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone()


def execute_query(query: str, params: tuple[Any, ...] = ()) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
        conn.commit()


def execute_transaction(callback: Callable):
    with get_connection() as conn:
        with conn.cursor() as cur:
            result = callback(conn, cur)
        conn.commit()
        return result