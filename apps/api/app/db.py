from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from psycopg import Connection
from psycopg.rows import dict_row

from apps.api.app.core.config import settings


@contextmanager
def db_session(user_id: str) -> Generator[Connection, None, None]:
    conn = Connection.connect(settings.database_url, row_factory=dict_row)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into auth.users(id, email)
                values (%s::uuid, %s)
                on conflict (id) do nothing
                """,
                (user_id, f"{user_id}@local.test"),
            )
            cur.execute("set local role authenticated")
            cur.execute("select set_config('request.jwt.claim.sub', %s, true)", (user_id,))
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
