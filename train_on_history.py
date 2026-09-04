"""
train_on_history.py — Script one-shot per generare client_patterns.json.

Legge le ultime N email dalla casella IMAP (anche già lette) e, per quelle
già registrate nel DB come processate, estrae il nome cliente classificato
da Gemini e lo mappa al nome dell'item corrispondente su Monday.com.

Il file generato (client_patterns.json) è usato da commessa_matcher.py per
migliorare il fuzzy matching.

Utilizzo:
    python train_on_history.py                  # ultime 500 email
    python train_on_history.py --limit 200      # ultime 200 email
    python train_on_history.py --dry-run        # mostra i mapping senza salvare
"""

import argparse
import imaplib
import email
import json
import logging
import os
import re
import sqlite3
from email.header import decode_header
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("train_on_history")

# ── Config ─────────────────────────────────────────────────────────────────────

IMAP_HOST       = os.getenv("IMAP_HOST", "mail.register.it")
IMAP_PORT       = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER       = os.getenv("IMAP_USER", "")
IMAP_PASSWORD   = os.getenv("IMAP_PASSWORD", "")
IMAP_FOLDER     = os.getenv("IMAP_FOLDER", "INBOX")

MONDAY_API_TOKEN = os.getenv("MONDAY_API_TOKEN", "")
BOARD_COMMERCIALE = os.getenv("MONDAY_BOARD_ID", "2133436509")
BOARD_PROGETTI    = "2136092569"
API_URL           = "https://api.monday.com/v2"

DB_PATH           = Path(__file__).parent / "processed_emails.db"
OUTPUT_PATH       = Path(__file__).parent / "client_patterns.json"


# ── Monday.com helpers ─────────────────────────────────────────────────────────

