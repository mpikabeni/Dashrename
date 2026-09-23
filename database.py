# database.py

import sqlite3
from pathlib import Path
from datetime import datetime

try:
    from config import DATABASE_FILE
except ImportError:
    DATABASE_FILE = str(
        Path(__file__).resolve().parent / "dash.db"
    )


# ============================================================
# DATABASE DASH
# ============================================================


def get_connection():
    connection = sqlite3.connect(
        DATABASE_FILE,
        timeout=30
    )

    connection.row_factory = sqlite3.Row

    connection.execute(
        "PRAGMA journal_mode=WAL"
    )

    connection.execute(
        "PRAGMA foreign_keys=ON"
    )

    return connection


# ============================================================
# INIT
# ============================================================


def init_db():

    connection = get_connection()

    try:

        connection.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                last_name TEXT DEFAULT '',
                language TEXT DEFAULT 'fr',
                created_at TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS rename_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                old_name TEXT NOT NULL,
                new_name TEXT NOT NULL,
                file_type TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                sent INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            )
        """)

        connection.commit()

    finally:
        connection.close()


# ============================================================
# USERS
# ============================================================


def save_user(
    user_id,
    username="",
    first_name="",
    last_name="",
    language="fr"
):

    now = datetime.utcnow().isoformat()

    connection = get_connection()

    try:

        connection.execute("""
            INSERT INTO users (
                user_id,
                username,
                first_name,
                last_name,
                language,
                created_at,
                last_seen
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(user_id)
            DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                language=excluded.language,
                last_seen=excluded.last_seen
        """, (
            user_id,
            username or "",
            first_name or "",
            last_name or "",
            language or "fr",
            now,
            now
        ))

        connection.commit()

    finally:
        connection.close()


def get_user(user_id):

    connection = get_connection()

    try:

        row = connection.execute("""
            SELECT *
            FROM users
            WHERE user_id=?
        """, (
            user_id,
        )).fetchone()

        return dict(row) if row else None

    finally:
        connection.close()


def get_users():

    connection = get_connection()

    try:

        rows = connection.execute("""
            SELECT user_id
            FROM users
            ORDER BY created_at ASC
        """).fetchall()

        return [
            row["user_id"]
            for row in rows
        ]

    finally:
        connection.close()


def get_all_users():

    connection = get_connection()

    try:

        rows = connection.execute("""
            SELECT *
            FROM users
            ORDER BY created_at DESC
        """).fetchall()

        return [
            dict(row)
            for row in rows
        ]

    finally:
        connection.close()


def user_count():

    connection = get_connection()

    try:

        row = connection.execute("""
            SELECT COUNT(*) AS total
            FROM users
        """).fetchone()

        return int(
            row["total"]
        )

    finally:
        connection.close()


# ============================================================
# RENAME HISTORY
# ============================================================


def add_rename(
    user_id,
    old_name,
    new_name,
    file_type=""
):

    connection = get_connection()

    try:

        connection.execute("""
            INSERT INTO rename_history (
                user_id,
                old_name,
                new_name,
                file_type,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            user_id,
            old_name,
            new_name,
            file_type,
            datetime.utcnow().isoformat()
        ))

        connection.commit()

    finally:
        connection.close()


def get_history(
    user_id,
    limit=20
):

    connection = get_connection()

    try:

        rows = connection.execute("""
            SELECT
                id,
                old_name,
                new_name,
                file_type,
                created_at
            FROM rename_history
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT ?
        """, (
            user_id,
            limit
        )).fetchall()

        return [
            dict(row)
            for row in rows
        ]

    finally:
        connection.close()


def rename_count():

    connection = get_connection()

    try:

        row = connection.execute("""
            SELECT COUNT(*) AS total
            FROM rename_history
        """).fetchone()

        return int(
            row["total"]
        )

    finally:
        connection.close()


# ============================================================
# BROADCAST
# ============================================================


def save_broadcast(
    admin_id,
    sent,
    failed
):

    connection = get_connection()

    try:

        connection.execute("""
            INSERT INTO broadcasts (
                admin_id,
                sent,
                failed,
                created_at
            )
            VALUES (?, ?, ?, ?)
        """, (
            admin_id,
            sent,
            failed,
            datetime.utcnow().isoformat()
        ))

        connection.commit()

    finally:
        connection.close()


def get_broadcast_count():

    connection = get_connection()

    try:

        row = connection.execute("""
            SELECT COUNT(*) AS total
            FROM broadcasts
        """).fetchone()

        return int(
            row["total"]
        )

    finally:
        connection.close()


# ============================================================
# SETTINGS
# ============================================================


def set_setting(
    key,
    value
):

    connection = get_connection()

    try:

        connection.execute("""
            INSERT INTO settings (
                key,
                value
            )
            VALUES (?, ?)

            ON CONFLICT(key)
            DO UPDATE SET
                value=excluded.value
        """, (
            key,
            str(value)
        ))

        connection.commit()

    finally:
        connection.close()


def get_setting(
    key,
    default=None
):

    connection = get_connection()

    try:

        row = connection.execute("""
            SELECT value
            FROM settings
            WHERE key=?
        """, (
            key,
        )).fetchone()

        if not row:
            return default

        return row["value"]

    finally:
        connection.close()


# ============================================================
# STATISTICS
# ============================================================


def get_statistics():

    connection = get_connection()

    try:

        users = connection.execute("""
            SELECT COUNT(*) AS total
            FROM users
        """).fetchone()["total"]

        renames = connection.execute("""
            SELECT COUNT(*) AS total
            FROM rename_history
        """).fetchone()["total"]

        broadcasts = connection.execute("""
            SELECT COUNT(*) AS total
            FROM broadcasts
        """).fetchone()["total"]

        return {
            "users": int(users),
            "renames": int(renames),
            "broadcasts": int(broadcasts)
        }

    finally:
        connection.close()


# ============================================================
# AUTO INITIALIZATION
# ============================================================

init_db()
