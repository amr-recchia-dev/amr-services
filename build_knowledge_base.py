"""
build_knowledge_base.py — Costruisce la knowledge base dei clienti AMR Recchia
analizzando le email archiviate nelle cartelle IMAP.

Flusso:
  1. Lista tutte le cartelle IMAP (escludendo INBOX, Sent, Spam, Trash, ecc.)
  2. Per ogni cartella (= cliente), legge le ultime 20 email
  3. Invia le email a Gemini 2.5 Flash per l'analisi
  4. Salva i risultati in client_knowledge_base.json
  5. Salva progressi intermedi ogni 10 clienti

Uso:
    .venv/bin/python3 build_knowledge_base.py
"""

import imaplib
import email
import json
import time
import os
import logging
import re
import requests
from datetime import datetime
from email.header import decode_header, make_header
from email.utils import parseaddr
from pathlib import Path
from dotenv import load_dotenv

# ── Configurazione ─────────────────────────────────────────────────────────────

load_dotenv()

IMAP_SERVER   = os.getenv("IMAP_HOST", os.getenv("IMAP_SERVER", ""))
IMAP_PORT     = int(os.getenv("IMAP_PORT", 993))
IMAP_USER     = os.getenv("IMAP_USER", "")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

BASE_DIR   = Path(__file__).parent
OUTPUT_FILE    = BASE_DIR / "client_knowledge_base.json"
PARTIAL_FILE   = BASE_DIR / "client_knowledge_base_partial.json"

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash:generateContent"
)

# Cartelle da escludere (case-insensitive) — sistema, non clienti
EXCLUDED_FOLDERS = {
    "inbox", "sent", "sent messages", "sent items", "posta inviata",
    "spam", "junk", "junk email", "trash", "deleted", "deleted items",
    "cestino", "posta eliminata", "bozze", "drafts", "outbox",
    "archive", "archivio", "notes", "note", "[gmail]",
    "all mail", "tutti i messaggi", "flagged", "important",
}

MAX_EMAILS_PER_FOLDER = 20
MAX_BODY_CHARS        = 1000
SAVE_EVERY_N          = 10      # salva parziale ogni N clienti analizzati
GEMINI_DELAY_SEC      = 3.0     # delay tra chiamate Gemini (~20 RPM)

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BASE_DIR / "build_knowledge_base.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ── Helpers di decodifica ──────────────────────────────────────────────────────

