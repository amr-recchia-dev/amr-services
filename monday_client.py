"""
monday_client.py — Client Monday.com GraphQL API.
Gestisce la creazione di item sulla board NEW COMMERCIALE.
"""

import json
import logging
import os
from typing import Dict, List, Optional, Tuple

import requests
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    import pathlib as _pl
    _ef = _pl.Path(__file__).parent / ".env"
    if _ef.exists():
        for _l in _ef.read_text(encoding="utf-8").splitlines():
            _l = _l.strip()
            if _l and not _l.startswith("#") and "=" in _l:
                _k, _v = _l.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

logger = logging.getLogger(__name__)

MONDAY_API_TOKEN = os.getenv("MONDAY_API_TOKEN", "")
MONDAY_BOARD_ID = os.getenv("MONDAY_BOARD_ID", "2133436509")
MONDAY_GROUP_ID = os.getenv("MONDAY_GROUP_ID", "topics")  # "Richieste commerciali AMR"

API_URL = "https://api.monday.com/v2"

# ── Column IDs reali della board NEW COMMERCIALE ──────────────────────────────
# Scoperti via API il 2026-06-10
COL_NOME_PROGETTO    = "testo_mkn1sqb4"           # text
COL_REFERENTE        = "dup__of_nome_referente_mkn3gf63"  # text
COL_EMAIL            = "email_mkn1v37b"            # email
COL_RICHIESTA        = "testo_lungo_mkn1ydyz"      # long_text → corpo email
COL_SPECIFICHE       = "text_mkypgtb8"             # text → tipo richiesta
COL_STATO_PREVENTIVO = "color_mkn4s77r"            # status → Stato preventivo
COL_PRIORITA         = "color_mknssm0t"            # status → Priorità

# Mappa tipo email → specifiche (etichetta visibile su Monday.com)
TIPO_SPEC_MAP = {
    "ordine":                  "📦 Ordine Confermato",
    "modifica_ordine":         "✏️ Modifica Ordine",
    "preventivo":              "📋 Richiesta Preventivo",
    "aggiornamento_preventivo":"🔄 Aggiornamento Preventivo",
    "nuova_richiesta":         "💡 Nuova Richiesta",
    "aggiornamento_generico":  "📨 Aggiornamento",
    # legacy (backward compat)
    "aggiornamento_ordine":    "✏️ Modifica Ordine",
}



