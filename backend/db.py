"""
db.py — Camada de acesso ao PostgreSQL para o EconRadar.

SQL puro (sem ORM), sempre com queries parametrizadas (%s) — nunca
concatenação de strings — para evitar SQL injection. Usa um pool de
conexões (DBUtils) sobre o psycopg2, com cursor em modo dicionário.

Convenção para INSERT que precisa do id gerado: adicione "RETURNING id"
no final da query e use db.execute(...) normalmente — a função detecta
o RETURNING e devolve o valor no lugar do rowcount.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterable, Optional

import psycopg2
import psycopg2.extras
from dbutils.pooled_db import PooledDB

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_USER = os.getenv("DB_USER", "econradar_app")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "econradar")

if not DB_PASSWORD:
    # Falha cedo: nunca subimos com senha de banco vazia.
    raise RuntimeError(
        "DB_PASSWORD não configurada. Defina as variáveis DB_HOST/DB_PORT/"
        "DB_USER/DB_PASSWORD/DB_NAME no .env antes de iniciar o servidor."
    )

_pool = PooledDB(
    creator=psycopg2,
    maxconnections=20,
    mincached=2,
    maxcached=5,
    blocking=True,
    host=DB_HOST,
    port=DB_PORT,
    user=DB_USER,
    password=DB_PASSWORD,
    database=DB_NAME,
    cursor_factory=psycopg2.extras.RealDictCursor,
)


@contextmanager
def get_conn():
    conn = _pool.connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetchone(sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            row = cur.fetchone()
            return dict(row) if row is not None else None


def fetchall(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()]


def execute(sql: str, params: Iterable[Any] = ()):
    """
    Executa INSERT/UPDATE/DELETE.

    Se a query terminar com "RETURNING <coluna>", devolve o valor dessa
    coluna (equivalente ao lastrowid do MySQL). Caso contrário, devolve o
    número de linhas afetadas (rowcount).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            if cur.description:  # só existe quando há RETURNING
                row = cur.fetchone()
                if row is None:
                    return None
                # RealDictCursor -> pega o único valor da linha
                return next(iter(dict(row).values()))
            return cur.rowcount


def execute_many(sql: str, seq_params: Iterable[Iterable[Any]]) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, [tuple(p) for p in seq_params])