def _graphql(query: str, variables: Optional[Dict] = None) -> Dict:
    if not MONDAY_API_TOKEN:
        raise ValueError("MONDAY_API_TOKEN non configurato")
    headers = {
        "Authorization": MONDAY_API_TOKEN,
        "Content-Type": "application/json",
        "API-Version": "2024-10",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(API_URL, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Errori GraphQL: {data['errors']}")
    return data.get("data", {})


def fetch_monday_items() -> Dict[str, str]:
    """
    Recupera tutti gli item da entrambe le board Monday.com.
    Restituisce {item_id: item_name}.
    """
    items_map: Dict[str, str] = {}

    query_first = """
    query ($board_id: [ID!], $limit: Int!) {
      boards(ids: $board_id) {
        items_page(limit: $limit) {
          cursor
          items { id name }
        }
      }
    }
    """
    query_next = """
    query ($cursor: String!, $limit: Int!) {
      next_items_page(limit: $limit, cursor: $cursor) {
        cursor
        items { id name }
      }
    }
    """

    for board_id in [BOARD_COMMERCIALE, BOARD_PROGETTI]:
        logger.info("Recupero item dalla board %s...", board_id)
        try:
            data = _graphql(query_first, {"board_id": [board_id], "limit": 200})
            page = data.get("boards", [{}])[0].get("items_page", {})
            for item in page.get("items", []):
                items_map[item["id"]] = item["name"]
            cursor = page.get("cursor")

            while cursor:
                data = _graphql(query_next, {"cursor": cursor, "limit": 200})
                page = data.get("next_items_page", {})
                for item in page.get("items", []):
                    items_map[item["id"]] = item["name"]
                cursor = page.get("cursor")
                if not page.get("items"):
                    break

            logger.info("  Board %s: %d item trovati", board_id, len(items_map))
        except Exception as e:
            logger.error("Errore recupero item board %s: %s", board_id, e)

    return items_map


# ── DB helpers ─────────────────────────────────────────────────────────────────

def fetch_processed_emails() -> List[Dict]:
    """
    Legge il DB SQLite delle email processate.
    Restituisce lista di dict con: sender, tipo, monday_item_id.
    """
    if not DB_PATH.exists():
        logger.warning("DB non trovato: %s", DB_PATH)
        return []

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT sender, tipo, monday_item_id, subject
            FROM processed_emails
            WHERE tipo != 'spam'
              AND monday_item_id IS NOT NULL
              AND monday_item_id != ''
            ORDER BY processed_at DESC
        """).fetchall()

    return [dict(r) for r in rows]


# ── IMAP helpers ───────────────────────────────────────────────────────────────

def _decode_header_value(value: str) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            charset = charset or "utf-8"
            try:
                decoded.append(part.decode(charset, errors="replace"))
            except (LookupError, UnicodeDecodeError):
                decoded.append(part.decode("utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def _parse_sender(from_header: str) -> Tuple[str, str]:
    from_header = _decode_header_value(from_header)
    match = re.match(r"^(.*?)\s*<([^>]+)>$", from_header.strip())
    if match:
        name = match.group(1).strip().strip('"')
        addr = match.group(2).strip()
    else:
        name = ""
        addr = from_header.strip()
    return name, addr


def fetch_recent_emails(limit: int = 500) -> List[Dict]:
    """
    Recupera le ultime `limit` email (anche già lette) dalla casella IMAP.
    Restituisce lista di dict con: message_id, subject, sender_name, sender_email.
    """
    if not IMAP_USER or not IMAP_PASSWORD:
        raise ValueError("Credenziali IMAP mancanti nel .env")

    emails = []
    try:
        logger.info("Connessione IMAP a %s:%s come %s...", IMAP_HOST, IMAP_PORT, IMAP_USER)
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(IMAP_USER, IMAP_PASSWORD)
        mail.select(IMAP_FOLDER)

        # Cerca TUTTE le email (non solo UNSEEN)
        status, data = mail.uid("search", None, "ALL")
        if status != "OK" or not data or not data[0]:
            logger.info("Nessuna email trovata.")
            mail.logout()
            return []

        uid_list = data[0].split()
        # Prendi le ultime `limit` email (le più recenti)
        uid_list = uid_list[-limit:]
        logger.info("Recupero le ultime %d email su %d totali...", len(uid_list), len(data[0].split()))

        for uid in reversed(uid_list):  # dalla più recente
            try:
                # Fetch solo headers per efficienza
                status, msg_data = mail.uid("fetch", uid, "(BODY[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT)])")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue

                raw_headers = msg_data[0][1]
                msg = email.message_from_bytes(raw_headers)

                message_id = msg.get("Message-ID", "").strip()
                if not message_id:
                    subj = _decode_header_value(msg.get("Subject", ""))
                    message_id = f"fallback-{subj}"

                subject = _decode_header_value(msg.get("Subject", ""))
                sender_name, sender_email = _parse_sender(msg.get("From", ""))

                emails.append({
                    "message_id": message_id,
                    "subject": subject,
                    "sender_name": sender_name,
                    "sender_email": sender_email,
                })
            except Exception as e:
                logger.debug("Errore parsing headers uid=%s: %s", uid, e)
                continue

        mail.logout()
        logger.info("Recuperate %d email dall'IMAP.", len(emails))

    except Exception as e:
        logger.error("Errore IMAP: %s", e)
        raise

    return emails


# ── Logica principale ──────────────────────────────────────────────────────────

def extract_sender_name(sender_name: str, sender_email: str) -> str:
    """
    Estrae il nome azienda dal mittente.
    Usa il dominio email come fallback se il nome è assente.
    """
    if sender_name and len(sender_name) > 2:
        return sender_name.strip()
    # Usa la parte del dominio email (es. "example" da "mario@example.com")
    domain = sender_email.split("@")[-1].split(".")[0]
    return domain.capitalize() if domain else sender_email


def build_patterns(
    processed_emails: List[Dict],
    monday_items: Dict[str, str],
    imap_emails: List[Dict],
) -> Dict[str, str]:
    """
    Costruisce il mapping {nome_email → nome_monday} incrociando:
    - DB email processate (ha il monday_item_id e il sender)
    - Item Monday (ha l'item_id → nome)
    - Email IMAP (ha il message_id → sender_name/email)

    Logica:
    1. Per ogni email nel DB con monday_item_id:
       a. Trova il nome Monday dell'item
       b. Trova il sender dell'email dall'IMAP (by message_id) o dal DB
       c. Crea il mapping sender_name → item_name
    """
    # Indice rapido IMAP: message_id → email_dict
    imap_index: Dict[str, Dict] = {e["message_id"]: e for e in imap_emails}

    patterns: Dict[str, str] = {}
    matched = 0
    skipped = 0

    for record in processed_emails:
        item_id = record.get("monday_item_id", "")
        item_name = monday_items.get(item_id)
        if not item_name:
            skipped += 1
            continue

        # Cerca il sender dall'IMAP
        sender_name = None
        sender_email = record.get("sender", "")

        # Cerca per message_id (se disponibile nell'IMAP)
        imap_record = imap_index.get(record.get("message_id", ""))
        if imap_record:
            sender_name = imap_record.get("sender_name", "")
            if not sender_name:
                sender_email = imap_record.get("sender_email", sender_email)

        # Se non trovato via IMAP, usa il sender dal DB
        if not sender_name:
            sender_name = extract_sender_name("", sender_email)

        if sender_name and item_name:
            patterns[sender_name] = item_name
            logger.debug("Pattern: '%s' → '%s'", sender_name, item_name)
            matched += 1
        else:
            skipped += 1

    logger.info(
        "Pattern generati: %d | Skippati (item non trovato): %d",
        matched, skipped
    )
    return patterns


def main():
    parser = argparse.ArgumentParser(
        description="Genera client_patterns.json dalla storia email IMAP + DB + Monday.com"
    )
    parser.add_argument(
        "--limit", type=int, default=500,
        help="Numero massimo di email IMAP da leggere (default: 500)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Mostra i mapping senza salvare il file"
    )
    parser.add_argument(
        "--output", type=str, default=str(OUTPUT_PATH),
        help=f"Percorso output JSON (default: {OUTPUT_PATH})"
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("🏋️  TRAINING: Generazione client_patterns.json")
    logger.info("=" * 60)

    # 1. Recupera item Monday
    logger.info("📋 Recupero item Monday.com...")
    monday_items = fetch_monday_items()
    logger.info("   Totale item: %d", len(monday_items))

    # 2. Leggi email già processate dal DB
    logger.info("🗄️  Lettura email processate dal DB...")
    processed = fetch_processed_emails()
    logger.info("   Totale record: %d", len(processed))

    # 3. Leggi email recenti dall'IMAP (per avere i sender)
    logger.info("📬 Lettura ultime %d email dall'IMAP...", args.limit)
    try:
        imap_emails = fetch_recent_emails(args.limit)
    except Exception as e:
        logger.error("Impossibile connettersi all'IMAP: %s", e)
        logger.warning("Procedo senza dati IMAP (mapping basato solo sul DB)")
        imap_emails = []

    # 4. Costruisci i pattern
    logger.info("🔗 Costruzione mapping nome_email → nome_monday...")
    patterns = build_patterns(processed, monday_items, imap_emails)

    # 5. Mostra risultato
    logger.info("\n📊 RISULTATI:")
    logger.info("-" * 40)
    if patterns:
        for sender, item in sorted(patterns.items()):
            logger.info("  %-40s → %s", sender, item)
    else:
        logger.info("  Nessun pattern trovato.")

    # 6. Salva (o dry-run)
    if args.dry_run:
        logger.info("\n🧪 DRY-RUN: file NON salvato.")
    else:
        output_path = Path(args.output)

        # Merge con eventuali pattern esistenti
        existing = {}
        if output_path.exists():
            try:
                with open(output_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                logger.info("📂 Pattern esistenti caricati: %d", len(existing))
            except Exception as e:
                logger.warning("Impossibile leggere file esistente: %s", e)

        merged = {**existing, **patterns}  # i nuovi sovrascrivono i vecchi

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)

        logger.info("\n✅ Salvato: %s (%d pattern totali)", output_path, len(merged))

    logger.info("=" * 60)
    logger.info("✅ Training completato!")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
