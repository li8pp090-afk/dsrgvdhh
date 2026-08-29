import os
import sqlite3


DB_PATH = os.getenv(
    "DB_PATH",
    "bot.db"
)


def connection():
    return sqlite3.connect(
        DB_PATH
    )


def init_file_database():
    conn = connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            content_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            media_type TEXT NOT NULL,
            file_id TEXT NOT NULL,
            PRIMARY KEY (
                content_id,
                mode,
                media_type
            )
        )
    """)

    conn.commit()
    conn.close()


def get_file(
    content_id,
    mode,
    media_type
):
    conn = connection()

    row = conn.execute(
        """
        SELECT file_id
        FROM files
        WHERE content_id = ?
        AND mode = ?
        AND media_type = ?
        """,
        (
            content_id,
            mode,
            media_type
        )
    ).fetchone()

    conn.close()

    return row[0] if row else None


def save_file(
    content_id,
    mode,
    media_type,
    file_id
):
    conn = connection()

    conn.execute(
        """
        INSERT OR REPLACE INTO files
        (
            content_id,
            mode,
            media_type,
            file_id
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            content_id,
            mode,
            media_type,
            file_id
        )
    )

    conn.commit()
    conn.close()