def _graphql(query: str, variables: Optional[Dict] = None) -> Dict:
    """Esegue una query GraphQL sulla Monday.com API."""
    if not MONDAY_API_TOKEN:
        raise ValueError("MONDAY_API_TOKEN non configurato nel file .env")

    headers = {
        "Authorization": MONDAY_API_TOKEN,
        "Content-Type": "application/json",
        "API-Version": "2024-10",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    response = requests.post(API_URL, headers=headers, json=payload, timeout=30)
    response.raise_for_status()

    data = response.json()
    if "errors" in data:
        raise RuntimeError(f"Errori GraphQL Monday.com: {data['errors']}")
    return data.get("data", {})


def get_board_columns() -> List[Dict]:
    """
    Restituisce l'elenco delle colonne della board con id, titolo e tipo.
    Utile per mappare i nomi alle column ID.
    """
    query = """
    query ($board_id: [ID!]) {
      boards(ids: $board_id) {
        columns {
          id
          title
          type
          settings_str
        }
      }
    }
    """
    data = _graphql(query, {"board_id": [MONDAY_BOARD_ID]})
    boards = data.get("boards", [])
    if not boards:
        return []
    return boards[0].get("columns", [])


def get_board_groups() -> List[Dict]:
    """Restituisce i gruppi della board."""
    query = """
    query ($board_id: [ID!]) {
      boards(ids: $board_id) {
        groups {
          id
          title
        }
      }
    }
    """
    data = _graphql(query, {"board_id": [MONDAY_BOARD_ID]})
    boards = data.get("boards", [])
    if not boards:
        return []
    return boards[0].get("groups", [])


def get_default_group_id() -> str:
    """
    Restituisce l'ID del primo gruppo della board.
    Se MONDAY_GROUP_ID è configurato nel .env, usa quello.
    """
    if MONDAY_GROUP_ID:
        return MONDAY_GROUP_ID

    groups = get_board_groups()
    if not groups:
        raise RuntimeError("Nessun gruppo trovato nella board Monday.com")

    # Prendi il primo gruppo (di solito "Richieste commerciali AMR")
    group_id = groups[0]["id"]
    group_title = groups[0]["title"]
    logger.info("Usando il gruppo: '%s' (id: %s)", group_title, group_id)
    return group_id


def _build_column_values_direct(classification) -> Tuple[Dict, str]:
    """
    Costruisce il dizionario dei valori colonna usando gli ID colonna reali.
    Nessuna chiamata API aggiuntiva necessaria.
    """
    item_name = (classification.azienda or classification.email_mittente or "Nuova Richiesta")[:255]
    tipo_spec = TIPO_SPEC_MAP.get(classification.tipo, classification.tipo)

    column_values: Dict = {}

    # Nome Progetto (text)
    if classification.nome_progetto:
        column_values[COL_NOME_PROGETTO] = classification.nome_progetto[:500]

    # Referente (text)
    if classification.referente:
        column_values[COL_REFERENTE] = classification.referente[:200]

    # Email (email)
    if classification.email_mittente:
        column_values[COL_EMAIL] = {
            "email": classification.email_mittente,
            "text": classification.email_mittente,
        }

    # Richiesta/Note (long_text) → corpo sintetico email
    if classification.note:
        column_values[COL_RICHIESTA] = {"text": classification.note}

    # Specifiche richiesta (text) → tipo classificazione
    column_values[COL_SPECIFICHE] = tipo_spec

    return column_values, item_name


MONDAY_BOARD_COMMERCIALE = os.getenv("MONDAY_BOARD_COMMERCIALE", "1865049112")


def upload_attachments_to_item(item_id: str, attachments: Optional[List[Tuple[str, bytes]]], col_id: str = "file_mkn13rg5") -> None:
    """Carica gli allegati dell'email (PDF, DWG, DXF, ecc.) nella colonna file dell'item Monday."""
    if not item_id or not attachments:
        return
    try:
        q_assets = """
        query ($item_id: [ID!]) {
          items(ids: $item_id) {
            assets { name }
          }
        }
        """
        res_a = _graphql(q_assets, {"item_id": [str(item_id)]})
        existing_names = {
            (a.get("name") or "").strip().lower()
            for a in (res_a.get("items") or [{}])[0].get("assets", [])
        }
    except Exception:
        existing_names = set()

    for fname, fbytes in attachments:
        if not fname or not fbytes or fname.strip().lower() in existing_names:
            continue
        try:
            q_file = f'mutation ($file: File!) {{ add_file_to_column (item_id: {item_id}, column_id: "{col_id}", file: $file) {{ id }} }}'
            r = requests.post(
                "https://api.monday.com/v2/file",
                headers={"Authorization": MONDAY_API_TOKEN, "API-Version": "2024-10"},
                data={"query": q_file, "map": json.dumps({"image": "variables.file"})},
                files={"image": (fname, fbytes, "application/octet-stream")},
                timeout=60,
            )
            if r.status_code == 200 and "errors" not in r.text:
                logger.info("📎 Allegato '%s' caricato su item #%s (colonna %s)", fname, item_id, col_id)
                existing_names.add(fname.strip().lower())
            else:
                logger.warning("Upload allegato '%s' su item #%s: %s", fname, item_id, r.text[:160])
        except Exception as e:
            logger.warning("Errore upload allegato '%s' su item #%s: %s", fname, item_id, e)


import re as _re
import difflib as _difflib


def normalize_client_name(name: str) -> str:
    """
    Normalizza il nome cliente rimuovendo date manuali (es. 'ipe 26.10.26' -> 'ipe'),
    punti nelle sigle ('I.P.E.' -> 'ipe') e suffissi societari ('s.r.l.', 'spa', ecc.).
    """
    if not name:
        return ""
    s = name.lower().strip()
    # Rimuovi date tipo 26.10.26, 05/11/2026, 02-11-26
    s = _re.sub(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b", " ", s)
    # Unisci sigle puntate: i.p.e. -> ipe, s.r.l. -> srl
    s = _re.sub(r"\b([a-z])\.(?=[a-z]\b|\s|$)", r"\1", s)
    s = s.replace(".", " ")
    # Rimuovi forme societarie
    s = _re.sub(r"\b(srl|srls|spa|snc|sas|ltd|gmbh|inc|s\s+r\s+l|s\s+p\s+a)\b", " ", s)
    s = _re.sub(r"[^\w\s]", " ", s)
    s = _re.sub(r"\s+", " ", s).strip()
    return s


_PROJ_STOPWORDS = {
    "conferma", "ordine", "ordini", "richiesta", "preventivo", "offerta", "quotazione",
    "modifica", "aggiornamento", "invio", "file", "fornitura", "polistirolo", "polistirene",
    "eps", "xps", "resina", "taglio", "fresa", "finitura", "del", "per", "con", "senza",
    "rif", "nr", "num", "numero", "codice", "cod", "nuovo", "nuova", "elementi", "pezzi",
}


def _extract_order_numbers(text: str) -> set:
    """Estrae numeri d'ordine/documento significativi ignorando date e dimensioni (es. 60x60)."""
    if not text:
        return set()
    s = text.lower().replace("_", " ")
    # Rimuovi date (02/11/2026, 09-11-2026, 26.10.26)
    s = _re.sub(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b", " ", s)
    # Rimuovi dimensioni (es. 60x60, 1000x500x10)
    s = _re.sub(r"\b\d+\s*[x*×]\s*\d+(?:\s*[x*×]\s*\d+)?\b", " ", s)
    # Rimuovi anni isolati 2024..2029
    s = _re.sub(r"\b202[4-9]\b", " ", s)

    nums = set()
    for m in _re.finditer(
        r"\b(?:ordine|ord|cliente|conferma|prev|preventivo|off|offerta|ddt|rif|oda|odv|prf)[^\d]{0,12}(\d{2,7})\b",
        s,
    ):
        nums.add(m.group(1).lstrip("0") or "0")
    for m in _re.finditer(r"\b([a-z]{1,4}\d{4,8}|\d{4,6})\b", s):
        nums.add(m.group(1).lstrip("0") or "0")
    return nums


def is_same_commessa_project(proj_a: str, proj_b: str, note_a: str = "", note_b: str = "") -> bool:
    """
    Determina se due stringhe di progetto/note si riferiscono alla STESSA commessa/ordine
    per evitare qualsiasi clone su Monday.com.
    """
    pa = (proj_a or "").strip().lower()
    pb = (proj_b or "").strip().lower()
    if not pa or not pb:
        return False

    # 1. Confronto numeri d'ordine / documento (es. Ordine n. 1037 vs Conferma Ordine n. 1037)
    nums_a = _extract_order_numbers(f"{proj_a} {note_a}")
    nums_b = _extract_order_numbers(f"{proj_b} {note_b}")
    proj_nums_a = _extract_order_numbers(proj_a)
    proj_nums_b = _extract_order_numbers(proj_b)

    if proj_nums_a and proj_nums_b:
        if proj_nums_a & proj_nums_b:
            return True
        return False
    if (proj_nums_a and not proj_nums_b) or (proj_nums_b and not proj_nums_a):
        # Se uno ha un numero ordine esplicito nel titolo progetto (es. "Conferma Ordine n. 1020")
        # e l'altro è solo generico ("Conferma Ordine"), NON sono lo stesso ordine
        if nums_a & nums_b:
            return True
        return False

    # 2. Confronto token specifici di progetto (ignorando date, punteggiatura e parole generiche)
    def _tokens(txt: str) -> set:
        t = _re.sub(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b", " ", txt.lower())
        t = _re.sub(r"[^\w\s]", " ", t)
        return {
            w for w in t.split()
            if len(w) >= 3 and w not in _PROJ_STOPWORDS and not w.isdigit()
        }

    tok_a = _tokens(pa)
    tok_b = _tokens(pb)
    if tok_a and tok_b:
        common = tok_a & tok_b
        overlap = len(common) / max(1, min(len(tok_a), len(tok_b)))
        if overlap >= 0.5 or len(common) >= 2:
            return True
        return False

    # Fallback: confronto normalizzato solo se non è una dicitura puramente generica
    clean_a = _re.sub(r"[^\w]", "", pa)
    clean_b = _re.sub(r"[^\w]", "", pb)
    if clean_a == clean_b and clean_a not in ("confermaordine", "ordine", "preventivo", "richiestapreventivo"):
        return True
    return False


def _create_or_update_on_board(
    board_id: str,
    group_id: str,
    item_name: str,
    column_values: Dict,
    classification,
    attachments: Optional[List[Tuple[str, bytes]]] = None,
) -> Optional[str]:
    """
    Crea o aggiorna (anti-clone) un item su una specifica board Monday (COMMERCIALE o NEW COMMERCIALE).
    Controlla anche la rispettiva board GESTIONE PROGETTI (1865197409) / GESTIONE PROGETTI NEW (2136092569)
    per evitare di ricreare commesse già passate in produzione.
    """
    import urllib.parse as _urlparse

    try:
        gp_board_id = "1865197409" if str(board_id) == "1865049112" else "2136092569"
        check_q = """
        query ($board_ids: [ID!]) {
          boards(ids: $board_ids) {
            id
            items_page(limit: 250) {
              items {
                id
                name
                column_values(ids: ["testo_mkn1sqb4", "email_mkn1v37b", "testo_lungo_mkn1ydyz", "lookup_mknz7srg", "lookup_mknzf7p1", "testo_mkmnxqsk", "file_mknpcqbv", "text_mm51yk45"]) {
                  id
                  text
                  ... on MirrorValue { display_value }
                }
              }
            }
          }
        }
        """
        existing = _graphql(check_q, {"board_ids": [str(board_id), gp_board_id]})
        boards_data = existing.get("boards") or []

        norm_target_client = normalize_client_name(item_name)
        sender_email = (classification.email_mittente or "").strip().lower()
        is_internal_sender = sender_email in ("ordini@amrrecchia.it", "info@amrrecchia.it", "")

        for b_obj in boards_data:
            curr_b_id = str(b_obj.get("id", board_id))
            for it in b_obj.get("items_page", {}).get("items", []):
                it_name_norm = normalize_client_name(it.get("name", ""))
                col_map = {
                    c["id"]: (c.get("text") or c.get("display_value") or "").strip()
                    for c in it.get("column_values", [])
                }
                it_proj = " ".join(
                    v for v in [
                        col_map.get("testo_mkn1sqb4"),
                        col_map.get("lookup_mknz7srg"),
                        col_map.get("testo_mkmnxqsk"),
                        col_map.get("text_mm51yk45"),
                    ] if v
                )
                it_email = col_map.get("email_mkn1v37b", "").lower()
                it_note = col_map.get("testo_lungo_mkn1ydyz") or col_map.get("lookup_mknzf7p1") or ""
                if col_map.get("file_mknpcqbv"):
                    fname_dec = _urlparse.unquote(col_map["file_mknpcqbv"].split("/")[-1])
                    it_proj = f"{it_proj} {fname_dec}".strip()
                    it_note = f"{it_note} {fname_dec}".strip()

                same_client = False
                if norm_target_client and it_name_norm:
                    if norm_target_client == it_name_norm or it_name_norm.startswith(norm_target_client + " "):
                        same_client = True
                    elif _difflib.SequenceMatcher(None, norm_target_client, it_name_norm).ratio() >= 0.84:
                        same_client = True
                if not same_client and not is_internal_sender and it_email and it_email == sender_email:
                    same_client = True

                if same_client and is_same_commessa_project(
                    classification.nome_progetto,
                    f"{it_proj} {it.get('name', '')}".strip(),
                    classification.note,
                    it_note,
                ):
                    logger.info(
                        "🛡️ [ANTI-CLONE] Commessa già presente su board %s: '%s' / '%s' (id: #%s). Aggiorno colonne/file senza inviare commenti email duplicati.",
                        curr_b_id, it.get("name"), it_proj, it["id"]
                    )
                    _append_note_to_column(curr_b_id, it["id"], col_map.get("testo_lungo_mkn1ydyz", ""), classification)
                    upload_attachments_to_item(it["id"], attachments)
                    return it["id"]
    except Exception as check_err:
        logger.warning("Controllo preliminare anti-clone su board %s fallito: %s", board_id, check_err)

    cols_to_send = dict(column_values)
    # La colonna text_mkypgtb8 esiste solo su NEW COMMERCIALE (2133436509)
    if str(board_id) == "1865049112":
        cols_to_send.pop(COL_SPECIFICHE, None)

    query = """
    mutation CreateItem(
        $board_id: ID!,
        $group_id: String!,
        $item_name: String!,
        $column_values: JSON!
    ) {
      create_item(
        board_id: $board_id,
        group_id: $group_id,
        item_name: $item_name,
        column_values: $column_values
      ) {
        id
        name
      }
    }
    """
    variables = {
        "board_id": str(board_id),
        "group_id": group_id,
        "item_name": item_name,
        "column_values": json.dumps(cols_to_send),
    }

    data = _graphql(query, variables)
    item_id = data.get("create_item", {}).get("id")
    item_name_returned = data.get("create_item", {}).get("name", "")

    if item_id:
        logger.info(
            "✅ Item creato su Monday.com (board %s): '%s' (id: %s)",
            board_id, item_name_returned, item_id
        )
        # Non chiamiamo create_update("Email ricevuta...") per non far partire
        # una mail di notifica Monday ridondante a ordini@amrrecchia.it (tutti i dati sono già nelle colonne)
        upload_attachments_to_item(item_id, attachments)
    else:
        logger.error("Creazione item fallita su board %s: risposta inattesa da Monday.com", board_id)

    return item_id


def _append_note_to_column(board_id: str, item_id: str, existing_note: str, classification) -> None:
    """Aggiorna la colonna Testo lungo / Richiesta senza usare create_update (evita notifiche email ridondanti da Monday)."""
    if str(board_id) == "1865197409":
        # Su GESTIONE PROGETTI (1865197409) la colonna note è mirror da COMMERCIALE
        return
    new_note = (classification.note or "").strip()
    if not new_note:
        return
    if existing_note and new_note[:40].lower() in existing_note.lower():
        return
    combined = f"{existing_note}\n\n--- Aggiornamento ({classification.nome_progetto or classification.tipo}) ---\n{new_note}".strip() if existing_note else new_note
    try:
        q = f'mutation ($val: JSON!) {{ change_multiple_column_values(board_id: {board_id}, item_id: {item_id}, column_values: $val) {{ id }} }}'
        _graphql(q, {"val": json.dumps({COL_RICHIESTA: {"text": combined[:2000]}})})
    except Exception as e:
        logger.debug("Impossibile aggiornare colonna note su item %s: %s", item_id, e)


def create_item(classification, attachments: Optional[List[Tuple[str, bytes]]] = None) -> Optional[str]:
    """
    Crea un nuovo item sia sulla scheda principale COMMERCIALE (1865049112)
    sia su NEW COMMERCIALE (2133436509).
    """
    try:
        group_id = MONDAY_GROUP_ID or "topics"  # "topics" = Richieste commerciali AMR
        column_values, item_name = _build_column_values_direct(classification)

        primary_id = None
        for b_id in [MONDAY_BOARD_COMMERCIALE, MONDAY_BOARD_ID]:
            if not b_id:
                continue
            try:
                created_id = _create_or_update_on_board(
                    b_id, group_id, item_name, column_values, classification, attachments=attachments
                )
                if created_id and not primary_id:
                    primary_id = created_id
            except Exception as b_err:
                logger.error("Errore creazione item su board %s: %s", b_id, b_err)

        return primary_id

    except Exception as e:
        logger.error("Errore creazione item Monday.com: %s", e)
        return None


def _add_update_to_item(item_id: str, classification) -> None:
    """Aggiunge un update/commento all'item con il dettaglio dell'email (con controllo anti-duplicato)."""
    try:
        check_q = """
        query ($item_id: [ID!]) {
          items(ids: $item_id) {
            updates(limit: 15) {
              body
            }
          }
        }
        """
        res_u = _graphql(check_q, {"item_id": [str(item_id)]})
        existing_updates = (res_u.get("items") or [{}])[0].get("updates", [])
        clean_note = (classification.note or "").strip()[:50].lower()
        clean_proj = (classification.nome_progetto or "").strip().lower()
        for u in existing_updates:
            ub = (u.get("body") or "").lower()
            if (clean_note and len(clean_note) > 15 and clean_note in ub) or (clean_proj and len(clean_proj) > 10 and clean_proj in ub):
                logger.debug("Update già presente su item %s, salto duplicato.", item_id)
                return

        note_text = (
            f"📧 **Email ricevuta** da {classification.referente} "
            f"({classification.email_mittente})\n\n"
            f"**Tipo**: {classification.tipo.replace('_', ' ').title()}\n"
            f"**Progetto**: {classification.nome_progetto}\n\n"
            f"**Sintesi**: {classification.note}"
        )
        query = """
        mutation AddUpdate($item_id: ID!, $body: String!) {
          create_update(item_id: $item_id, body: $body) {
            id
          }
        }
        """
        _graphql(query, {"item_id": item_id, "body": note_text})
        logger.debug("Update aggiunto all'item %s", item_id)
    except Exception as e:
        logger.warning("Impossibile aggiungere update all'item %s: %s", item_id, e)


def setup_board_columns() -> None:
    """
    Aggiunge le colonne mancanti alla board NEW COMMERCIALE:
    - Tipo Richiesta (Status)
    - Data Ricezione (Date)
    - Note Email (Long Text)
    """
    existing_columns = get_board_columns()
    existing_titles = {col["title"] for col in existing_columns}

    columns_to_add = []

    if "Tipo Richiesta" not in existing_titles:
        columns_to_add.append({
            "title": "Tipo Richiesta",
            "column_type": "status",
        })

    if "Data Ricezione" not in existing_titles:
        columns_to_add.append({
            "title": "Data Ricezione",
            "column_type": "date",
        })

    if "Note Email" not in existing_titles:
        columns_to_add.append({
            "title": "Note Email",
            "column_type": "long_text",
        })

    if not columns_to_add:
        logger.info("Tutte le colonne necessarie sono già presenti sulla board.")
        return

    for col in columns_to_add:
        try:
            query = """
            mutation AddColumn($board_id: ID!, $title: String!, $column_type: ColumnType!) {
              create_column(board_id: $board_id, title: $title, column_type: $column_type) {
                id
                title
              }
            }
            """
            data = _graphql(query, {
                "board_id": MONDAY_BOARD_ID,
                "title": col["title"],
                "column_type": col["column_type"],
            })
            new_col = data.get("create_column", {})
            logger.info("✅ Colonna creata: '%s' (id: %s)", new_col.get("title"), new_col.get("id"))
        except Exception as e:
            logger.error("Errore creazione colonna '%s': %s", col["title"], e)
