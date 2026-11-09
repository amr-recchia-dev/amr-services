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
    folder: str = "INBOX"
    attachments: List[Tuple[str, bytes]] = field(default_factory=list)

    @property
    def body(self) -> str:
        """Restituisce il corpo migliore disponibile (plain text preferito) più i nomi degli allegati."""
        base = ""
        if self.body_text:
            base = self.body_text[:5500]
        elif self.body_html:
            base = _html_to_text(self.body_html)[:5500]
        if self.attachments:
            att_names = ", ".join(fn for fn, _ in self.attachments)
            base = f"{base}\n\n[File allegati all'email: {att_names}]".strip()
        return base[:6000]


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


def _extract_parts_and_attachments(msg: Message) -> Tuple[str, str, List[Tuple[str, bytes]]]:
    """Estrae il corpo text/plain, text/html e gli allegati reali da un messaggio MIME."""
    body_text = ""
    body_html = ""
    attachments: List[Tuple[str, bytes]] = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            raw_fname = part.get_filename()
            fname = _decode_header_value(raw_fname).strip() if raw_fname else ""

            if fname or "attachment" in content_disposition.lower():
                if fname:
                    try:
                        payload_bytes = part.get_payload(decode=True) or b""
                        fname_low = fname.lower()
                        # Ignora piccole immagini di firma (es. image001.png < 25KB)
                        is_small_sig_img = (
                            fname_low.endswith((".png", ".jpg", ".jpeg", ".gif"))
                            and (len(payload_bytes) < 25000 or fname_low.startswith("image00"))
                        )
                        if payload_bytes and not is_small_sig_img and len(payload_bytes) <= 15_000_000:
                            attachments.append((fname, payload_bytes))
                    except Exception:
                        pass
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

    return body_text, body_html, attachments


def _extract_parts(msg: Message) -> Tuple[str, str]:
    """Compatibilità: estrae solo text/plain e text/html."""
    bt, bh, _ = _extract_parts_and_attachments(msg)
    return bt, bh


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


# Cartelle di sistema da non scansionare come sottocartelle clienti
_SKIP_FOLDERS = {
    "INBOX", "Trash", "Sent", "Drafts", "Spam", "Junk", "Templates", "Archive", "Archives",
    "INBOX.Trash", "INBOX.Sent", "INBOX.Drafts", "INBOX.Spam", "INBOX.Templates",
    "INBOX.shallo", '"INBOX.permessi e avvisi"', "INBOX.permessi e avvisi",
}

# Sottocartelle clienti ad alta frequenza controllate ad ogni singolo ciclo (oltre a INBOX e alle \Marked)
_PRIORITY_CLIENT_FOLDERS = [
    "INBOX.IPE",
    '"INBOX.Ditre italia"',
    '"INBOX.lab 05"',
    "INBOX.INMATERIA",
    '"INBOX.azimut technology"',
    '"INBOX.galleria frilli"',
    "INBOX.FORPRESS",
    "INBOX.DALPIAN",
    '"INBOX.QUARTA DIMENSIONE"',
    '"INBOX.DAL BELLO VINI"',
    '"INBOX.contato da sito"',
    "INBOX.EXTREAM",
    "INBOX.archimede",
    '"INBOX.BERTONE DESIGN"',
    '"INBOX.FONDERIA DE POLI"',
    "INBOX.niva",
    "INBOX.vanin",
    "INBOX.IDEABITMAP",
]

_folder_rotation_idx = 0


