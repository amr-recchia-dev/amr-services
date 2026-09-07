#!/usr/bin/env python3
"""
board_sync_guardian.py — Sincronizzatore Automatico Bidirezionale per Monday.com
AMR Recchia

Mantiene perfettamente allineate in tempo reale:
1. COMMERCIALE (1865049112) <——> NEW COMMERCIALE (2133436509)
2. GESTIONE PROGETTI (1865197409) <——> GESTIONE PROGETTI NEW (2136092569)

Funzionalità:
- Crea gli elementi mancanti da Vecchia -> Nuova e da Nuova -> Vecchia
- Sincronizza lo stato ("Stato preventivo", "Preventivo Accettato", "Stato Progetto")
- Risolve i conflitti basandosi sull'ultimo aggiornamento (updated_at)
- Mantiene una mappa persistente degli ID (sync_mapping.json)
"""

import os
import sys
import json
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import requests
from dotenv import load_dotenv

load_dotenv("/Users/shallo/Documents/Antigravity/email-agent/.env")

# ── Setup Logging ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [SYNC_GUARDIAN] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("sync_guardian")

# ── Configurazione Board Monday.com ──────────────────────────────────────────
MONDAY_TOKEN = os.getenv("MONDAY_API_TOKEN")
API_URL = "https://api.monday.com/v2"

BOARD_OLD_COMMERCIALE = "1865049112"
BOARD_NEW_COMMERCIALE = "2133436509"

BOARD_OLD_PROGETTI = "1865197409"
BOARD_NEW_PROGETTI = "2136092569"

MAPPING_FILE = Path(__file__).parent / "sync_mapping.json"

# Mappatura Stati Gestione Progetti (Old <-> New)
OLD_TO_NEW_PROGETTI_STATUS = {
    "in produzione": "In corso",
    "Fatto": "Fatto",
    "Bloccato": "Bloccato",
    "non in produzione": "Da iniziare",
    "in attesa file": "Da iniziare",
    "conto lavoro": "In corso",
}

NEW_TO_OLD_PROGETTI_STATUS = {
    "In corso": "in produzione",
    "Fatto": "Fatto",
    "Bloccato": "Bloccato",
    "Da iniziare": "non in produzione",
}

# Colonne condivise tra COMMERCIALE (Old) e NEW COMMERCIALE (New)
COMMERCIALE_SHARED_COLS = [
    "testo_mkn1sqb4",                  # Nome Progetto
    "dup__of_nome_referente_mkn3gf63",  # Referente
    "email_mkn1v37b",                   # Email
    "telefono_mkn3gd8v",                # Telefono
    "testo_lungo_mkn1ydyz",             # Note / Richiesta
    "color_mkn4s77r",                   # Stato preventivo
    "label_mkn37zp7",                   # Preventivo Accettato
    "color_mknssm0t",                   # Priorità
    "date4",                            # Data consegna
    "date_mknkb99e",                    # Data Preventivo
    "color_mkrkwak4",                   # Disegno tecnico
    "color_mknrnt50",                   # Pagamento
    "color_mknsezab",                   # Fresa
    "color_mkns43r0",                   # Taglio
    "color_mknsghqz",                   # Finitura
]


