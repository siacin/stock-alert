from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .models import AlertEvent


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS states (
                trade_date TEXT NOT NULL,
                code TEXT NOT NULL,
                scope TEXT NOT NULL,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (trade_date, code, scope)
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT NOT NULL,
                event_type TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                price REAL NOT NULL,
                line_price REAL,
                sources TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_alerts_dedupe_time
            ON alerts (dedupe_key, occurred_at DESC);
            """
        )
        self.connection.commit()

    def load_states(self, trade_date: str) -> dict[tuple[str, str], dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT code, scope, state_json FROM states WHERE trade_date = ?",
            (trade_date,),
        ).fetchall()
        result: dict[tuple[str, str], dict[str, Any]] = {}
        for code, scope, state_json in rows:
            try:
                result[(code, scope)] = json.loads(state_json)
            except json.JSONDecodeError:
                continue
        return result

    def save_states(self, trade_date: str, states: dict[tuple[str, str], dict[str, Any]], now: datetime) -> None:
        rows = [
            (
                trade_date,
                code,
                scope,
                json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                now.isoformat(),
            )
            for (code, scope), state in states.items()
        ]
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO states (trade_date, code, scope, state_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, code, scope) DO UPDATE SET
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at
                """,
                rows,
            )

    def can_emit(self, event: AlertEvent, cooldown_seconds: int) -> bool:
        row = self.connection.execute(
            "SELECT occurred_at FROM alerts WHERE dedupe_key = ? ORDER BY occurred_at DESC LIMIT 1",
            (event.dedupe_key,),
        ).fetchone()
        if not row:
            return True
        try:
            previous = datetime.fromisoformat(row[0])
        except ValueError:
            return True
        return event.occurred_at - previous >= timedelta(seconds=max(0, cooldown_seconds))

    def record_event(self, event: AlertEvent) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO alerts (
                    dedupe_key, event_type, code, name, occurred_at, price,
                    line_price, sources, severity, message, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.dedupe_key,
                    event.event_type,
                    event.code,
                    event.name,
                    event.occurred_at.isoformat(),
                    event.price,
                    event.line_price,
                    ",".join(event.sources),
                    event.severity,
                    event.message,
                    json.dumps(event.metadata, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def close(self) -> None:
        self.connection.close()
