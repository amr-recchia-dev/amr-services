"""
db.py — Database SQLite per tracciare le email già processate.
Evita duplicati anche se l'agente viene riavviato.
"""

import sqlite3
import logging
from pathlib import Path

import os
DB_PATH = os.getenv("DB_PATH", str(Path(__file__).parent / "processed_emails.db"))
logger = logging.getLogger(__name__)


def init_db() -> None:
    """Crea il database e le tabelle se non esistono."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_emails (
                message_id     TEXT PRIMARY KEY,
                subject        TEXT,
                sender         TEXT,
                tipo           TEXT,
                monday_item_id TEXT,
                processed_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_processed_at
            ON processed_emails (processed_at)
        """)
        conn.commit()
    logger.debug("Database inizializzato: %s", DB_PATH)


def is_already_processed(message_id: str) -> bool:
    """Restituisce True se l'email è già stata processata."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_emails WHERE message_id = ?",
            (message_id,)
        ).fetchone()
    return row is not None


def mark_as_processed(
    message_id: str,
    subject: str = "",
    sender: str = "",
    tipo: str = "spam",
    monday_item_id: str = ""
) -> None:
    """Marca un'email come processata nel DB."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT OR IGNORE INTO processed_emails
                (message_id, subject, sender, tipo, monday_item_id)
            VALUES (?, ?, ?, ?, ?)
        """, (message_id, subject, sender, tipo, monday_item_id))
        conn.commit()
    logger.debug("Email marcata come processata: %s", message_id)


def get_stats() -> dict:
    """Restituisce statistiche sull'elaborazione."""
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM processed_emails").fetchone()[0]
        by_type = conn.execute("""
            SELECT tipo, COUNT(*) as cnt
            FROM processed_emails
            GROUP BY tipo
        """).fetchall()
    return {"total": total, "by_type": dict(by_type)}