def _decode_str(raw) -> str:
    """Decodifica un header o stringa con charset multipli."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        for enc in ("utf-8", "iso-8859-1", "windows-1252", "latin-1"):
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                pass
        return raw.decode("utf-8", errors="replace")
    try:
        parts = decode_header(str(raw))
        decoded_parts = []
        for part, charset in parts:
            if isinstance(part, bytes):
                if charset:
                    try:
                        decoded_parts.append(part.decode(charset, errors="replace"))
                    except (LookupError, UnicodeDecodeError):
                        decoded_parts.append(part.decode("utf-8", errors="replace"))
                else:
                    decoded_parts.append(part.decode("utf-8", errors="replace"))
            else:
                decoded_parts.append(str(part))
        return " ".join(decoded_parts).strip()
    except Exception:
        return str(raw)


def _decode_folder_name(raw_name: bytes) -> str:
    """Decodifica il nome di una cartella IMAP (possibile modified UTF-7)."""
    if isinstance(raw_name, bytes):
        # Prova UTF-8 prima
        try:
            return raw_name.decode("utf-8")
        except UnicodeDecodeError:
            pass
        # Prova modified UTF-7 (standard IMAP)
        try:
            return raw_name.decode("utf-7")
        except (UnicodeDecodeError, LookupError):
            pass
        return raw_name.decode("latin-1", errors="replace")
    return str(raw_name)


def _extract_folder_name(folder_line) -> str:
    """Estrae il nome della cartella dalla risposta LIST di IMAP."""
    if isinstance(folder_line, bytes):
        folder_line = _decode_folder_name(folder_line)

    # Formato tipico: (\HasNoChildren) "/" "NOME" oppure (\HasNoChildren) "/" NOME
    # Cerca l'ultimo token dopo il separatore
    match = re.search(r'"([^"]+)"\s*$', folder_line)
    if match:
        return match.group(1)

    # Prova senza virgolette
    parts = folder_line.strip().rsplit(" ", 1)
    if parts:
        name = parts[-1].strip('"').strip()
        return name

    return folder_line.strip()


def _get_email_body(msg) -> str:
    """Estrae il corpo in plain text da un messaggio email (max MAX_BODY_CHARS chars)."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disp  = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in content_disp:
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                if payload:
                    try:
                        body = payload.decode(charset, errors="replace")
                    except (LookupError, UnicodeDecodeError):
                        body = payload.decode("utf-8", errors="replace")
                    break
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            try:
                body = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                body = payload.decode("utf-8", errors="replace")

    # Pulisci e tronca
    body = re.sub(r"\r\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body[:MAX_BODY_CHARS]


# ── IMAP ───────────────────────────────────────────────────────────────────────

IMAP_TIMEOUT = 30  # secondi di timeout socket IMAP


def connect_imap() -> imaplib.IMAP4_SSL:
    """Apre la connessione IMAP con timeout e fa il login."""
    import socket
    log.info("📡 Connessione IMAP a %s:%d come %s", IMAP_SERVER, IMAP_PORT, IMAP_USER)
    imap = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    # Imposta timeout sul socket sottostante per evitare blocchi infiniti
    imap.sock.settimeout(IMAP_TIMEOUT)
    imap.login(IMAP_USER, IMAP_PASSWORD)
    log.info("✅ Login IMAP riuscito")
    return imap


def list_client_folders(imap: imaplib.IMAP4_SSL) -> list:
    """
    Lista tutte le cartelle IMAP ed esclude quelle di sistema.
    Restituisce una lista di nomi cartella (stringhe).
    """
    _, raw_folders = imap.list()
    client_folders = []

    for raw in raw_folders:
        if raw is None:
            continue
        name = _extract_folder_name(raw)
        if not name:
            continue

        # Normalizza per confronto
        name_lower = name.lower().strip()

        # Escludi cartelle di sistema
        if name_lower in EXCLUDED_FOLDERS:
            log.debug("⏭️  Esclusa cartella sistema: %s", name)
            continue

        # Escludi cartelle che iniziano con simboli/prefissi tipici di sistema
        if name_lower.startswith("[") or name_lower.startswith("."):
            log.debug("⏭️  Esclusa cartella speciale: %s", name)
            continue

        client_folders.append(name)

    log.info("📂 Trovate %d cartelle cliente (su %d totali)", len(client_folders), len(raw_folders))
    return sorted(client_folders)


def fetch_emails_from_folder(imap: imaplib.IMAP4_SSL, folder_name: str) -> list:
    """
    Seleziona la cartella e recupera le ultime MAX_EMAILS_PER_FOLDER email.
    Restituisce una lista di dict con: subject, sender, date, body.
    Usa BODY.PEEK per non marcare come lette e per fetch selettivo (più veloce).
    """
    emails = []

    # Seleziona cartella (readonly = non marca come lette)
    try:
        folder_quoted = f'"{folder_name}"'
        status, data = imap.select(folder_quoted, readonly=True)
        if status != "OK":
            # Prova senza virgolette
            status, data = imap.select(folder_name, readonly=True)
        if status != "OK":
            log.warning("⚠️  Impossibile selezionare cartella '%s': %s", folder_name, data)
            return []
    except (imaplib.IMAP4.error, OSError) as e:
        log.warning("⚠️  Errore selezione cartella '%s': %s", folder_name, e)
        return []

    # Conta i messaggi
    try:
        num_messages = int(data[0]) if data and data[0] else 0
    except (ValueError, TypeError):
        num_messages = 0

    if num_messages == 0:
        log.debug("📭 Cartella '%s' vuota", folder_name)
        return []

    # Cerca gli UID degli ultimi N messaggi con SEARCH
    try:
        status, search_data = imap.search(None, "ALL")
        if status != "OK" or not search_data or not search_data[0]:
            log.warning("⚠️  SEARCH fallita per '%s'", folder_name)
            return []
        all_ids = search_data[0].split()
    except (imaplib.IMAP4.error, OSError) as e:
        log.warning("⚠️  Errore SEARCH '%s': %s", folder_name, e)
        return []

    # Prendi solo gli ultimi N
    ids_to_fetch = all_ids[-MAX_EMAILS_PER_FOLDER:]

    # Fetch uno per uno per evitare timeout su messaggi grandi
    for msg_id in ids_to_fetch:
        try:
            # Fetch header + prima parte del body (max 2KB)
            status, header_data = imap.fetch(
                msg_id,
                "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] BODY.PEEK[1]<0.2048>)"
            )
            if status != "OK" or not header_data:
                continue

            # Ricostruisci un messaggio dai dati grezzi disponibili
            raw_parts = [p for p in header_data if isinstance(p, tuple)]
            if not raw_parts:
                continue

            # Il primo tuple contiene gli header
            raw_header = raw_parts[0][1] if raw_parts else b""
            # Il secondo tuple (se presente) contiene il body
            raw_body = raw_parts[1][1] if len(raw_parts) > 1 else b""

            # Parse header
            msg_hdr = email.message_from_bytes(raw_header)
            subject = _decode_str(msg_hdr.get("Subject", ""))
            sender  = _decode_str(msg_hdr.get("From", ""))
            date    = _decode_str(msg_hdr.get("Date", ""))

            # Body grezzo (limitato)
            body = ""
            if raw_body:
                for enc in ("utf-8", "iso-8859-1", "windows-1252", "latin-1"):
                    try:
                        body = raw_body.decode(enc)
                        break
                    except (UnicodeDecodeError, LookupError):
                        pass
                if not body:
                    body = raw_body.decode("utf-8", errors="replace")
                # Rimuovi righe di confine MIME e tronca
                body = re.sub(r"--[\w\-]+\r?\n?", "", body)
                body = re.sub(r"\r\n", "\n", body).strip()
                body = body[:MAX_BODY_CHARS]

            # Estrai email address
            _, sender_email = parseaddr(sender)

            emails.append({
                "subject": subject,
                "sender":  sender,
                "sender_email": sender_email.lower() if sender_email else "",
                "date":    date,
                "body":    body,
            })

        except (imaplib.IMAP4.error, OSError) as e:
            log.debug("Errore fetch msg %s in '%s': %s", msg_id, folder_name, e)
            continue
        except Exception as e:
            log.debug("Errore parsing msg %s in '%s': %s", msg_id, folder_name, e)
            continue

    log.info("📬 Cartella '%s': %d messaggi totali, %d letti",
             folder_name, num_messages, len(emails))
    return emails


# ── Gemini AI ──────────────────────────────────────────────────────────────────

def _format_emails_for_prompt(emails: list, folder_name: str) -> str:
    """Formatta le email come testo per il prompt Gemini."""
    lines = [f"Cartella IMAP (nome cliente): {folder_name}\n"]
    for i, em in enumerate(emails, 1):
        lines.append(f"--- EMAIL {i} ---")
        lines.append(f"Da: {em['sender']}")
        lines.append(f"Data: {em['date']}")
        lines.append(f"Oggetto: {em['subject']}")
        if em["body"]:
            lines.append(f"Testo: {em['body']}")
        lines.append("")
    return "\n".join(lines)


def analyze_with_gemini(folder_name: str, emails: list) -> dict:
    """
    Invia le email a Gemini 2.5 Flash e restituisce un dict con l'analisi del cliente.
    In caso di errore restituisce i dati grezzi minimi.
    """
    if not GEMINI_API_KEY:
        log.warning("⚠️  GEMINI_API_KEY non configurata, skip analisi AI")
        return _fallback_data(folder_name, emails)

    email_text = _format_emails_for_prompt(emails, folder_name)

    prompt = f"""Sei un assistente che analizza email B2B italiane.
Analizza le email qui sotto di un cliente di AMR Recchia (produzione insegne, lettere 3D, display).
Rispondi SOLO con un oggetto JSON valido, senza markdown, senza spiegazioni.

JSON da restituire (tutti i campi obbligatori):
{{
  "nome_cliente": "ragione sociale completa",
  "nomi_usati": ["lista nomi con cui firma"],
  "email_mittenti": ["lista email del cliente"],
  "prodotti_tipici": ["prodotti ordinati"],
  "terminologia_chiave": ["parole chiave, acronimi, codici"],
  "formato_riferimento_ordine": "pattern tipico (es. PO#123, ORD.456, non strutturato)",
  "note": "osservazioni brevi"
}}

Email del cliente (cartella IMAP: {folder_name}):

{email_text}
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 2048,
            "response_mime_type": "application/json",
        },
    }

    for attempt in range(1, 5):  # max 4 tentativi
        try:
            resp = requests.post(
                GEMINI_URL,
                params={"key": GEMINI_API_KEY},
                json=payload,
                timeout=60,
            )

            # Gestione esplicita 429 con backoff esponenziale
            if resp.status_code == 429:
                backoff = 15 * (2 ** (attempt - 1))  # 15s, 30s, 60s, 120s
                log.warning(
                    "⚠️  429 Rate Limit per '%s' (tentativo %d) — attendo %ds...",
                    folder_name, attempt, backoff
                )
                time.sleep(backoff)
                continue

            resp.raise_for_status()
            result = resp.json()

            # Estrai il testo dalla risposta
            candidates = result.get("candidates", [])
            if not candidates:
                log.warning("⚠️  Gemini: nessun candidato per '%s' (tentativo %d)", folder_name, attempt)
                time.sleep(2)
                continue

            text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            if not text:
                log.warning("⚠️  Gemini: testo vuoto per '%s' (tentativo %d)", folder_name, attempt)
                time.sleep(2)
                continue

            # Pulisci eventuale markdown
            text = text.strip()
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
            text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
            text = text.strip()

            # Estrazione robusta: trova il primo blocco JSON {...} nel testo
            json_match = re.search(r"(\{.*\})", text, re.DOTALL)
            if json_match:
                text = json_match.group(1)

            # Parse JSON
            analysis = json.loads(text)
            log.info("🤖 Gemini OK per '%s': %s", folder_name,
                     analysis.get("nome_cliente", "?"))
            return analysis

        except json.JSONDecodeError as e:
            log.warning("⚠️  JSON invalido da Gemini per '%s' (tentativo %d): %s", folder_name, attempt, e)
            if attempt < 4:
                time.sleep(2)
                continue
        except requests.RequestException as e:
            log.error("❌ Errore HTTP Gemini per '%s' (tentativo %d): %s", folder_name, attempt, e)
            if attempt < 4:
                time.sleep(5)
                continue
        except Exception as e:
            log.error("❌ Errore imprevisto Gemini per '%s': %s", folder_name, e)
            break

    # Tutti i tentativi falliti → fallback con dati grezzi
    return _fallback_data(folder_name, emails)


def _fallback_data(folder_name: str, emails: list) -> dict:
    """Dati minimi di fallback quando Gemini non è disponibile o fallisce."""
    senders = list({e["sender_email"] for e in emails if e.get("sender_email")})
    subjects = [e["subject"] for e in emails if e.get("subject")][:5]
    return {
        "nome_cliente": folder_name,
        "nomi_usati": [folder_name],
        "email_mittenti": senders,
        "prodotti_tipici": [],
        "terminologia_chiave": [],
        "formato_riferimento_ordine": "non strutturato",
        "note": f"Analisi Gemini non disponibile. Soggetti campione: {'; '.join(subjects)}",
        "_fallback": True,
    }


# ── Persistenza ────────────────────────────────────────────────────────────────

def _save_json(data: dict, path: Path):
    """Salva i dati in formato JSON con indentazione."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.debug("💾 Salvato: %s", path)


def load_partial() -> dict:
    """Carica il file parziale se esiste (per riprendere da dove ci si era fermati)."""
    if PARTIAL_FILE.exists():
        try:
            with open(PARTIAL_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            log.info("♻️  File parziale trovato: %d clienti già analizzati",
                     len(data.get("clients", {})))
            return data
        except Exception as e:
            log.warning("⚠️  Impossibile leggere file parziale: %s", e)
    return {"clients": {}}


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    start_time = datetime.now()
    log.info("=" * 60)
    log.info("🚀 BUILD KNOWLEDGE BASE — AMR Recchia Clienti")
    log.info("   Inizio: %s", start_time.strftime("%Y-%m-%d %H:%M:%S"))
    log.info("=" * 60)

    # Carica progressi precedenti (se esistono)
    partial = load_partial()
    already_done = set(partial.get("clients", {}).keys())
    knowledge_base = partial.get("clients", {})

    if already_done:
        log.info("♻️  Riprendo da %d clienti già analizzati", len(already_done))

    # Connetti IMAP
    try:
        imap = connect_imap()
    except Exception as e:
        log.critical("❌ Connessione IMAP fallita: %s", e)
        return

    # Step 1: Lista cartelle
    try:
        client_folders = list_client_folders(imap)
    except Exception as e:
        log.critical("❌ Impossibile listare le cartelle: %s", e)
        imap.logout()
        return

    total_folders = len(client_folders)
    log.info("📂 Totale cartelle clienti da analizzare: %d", total_folders)

    # Statistiche
    analyzed_ok    = len(already_done)
    analyzed_error = 0
    email_counts   = {}

    # Step 2+3+4: Analisi per ogni cartella
    new_this_run = 0
    for idx, folder_name in enumerate(client_folders, 1):

        # Nome breve (senza prefisso INBOX. o simili) da usare come chiave e display
        short_name = folder_name
        for prefix in ("INBOX.", "inbox.", "Inbox."):
            if folder_name.startswith(prefix):
                short_name = folder_name[len(prefix):]
                break

        # Salta se già fatto (controlla sia full name che short name)
        if folder_name in already_done or short_name in already_done:
            log.info("[%d/%d] ⏭️  Già analizzato: %s", idx, total_folders, short_name)
            key = folder_name if folder_name in knowledge_base else short_name
            if key in knowledge_base:
                email_counts[short_name] = knowledge_base[key].get("emails_analizzate", 0)
            continue

        log.info("[%d/%d] 🔍 Analisi cartella: %s", idx, total_folders, short_name)

        # Fetch email
        try:
            emails = fetch_emails_from_folder(imap, folder_name)
        except Exception as e:
            log.error("❌ Errore fetch '%s': %s", folder_name, e)
            analyzed_error += 1
            # Riconnetti in caso di timeout
            try:
                imap.logout()
            except Exception:
                pass
            try:
                imap = connect_imap()
            except Exception as reconnect_err:
                log.critical("❌ Riconnessione IMAP fallita: %s", reconnect_err)
                break
            continue

        num_emails = len(emails)
        email_counts[short_name] = num_emails

        if num_emails == 0:
            log.info("   📭 Nessuna email, cartella vuota — skip Gemini")
            # Salva comunque con dati minimi
            knowledge_base[short_name] = {
                "folder": folder_name,
                "nome_cliente": short_name,
                "nomi_usati": [short_name],
                "email_mittenti": [],
                "prodotti_tipici": [],
                "terminologia_chiave": [],
                "formato_riferimento_ordine": "non strutturato",
                "emails_analizzate": 0,
                "note": "Cartella vuota",
            }
            analyzed_ok += 1
            new_this_run += 1
        else:
            # Analisi Gemini
            analysis = analyze_with_gemini(short_name, emails)
            time.sleep(GEMINI_DELAY_SEC)  # Rate limit

            # Arricchisci con metadati
            entry = {
                "folder": folder_name,
                **analysis,
                "emails_analizzate": num_emails,
            }
            # Assicura che i campi chiave esistano
            entry.setdefault("nome_cliente", short_name)
            entry.setdefault("nomi_usati", [short_name])
            entry.setdefault("email_mittenti", [])
            entry.setdefault("prodotti_tipici", [])
            entry.setdefault("terminologia_chiave", [])
            entry.setdefault("formato_riferimento_ordine", "non strutturato")
            entry.setdefault("note", "")

            knowledge_base[short_name] = entry
            analyzed_ok += 1
            new_this_run += 1

        # Salva parziale ogni SAVE_EVERY_N nuovi clienti
        if new_this_run % SAVE_EVERY_N == 0 and new_this_run > 0:
            partial_data = {
                "generated_at": datetime.now().isoformat(),
                "total_clients": len(knowledge_base),
                "clients": knowledge_base,
            }
            _save_json(partial_data, PARTIAL_FILE)
            log.info("💾 Parziale salvato: %d clienti", len(knowledge_base))

        # Pausa ogni 50 email per non stressare il server IMAP
        if new_this_run % 50 == 0 and new_this_run > 0:
            log.info("⏸️  Pausa 5s per ridurre carico IMAP...")
            time.sleep(5)

    # Chiudi connessione
    try:
        imap.logout()
    except Exception:
        pass

    # Step 4: Salva file finale
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()

    output = {
        "generated_at": end_time.isoformat(),
        "duration_seconds": round(duration),
        "total_clients": len(knowledge_base),
        "analyzed_ok": analyzed_ok,
        "analyzed_error": analyzed_error,
        "clients": knowledge_base,
    }

    _save_json(output, OUTPUT_FILE)
    log.info("✅ Knowledge base salvata: %s", OUTPUT_FILE)

    # Rimuovi il file parziale se tutto è andato bene
    if PARTIAL_FILE.exists() and analyzed_error == 0:
        PARTIAL_FILE.unlink()
        log.info("🗑️  File parziale rimosso (completato senza errori)")

    # Step 5: Statistiche finali
    log.info("")
    log.info("=" * 60)
    log.info("📊 STATISTICHE FINALI")
    log.info("=" * 60)
    log.info("  Cartelle IMAP totali trovate : %d", total_folders)
    log.info("  Clienti analizzati con successo: %d", analyzed_ok)
    log.info("  Errori                         : %d", analyzed_error)
    log.info("  Durata totale                  : %ds (~%.1f min)",
             round(duration), duration / 60)

    # Top 10 per numero email
    log.info("")
    log.info("🏆 TOP 10 CLIENTI PER NUMERO EMAIL:")
    sorted_clients = sorted(email_counts.items(), key=lambda x: x[1], reverse=True)
    for i, (name, count) in enumerate(sorted_clients[:10], 1):
        log.info("  %2d. %-40s %d email", i, name, count)

    log.info("")
    log.info("✅ COMPLETATO: %s", OUTPUT_FILE)
    log.info("=" * 60)


if __name__ == "__main__":
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="Build AMR Recchia client knowledge base")
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Salta Gemini: raccoglie solo dati IMAP grezzi (utile quando la quota API è esaurita)",
    )
    parser.add_argument(
        "--retry-ai",
        action="store_true",
        help="Rianalizza con Gemini solo le entry salvate come fallback (_fallback=True)",
    )
    args = parser.parse_args()

    if args.no_ai:
        # Modalità IMAP-only: ignora Gemini, usa solo _fallback_data
        _orig_analyze = analyze_with_gemini
        def analyze_with_gemini(folder_name, emails):  # type: ignore[misc]
            return _fallback_data(folder_name, emails)
        log.info("🚫 Modalità --no-ai: Gemini disabilitato, solo dati IMAP grezzi")

    if args.retry_ai:
        # Modalità --retry-ai: ricarica il parziale e rianalizza solo i fallback
        log.info("🔄 Modalità --retry-ai: rianalizza solo entry con _fallback=True")
        partial = load_partial()
        if not partial.get("clients"):
            # Prova il file finale
            if OUTPUT_FILE.exists():
                with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                    partial = json.load(f)

        kb = partial.get("clients", {})
        # Filtra solo i fallback che hanno almeno 3 email storiche
        to_retry = [k for k, v in kb.items() if v.get("_fallback") and v.get("emails_analizzate", 0) >= 3]
        log.info("   Entry da rianalizzare (con >= 3 email): %d", len(to_retry))

        success = 0
        for i, key in enumerate(to_retry, 1):
            entry = kb[key]
            folder = entry.get("folder", key)
            log.info("[%d/%d] 🤖 Gemini AI su '%s' (emails: %d)", i, len(to_retry), key, entry.get("emails_analizzate", 0))
            
            # Cerca le email reali salvate nel log o recuperale
            # Se lo script è stato eseguito con --no-ai, non abbiamo il body completo ma abbiamo
            # i mittenti e i soggetti salvati in entry['email_mittenti']. 
            # Per l'analisi Gemini, proviamo a riconnetterci a IMAP e leggere i corpi reali di questa cartella!
            raw_emails = []
            try:
                if 'imap' not in locals():
                    imap = connect_imap()
                raw_emails = fetch_emails_from_folder(imap, folder)
            except Exception as e:
                log.warning("   ⚠️ Errore lettura email da IMAP per '%s': %s", key, e)
                
            if not raw_emails:
                # Fallback usando solo le intestazioni grezze già presenti
                raw_emails = [{
                    "subject": s, "sender": "", "sender_email": e,
                    "date": "", "body": ""
                } for e in entry.get("email_mittenti", []) for s in ["(email archiviata)"]]

            analysis = analyze_with_gemini(key, raw_emails)
            time.sleep(GEMINI_DELAY_SEC)
            if not analysis.get("_fallback"):
                # Rimuovi flag fallback e unisci
                analysis.pop("_fallback", None)
                kb[key].update(analysis)
                kb[key]["folder"] = folder
                kb[key]["_fallback"] = False
                success += 1
                log.info("   ✅ Riconosciuto: %s", analysis.get("nome_cliente", "?"))
            
            # Salva progresso intermedio ogni 10
            if i % SAVE_EVERY_N == 0:
                output = {
                    "generated_at": datetime.now().isoformat(),
                    "total_clients": len(kb),
                    "clients": kb,
                }
                _save_json(output, OUTPUT_FILE)
                log.info("   💾 Parziale salvato (%d/%d)", i, len(to_retry))


        # Salva
        output = {
            "generated_at": datetime.now().isoformat(),
            "total_clients": len(kb),
            "clients": kb,
        }
        _save_json(output, OUTPUT_FILE)
        log.info("✅ Retry completato: %d/%d aggiornati → %s", success, len(to_retry), OUTPUT_FILE)
        sys.exit(0)

    main()
