#!/usr/bin/env python3
"""
drive_archiver.py - Archiviatore completo di un progetto Monday.com su Google Drive.
Scarica tutti i dati di un item (file, conversazioni, dati) e li organizza in cartelle.
"""

import os
import re
import json
import time
import logging
import tempfile
import requests
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
import drive_client as drive

load_dotenv()
logger = logging.getLogger("drive_archiver")

# === Configurazione Monday.com ===
MONDAY_TOKEN = os.getenv("MONDAY_API_TOKEN", "")
MONDAY_HEADERS = {
    "Authorization": MONDAY_TOKEN,
    "Content-Type":  "application/json",
    "API-Version":   "2024-10",
}
MONDAY_API = "https://api.monday.com/v2"

# ID board GESTIONE PROGETTI NEW (da aggiornare con il valore corretto)
GESTIONE_BOARD_ID = os.getenv("MONDAY_GESTIONE_BOARD_ID", "2136092569")


def _monday_query(query: str, variables: dict = None) -> dict:
    """Esegue una query GraphQL su Monday.com."""
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    r = requests.post(MONDAY_API, headers=MONDAY_HEADERS, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def _get_board_column_titles(board_id: str) -> dict:
    """
    Recupera la mappa {column_id: column_title} dalla board.
    La API 2024-10 non restituisce 'title' sulle column_values degli item,
    ma lo restituisce sulle colonne della board.
    """
    query = """
    query GetBoardColumns($board_id: [ID!]!) {
      boards(ids: $board_id) {
        columns {
          id
          title
        }
      }
    }
    """
    result = _monday_query(query, {"board_id": [str(board_id)]})
    columns = result.get("data", {}).get("boards", [{}])[0].get("columns", [])
    return {col["id"]: col["title"] for col in columns}


def get_item_data(item_id: str) -> dict:
    """Recupera tutti i dati di un item Monday.com: colonne, assets, updates."""
    logger.info(f"📋 Recupero dati item {item_id} da Monday.com...")

    query = """
    query GetItem($ids: [ID!]!) {
      items(ids: $ids) {
        id
        name
        board { id name }
        group { id title }
        column_values {
          id
          text
          value
          type
        }
        assets {
          id
          name
          url
          public_url
          file_extension
          created_at
        }
        updates(limit: 100) {
          id
          body
          text_body
          created_at
          creator { id name }
          assets {
            id
            name
            url
            public_url
            file_extension
          }
          replies {
            id
            body
            created_at
            creator { id name }
          }
        }
        subitems {
          id
          name
          column_values { id text }
        }
      }
    }
    """
    result = _monday_query(query, {"ids": [str(item_id)]})
    items = result.get("data", {}).get("items", [])
    if not items:
        raise ValueError(f"Item {item_id} non trovato su Monday.com")

    item = items[0]

    # Recupera i titoli delle colonne dalla board e iniettali nelle column_values
    board_id = item.get("board", {}).get("id")
    if board_id:
        titles_map = _get_board_column_titles(board_id)
        for col in item.get("column_values", []):
            col["title"] = titles_map.get(col["id"], col["id"])
        # Stesso per subitems
        for sub in item.get("subitems", []):
            for col in sub.get("column_values", []):
                col["title"] = titles_map.get(col["id"], col["id"])

    return item


def _extract_field(item: dict, search_terms: list[str]) -> str:
    """Cerca un valore nelle colonne per parole chiave nel titolo."""
    search_lower = [t.lower() for t in search_terms]
    for col in item.get("column_values", []):
        title_lower = col.get("title", "").lower()
        if any(t in title_lower for t in search_lower) and col.get("text"):
            return col["text"].strip()
    return ""


def _safe_name(name: str) -> str:
    """Rimuove caratteri non validi per nomi cartella Drive/filesystem."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    cleaned = cleaned.strip(". ")
    return cleaned[:100] or "Senza Nome"


def build_folder_structure(item: dict):  # -> tuple[str, str, str]
    """
    Determina: nome_cliente, nome_cartella_progetto, data_str.
    Il nome del cliente corrisponde al nome dell'item (azienda).
    Il nome della cartella progetto è: '[Nome Item] - [YYYY-MM-DD]'.
    """
    # Nome cliente = nome dell'item (es. "Fiera Milano S.p.A")
    nome_cliente = _safe_name(item.get("name", "Cliente Sconosciuto"))

    # Nome progetto: cerca SOLO colonne dedicate al progetto con valori significativi (>5 chars).
    # Evita di raccogliere valori booleani come "SI"/"NO" o etichette brevi.
    def _extract_project_name(search_terms):
        search_lower = [t.lower() for t in search_terms]
        for col in item.get("column_values", []):
            title_lower = col.get("title", "").lower()
            val = (col.get("text") or "").strip()
            # Deve essere un match esatto di colonna "progetto/project" E avere un valore utile
            if any(t == title_lower or title_lower.startswith(t) for t in search_lower):
                if len(val) > 5:  # ignora valori brevissimi come "SI", "NO", "N/D"
                    return val
        return ""

    nome_progetto = (
        _extract_project_name(["progetto", "project", "titolo progetto", "titolo", "nome progetto", "oggetto"]) or
        nome_cliente  # fallback sicuro: usa il nome dell'azienda
    )
    nome_progetto = _safe_name(nome_progetto)[:60]

    data_str = datetime.now().strftime("%Y-%m-%d")
    nome_cartella = f"{nome_progetto} - {data_str}"

    return nome_cliente, nome_cartella, data_str


def generate_scheda_progetto(item: dict) -> str:
    """Genera il file testo 'Scheda Progetto.txt' con tutti i dati."""
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    lines = [
        "=" * 60,
        f"  SCHEDA PROGETTO — AMR RECCHIA",
        f"  Archiviata il: {now}",
        "=" * 60,
        "",
        f"CLIENTE / AZIENDA : {item.get('name', 'N/D')}",
        f"BOARD             : {item.get('board', {}).get('name', 'N/D')}",
        f"GRUPPO            : {item.get('group', {}).get('title', 'N/D')}",
        f"ID ITEM           : {item.get('id', 'N/D')}",
        "",
        "─" * 60,
        "  DATI PROGETTO",
        "─" * 60,
    ]

    # Tutte le colonne con valore
    for col in item.get("column_values", []):
        val = (col.get("text") or "").strip()
        if val and val not in ("", "null", "None"):
            title = col.get("title") or col.get("id") or ""
            lines.append(f"{title:<30}: {val}")

    lines += ["", "─" * 60, "  ALLEGATI", "─" * 60]

    assets = item.get("assets", [])
    if assets:
        for a in assets:
            lines.append(f"  • {a.get('name', 'N/D')} [{a.get('file_extension', '').upper()}] — {a.get('created_at', '')[:10]}")
    else:
        lines.append("  (nessun allegato diretto)")

    lines += ["", "─" * 60, "  CONVERSAZIONI E AGGIORNAMENTI", "─" * 60]

    updates = item.get("updates", [])
    if updates:
        lines.append(f"  {len(updates)} aggiornamento/i trovati. Vedi file 'Conversazioni/conversazioni.txt'")
    else:
        lines.append("  (nessun aggiornamento)")

    subitems = item.get("subitems", [])
    if subitems:
        lines += ["", "─" * 60, "  SOTTO-ELEMENTI", "─" * 60]
        for sub in subitems:
            lines.append(f"  • {sub.get('name', 'N/D')}")
            for col in sub.get("column_values", []):
                val = col.get("text", "").strip()
                if val:
                    lines.append(f"      {col.get('title','')}: {val}")

    lines += ["", "=" * 60, "  Fine scheda progetto", "=" * 60]
    return "\n".join(lines)


def generate_conversazioni(item: dict) -> str:
    """Genera il file testo con tutte le conversazioni/updates."""
    lines = [
        "=" * 60,
        f"  CONVERSAZIONI — {item.get('name', '')}",
        "=" * 60, ""
    ]
    updates = item.get("updates", [])
    if not updates:
        lines.append("(nessuna conversazione registrata)")
        return "\n".join(lines)

    for upd in updates:
        creator     = upd.get("creator", {})
        author      = creator.get("name", creator.get("email", "Sconosciuto"))
        created_raw = upd.get("created_at", "")
        date_str    = created_raw[:10] if created_raw else "N/D"
        time_str    = created_raw[11:16] if len(created_raw) > 16 else ""
        body        = upd.get("text_body") or upd.get("body") or "(vuoto)"

        lines += [
            f"── {date_str} {time_str} | {author} ──",
            body,
        ]

        # Allegati nell'update
        for a in upd.get("assets", []):
            lines.append(f"  📎 Allegato: {a.get('name', 'N/D')}")

        # Risposte
        for reply in upd.get("replies", []):
            reply_author = reply.get("creator", {}).get("name", "N/D")
            reply_date   = reply.get("created_at", "")[:10]
            reply_body   = reply.get("body", "(vuoto)")
            lines += [
                f"   ↳ {reply_date} | {reply_author}: {reply_body}"
            ]
        lines.append("")

    return "\n".join(lines)


def download_asset(asset: dict, tmp_dir: str) -> str:
    """
    Scarica un asset Monday.com in una cartella temporanea.
    Prova prima con public_url (no auth), poi con url autenticato.
    Ritorna il path locale o None in caso di errore.
    """
    public_url = asset.get("public_url")
    auth_url   = asset.get("url")

    filename = asset.get("name") or f"file_{asset.get('id', 'unknown')}"
    # Sanifica il nome del file
    filename = re.sub(r'[\/\\:\*?"<>|]', "_", filename)
    local_path = Path(tmp_dir) / filename

    for url, use_auth in [(public_url, False), (auth_url, True)]:
        if not url:
            continue
        try:
            headers = {}
            if use_auth:
                headers["Authorization"] = MONDAY_TOKEN
            r = requests.get(url, headers=headers, timeout=120, stream=True)
            if r.status_code == 200:
                with open(local_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=16384):
                        f.write(chunk)
                size_kb = local_path.stat().st_size // 1024
                logger.info(f"⬇️  Scaricato: {filename} ({size_kb} KB)")
                return str(local_path)
            else:
                logger.debug(f"Download {filename} status {r.status_code} con {'auth' if use_auth else 'public'}, provo prossimo...")
        except Exception as e:
            logger.debug(f"Download {filename} errore: {e}, provo prossimo...")

    logger.warning(f"⚠️  Impossibile scaricare '{filename}' — saltato")
    return None


def _update_monday_status(item_id: str, board_id: str, column_id: str, status_label: str):
    """
    Cambia il valore di una colonna Status su Monday.com.
    status_label: il testo esatto dell'etichetta status (es. 'Archiviato').
    """
    # Prima recupera l'indice dell'etichetta dalla board
    query_labels = """
    query GetStatusLabels($board_id: [ID!]!) {
      boards(ids: $board_id) {
        columns {
          id
          settings_str
        }
      }
    }
    """
    try:
        result = _monday_query(query_labels, {"board_id": [str(board_id)]})
        columns = result.get("data", {}).get("boards", [{}])[0].get("columns", [])
        settings_str = next((c["settings_str"] for c in columns if c["id"] == column_id), "{}")
        settings = json.loads(settings_str)
        labels = settings.get("labels", {})
        # Trova l'indice dell'etichetta
        index = next((k for k, v in labels.items() if v.lower() == status_label.lower()), None)
        if index is None:
            logger.warning(f"⚠️  Etichetta '{status_label}' non trovata in colonna {column_id}. Labels disponibili: {labels}")
            return
        # Aggiorna la colonna
        mutation = """
        mutation UpdateStatus($item_id: ID!, $board_id: ID!, $col_id: String!, $value: JSON!) {
          change_column_value(item_id: $item_id, board_id: $board_id, column_id: $col_id, value: $value) { id }
        }
        """
        value = json.dumps({"index": int(index)})
        _monday_query(mutation, {
            "item_id":  str(item_id),
            "board_id": str(board_id),
            "col_id":   column_id,
            "value":    value,
        })
        logger.info(f"✅ Stato colonna {column_id} → '{status_label}' (index {index})")
    except Exception as e:
        logger.warning(f"⚠️  Errore aggiornamento stato Monday: {e}")


def archive_item(item_id: str) -> dict:
    """
    Funzione principale: archivia un item Monday.com su Google Drive.
    Ritorna un dict con i risultati dell'archiviazione.
    """
    start_time = time.time()
    logger.info(f"🚀 Avvio archiviazione item {item_id}")

    result = {
        "item_id":      item_id,
        "success":      False,
        "folder_url":   None,
        "files_count":  0,
        "error":        None,
    }

    try:
        # 1. Recupera dati Monday.com
        item = get_item_data(item_id)
        nome_cliente, nome_cartella, data_str = build_folder_structure(item)
        logger.info(f"📦 Archivio: {nome_cliente} / {nome_cartella}")

        # 2. Crea struttura cartelle su Drive
        root = drive.DRIVE_ROOT
        cartella_cliente  = drive.get_or_create_folder(nome_cliente,  root)
        cartella_progetto = drive.get_or_create_folder(nome_cartella, cartella_cliente)

        # 3. Carica Scheda Progetto.txt
        scheda = generate_scheda_progetto(item).encode("utf-8")
        drive.upload_bytes(scheda, "Scheda Progetto.txt", cartella_progetto, "text/plain")
        result["files_count"] += 1

        # 4. Carica Conversazioni/conversazioni.txt
        conv = generate_conversazioni(item).encode("utf-8")
        cartella_conv = drive.get_or_create_folder("Conversazioni", cartella_progetto)
        drive.upload_bytes(conv, "conversazioni.txt", cartella_conv, "text/plain")
        result["files_count"] += 1

        # 5. Scarica e carica allegati
        all_assets = list(item.get("assets", []))
        # Allegati anche dagli updates
        for upd in item.get("updates", []):
            all_assets.extend(upd.get("assets", []))

        if all_assets:
            with tempfile.TemporaryDirectory() as tmp_dir:
                # Pre-crea le sottocartelle necessarie
                subcartelle_cache = {}
                def get_subcartella(subfolder_name: str) -> str:
                    if subfolder_name not in subcartelle_cache:
                        subcartelle_cache[subfolder_name] = drive.get_or_create_folder(
                            subfolder_name, cartella_progetto
                        )
                    return subcartelle_cache[subfolder_name]

                for asset in all_assets:
                    local_path = download_asset(asset, tmp_dir)
                    if not local_path:
                        continue
                    # Determina sottocartella
                    subfolder = drive.get_subfolder_for_file(asset.get("name", ""))
                    subfolder_id = get_subcartella(subfolder)
                    # Carica su Drive
                    try:
                        drive.upload_file(local_path, subfolder_id)
                        result["files_count"] += 1
                    except Exception as e:
                        logger.error(f"❌ Upload fallito per '{asset.get('name')}': {e}")
        else:
            logger.info("ℹ️  Nessun allegato da caricare per questo item")

        # 6. URL cartella progetto
        folder_url = drive.get_folder_url(cartella_progetto)
        result["folder_url"] = folder_url
        result["success"]    = True

        elapsed = round(time.time() - start_time, 1)

        # 7. Aggiorna stato colonna Monday.com → 'Archiviato'
        board_id = item.get("board", {}).get("id", GESTIONE_BOARD_ID)
        try:
            _update_monday_status(item_id, board_id, "color_mm45raj9", "Archiviato")
        except Exception as e:
            logger.warning(f"⚠️  Impossibile aggiornare stato: {e}")

        # 8. Aggiunge commento di archiviazione con link Drive
        n_allegati = result['files_count'] - 2  # escludi scheda + conversazioni
        update_text = (
            f"📦 **Progetto archiviato su Google Drive** il {data_str}\n\n"
            f"📁 **Cartella:** [{nome_cliente} / {nome_cartella}]({folder_url})\n"
            f"📄 File caricati: {result['files_count']} totali"
            + (f" (di cui {n_allegati} allegati)" if n_allegati > 0 else "") +
            f"\n⏱️  Completato in {elapsed}s"
        )
        _add_monday_update(item_id, update_text)

        logger.info(f"✅ Archiviazione completata: {folder_url} ({result['files_count']} file, {elapsed}s)")

    except drive.DriveAuthRequired as e:
        result["error"] = f"Auth Drive richiesta: {e}"
        logger.error(f"🔐 {result['error']}")
    except Exception as e:
        result["error"] = str(e)
        logger.error(f"❌ Errore archiviazione: {e}", exc_info=True)

    return result


def _add_monday_update(item_id: str, text: str):
    """Aggiunge un commento/update all'item Monday.com."""
    query = """
    mutation AddUpdate($item_id: ID!, $body: String!) {
      create_update(item_id: $item_id, body: $body) { id }
    }
    """
    try:
        _monday_query(query, {"item_id": str(item_id), "body": text})
        logger.info(f"✅ Update Monday.com aggiunto all'item {item_id}")
    except Exception as e:
        logger.warning(f"⚠️  Errore aggiunta update Monday: {e}")


# ======================== CLI ========================

if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if len(sys.argv) < 2:
        print("Uso: python3 drive_archiver.py <item_id>")
        print("      python3 drive_archiver.py --test")
        sys.exit(1)

    if sys.argv[1] == "--test":
        # Usa il primo item disponibile della board
        print("🔍 Test archiviazione con item di prova...")
        q = f"""{{ boards(ids: [{GESTIONE_BOARD_ID}]) {{ items_page(limit:1) {{ items {{ id name }} }} }} }}"""
        r = _monday_query(q)
        items = r.get("data",{}).get("boards",[{}])[0].get("items_page",{}).get("items",[])
        if not items:
            print("❌ Nessun item trovato nella board GESTIONE PROGETTI NEW")
            sys.exit(1)
        item_id = items[0]["id"]
        print(f"   Item di test: {items[0]['name']} (ID: {item_id})")
        result = archive_item(item_id)
    else:
        item_id = sys.argv[1]
        result = archive_item(item_id)

    print("\n" + "=" * 50)
    if result["success"]:
        print(f"✅ Successo!")
        print(f"   📁 {result['folder_url']}")
        print(f"   📄 {result['files_count']} file caricati")
    else:
        print(f"❌ Errore: {result['error']}")
