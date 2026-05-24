from __future__ import annotations

import sqlite3
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .paths import history_db_path


@dataclass
class HistoryItem:
    id: int
    created_at: str
    model_key: str
    duration_seconds: float
    inserted: bool
    text: str
    source: str = "dictation"
    file_name: str = ""
    file_path: str = ""
    segments_json: str = ""
    summary: str = ""


class HistoryStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or history_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transcripts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    model_key TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    inserted INTEGER NOT NULL,
                    text TEXT NOT NULL
                )
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(transcripts)").fetchall()}
            if "source" not in columns:
                conn.execute("ALTER TABLE transcripts ADD COLUMN source TEXT NOT NULL DEFAULT 'dictation'")
            if "file_name" not in columns:
                conn.execute("ALTER TABLE transcripts ADD COLUMN file_name TEXT NOT NULL DEFAULT ''")
            if "file_path" not in columns:
                conn.execute("ALTER TABLE transcripts ADD COLUMN file_path TEXT NOT NULL DEFAULT ''")
            if "segments_json" not in columns:
                conn.execute("ALTER TABLE transcripts ADD COLUMN segments_json TEXT NOT NULL DEFAULT ''")
            if "summary" not in columns:
                conn.execute("ALTER TABLE transcripts ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
            conn.commit()

    def add(
        self,
        model_key: str,
        duration_seconds: float,
        inserted: bool,
        text: str,
        *,
        source: str = "dictation",
        file_name: str = "",
        file_path: str = "",
        segments_json: str = "",
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO transcripts(created_at, model_key, duration_seconds, inserted, text, source, file_name, file_path, segments_json, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (datetime.now().isoformat(timespec="seconds"), model_key, duration_seconds, 1 if inserted else 0, text, source, file_name, file_path, segments_json, ""),
            )
            conn.commit()
            return int(cur.lastrowid)

    def recent(self, limit: int = 100) -> list[HistoryItem]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM transcripts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [
            HistoryItem(
                id=int(r["id"]),
                created_at=str(r["created_at"]),
                model_key=str(r["model_key"]),
                duration_seconds=float(r["duration_seconds"]),
                inserted=bool(r["inserted"]),
                text=str(r["text"]),
                source=str(r["source"] if "source" in r.keys() else "dictation"),
                file_name=str(r["file_name"] if "file_name" in r.keys() else ""),
                file_path=str(r["file_path"] if "file_path" in r.keys() else ""),
                segments_json=str(r["segments_json"] if "segments_json" in r.keys() else ""),
                summary=str(r["summary"] if "summary" in r.keys() else ""),
            )
            for r in rows
        ]

    def update_summary(self, item_id: int, summary: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE transcripts SET summary = ? WHERE id = ?", (summary, item_id))
            conn.commit()

    def delete(self, item_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM transcripts WHERE id = ?", (item_id,))
            conn.commit()

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM transcripts")
            conn.commit()
