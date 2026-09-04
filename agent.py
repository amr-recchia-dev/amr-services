"""
agent.py — Loop principale dell'agente email.
Legge email non lette, le classifica con Gemini AI e crea item su Monday.com.

Utilizzo:
    python agent.py              # esegue una volta e termina
    python agent.py --daemon     # esegue in loop ogni CHECK_INTERVAL_MINUTES minuti
    python agent.py --test       # modalità dry-run (non crea item, non marca email)
    python agent.py --setup-board # aggiunge colonne mancanti alla board Monday.com
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime

import schedule
from dotenv import load_dotenv

from db import init_db, is_already_processed, mark_as_processed, get_stats
from email_reader import fetch_unseen_emails, mark_email_as_read
from ai_classifier import classify_email
from monday_client import create_item, setup_board_columns
from commessa_matcher import find_commessa, add_email_update

load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────────────

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(os.path.dirname(__file__), "agent.log"),
            encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger("email_agent")


# ── Logica Principale ─────────────────────────────────────────────────────────

def run_once(dry_run: bool = False) -> None:
    """
    Esegue un ciclo completo:
    1. Fetch email non lette
    2. Classifica con Gemini
    3. Crea item su Monday.com
    4. Segna email come processata
    """
    logger.info("═" * 60)
    logger.info("⏰ Avvio ciclo — %s", datetime.now().strftime("%d/%m/%Y %H:%M:%S"))
    if dry_run:
        logger.info("🧪 MODALITÀ DRY-RUN: nessuna modifica sarà effettuata")
    logger.info("═" * 60)

    # 1. Leggi email
    try:
        emails = fetch_unseen_emails()
    except Exception as e:
        logger.error("❌ Impossibile leggere le email: %s", e)
        return

    if not emails:
        logger.info("📭 Nessuna nuova email da processare.")
        return

    logger.info("📬 %d email da analizzare.", len(emails))

    processed = 0
    skipped_duplicate = 0
    skipped_spam = 0
    errors = 0

    for email_obj in emails:
        try:
            # 2. Controlla duplicati
            if is_already_processed(email_obj.message_id):
                logger.debug("⏭️  Già processata: %s", email_obj.subject[:40])
                skipped_duplicate += 1
                continue

            logger.info("📧 Analisi: [%s] %s", email_obj.sender_email, email_obj.subject[:50])

            # 3. Classifica con Gemini
            result = classify_email(
                subject=email_obj.subject,
                body=email_obj.body,
                sender_name=email_obj.sender_name,
                sender_email=email_obj.sender_email,
            )

            if not result.is_relevant:
                logger.info("🗑️  Ignorata (spam): %s", email_obj.subject[:40])
                skipped_spam += 1
                if not dry_run:
                    mark_as_processed(
                        message_id=email_obj.message_id,
                        subject=email_obj.subject,
                        sender=email_obj.sender_email,
                        tipo="spam",
                    )
                    if email_obj.raw_uid:
                        mark_email_as_read(email_obj.raw_uid)
                continue

            # 4. Crea item su Monday.com
            logger.info(
                "🏢 Tipo=%s | Azienda=%s | Progetto=%s",
                result.tipo, result.azienda, result.nome_progetto[:40]
            )

            monday_item_id = None
            if not dry_run:
                # ── Cerca commessa esistente su Monday.com ─────────────────
                match = find_commessa(result)

                if match.found and not match.ambiguous:
                    # Aggiorna commessa esistente con un nuovo update
                    logger.info(
                        "🔗 Match trovato: '%s' (id: %s, board: %s)",
                        match.item_name, match.item_id, match.board_id
                    )
                    success = add_email_update(
                        match.item_id, match.board_id, result, email_obj.subject
                    )
                    monday_item_id = match.item_id if success else None
                    if not success:
                        logger.error(
                            "❌ Aggiornamento commessa fallito per: %s",
                            email_obj.subject
                        )
                        errors += 1
                        continue

                elif match.ambiguous:
                    # Match ambiguo → crea nuovo item con flag di verifica
                    candidati_str = ", ".join(c["name"] for c in match.candidates)
                    logger.warning(
                        "⚠️ Match ambiguo per '%s' (%d candidati) → creo item con flag",
                        result.azienda, len(match.candidates)
                    )
                    result.note = (
                        f"⚠️ Da verificare: possibili commesse esistenti: {candidati_str}\n\n"
                        + result.note
                    )
                    monday_item_id = create_item(result)
                    if not monday_item_id:
                        logger.error(
                            "❌ Creazione item (ambiguo) fallita per: %s",
                            email_obj.subject
                        )
                        errors += 1
                        continue

                else:
                    # Nessun match → crea nuovo item (flusso originale)
                    logger.info(
                        "➕ Nessuna commessa trovata → creo nuovo item per '%s'",
                        result.azienda
                    )
                    monday_item_id = create_item(result)
                    if not monday_item_id:
                        logger.error(
                            "❌ Creazione item fallita per: %s", email_obj.subject
                        )
                        errors += 1
                        continue

                # Segna come processata
                mark_as_processed(
                    message_id=email_obj.message_id,
                    subject=email_obj.subject,
                    sender=email_obj.sender_email,
                    tipo=result.tipo,
                    monday_item_id=monday_item_id or "",
                )
                if email_obj.raw_uid:
                    mark_email_as_read(email_obj.raw_uid)

            logger.info(
                "✅ Processata con successo: %s → Monday item #%s",
                result.azienda, monday_item_id or "DRY_RUN"
            )
            processed += 1

            # Ritardo per non superare il rate limit dell'API Gemini (max 15 RPM su free tier)
            if not dry_run:
                logger.info("⏳ Attesa 6 secondi prima della prossima chiamata API Gemini...")
                time.sleep(6)

        except Exception as e:
            logger.error("❌ Errore processando email '%s': %s", email_obj.subject[:40], e)
            errors += 1
            continue

    # Report finale del ciclo
    logger.info("─" * 60)
    logger.info(
        "📊 Ciclo completato — ✅ %d inseriti | 🗑️  %d spam | ⏭️  %d duplicati | ❌ %d errori",
        processed, skipped_spam, skipped_duplicate, errors
    )

    # Statistiche DB
    stats = get_stats()
    logger.info("📈 Totale email nel DB: %d (%s)", stats["total"], stats["by_type"])


# ── Entry Point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Agente Email → Monday.com NEW COMMERCIALE per AMR Recchia",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="Esegui in loop continuo ogni CHECK_INTERVAL_MINUTES minuti"
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Dry-run: analizza le email ma non crea item né marca come lette"
    )
    parser.add_argument(
        "--setup-board", action="store_true",
        help="Aggiunge le colonne mancanti alla board Monday.com e termina"
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Mostra le statistiche del database e termina"
    )
    args = parser.parse_args()

    # Inizializza DB
    init_db()

    if args.stats:
        stats = get_stats()
        print(f"\n📊 Statistiche Database Email:\n  Totale processate: {stats['total']}")
        for tipo, cnt in stats["by_type"].items():
            print(f"  {tipo}: {cnt}")
        return

    if args.setup_board:
        logger.info("🔧 Configurazione colonne board Monday.com...")
        setup_board_columns()
        logger.info("✅ Setup board completato.")
        return

    if args.daemon:
        logger.info(
            "🤖 Agente avviato in modalità DAEMON (ogni %d min)",
            CHECK_INTERVAL
        )
        # Prima esecuzione immediata
        run_once(dry_run=args.test)

        # Poi schedula ogni N minuti
        schedule.every(CHECK_INTERVAL).minutes.do(run_once, dry_run=args.test)

        while True:
            schedule.run_pending()
            time.sleep(30)
    else:
        # Esecuzione singola
        logger.info("🤖 Agente avviato (esecuzione singola)")
        run_once(dry_run=args.test)
        logger.info("✅ Esecuzione completata.")


if __name__ == "__main__":
    main()
