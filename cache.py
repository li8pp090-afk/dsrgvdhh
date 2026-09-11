import hashlib
import json

from database import database


def make_cache_key(source, mode, route):
    value = f"{route}:{mode}:{source}"

    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()


def get_cache(cache_key):
    connection = database()

    row = connection.execute(
        "SELECT file_ids FROM media_cache WHERE cache_key = ?",
        (cache_key,),
    ).fetchone()

    connection.close()

    if not row:
        return None

    try:
        return json.loads(row[0])
    except Exception:
        return None


def set_cache(cache_key, file_ids):
    connection = database()

    connection.execute(
        """
        INSERT INTO media_cache(cache_key, file_ids)
        VALUES(?, ?)
        ON CONFLICT(cache_key)
        DO UPDATE SET file_ids = excluded.file_ids
        """,
        (
            cache_key,
            json.dumps(
                file_ids,
                ensure_ascii=False,
            ),
        ),
    )

    connection.commit()
    connection.close()