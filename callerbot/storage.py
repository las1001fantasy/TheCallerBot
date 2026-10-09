"""Persistencia en SQLite: una liga por canal (grupo o tema) y los usuarios de Telegram de cada liga."""

from __future__ import annotations

import sqlite3

# thread_key = id del tema del grupo, o 0 si el grupo no tiene temas.
SCHEMA = """
CREATE TABLE IF NOT EXISTS leagues (
    chat_id          INTEGER NOT NULL,
    thread_key       INTEGER NOT NULL DEFAULT 0,
    league_id        INTEGER NOT NULL,
    sport            TEXT    NOT NULL,
    commish          TEXT,
    drafting         INTEGER NOT NULL DEFAULT 0,  -- 1 entre /startDraft y /stopDraft
    last_overall     INTEGER,                     -- pick anunciado como OTC
    otc_since        REAL,                        -- cuándo empezó ese turno (epoch)
    last_reminder    REAL,
    commish_notified INTEGER NOT NULL DEFAULT 0,
    reminder_hours   INTEGER NOT NULL DEFAULT 2,
    notify_hours     INTEGER NOT NULL DEFAULT 8,
    PRIMARY KEY (chat_id, thread_key)
);
CREATE TABLE IF NOT EXISTS users (
    chat_id    INTEGER NOT NULL,
    thread_key INTEGER NOT NULL DEFAULT 0,
    ff_user    TEXT    NOT NULL,   -- usuario de Fleaflicker en minúsculas
    telegram   TEXT    NOT NULL,
    PRIMARY KEY (chat_id, thread_key, ff_user)
);
"""

LEAGUE_FIELDS = {
    "commish", "drafting", "last_overall", "otc_since", "last_reminder",
    "commish_notified", "reminder_hours", "notify_hours",
}


class Storage:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def get_league(self, chat_id: int, thread_key: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM leagues WHERE chat_id = ? AND thread_key = ?", (chat_id, thread_key)
        ).fetchone()

    def set_league(self, chat_id: int, thread_key: int, league_id: int, sport: str, commish: str | None) -> None:
        self.db.execute(
            "DELETE FROM leagues WHERE chat_id = ? AND thread_key = ?", (chat_id, thread_key)
        )
        self.db.execute(
            "INSERT INTO leagues (chat_id, thread_key, league_id, sport, commish) VALUES (?, ?, ?, ?, ?)",
            (chat_id, thread_key, league_id, sport, commish),
        )
        self.db.commit()

    def update_league(self, chat_id: int, thread_key: int, **fields) -> None:
        assert fields and set(fields) <= LEAGUE_FIELDS
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(
            f"UPDATE leagues SET {cols} WHERE chat_id = ? AND thread_key = ?",
            (*fields.values(), chat_id, thread_key),
        )
        self.db.commit()

    def unset_league(self, chat_id: int, thread_key: int) -> None:
        for table in ("leagues", "users"):
            self.db.execute(f"DELETE FROM {table} WHERE chat_id = ? AND thread_key = ?", (chat_id, thread_key))
        self.db.commit()

    def drafting_leagues(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM leagues WHERE drafting = 1").fetchall()

    def add_user(self, chat_id: int, thread_key: int, ff_user: str, telegram: str) -> None:
        self.db.execute(
            """INSERT INTO users (chat_id, thread_key, ff_user, telegram) VALUES (?, ?, ?, ?)
               ON CONFLICT (chat_id, thread_key, ff_user) DO UPDATE SET telegram = excluded.telegram""",
            (chat_id, thread_key, ff_user.lower(), telegram),
        )
        self.db.commit()

    def users(self, chat_id: int, thread_key: int) -> dict[str, str]:
        rows = self.db.execute(
            "SELECT ff_user, telegram FROM users WHERE chat_id = ? AND thread_key = ?", (chat_id, thread_key)
        ).fetchall()
        return {r["ff_user"]: r["telegram"] for r in rows}
