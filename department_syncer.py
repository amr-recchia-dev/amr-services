"""
department_syncer.py - Inoltro e sincronizzazione automatica tra GESTIONE PROGETTI NEW / NEW COMMERCIALE
e le schede operative di reparto:
- TAGLIO E FRESA (5086546323)
- FINITURE (5088215890)
- PROGETTAZIONE NEW (5089194104)

REGOLE DI SICUREZZA ASSOLUTE:
- NON tocca MAI le vecchie board (1865049112 COMMERCIALE, 1865197409 GESTIONE PROGETTI, 1865050352 PRODUZIONE, 1988908927 PROGETTAZIONE).
- Flusso unidirezionale: Nuovo Gestionale / Nuovo Commerciale -> Schede Reparto.
- Bypassa i limiti dei permessi Monday per gli operatori di reparto.
"""

import os
import json
import time
import tempfile
import subprocess
import logging
import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("department_syncer")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [DEPT_SYNC] %(message)s")

MONDAY_TOKEN = os.getenv("MONDAY_API_TOKEN")
MONDAY_API_URL = "https://api.monday.com/v2"

# Board Nuove e Reparti
BOARD_GESTIONE_PROGETTI_NEW = "2136092569"
BOARD_NEW_COMMERCIALE = "2133436509"

BOARD_TAGLIO_FRESA = "5086546323"
BOARD_FINITURE = "5088215890"
BOARD_PROGETTAZIONE_NEW = "5089194104"

# Blacklist tassativa di sicurezza: MAI interagire con questi ID
FORBIDDEN_BOARDS = {"1865049112", "1865197409", "1865050352", "1988908927"}

PRIORITY_MAP = {
    "normale": "MEDIA",
    "media": "MEDIA",
    "alta": "ALTA",
    "urgente": "MOLTO ALTA",
    "molto alta": "MOLTO ALTA",
    "bassa": "BASSA"
}

headers = {
    "Authorization": MONDAY_TOKEN,
    "API-Version": "2024-10",
    "Content-Type": "application/json"
}


