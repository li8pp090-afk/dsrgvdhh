import hashlib
import json

from database import database


def make_cache_key(source, mode, route):
    if mode == "voice":
        value = f"voice:{source}"
    else:
        value = f"{route}:{mode}:{source}"

    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()


def get_cache(cache_key):
    connection = database()

    try:
        row = connection.execute(
            "SELECT file_ids FROM media_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
    finally:
        connection.close()

    if not row:
        return None

    try:
        file_ids = json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None

    if not isinstance(file_ids, list):
        return None

    file_ids = [
        file_id
        for file_id in file_ids
        if isinstance(file_id, str) and file_id
    ]

    return file_ids or None


def set_cache(cache_key, file_ids):
    if not file_ids:
        return

    connection = database()

    try:
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
                    list(file_ids),
                    ensure_ascii=False,
                ),
            ),
        )

        connection.commit()
    finally:
        connection.close()