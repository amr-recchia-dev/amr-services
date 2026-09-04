"""
email_reader.py — Connessione IMAP a Register.it e lettura email.
Supporta email in formato text/plain e text/html.
"""

import imaplib
import email
import logging
import html
import re
from email.header import decode_header
from email.message import Message
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
import os

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

IMAP_HOST = os.getenv("IMAP_HOST", "mail.register.it")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("IMAP_USER", "")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "")
IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX")
MARK_AS_READ = os.getenv("MARK_AS_READ", "True").lower() == "true"


@dataclass
class ParsedEmail:
    """Struttura dati per un'email parsata."""
    message_id: str
    subject: str
    sender_name: str
    sender_email: str
    date: str
    body_text: str
    body_html: str = ""
    raw_uid: Optional[bytes] = None

    @property
    def body(self) -> str:
        """Restituisce il corpo migliore disponibile (plain text preferito)."""
        if self.body_text:
            return self.body_text[:6000]  # Limita per Gemini
        if self.body_html:
            return _html_to_text(self.body_html)[:6000]
        return ""


def _decode_header_value(value: str) -> str:
    """Decodifica header MIME (es. subject, from) in stringa leggibile."""
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


def _html_to_text(html_content: str) -> str:
    """Converte HTML in testo plain rimuovendo i tag."""
    # Rimuovi stili e script
    html_content = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", html_content, flags=re.DOTALL | re.IGNORECASE)
    # Sostituisci <br> e <p> con newline
    html_content = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", html_content, flags=re.IGNORECASE)
    # Rimuovi tutti gli altri tag HTML
    html_content = re.sub(r"<[^>]+>", "", html_content)
    # Decodifica entità HTML
    html_content = html.unescape(html_content)
    # Normalizza spazi e newline
    html_content = re.sub(r"\n{3,}", "\n\n", html_content)
    html_content = re.sub(r"[ \t]+", " ", html_content)
    return html_content.strip()


def _extract_parts(msg: Message) -> Tuple[str, str]:
    """Estrae il corpo text/plain e text/html da un messaggio MIME."""
    body_text = ""
    body_html = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))

            # Salta allegati
            if "attachment" in content_disposition:
                continue

            if content_type == "text/plain" and not body_text:
                charset = part.get_content_charset() or "utf-8"
                try:
                    body_text = part.get_payload(decode=True).decode(charset, errors="replace")
                except Exception:
                    body_text = str(part.get_payload())

            elif content_type == "text/html" and not body_html:
                charset = part.get_content_charset() or "utf-8"
                try:
                    body_html = part.get_payload(decode=True).decode(charset, errors="replace")
                except Exception:
                    body_html = str(part.get_payload())
    else:
        content_type = msg.get_content_type()
        charset = msg.get_content_charset() or "utf-8"
        try:
            payload = msg.get_payload(decode=True).decode(charset, errors="replace")
        except Exception:
            payload = str(msg.get_payload())

        if content_type == "text/plain":
            body_text = payload
        elif content_type == "text/html":
            body_html = payload

    return body_text, body_html


def _parse_sender(from_header: str) -> Tuple[str, str]:
    """Estrae nome e email dal campo From."""
    from_header = _decode_header_value(from_header)
    # Formato: "Nome Cognome <email@domain.com>"
    match = re.match(r"^(.*?)\s*<([^>]+)>$", from_header.strip())
    if match:
        name = match.group(1).strip().strip('"')
        email_addr = match.group(2).strip()
    else:
        # Solo email senza nome
        name = ""
        email_addr = from_header.strip()
    return name, email_addr


def fetch_unseen_emails() -> List[ParsedEmail]:
    """
    Si connette via IMAP SSL a Register.it e recupera le email non lette.
    Restituisce una lista di ParsedEmail.
    """
    if not IMAP_USER or not IMAP_PASSWORD:
        raise ValueError("Credenziali IMAP mancanti nel file .env")

    emails: List[ParsedEmail] = []

    try:
        logger.info("Connessione IMAP a %s:%s come %s", IMAP_HOST, IMAP_PORT, IMAP_USER)
        import socket
        socket.setdefaulttimeout(15)
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(IMAP_USER, IMAP_PASSWORD)
        mail.select(IMAP_FOLDER)

        # Cerca sia le non lette/flaggate che le ultime 30 email recenti della casella
        # (questo garantisce che le email già visualizzate da client come Apple Mail o Outlook
        # vengano comunque intercettate e processate se non ancora nel DB!)
        status_unseen, data_unseen = mail.uid("search", None, "OR", "UNSEEN", "FLAGGED")
        status_all, data_all = mail.uid("search", None, "ALL")

        uids_unseen = data_unseen[0].split() if (status_unseen == "OK" and data_unseen and data_unseen[0]) else []
        uids_all = data_all[0].split() if (status_all == "OK" and data_all and data_all[0]) else []

        # Prendi le ultime 30 email recenti + tutte le non lette
        recent_uids = uids_all[-30:] if len(uids_all) > 30 else uids_all
        combined_uids = sorted(list(set(uids_unseen + recent_uids)), key=lambda x: int(x))

        if not combined_uids:
            logger.info("Nessuna email da esaminare.")
            mail.logout()
            return []

        logger.info("Trovate %d email da verificare (tra non lette e recenti).", len(combined_uids))
        uid_list = combined_uids

        for uid in uid_list:
            try:
                # Fetch parziale sicuro: scarica solo i primi 50KB del messaggio intero.
                # Questo garantisce di avere header e corpo testuale ma esclude allegati pesanti,
                # evitando blocchi o timeout con il server IMAP di Register.it.
                status, msg_data = mail.uid("fetch", uid, "(BODY.PEEK[]<0.50000>)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue

                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                # Estrai campi
                message_id = msg.get("Message-ID", "").strip()
                if not message_id:
                    # Genera un ID di fallback dal subject + date
                    message_id = f"fallback-{msg.get('Subject','')}-{msg.get('Date','')}"

                subject = _decode_header_value(msg.get("Subject", "(nessun oggetto)"))
                from_header = msg.get("From", "")
                sender_name, sender_email = _parse_sender(from_header)
                date = msg.get("Date", "")

                body_text, body_html = _extract_parts(msg)

                parsed = ParsedEmail(
                    message_id=message_id,
                    subject=subject,
                    sender_name=sender_name,
                    sender_email=sender_email,
                    date=date,
                    body_text=body_text,
                    body_html=body_html,
                    raw_uid=uid,
                )
                emails.append(parsed)
                logger.debug("Email parsata: [%s] %s da %s", uid, subject, sender_email)

            except Exception as e:
                logger.error("Errore parsing email uid=%s: %s", uid, e)
                continue

        mail.logout()

    except imaplib.IMAP4.error as e:
        logger.error("Errore IMAP: %s", e)
        raise
    except Exception as e:
        logger.error("Errore connessione IMAP: %s", e)
        raise

    return emails


def mark_email_as_read(uid: bytes) -> None:
    """Marca un'email come LETTA sul server IMAP."""
    if not MARK_AS_READ:
        return
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(IMAP_USER, IMAP_PASSWORD)
        mail.select(IMAP_FOLDER)
        mail.uid("store", uid, "+FLAGS", "\\Seen")
        mail.logout()
        logger.debug("Email uid=%s marcata come letta.", uid)
    except Exception as e:
        logger.warning("Impossibile marcare email come letta: %s", e)