def monday_query(query: str, variables: dict = None) -> dict:
    for b in FORBIDDEN_BOARDS:
        if b in query or (variables and any(str(b) in str(v) for v in variables.values())):
            raise ValueError(f"CRITICAL VIOLATION: Tentativo di accesso alla vecchia board proibita {b}")

    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    for attempt in range(4):
        try:
            resp = requests.post(MONDAY_API_URL, headers=headers, json=payload, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                if "errors" in data:
                    logger.warning("Monday API errors: %s", data["errors"])
                return data
            time.sleep((attempt + 1) * 2)
        except Exception as e:
            logger.error("Request exception: %s", e)
            time.sleep(2)
    return {}


def upload_file_curl(item_id: int, column_id: str, file_name: str, file_bytes: bytes) -> bool:
    clean_name = file_name.replace("'", "").replace('"', "").replace(" ", "_")
    with tempfile.NamedTemporaryFile(delete=False, suffix="_" + clean_name) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    try:
        query = f'mutation ($file: File!) {{ add_file_to_column (item_id: {item_id}, column_id: "{column_id}", file: $file) {{ id name }} }}'
        cmd = [
            "curl", "-s", "-X", "POST", "https://api.monday.com/v2/file",
            "-H", f"Authorization: {MONDAY_TOKEN}",
            "-F", f"query={query}",
            "-F", f"variables[file]=@{tmp_path};filename={clean_name}"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        data = json.loads(res.stdout)
        return bool(data.get("data", {}).get("add_file_to_column"))
    except Exception as e:
        logger.error(f"Errore caricamento file {file_name}: {e}")
        return False
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def fetch_department_existing_commesse(board_id: str, commessa_col_id: str) -> dict:
    """Restituisce un dizionario {commessa_code: item_id, item_name_lower: item_id} per evitare duplicati."""
    q = f"""
    query {{
      boards(ids: ["{board_id}"]) {{
        items_page(limit: 500) {{
          items {{
            id
            name
            column_values(ids: ["{commessa_col_id}"]) {{
              text
            }}
          }}
        }}
      }}
    }}
    """
    data = monday_query(q).get("data", {}).get("boards", [{}])[0].get("items_page", {}).get("items", [])
    mapping = {}
    for it in data:
        iid = str(it["id"])
        mapping[it["name"].strip().lower()] = iid
        comm = it.get("column_values", [{}])[0].get("text")
        if comm:
            mapping[comm.strip()] = iid
    return mapping


def sync_project_to_departments(item_id: str) -> dict:
    """
    Legge un progetto (da GESTIONE PROGETTI NEW o NEW COMMERCIALE)
    e lo inoltra/aggiorna sulle schede operative:
    - TAGLIO E FRESA (5086546323) se Taglio o Fresa = SI
    - FINITURE (5088215890) se Finitura = SI
    - PROGETTAZIONE NEW (5089194104) se Disegno tecnico = SI
    """
    logger.info(f"🔄 Verifica assegnazione reparti per item #{item_id}...")
    q = f"""
    query {{
      items(ids: ["{item_id}"]) {{
        id
        name
        board {{ id name }}
        assets {{ id name public_url }}
        column_values {{ id text value type }}
      }}
    }}
    """
    res = monday_query(q)
    items = res.get("data", {}).get("items", [])
    if not items:
        return {"synced": False, "reason": "Item non trovato su Monday"}

    it = items[0]
    board_id = str(it.get("board", {}).get("id", ""))
    if board_id in FORBIDDEN_BOARDS:
        raise ValueError(f"Tentativo di sincronizzare un item dalla vecchia board {board_id}!")

    item_name = it["name"].strip()
    cols = {cv["id"]: cv for cv in it["column_values"] if cv.get("text")}

    # Mappatura campi (supporta sia GESTIONE PROGETTI NEW che NEW COMMERCIALE)
    commessa_code = (
        cols.get("text_mm51yk45", {}).get("text") or  # GESTIONE PROGETTI NEW
        cols.get("text_mm51yvbk", {}).get("text") or  # NEW COMMERCIALE
        f"COMM-{item_id}"
    ).strip()

    nome_progetto = cols.get("testo_mkn1sqb4", {}).get("text", "").strip()
    data_consegna = cols.get("date4", {}).get("text", "").strip()
    specifiche = (cols.get("testo_lungo_mkn1ydyz", {}).get("text") or cols.get("text_mkypgtb8", {}).get("text") or "").strip()
    raw_priorita = cols.get("color_mknssm0t", {}).get("text", "normale").strip().lower()
    priorita_label = PRIORITY_MAP.get(raw_priorita, "MEDIA")
    stato_progetto = cols.get("color_mm45raj9", {}).get("text", "Da iniziare")

    if stato_progetto == "In corso":
        stato_prod = "In produzione"
    elif stato_progetto == "Fatto":
        stato_prod = "Fatto"
    elif stato_progetto == "Bloccato":
        stato_prod = "Bloccato"
    else:
        stato_prod = "Non iniziato"

    # Flag reparti
    is_taglio = cols.get("color_mkns43r0", {}).get("text") == "SI"
    is_fresa = cols.get("color_mknsezab", {}).get("text") == "SI"
    is_finitura = cols.get("color_mknsghqz", {}).get("text") == "SI"
    is_progettazione = cols.get("color_mkrkwak4", {}).get("text") == "SI"

    results = {
        "item_id": item_id,
        "name": item_name,
        "commessa": commessa_code,
        "taglio_fresa": None,
        "finiture": None,
        "progettazione": None
    }

    assets = it.get("assets", [])

    # ─────────────────────────────────────────────────────────────
    # 1. TAGLIO E FRESA (5086546323)
    # ─────────────────────────────────────────────────────────────
    if is_taglio or is_fresa:
        taglio_map = fetch_department_existing_commesse(BOARD_TAGLIO_FRESA, "text_mkvq6d5t")
        existing_t_id = taglio_map.get(commessa_code) or taglio_map.get(item_name.lower())

        taglio_values = {
            "text_mkvq39sw": nome_progetto,
            "text_mkvq6d5t": commessa_code,
            "color_mkwqwf0x": {"label": priorita_label},
            "color_mkwq254a": {"label": stato_prod}
        }
        if specifiche:
            taglio_values["text_mkwqxf4n"] = specifiche
            taglio_values["text_mky1yd9m"] = specifiche
        if is_taglio:
            taglio_values["color_mkwq1c60"] = {"label": "In svolgimento"}
        if is_fresa:
            taglio_values["color_mkwqmw4c"] = {"label": "In svolgimento"}
        if data_consegna:
            taglio_values["date_mkwq7a73"] = {"date": data_consegna}

        if not existing_t_id:
            logger.info(f"✂️ Creazione item su TAGLIO E FRESA: '{item_name}' ({commessa_code})...")
            mut = """
            mutation ($board_id: ID!, $group_id: String!, $item_name: String!, $column_values: JSON!) {
              create_item (board_id: $board_id, group_id: $group_id, item_name: $item_name, column_values: $column_values) {
                id
                name
              }
            }
            """
            res_c = monday_query(mut, {
                "board_id": BOARD_TAGLIO_FRESA,
                "group_id": "duplicate_of_questo_mese_mkmvm6x7",
                "item_name": item_name,
                "column_values": json.dumps(taglio_values)
            })
            new_id = res_c.get("data", {}).get("create_item", {}).get("id")
            if new_id:
                results["taglio_fresa"] = f"Created #{new_id}"
                for asset in assets:
                    p_url = asset.get("public_url")
                    a_name = asset.get("name")
                    if p_url:
                        try:
                            fb = requests.get(p_url, timeout=30).content
                            col_target = "file_mkwqf7mk" if any(ext in a_name.lower() for ext in [".dwg", ".pdf", ".dxf", ".obj", ".stl", ".3dm"]) else "file_mkvqs303"
                            upload_file_curl(int(new_id), col_target, a_name, fb)
                        except Exception as e:
                            logger.error(f"Errore upload file taglio {a_name}: {e}")
        else:
            results["taglio_fresa"] = f"Already exists #{existing_t_id}"

    # ─────────────────────────────────────────────────────────────
    # 2. FINITURE (5088215890)
    # ─────────────────────────────────────────────────────────────
    if is_finitura:
        fin_map = fetch_department_existing_commesse(BOARD_FINITURE, "text_mkvq6d5t")
        existing_f_id = fin_map.get(commessa_code) or fin_map.get(item_name.lower())

        fin_values = {
            "text_mkvq39sw": nome_progetto,
            "text_mkvq6d5t": commessa_code,
            "color_mkwqwf0x": {"label": priorita_label},
            "color_mkwq254a": {"label": stato_prod}
        }
        if specifiche:
            fin_values["text_mkwqxf4n"] = specifiche
            fin_values["text_mky1yd9m"] = specifiche
        if data_consegna:
            fin_values["date_mkwq7a73"] = {"date": data_consegna}

        if not existing_f_id:
            logger.info(f"🎨 Creazione item su FINITURE: '{item_name}' ({commessa_code})...")
            mut = """
            mutation ($board_id: ID!, $group_id: String!, $item_name: String!, $column_values: JSON!) {
              create_item (board_id: $board_id, group_id: $group_id, item_name: $item_name, column_values: $column_values) {
                id
                name
              }
            }
            """
            res_c = monday_query(mut, {
                "board_id": BOARD_FINITURE,
                "group_id": "duplicate_of_questo_mese_mkmvm6x7",
                "item_name": item_name,
                "column_values": json.dumps(fin_values)
            })
            new_id = res_c.get("data", {}).get("create_item", {}).get("id")
            if new_id:
                results["finiture"] = f"Created #{new_id}"
                for asset in assets:
                    p_url = asset.get("public_url")
                    a_name = asset.get("name")
                    if p_url:
                        try:
                            fb = requests.get(p_url, timeout=30).content
                            col_target = "file_mkwqf7mk" if any(ext in a_name.lower() for ext in [".dwg", ".pdf", ".dxf", ".obj", ".stl", ".3dm"]) else "file_mkrm23m5"
                            upload_file_curl(int(new_id), col_target, a_name, fb)
                        except Exception as e:
                            logger.error(f"Errore upload file finiture {a_name}: {e}")
        else:
            results["finiture"] = f"Already exists #{existing_f_id}"

    # ─────────────────────────────────────────────────────────────
    # 3. PROGETTAZIONE NEW (5089194104)
    # ─────────────────────────────────────────────────────────────
    if is_progettazione:
        prog_map = fetch_department_existing_commesse(BOARD_PROGETTAZIONE_NEW, "text_mm7nfz5s")
        existing_p_id = prog_map.get(commessa_code) or prog_map.get(item_name.lower())

        prog_values = {
            "text_mm7nfz5s": commessa_code,
            "text_mm7nknmn": nome_progetto,
            "color_mkrm1ccx": {"label": "Non Disegnato"},
            "color_mkrmv6n9": {"label": "Da Fare"},
            "color_mkrmmwmw": {"label": "Da Fare"}
        }
        if data_consegna:
            prog_values["date_mm7navx4"] = {"date": data_consegna}
        if specifiche:
            prog_values["long_text_mm7ntbap"] = {"text": specifiche}

        if not existing_p_id:
            logger.info(f"📐 Creazione item su PROGETTAZIONE NEW: '{item_name}' ({commessa_code})...")
            mut = """
            mutation ($board_id: ID!, $group_id: String!, $item_name: String!, $column_values: JSON!) {
              create_item (board_id: $board_id, group_id: $group_id, item_name: $item_name, column_values: $column_values) {
                id
                name
              }
            }
            """
            res_c = monday_query(mut, {
                "board_id": BOARD_PROGETTAZIONE_NEW,
                "group_id": "topics",
                "item_name": item_name,
                "column_values": json.dumps(prog_values)
            })
            new_id = res_c.get("data", {}).get("create_item", {}).get("id")
            if new_id:
                results["progettazione"] = f"Created #{new_id}"
                for asset in assets:
                    p_url = asset.get("public_url")
                    a_name = asset.get("name")
                    if p_url:
                        try:
                            fb = requests.get(p_url, timeout=30).content
                            upload_file_curl(int(new_id), "file_mm7nd89s", a_name, fb)
                        except Exception as e:
                            logger.error(f"Errore upload file progettazione {a_name}: {e}")
        else:
            results["progettazione"] = f"Already exists #{existing_p_id}"

    return results


def sync_all_active_departments() -> dict:
    """
    Esegue la scansione di GESTIONE PROGETTI NEW e sincronizza
    tutti i progetti assegnati ai reparti TAGLIO, FINITURE e PROGETTAZIONE NEW.
    """
    logger.info("🚀 Avvio riconciliazione automatica reparti (Gestione Progetti NEW -> Reparti)...")
    q = f"""
    query {{
      boards(ids: ["{BOARD_GESTIONE_PROGETTI_NEW}"]) {{
        items_page(limit: 500) {{
          items {{
            id
            name
            column_values(ids: ["color_mkns43r0", "color_mknsezab", "color_mknsghqz", "color_mkrkwak4"]) {{
              id
              text
            }}
          }}
        }}
      }}
    }}
    """
    items = monday_query(q).get("data", {}).get("boards", [{}])[0].get("items_page", {}).get("items", [])
    logger.info(f"Letti {len(items)} progetti attivi da GESTIONE PROGETTI NEW.")

    summary = {
        "scanned": len(items),
        "taglio_created": 0,
        "finiture_created": 0,
        "progettazione_created": 0,
        "already_synced": 0
    }

    for it in items:
        cols = {cv["id"]: cv.get("text") for cv in it["column_values"]}
        has_dept = (
            cols.get("color_mkns43r0") == "SI" or
            cols.get("color_mknsezab") == "SI" or
            cols.get("color_mknsghqz") == "SI" or
            cols.get("color_mkrkwak4") == "SI"
        )
        if not has_dept:
            continue

        res = sync_project_to_departments(str(it["id"]))
        if "Created" in str(res.get("taglio_fresa")):
            summary["taglio_created"] += 1
        if "Created" in str(res.get("finiture")):
            summary["finiture_created"] += 1
        if "Created" in str(res.get("progettazione")):
            summary["progettazione_created"] += 1

    logger.info(f"🏁 Riconciliazione completata: {summary}")
    return summary


if __name__ == "__main__":
    sync_all_active_departments()
