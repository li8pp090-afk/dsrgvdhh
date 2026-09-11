import sqlite3

from config import DATABASE


def database():
    connection = sqlite3.connect(DATABASE)

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_modes (
            chat_key TEXT PRIMARY KEY,
            mode TEXT NOT NULL DEFAULT 'default'
        )
        """
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS media_cache (
            cache_key TEXT PRIMARY KEY,
            file_ids TEXT NOT NULL
        )
        """
    )

    connection.commit()
    return connection


def get_mode(chat_key):
    connection = database()

    row = connection.execute(
        "SELECT mode FROM chat_modes WHERE chat_key = ?",
        (chat_key,),
    ).fetchone()

    connection.close()

    return row[0] if row else "default"


def set_mode(chat_key, mode):
    connection = database()

    connection.execute(
        """
        INSERT INTO chat_modes(chat_key, mode)
        VALUES(?, ?)
        ON CONFLICT(chat_key)
        DO UPDATE SET mode = excluded.mode
        """,
        (chat_key, mode),
    )

    connection.commit()
    connection.close()