def _discover_folders_to_scan(mail: imaplib.IMAP4_SSL) -> List[str]:
    """
    Restituisce la lista di cartelle IMAP da controllare in questo ciclo:
    1. INBOX (sempre)
    2. Tutte le sottocartelle con flag \\Marked (es. regole filtro Thunderbird come INBOX.IPE)
    3. Le sottocartelle clienti ad alta frequenza (_PRIORITY_CLIENT_FOLDERS)
    4. Una finestra rotante di 70 sottocartelle clienti per coprire tutte le ~700 cartelle
       anche quando Gary sposta manualmente l'email pochi secondi dopo la ricezione.
    """
    global _folder_rotation_idx
    folders_to_scan = [IMAP_FOLDER]
    seen = {IMAP_FOLDER}

    all_client_folders = []
    marked_folders = []

    try:
        typ, folder_lines = mail.list()
        if typ == "OK" and folder_lines:
            for fl in folder_lines:
                s = fl.decode("utf-8", errors="replace")
                m = re.match(r'\(([^)]*)\)\s+"([^"]+)"\s+(.+)$', s)
                if not m:
                    continue
                flags, _delim, raw_fname = m.group(1), m.group(2), m.group(3).strip()
                clean_fname = raw_fname.strip('"')
                if raw_fname in _SKIP_FOLDERS or clean_fname in _SKIP_FOLDERS:
                    continue
                if any(x in clean_fname.lower() for x in ("trash", "sent", "draft", "spam", "junk", "template", "shallo", "permessi e avvisi")):
                    continue
                if "\\Marked" in flags and raw_fname not in seen:
                    marked_folders.append(raw_fname)
                all_client_folders.append(raw_fname)
    except Exception as e:
        logger.warning("Impossibile elencare le sottocartelle IMAP: %s", e)

    for mf in marked_folders:
        if mf not in seen:
            folders_to_scan.append(mf)
            seen.add(mf)

    existing_set = set(all_client_folders)
    for pf in _PRIORITY_CLIENT_FOLDERS:
        if pf in existing_set and pf not in seen:
            folders_to_scan.append(pf)
            seen.add(pf)

    remaining = [f for f in all_client_folders if f not in seen]
    if remaining:
        batch_size = 70
        start = _folder_rotation_idx % len(remaining)
        batch = remaining[start:start + batch_size]
        if len(batch) < batch_size:
            batch += remaining[:batch_size - len(batch)]
        _folder_rotation_idx = (start + batch_size) % len(remaining)
        for rf in batch:
            if rf not in seen:
                folders_to_scan.append(rf)
                seen.add(rf)

    return folders_to_scan