def monday_query(query: str, variables: Optional[dict] = None) -> dict:
    headers = {
        "Authorization": MONDAY_TOKEN,
        "API-Version": "2024-10",
        "Content-Type": "application/json"
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    for attempt in range(4):
        try:
            resp = requests.post(API_URL, headers=headers, json=payload, timeout=35)
            if resp.status_code == 200:
                data = resp.json()
                if "errors" in data:
                    logger.warning("Monday API errors: %s", data["errors"])
                return data.get("data", {})
            elif resp.status_code in (429, 500, 502, 503, 504):
                wait = (attempt + 1) * 3
                logger.warning("HTTP %d, attesa %ds...", resp.status_code, wait)
                time.sleep(wait)
            else:
                logger.error("HTTP %d: %s", resp.status_code, resp.text)
                return {}
        except Exception as e:
            logger.error("Eccezione richiesta Monday: %s", e)
            time.sleep(2)
    return {}


def load_mapping() -> dict:
    if MAPPING_FILE.exists():
        try:
            with open(MAPPING_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                d.setdefault("commerciale", {})
                d.setdefault("progetti", {})
                d.setdefault("archived", {"commerciale": [], "progetti": []})
                return d
        except Exception:
            pass
    return {"commerciale": {}, "progetti": {}, "archived": {"commerciale": [], "progetti": []}}


def save_mapping(mapping: dict) -> None:
    try:
        with open(MAPPING_FILE, "w", encoding="utf-8") as f:
            json.dump(mapping, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error("Errore salvataggio sync_mapping.json: %s", e)


def fetch_all_items(board_id: str) -> List[dict]:
    items = []
    cursor = None
    while True:
        if cursor:
            q = """
            query ($cursor: String!) {
              next_items_page(cursor: $cursor, limit: 100) {
                cursor
                items {
                  id
                  name
                  created_at
                  updated_at
                  group { id title }
                  column_values {
                    id
                    text
                    value
                    type
                  }
                }
              }
            }
            """
            data = monday_query(q, {"cursor": cursor}).get("next_items_page", {})
        else:
            q = """
            query ($board_id: [ID!]) {
              boards(ids: $board_id) {
                items_page(limit: 100) {
                  cursor
                  items {
                    id
                    name
                    created_at
                    updated_at
                    group { id title }
                    column_values {
                      id
                      text
                      value
                      type
                    }
                  }
                }
              }
            }
            """
            data = monday_query(q, {"board_id": [board_id]}).get("boards", [{}])[0].get("items_page", {})

        batch = data.get("items", [])
        items.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    return items


def extract_cols(item: dict) -> dict:
    res = {}
    for cv in item.get("column_values", []):
        res[cv["id"]] = {
            "text": cv.get("text") or "",
            "value": cv.get("value"),
            "type": cv.get("type")
        }
    return res


# ── Sincronizzazione COMMERCIALE ──────────────────────────────────────────────

def sync_commerciale() -> None:
    logger.info("── Avvio sincronizzazione COMMERCIALE (Old <-> New) ──")
    mapping = load_mapping()
    comm_map = mapping.setdefault("commerciale", {})  # old_id <-> new_id
    archived_comm = set(str(x) for x in mapping.setdefault("archived", {}).setdefault("commerciale", []))

    old_items = fetch_all_items(BOARD_OLD_COMMERCIALE)
    new_items = fetch_all_items(BOARD_NEW_COMMERCIALE)

    logger.info("Old COMMERCIALE: %d elementi | NEW COMMERCIALE: %d elementi", len(old_items), len(new_items))

    old_by_id = {it["id"]: it for it in old_items}
    new_by_id = {it["id"]: it for it in new_items}

    old_by_name = {it["name"].strip().lower(): it for it in old_items}
    new_by_name = {it["name"].strip().lower(): it for it in new_items}

    # 1. Popola o verifica mapping esistente
    for old_it in old_items:
        old_id = old_it["id"]
        old_name = old_it["name"].strip().lower()
        if old_id not in comm_map:
            # Match per nome esatto
            if old_name in new_by_name:
                comm_map[old_id] = new_by_name[old_name]["id"]

    for new_it in new_items:
        new_id = new_it["id"]
        new_name = new_it["name"].strip().lower()
        reverse_matched = False
        for oid, nid in comm_map.items():
            if nid == new_id:
                reverse_matched = True
                break
        if not reverse_matched and new_name in old_by_name:
            comm_map[old_by_name[new_name]["id"]] = new_id

    save_mapping(mapping)

    # 2. Sincronizzazione ARCHIVIAZIONI bidirezionale
    # Se un elemento mappato non è più attivo su una board, propaga l'archiviazione all'altra (MAI ricreare!)
    for old_id, new_id in list(comm_map.items()):
        old_id_str, new_id_str = str(old_id), str(new_id)
        if old_id_str in archived_comm or new_id_str in archived_comm:
            continue

        old_active = old_id in old_by_id
        new_active = new_id in new_by_id

        if not old_active and new_active:
            logger.info("📦 Archiviazione propagata [OLD -> NEW]: '%s' (new_id: %s)", new_by_id[new_id]["name"], new_id)
            q = f'mutation {{ archive_item(item_id: "{new_id}") {{ id }} }}'
            monday_query(q)
            archived_comm.add(old_id_str)
            archived_comm.add(new_id_str)
            mapping["archived"]["commerciale"] = list(archived_comm)
            save_mapping(mapping)
            continue
        elif old_active and not new_active:
            logger.info("📦 Archiviazione propagata [NEW -> OLD]: '%s' (old_id: %s)", old_by_id[old_id]["name"], old_id)
            q = f'mutation {{ archive_item(item_id: "{old_id}") {{ id }} }}'
            monday_query(q)
            archived_comm.add(old_id_str)
            archived_comm.add(new_id_str)
            mapping["archived"]["commerciale"] = list(archived_comm)
            save_mapping(mapping)
            continue

    # 3. Copia elementi da OLD -> NEW SOLO se GENUINAMENTE NUOVI (mai visti e mai archiviati)
    for old_it in old_items:
        old_id = old_it["id"]
        if old_id in comm_map or str(old_id) in archived_comm:
            continue
        old_name = old_it["name"].strip()
        if old_name.lower() in new_by_name:
            comm_map[old_id] = new_by_name[old_name.lower()]["id"]
            save_mapping(mapping)
            continue

        logger.info("➕ Creazione elemento da OLD a NEW: '%s' (old_id: %s)", old_name, old_id)
        cols = extract_cols(old_it)
        create_vals = {}
        for col_id in COMMERCIALE_SHARED_COLS:
            if col_id in cols and cols[col_id]["value"]:
                try:
                    create_vals[col_id] = json.loads(cols[col_id]["value"])
                except Exception:
                    pass

        q = """
        mutation ($board_id: ID!, $item_name: String!, $column_values: JSON!) {
          create_item(board_id: $board_id, item_name: $item_name, column_values: $column_values) {
            id
            name
          }
        }
        """
        res = monday_query(q, {
            "board_id": BOARD_NEW_COMMERCIALE,
            "item_name": old_name,
            "column_values": json.dumps(create_vals)
        })
        new_id = res.get("create_item", {}).get("id")
        if new_id:
            logger.info("✅ Creato su NEW COMMERCIALE: '%s' (new_id: %s)", old_name, new_id)
            comm_map[old_id] = new_id
            save_mapping(mapping)
            time.sleep(1)

    # 4. Copia elementi da NEW -> OLD SOLO se GENUINAMENTE NUOVI (es. creati da email agent)
    reverse_map = {nid: oid for oid, nid in comm_map.items()}
    for new_it in new_items:
        new_id = new_it["id"]
        if new_id in reverse_map or str(new_id) in archived_comm:
            continue
        new_name = new_it["name"].strip()
        if new_name.lower() in old_by_name:
            comm_map[old_by_name[new_name.lower()]["id"]] = new_id
            save_mapping(mapping)
            continue

        logger.info("➕ Creazione elemento da NEW a OLD: '%s' (new_id: %s)", new_name, new_id)
        cols = extract_cols(new_it)
        create_vals = {}
        for col_id in COMMERCIALE_SHARED_COLS:
            if col_id in cols and cols[col_id]["value"]:
                try:
                    create_vals[col_id] = json.loads(cols[col_id]["value"])
                except Exception:
                    pass

        q = """
        mutation ($board_id: ID!, $item_name: String!, $column_values: JSON!) {
          create_item(board_id: $board_id, item_name: $item_name, column_values: $column_values) {
            id
            name
          }
        }
        """
        res = monday_query(q, {
            "board_id": BOARD_OLD_COMMERCIALE,
            "item_name": new_name,
            "column_values": json.dumps(create_vals)
        })
        old_id = res.get("create_item", {}).get("id")
        if old_id:
            logger.info("✅ Creato su OLD COMMERCIALE: '%s' (old_id: %s)", new_name, old_id)
            comm_map[old_id] = new_id
            save_mapping(mapping)
            time.sleep(1)

    # 4. Sincronizzazione Stati (bidirezionale con conflict resolution su updated_at)
    for old_id, new_id in comm_map.items():
        if old_id not in old_by_id or new_id not in new_by_id:
            continue

        old_it = old_by_id[old_id]
        new_it = new_by_id[new_id]

        old_upd = old_it.get("updated_at") or old_it.get("created_at") or ""
        new_upd = new_it.get("updated_at") or new_it.get("created_at") or ""

        old_cols = extract_cols(old_it)
        new_cols = extract_cols(new_it)

        # Controlla Preventivo Accettato (label_mkn37zp7) e Stato Preventivo (color_mkn4s77r)
        status_cols_to_sync = ["label_mkn37zp7", "color_mkn4s77r", "color_mknssm0t"]
        for scol in status_cols_to_sync:
            old_txt = old_cols.get(scol, {}).get("text", "")
            new_txt = new_cols.get(scol, {}).get("text", "")

            if old_txt != new_txt and (old_txt or new_txt):
                if old_upd > new_upd and old_cols.get(scol, {}).get("value"):
                    # Old è più recente -> aggiorna New
                    val = old_cols[scol]["value"]
                    logger.info("🔄 Sync stato %s [OLD -> NEW]: '%s' -> '%s' (item: %s)", scol, new_txt, old_txt, new_it["name"])
                    q = f'mutation {{ change_column_value(board_id: {BOARD_NEW_COMMERCIALE}, item_id: {new_id}, column_id: "{scol}", value: {json.dumps(val)}) {{ id }} }}'
                    monday_query(q)
                elif new_upd >= old_upd and new_cols.get(scol, {}).get("value"):
                    # New è più recente -> aggiorna Old
                    val = new_cols[scol]["value"]
                    logger.info("🔄 Sync stato %s [NEW -> OLD]: '%s' -> '%s' (item: %s)", scol, old_txt, new_txt, old_it["name"])
                    q = f'mutation {{ change_column_value(board_id: {BOARD_OLD_COMMERCIALE}, item_id: {old_id}, column_id: "{scol}", value: {json.dumps(val)}) {{ id }} }}'
                    monday_query(q)


# ── Sincronizzazione GESTIONE PROGETTI ─────────────────────────────────────────

def sync_gestione_progetti() -> None:
    logger.info("── Avvio sincronizzazione GESTIONE PROGETTI (Old <-> New) ──")
    mapping = load_mapping()
    prog_map = mapping.setdefault("progetti", {})
    archived_prog = set(str(x) for x in mapping.setdefault("archived", {}).setdefault("progetti", []))

    old_items = fetch_all_items(BOARD_OLD_PROGETTI)
    new_items = fetch_all_items(BOARD_NEW_PROGETTI)

    logger.info("Old PROGETTI: %d elementi | NEW PROGETTI: %d elementi", len(old_items), len(new_items))

    old_by_id = {it["id"]: it for it in old_items}
    new_by_id = {it["id"]: it for it in new_items}

    old_by_name = {it["name"].strip().lower(): it for it in old_items}
    new_by_name = {it["name"].strip().lower(): it for it in new_items}

    # 1. Matching per nome tra elementi attivi
    for old_it in old_items:
        old_id = old_it["id"]
        old_name = old_it["name"].strip().lower()
        if old_id not in prog_map and old_name in new_by_name:
            prog_map[old_id] = new_by_name[old_name]["id"]

    for new_it in new_items:
        new_id = new_it["id"]
        new_name = new_it["name"].strip().lower()
        rev_matched = any(nid == new_id for nid in prog_map.values())
        if not rev_matched and new_name in old_by_name:
            prog_map[old_by_name[new_name]["id"]] = new_id

    save_mapping(mapping)

    # 2. Sincronizzazione ARCHIVIAZIONI bidirezionale
    # Se un progetto mappato non è più presente tra gli attivi di una board, propaga l'archiviazione (MAI ricreare!)
    for old_id, new_id in list(prog_map.items()):
        old_id_str, new_id_str = str(old_id), str(new_id)
        if old_id_str in archived_prog or new_id_str in archived_prog:
            continue

        old_active = old_id in old_by_id
        new_active = new_id in new_by_id

        if not old_active and new_active:
            # Gary ha archiviato su OLD -> archivia anche su NEW!
            logger.info("📦 Gary/utente ha archiviato su OLD PROGETTI -> archiviazione su NEW: '%s' (new_id: %s)", new_by_id[new_id]["name"], new_id)
            q = f'mutation {{ archive_item(item_id: "{new_id}") {{ id }} }}'
            monday_query(q)
            archived_prog.add(old_id_str)
            archived_prog.add(new_id_str)
            mapping["archived"]["progetti"] = list(archived_prog)
            save_mapping(mapping)
            continue
        elif old_active and not new_active:
            # Archiviazione su NEW -> propaga su OLD!
            logger.info("📦 Archiviazione su NEW PROGETTI -> archiviazione su OLD: '%s' (old_id: %s)", old_by_id[old_id]["name"], old_id)
            q = f'mutation {{ archive_item(item_id: "{old_id}") {{ id }} }}'
            monday_query(q)
            archived_prog.add(old_id_str)
            archived_prog.add(new_id_str)
            mapping["archived"]["progetti"] = list(archived_prog)
            save_mapping(mapping)
            continue

    # 3. Copia elementi da OLD -> NEW SOLO se GENUINAMENTE NUOVI (mai visti e mai archiviati)
    for old_it in old_items:
        old_id = old_it["id"]
        if old_id in prog_map or str(old_id) in archived_prog:
            continue
        old_name = old_it["name"].strip()
        if old_name.lower() in new_by_name:
            prog_map[old_id] = new_by_name[old_name.lower()]["id"]
            save_mapping(mapping)
            continue

        logger.info("➕ Creazione progetto da OLD a NEW: '%s' (old_id: %s)", old_name, old_id)
        cols = extract_cols(old_it)
        old_status = cols.get("project_status", {}).get("text", "")
        new_status_label = OLD_TO_NEW_PROGETTI_STATUS.get(old_status, "Da iniziare")

        create_vals = {
            "color_mm45raj9": {"label": new_status_label}
        }

        q = """
        mutation ($board_id: ID!, $item_name: String!, $column_values: JSON!) {
          create_item(board_id: $board_id, item_name: $item_name, column_values: $column_values) {
            id
            name
          }
        }
        """
        res = monday_query(q, {
            "board_id": BOARD_NEW_PROGETTI,
            "item_name": old_name,
            "column_values": json.dumps(create_vals)
        })
        new_id = res.get("create_item", {}).get("id")
        if new_id:
            logger.info("✅ Creato su GESTIONE PROGETTI NEW: '%s' (new_id: %s)", old_name, new_id)
            prog_map[old_id] = new_id
            save_mapping(mapping)
            time.sleep(1)

    # 4. Copia elementi da NEW -> OLD SOLO se GENUINAMENTE NUOVI (mai visti e mai archiviati)
    rev_prog_map = {nid: oid for oid, nid in prog_map.items()}
    for new_it in new_items:
        new_id = new_it["id"]
        if new_id in rev_prog_map or str(new_id) in archived_prog:
            continue
        new_name = new_it["name"].strip()
        if new_name.lower() in old_by_name:
            prog_map[old_by_name[new_name.lower()]["id"]] = new_id
            save_mapping(mapping)
            continue

        logger.info("➕ Creazione progetto da NEW a OLD: '%s' (new_id: %s)", new_name, new_id)
        cols = extract_cols(new_it)
        new_status = cols.get("color_mm45raj9", {}).get("text", "")
        old_status_label = NEW_TO_OLD_PROGETTI_STATUS.get(new_status, "non in produzione")

        create_vals = {
            "project_status": {"label": old_status_label}
        }

        q = """
        mutation ($board_id: ID!, $item_name: String!, $column_values: JSON!) {
          create_item(board_id: $board_id, item_name: $item_name, column_values: $column_values) {
            id
            name
          }
        }
        """
        res = monday_query(q, {
            "board_id": BOARD_OLD_PROGETTI,
            "item_name": new_name,
            "column_values": json.dumps(create_vals)
        })
        old_id = res.get("create_item", {}).get("id")
        if old_id:
            logger.info("✅ Creato su OLD GESTIONE PROGETTI: '%s' (old_id: %s)", new_name, old_id)
            prog_map[old_id] = new_id
            save_mapping(mapping)
            time.sleep(1)

    # 4. Sincronizzazione Stati Progetto (bidirezionale)
    for old_id, new_id in prog_map.items():
        if old_id not in old_by_id or new_id not in new_by_id:
            continue

        old_it = old_by_id[old_id]
        new_it = new_by_id[new_id]

        old_upd = old_it.get("updated_at") or old_it.get("created_at") or ""
        new_upd = new_it.get("updated_at") or new_it.get("created_at") or ""

        old_cols = extract_cols(old_it)
        new_cols = extract_cols(new_it)

        old_status = old_cols.get("project_status", {}).get("text", "").strip()
        new_status = new_cols.get("color_mm45raj9", {}).get("text", "").strip()

        # Verifica se gli stati corrispondono secondo la mappa
        expected_new_status = OLD_TO_NEW_PROGETTI_STATUS.get(old_status, "")
        expected_old_status = NEW_TO_OLD_PROGETTI_STATUS.get(new_status, "")

        if new_status != expected_new_status:
            if old_upd > new_upd and expected_new_status:
                logger.info("🔄 Sync stato progetto [OLD -> NEW]: '%s' -> '%s' (item: %s)", new_status, expected_new_status, new_it["name"])
                val = {"label": expected_new_status}
                q = f'mutation {{ change_column_value(board_id: {BOARD_NEW_PROGETTI}, item_id: {new_id}, column_id: "color_mm45raj9", value: {json.dumps(json.dumps(val))}) {{ id }} }}'
                monday_query(q)
            elif new_upd >= old_upd and expected_old_status:
                logger.info("🔄 Sync stato progetto [NEW -> OLD]: '%s' -> '%s' (item: %s)", old_status, expected_old_status, old_it["name"])
                val = {"label": expected_old_status}
                q = f'mutation {{ change_column_value(board_id: {BOARD_OLD_PROGETTI}, item_id: {old_id}, column_id: "project_status", value: {json.dumps(json.dumps(val))}) {{ id }} }}'
                monday_query(q)

    logger.info("🏁 Sincronizzazione completata.")


def run_full_sync():
    """Esegue un ciclo completo di sincronizzazione."""
    try:
        sync_commerciale()
        sync_gestione_progetti()
    except Exception as e:
        logger.error("Errore critico durante la sincronizzazione: %s", e, exc_info=True)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        logger.info("🚀 Avvio Board Sync Guardian in modalità loop continuo (ogni 5 minuti)...")
        while True:
            run_full_sync()
            time.sleep(300)
    else:
        run_full_sync()
