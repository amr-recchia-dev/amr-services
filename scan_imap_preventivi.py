#!/usr/bin/env python3
"""
AMR OS — Estrattore Storico Preventivi & Fatture da IMAP (ordini@amrrecchia.it)
================================================================================
Scansiona in modalità rigorosamente READ-ONLY (readonly=True + BODY.PEEK)
le cartelle cliente e la posta inviata di ordini@amrrecchia.it per estrarre
tutti i PDF generati da Gestionale Eureka (Docp700r: PREVENTIVO-*.pdf, FATTURA-*.pdf)
e arricchire il database storico `amr_storico_preventivi.json` usato dal Preventivatore.

Sicurezza IMAP:
- Aperta sempre con `mail.select(folder, readonly=True)`
- Fetch sempre con `BODY.PEEK[]` per NON alterare mai il flag \\Seen delle email.
"""

from __future__ import annotations
import os
import re
import json
import email
import imaplib
import argparse
from email.header import decode_header
import fitz  # PyMuPDF

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORICO_JSON_PATH = os.path.join(BASE_DIR, "amr_storico_preventivi.json")


def load_env():
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    os.environ.setdefault(k, v.strip('"').strip("'"))


def decode_str(s):
    if not s:
        return ""
    parts = decode_header(s)
    out = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            out += part.decode(enc or "utf-8", errors="replace")
        else:
            out += str(part)
    return out