def fetch_unseen_emails() -> List[ParsedEmail]:
    """
    Si connette via IMAP SSL a Register.it e recupera le email non ancora processate
    sia da INBOX sia dalle sottocartelle clienti (es. INBOX.IPE).
    Restituisce una lista di ParsedEmail.
    """
    if not IMAP_USER or not IMAP_PASSWORD:
        raise ValueError("Credenziali IMAP mancanti nel file .env")

    try:
        from db import is_already_processed
    except Exception:
        is_already_processed = lambda _mid: False

    emails: List[ParsedEmail] = []
    seen_message_ids = set()

    try:
        logger.info("Connessione IMAP a %s:%s come %s", IMAP_HOST, IMAP_PORT, IMAP_USER)
        import socket
        from datetime import datetime, timedelta
        socket.setdefaulttimeout(20)
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(IMAP_USER, IMAP_PASSWORD)

        folders = _discover_folders_to_scan(mail)
        months_en = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        dt_since = datetime.now() - timedelta(days=4)
        since_str = f"{dt_since.day:02d}-{months_en[dt_since.month - 1]}-{dt_since.year}"

        for folder in folders:
            try:
                sel_status, _ = mail.select(folder, readonly=True)
                if sel_status != "OK":
                    continue

                if folder == IMAP_FOLDER:
                    status_unseen, data_unseen = mail.uid("search", None, "OR", "UNSEEN", "FLAGGED")
                    status_all, data_all = mail.uid("search", None, "ALL")
                    uids_unseen = data_unseen[0].split() if (status_unseen == "OK" and data_unseen and data_unseen[0]) else []
                    uids_all = data_all[0].split() if (status_all == "OK" and data_all and data_all[0]) else []
                    recent_uids = uids_all[-30:] if len(uids_all) > 30 else uids_all
                    uid_list = sorted(list(set(uids_unseen + recent_uids)), key=lambda x: int(x))
                else:
                    # Nelle sottocartelle clienti cerca le email degli ultimi 4 giorni (o non lette)
                    st_recent, dt_recent = mail.uid("search", None, f'(OR UNSEEN (SINCE "{since_str}"))')
                    uid_list = dt_recent[0].split() if (st_recent == "OK" and dt_recent and dt_recent[0]) else []
                    if len(uid_list) > 15:
                        uid_list = uid_list[-15:]

                if not uid_list:
                    continue

                for uid in uid_list:
                    try:
                        # 1. Pre-fetch leggero dei soli header per saltare subito le email già nel DB o inviate da noi
                        st_hdr, hdr_data = mail.uid(
                            "fetch", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM DATE)])"
                        )
                        if st_hdr != "OK" or not hdr_data or not hdr_data[0] or not isinstance(hdr_data[0], tuple):
                            continue

                        hdr_msg = email.message_from_bytes(hdr_data[0][1])
                        message_id = (hdr_msg.get("Message-ID") or "").strip()
                        if not message_id:
                            message_id = f"fallback-{hdr_msg.get('Subject','')}-{hdr_msg.get('Date','')}"

                        if message_id in seen_message_ids or is_already_processed(message_id):
                            continue

                        subject = _decode_header_value(hdr_msg.get("Subject", "(nessun oggetto)"))
                        from_header = hdr_msg.get("From", "")
                        sender_name, sender_email = _parse_sender(from_header)
                        date = hdr_msg.get("Date", "")

                        # Ignora notifiche interne Timbrature AMR / Sentinella / Monday
                        if (
                            sender_name in ("Timbrature AMR", "Sentinella Presenze AMR")
                            or subject.startswith(("Nuova richiesta ", "Richiesta annullata", "[Sentinella Presenze AMR]"))
                            or sender_email.lower().endswith(("@monday.com", "@euc1.monday.com"))
                        ):
                            continue

                        # Ignora le email INVIATE da Gary (ordini@amrrecchia.it) spostate nelle cartelle clienti,
                        # tranne i lead che arrivano dal Sito Web AMR Recchia
                        if sender_email.lower() == IMAP_USER.lower() and "[lead sito web amr]" not in subject.lower():
                            continue

                        # 2. Scarica il messaggio completo (inclusi allegati PDF/DWG) solo per le nuove email da processare
                        status, msg_data = mail.uid("fetch", uid, "(BODY.PEEK[])")
                        if status != "OK" or not msg_data or not msg_data[0] or not isinstance(msg_data[0], tuple):
                            status, msg_data = mail.uid("fetch", uid, "(BODY.PEEK[]<0.100000>)")
                            if status != "OK" or not msg_data or not msg_data[0] or not isinstance(msg_data[0], tuple):
                                continue

                        raw_email = msg_data[0][1]
                        msg = email.message_from_bytes(raw_email)
                        body_text, body_html, attachments = _extract_parts_and_attachments(msg)

                        seen_message_ids.add(message_id)
                        parsed = ParsedEmail(
                            message_id=message_id,
                            subject=subject,
                            sender_name=sender_name,
                            sender_email=sender_email,
                            date=date,
                            body_text=body_text,
                            body_html=body_html,
                            raw_uid=uid,
                            folder=folder,
                            attachments=attachments,
                        )
                        emails.append(parsed)
                        logger.info(
                            "📥 Nuova email trovata in [%s]: '%s' da %s (%d allegati)",
                            folder, subject, sender_email, len(attachments)
                        )

                    except Exception as e:
                        logger.error("Errore parsing email uid=%s in %s: %s", uid, folder, e)
                        continue
            except Exception as folder_err:
                logger.debug("Impossibile controllare cartella %s: %s", folder, folder_err)
                continue

        mail.logout()

    except imaplib.IMAP4.error as e:
        logger.error("Errore IMAP: %s", e)
        raise
    except Exception as e:
        logger.error("Errore connessione IMAP: %s", e)
        raise

    return emails


def mark_email_as_read(uid: bytes, folder: str = IMAP_FOLDER) -> None:
    """Marca un'email come LETTA sul server IMAP."""
    if not MARK_AS_READ:
        return
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(IMAP_USER, IMAP_PASSWORD)
        mail.select(folder)
        mail.uid("store", uid, "+FLAGS", "\\Seen")
        mail.logout()
        logger.debug("Email uid=%s marcata come letta in %s.", uid, folder)
    except Exception as e:
        logger.warning("Impossibile marcare email come letta: %s", e)
