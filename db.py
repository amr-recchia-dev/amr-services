"""
db.py — Database SQLite per tracciare le email già processate.
Evita duplicati anche se l'agente viene riavviato.
"""

import sqlite3
import logging
from pathlib import Path

import csv
import io
import os
import shutil

_BUNDLE_DB_PATH = str(Path(__file__).parent / "processed_emails.db")


def _resolve_db_path() -> str:
    env_path = os.getenv("DB_PATH", "").strip()
    if env_path:
        return env_path
    # Se siamo su Render col disco persistente montato (/var/data), usiamo quello
    base_dir = os.getenv("TIMBRA_LOG_DIR", "").strip() or "/var/data"
    if os.path.isdir(base_dir) and (os.getenv("TIMBRA_LOG_DIR") or os.path.ismount(base_dir)):
        persist_dir = os.path.join(base_dir, "timbrature")
        os.makedirs(persist_dir, exist_ok=True)
        return os.path.join(persist_dir, "processed_emails.db")
    return _BUNDLE_DB_PATH


DB_PATH = _resolve_db_path()
logger = logging.getLogger(__name__)


def init_db() -> None:
    """Crea il database e le tabelle se non esistono, migrando eventuali record dal file locale del repository."""
    if DB_PATH != _BUNDLE_DB_PATH and not os.path.exists(DB_PATH) and os.path.exists(_BUNDLE_DB_PATH):
        try:
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
            shutil.copy2(_BUNDLE_DB_PATH, DB_PATH)
            logger.info("📦 Copiato database iniziale da %s a %s", _BUNDLE_DB_PATH, DB_PATH)
        except Exception as e:
            logger.warning("Impossibile copiare DB iniziale su disco persistente: %s", e)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
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

    # Se il DB persistente esiste già e abbiamo anche il DB del bundle, uniamo eventuali record mancanti
    if DB_PATH != _BUNDLE_DB_PATH and os.path.exists(_BUNDLE_DB_PATH):
        try:
            with sqlite3.connect(_BUNDLE_DB_PATH) as src_conn:
                rows = src_conn.execute(
                    "SELECT message_id, subject, sender, tipo, monday_item_id, processed_at FROM processed_emails"
                ).fetchall()
            if rows:
                with sqlite3.connect(DB_PATH) as dst_conn:
                    dst_conn.executemany(
                        """INSERT OR IGNORE INTO processed_emails
                           (message_id, subject, sender, tipo, monday_item_id, processed_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        rows,
                    )
                    dst_conn.commit()
        except Exception as e:
            logger.debug("Merge opzionale da bundle DB ignorato: %s", e)

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
    return {"total": total, "by_type": dict(by_type), "db_path": DB_PATH}


def esporta_csv() -> str:
    """Esporta l'intero storico processed_emails in formato CSV."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["message_id", "subject", "sender", "tipo", "monday_item_id", "processed_at"])
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT message_id, subject, sender, tipo, monday_item_id, processed_at "
                "FROM processed_emails ORDER BY processed_at DESC"
            ).fetchall()
        for r in rows:
            writer.writerow(r)
    except Exception as e:
        logger.warning("Errore esportazione CSV processed_emails: %s", e)
    return buf.getvalue()