def parse_it_num(s):
    s = s.strip().replace(".", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return 0.0


def parse_eureka_pdf_bytes(pdf_bytes: bytes, filename: str, folder_name: str = "") -> dict | None:
    """Estrae intestazione, righe, quantità, prezzi unitari e totali da un PDF Gestionale Eureka (Docp700r)."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        full_text = ""
        for page in doc:
            full_text += page.get_text("text") + "\n"
    except Exception:
        return None

    if "Docp700r" not in full_text and "PREVENTIVO" not in full_text.upper() and "FATTURA" not in full_text.upper():
        return None

    num_m = re.search(r"(?:PRD\s+PREVENTIVO|FATTURA|ORDINE)\s+(\d+)\s+(\d{2}/\d{2}/\d{4})", full_text)
    doc_num = num_m.group(1) if num_m else ""
    doc_date = num_m.group(2) if num_m else ""

    lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]
    spett_idx = -1
    for idx, ln in enumerate(lines):
        if "Spett.le" in ln:
            spett_idx = idx
            break
    client_name = folder_name
    if spett_idx != -1 and spett_idx + 1 < len(lines):
        candidates = []
        for k in range(spett_idx + 1, min(spett_idx + 8, len(lines))):
            l = lines[k]
            if any(x in l for x in ["Indirizzo", "Telefono", "Condizioni", "Documento", "Partita IVA", "PRD "]):
                continue
            candidates.append(l)
        if candidates:
            client_name = candidates[0]

    body = full_text
    if "Importo" in full_text and "Sc%" in full_text:
        body = full_text.split("Sc%", 1)[-1]
    if "Totale Netto" in body:
        body = body.split("Totale Netto", 1)[0]

    body_lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    line_items = []
    desc_buf = []
    i = 0
    while i < len(body_lines):
        ln = body_lines[i]
        if ln in ("NR", "M2", "M3", "ML", "KG", "PZ", "CORPO") and i + 3 < len(body_lines):
            q_str = body_lines[i + 1]
            pu_str = body_lines[i + 2]
            imp_str = body_lines[i + 3]
            if re.match(r"^[\d\.,]+$", q_str) and re.match(r"^[\d\.,]+$", pu_str) and re.match(r"^[\d\.,]+$", imp_str):
                q = parse_it_num(q_str)
                pu = parse_it_num(pu_str)
                imp = parse_it_num(imp_str)
                next_i = i + 4
                if next_i < len(body_lines) and re.match(r"^\d{1,2}$", body_lines[next_i]):
                    next_i += 1
                desc_text = " ".join(desc_buf).strip()
                dims = re.findall(
                    r"(\d+(?:[\.,]\d+)?)\s*(?:cm|mm|m)?\s*[xX\*]\s*(\d+(?:[\.,]\d+)?)(?:\s*(?:cm|mm|m)?\s*[xX\*]\s*(\d+(?:[\.,]\d+)?))?",
                    desc_text,
                )
                line_items.append({
                    "description": desc_text,
                    "um": ln,
                    "qty": q,
                    "unit_price": pu,
                    "total_price": imp,
                    "dimensions_found": dims[:3],
                })
                desc_buf = []
                i = next_i
                continue
        if not ln.startswith("Consegna") and not ln.startswith("Validità") and not ln.startswith("Trasporto"):
            desc_buf.append(ln)
        i += 1

    if not line_items:
        return None

    tot_netto = sum(x["total_price"] for x in line_items)
    return {
        "file": filename,
        "doc_num": doc_num,
        "doc_date": doc_date,
        "client": client_name,
        "monday_item": f"IMAP:{folder_name}",
        "tot_netto_calc": round(tot_netto, 2),
        "items": line_items,
    }


def scan_imap_folders(max_folders: int = 50, max_msgs_per_folder: int = 30):
    load_env()
    host = os.getenv("IMAP_HOST", "imap.ionos.it")
    user = os.getenv("IMAP_USER", "ordini@amrrecchia.it")
    password = os.getenv("IMAP_PASS") or os.getenv("IMAP_PASSWORD", "")

    if not password:
        print("❌ IMAP_PASSWORD non configurata in .env")
        return

    existing = []
    if os.path.exists(STORICO_JSON_PATH):
        with open(STORICO_JSON_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)
    seen_keys = {(q.get("doc_num", ""), q.get("doc_date", ""), round(q.get("tot_netto_calc", 0), 2)) for q in existing}

    print(f"🔌 Connessione IMAP READ-ONLY a {host} ({user})...")
    mail = imaplib.IMAP4_SSL(host, 993)
    mail.login(user, password)

    status, folders_raw = mail.list()
    folders = []
    for f_line in folders_raw or []:
        decoded = f_line.decode("utf-8", errors="replace")
        m = re.search(r'"([^"]+)"$', decoded)
        if m:
            fname = m.group(1)
            if not any(skip in fname.upper() for skip in ["TRASH", "SPAM", "JUNK", "DRAFTS", "BOZZE"]):
                folders.append(fname)

    # Prioritizza Posta Inviata e cartelle clienti
    folders.sort(key=lambda x: (0 if "SENT" in x.upper() or "INVIAT" in x.upper() else 1, x))
    added = 0

    for folder in folders[:max_folders]:
        try:
            st, _ = mail.select(f'"{folder}"', readonly=True)
            if st != "OK":
                continue
            # Cerca messaggi con allegati PDF o parola PREVENTIVO
            st, data = mail.search(None, 'OR SUBJECT "PREVENTIVO" SUBJECT "Preventivo"')
            msg_ids = data[0].split() if st == "OK" and data and data[0] else []
            if not msg_ids:
                continue

            for mid in msg_ids[-max_msgs_per_folder:]:
                st, msg_data = mail.fetch(mid, "(BODY.PEEK[])")
                if st != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)
                for part in msg.walk():
                    fname = decode_str(part.get_filename() or "")
                    if fname.lower().endswith(".pdf") and ("preventiv" in fname.lower() or "fattur" in fname.lower() or "ord" in fname.lower()):
                        payload = part.get_payload(decode=True)
                        if payload:
                            parsed = parse_eureka_pdf_bytes(payload, fname, folder_name=folder)
                            if parsed:
                                key = (parsed["doc_num"], parsed["doc_date"], parsed["tot_netto_calc"])
                                if key not in seen_keys:
                                    seen_keys.add(key)
                                    existing.append(parsed)
                                    added += 1
                                    print(f"  ✅ Aggiunto Preventivo #{parsed['doc_num']} ({parsed['doc_date']}) - {parsed['client']} - €{parsed['tot_netto_calc']}")
        except Exception as e:
            continue

    mail.logout()
    if added > 0:
        with open(STORICO_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    print(f"🎉 Scansione completata: +{added} nuovi preventivi aggiunti (Totale nel DB: {len(existing)}).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scansiona cartelle IMAP ordini@amrrecchia.it in READ-ONLY per estrarre preventivi PDF Eureka.")
    parser.add_argument("--folders", type=int, default=60, help="Numero massimo di cartelle IMAP da scansionare")
    parser.add_argument("--msgs", type=int, default=30, help="Numero massimo di messaggi per cartella")
    args = parser.parse_args()
    scan_imap_folders(max_folders=args.folders, max_msgs_per_folder=args.msgs)
