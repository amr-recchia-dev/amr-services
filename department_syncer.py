"""
department_syncer.py - Inoltro e sincronizzazione automatica tra GESTIONE PROGETTI NEW / NEW COMMERCIALE
e le schede operative di reparto:
- TAGLIO E FRESA (5086546323)
- FINITURE (5088215890)
- PROGETTAZIONE NEW (5089194104)

Include:
1. Inoltro completo (creazione + aggiornamento continuo di colonne, file e commenti) da GESTIONE PROGETTI NEW ai reparti.
2. Ritorno stato e file esecutivi dai reparti (TAGLIO E FRESA, FINITURE, PROGETTAZIONE NEW) verso GESTIONE PROGETTI NEW.

REGOLE DI SICUREZZA ASSOLUTE:
- NON tocca MAI le vecchie board (1865049112 COMMERCIALE, 1865197409 GESTIONE PROGETTI, 1865050352 PRODUZIONE, 1988908927 PROGETTAZIONE).
- Bypassa i limiti dei permessi Monday per gli operatori di reparto (nessuna colonna Mirror necessaria).
"""

from __future__ import annotations
import os
import json
import time
import threading
import tempfile
import subprocess
import logging
import requests
from dotenv import load_dotenv

_dept_sync_lock = threading.Lock()
_recent_dept_sync_ts: dict[str, float] = {}

load_dotenv()
logger = logging.getLogger("department_syncer")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [DEPT_SYNC] %(message)s")

MONDAY_TOKEN = os.getenv("MONDAY_API_TOKEN")
MONDAY_API_URL = "https://api.monday.com/v2"

# Board Nuove e Reparti
BOARD_GESTIONE_PROGETTI_NEW = "2136092569"
BOARD_NEW_COMMERCIALE = "2133436509"
BOARD_AMMINISTRAZIONE_NEW = "5107455153"

BOARD_TAGLIO_FRESA = "5086546323"
BOARD_FINITURE = "5088215890"
BOARD_PROGETTAZIONE_NEW = "5089194104"

# Blacklist tassativa di sicurezza: MAI interagire con questi ID
FORBIDDEN_BOARDS = {"1865049112", "1865197409", "1865050352", "1988908927", "1865824381"}

PRIORITY_MAP = {
    "normale": "MEDIA",
    "media": "MEDIA",
    "alta": "ALTA",
    "urgente": "MOLTO ALTA",
    "molto alta": "MOLTO ALTA",
    "⚠️️ molto alta": "MOLTO ALTA",
    "⚠️️ critica": "MOLTO ALTA",
    "critica": "MOLTO ALTA",
    "bassa": "BASSA",
}

headers = {
    "Authorization": MONDAY_TOKEN,
    "API-Version": "2024-10",
    "Content-Type": "application/json",
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
            "-F", f"variables[file]=@{tmp_path};filename={clean_name}",
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


def fetch_department_existing_items(board_id: str, commessa_col_id: str) -> dict:
    """Restituisce un dizionario {commessa_code_or_name: item_dict} per evitare duplicati e consentire aggiornamenti."""
    q = f"""
    query {{
      boards(ids: ["{board_id}"]) {{
        items_page(limit: 500) {{
          items {{
            id
            name
            assets {{ id name }}
            updates(limit: 20) {{ id body text_body }}
            column_values {{
              id
              text
              value
            }}
          }}
        }}
      }}
    }}
    """
    data = monday_query(q).get("data", {}).get("boards", [{}])[0].get("items_page", {}).get("items", [])
    mapping = {}
    for it in data:
        mapping[it["name"].strip().lower()] = it
        for cv in it.get("column_values", []):
            if cv["id"] == commessa_col_id and cv.get("text"):
                mapping[cv["text"].strip()] = it
    return mapping


def _sync_missing_assets_and_updates(source_assets: list, source_updates: list, target_item_id: str, existing_target_item: dict | None, file_col_resolver):
    """Carica solo i file e i commenti non ancora presenti sull'item di reparto."""
    existing_asset_names = set()
    existing_update_prefixes = set()
    if existing_target_item:
        for a in existing_target_item.get("assets", []):
            if a.get("name"):
                existing_asset_names.add(a["name"].strip().lower())
                existing_asset_names.add(a["name"].replace(" ", "_").strip().lower())
        for u in existing_target_item.get("updates", []):
            tb = (u.get("text_body") or "").strip()
            if tb:
                existing_update_prefixes.add(tb[:120])

    for asset in source_assets:
        a_name = asset.get("name") or ""
        if not a_name:
            continue
        clean_cmp = a_name.replace(" ", "_").strip().lower()
        if a_name.strip().lower() in existing_asset_names or clean_cmp in existing_asset_names:
            continue
        p_url = asset.get("public_url") or asset.get("url")
        if p_url:
            try:
                fb = requests.get(p_url, timeout=30).content
                col_target = file_col_resolver(a_name)
                if upload_file_curl(int(target_item_id), col_target, a_name, fb):
                    existing_asset_names.add(a_name.strip().lower())
                    existing_asset_names.add(clean_cmp)
            except Exception as e:
                logger.error("Errore upload file %s su #%s: %s", a_name, target_item_id, e)

    for upd in reversed(source_updates or []):
        tb = (upd.get("text_body") or "").strip()
        hb = (upd.get("body") or "").strip()
        if not tb or tb.startswith("🏭 [") or tb.startswith("📐 [") or tb.startswith("🎨 [") or tb.startswith("✂️ ["):
            continue
        if tb[:120] in existing_update_prefixes:
            continue
        monday_query(
            "mutation ($item_id: ID!, $body: String!) { create_update(item_id: $item_id, body: $body) { id } }",
            {"item_id": str(target_item_id), "body": hb or tb},
        )
        existing_update_prefixes.add(tb[:120])


def sync_project_to_departments(item_id: str, force: bool = False) -> dict:
    """
    Legge un progetto (da GESTIONE PROGETTI NEW o NEW COMMERCIALE)
    e lo crea o aggiorna sulle schede operative:
    - TAGLIO E FRESA (5086546323) se Taglio o Fresa = SI
    - FINITURE (5088215890) se Finitura = SI
    - PROGETTAZIONE NEW (5089194104) se Disegno tecnico = SI
    - AMMINISTRAZIONE NEW (5107455153)
    Protetto da lock e debounce contro raffiche di webhook simultanei.
    """
    with _dept_sync_lock:
        now = time.time()
        last_ts = _recent_dept_sync_ts.get(str(item_id), 0.0)
        if not force and (now - last_ts) < 4.0:
            logger.info("⏭️ [DEBOUNCE] Sync reparti per item #%s già eseguito %.1fs fa, ignoro evento duplicato.", item_id, now - last_ts)
            return {"synced": True, "debounced": True, "item_id": str(item_id)}
        _recent_dept_sync_ts[str(item_id)] = now
        return _sync_project_to_departments_locked(item_id)


def _sync_project_to_departments_locked(item_id: str) -> dict:
    logger.info(f"🔄 Verifica assegnazione reparti per item #{item_id}...")
    q = f"""
    query {{
      items(ids: ["{item_id}"]) {{
        id
        name
        board {{ id name }}
        assets {{ id name url public_url }}
        updates(limit: 20) {{ id body text_body }}
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
    cols = {cv["id"]: cv for cv in it["column_values"] if cv.get("text") or cv.get("value")}

    # Mappatura campi (supporta sia GESTIONE PROGETTI NEW che NEW COMMERCIALE)
    commessa_code = (
        cols.get("text_mm51yk45", {}).get("text")
        or cols.get("text_mm51yvbk", {}).get("text")
        or f"COMM-{item_id}"
    ).strip()

    nome_progetto = (cols.get("testo_mkn1sqb4", {}).get("text") or "").strip() or item_name
    data_consegna = (cols.get("date4", {}).get("text") or "").strip()
    specifiche = (
        cols.get("testo_lungo_mkn1ydyz", {}).get("text")
        or cols.get("text_mkypgtb8", {}).get("text")
        or ""
    ).strip()
    raw_priorita = (cols.get("color_mknssm0t", {}).get("text") or "normale").strip().lower()
    priorita_label = PRIORITY_MAP.get(raw_priorita, "MEDIA")
    stato_progetto = (cols.get("color_mm45raj9", {}).get("text") or "Da iniziare").strip()
    durata_stimata = (cols.get("numeric_mkvka3b0", {}).get("text") or "").strip()
    timeline_raw = cols.get("timerange_mkvkv498", {}).get("value")

    if stato_progetto == "In corso":
        stato_prod = "In produzione"
    elif stato_progetto == "Fatto":
        stato_prod = "Fatto"
    elif stato_progetto == "Bloccato":
        stato_prod = "Bloccato"
    else:
        stato_prod = "Non iniziato"

    # Flag reparti
    is_taglio = (cols.get("color_mkns43r0", {}).get("text") or "").strip() == "SI"
    is_fresa = (cols.get("color_mknsezab", {}).get("text") or "").strip() == "SI"
    is_finitura = (cols.get("color_mknsghqz", {}).get("text") or "").strip() == "SI"
    is_progettazione = (cols.get("color_mkrkwak4", {}).get("text") or "").strip() == "SI"

    results = {
        "item_id": item_id,
        "name": item_name,
        "commessa": commessa_code,
        "taglio_fresa": None,
        "finiture": None,
        "progettazione": None,
    }

    assets = it.get("assets", [])
    updates = it.get("updates", [])

    # ─────────────────────────────────────────────────────────────
    # 1. TAGLIO E FRESA (5086546323)
    # ─────────────────────────────────────────────────────────────
    if is_taglio or is_fresa:
        taglio_map = fetch_department_existing_items(BOARD_TAGLIO_FRESA, "text_mkvq6d5t")
        existing_t = taglio_map.get(commessa_code) or taglio_map.get(item_name.lower())

        taglio_values = {
            "text_mkvq39sw": nome_progetto,
            "text_mkvq6d5t": commessa_code,
            "color_mkwqwf0x": {"label": priorita_label},
        }
        if specifiche:
            taglio_values["text_mkwqxf4n"] = specifiche
            taglio_values["text_mky1yd9m"] = specifiche
        if data_consegna:
            taglio_values["date_mkwq7a73"] = {"date": data_consegna}
        if durata_stimata:
            taglio_values["numeric_mky139bd"] = durata_stimata
        if timeline_raw:
            try:
                tl = json.loads(timeline_raw)
                if tl.get("from") and tl.get("to"):
                    taglio_values["timerange_mkwqbb63"] = {"from": tl["from"], "to": tl["to"]}
            except Exception:
                pass

        if not existing_t:
            taglio_values["color_mkwq254a"] = {"label": stato_prod}
            if is_taglio:
                taglio_values["color_mkwq1c60"] = {"label": "In svolgimento"}
            if is_fresa:
                taglio_values["color_mkwqmw4c"] = {"label": "In svolgimento"}

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
                "column_values": json.dumps(taglio_values),
            })
            target_t_id = res_c.get("data", {}).get("create_item", {}).get("id")
            if target_t_id:
                results["taglio_fresa"] = f"Created #{target_t_id}"
        else:
            target_t_id = str(existing_t["id"])
            existing_t_cols = {c["id"]: (c.get("text") or "").strip() for c in existing_t.get("column_values", [])}
            if is_taglio and not existing_t_cols.get("color_mkwq1c60"):
                taglio_values["color_mkwq1c60"] = {"label": "In svolgimento"}
            if is_fresa and not existing_t_cols.get("color_mkwqmw4c"):
                taglio_values["color_mkwqmw4c"] = {"label": "In svolgimento"}

            monday_query(
                f"mutation ($val: JSON!) {{ change_multiple_column_values(board_id: {BOARD_TAGLIO_FRESA}, item_id: {target_t_id}, column_values: $val) {{ id }} }}",
                {"val": json.dumps(taglio_values)},
            )
            results["taglio_fresa"] = f"Updated #{target_t_id}"

        if target_t_id:
            _sync_missing_assets_and_updates(
                assets,
                updates,
                str(target_t_id),
                existing_t,
                lambda fname: "file_mkwqf7mk" if any(ext in fname.lower() for ext in [".dwg", ".pdf", ".dxf", ".obj", ".stl", ".3dm", ".stp", ".step"]) else "file_mkvqs303",
            )

    # ─────────────────────────────────────────────────────────────
    # 2. FINITURE (5088215890)
    # ─────────────────────────────────────────────────────────────
    if is_finitura:
        fin_map = fetch_department_existing_items(BOARD_FINITURE, "text_mkvq6d5t")
        existing_f = fin_map.get(commessa_code) or fin_map.get(item_name.lower())

        fin_values = {
            "text_mkvq39sw": nome_progetto,
            "text_mkvq6d5t": commessa_code,
            "color_mkwqwf0x": {"label": priorita_label},
        }
        if specifiche:
            fin_values["text_mkwqxf4n"] = specifiche
            fin_values["text_mky1yd9m"] = specifiche
        if data_consegna:
            fin_values["date_mkwq7a73"] = {"date": data_consegna}
        if durata_stimata:
            fin_values["numeric_mky139bd"] = durata_stimata
        if timeline_raw:
            try:
                tl = json.loads(timeline_raw)
                if tl.get("from") and tl.get("to"):
                    fin_values["timerange_mkwqbb63"] = {"from": tl["from"], "to": tl["to"]}
            except Exception:
                pass

        if not existing_f:
            fin_values["color_mkwq254a"] = {"label": stato_prod}
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
                "column_values": json.dumps(fin_values),
            })
            target_f_id = res_c.get("data", {}).get("create_item", {}).get("id")
            if target_f_id:
                results["finiture"] = f"Created #{target_f_id}"
        else:
            target_f_id = str(existing_f["id"])
            monday_query(
                f"mutation ($val: JSON!) {{ change_multiple_column_values(board_id: {BOARD_FINITURE}, item_id: {target_f_id}, column_values: $val) {{ id }} }}",
                {"val": json.dumps(fin_values)},
            )
            results["finiture"] = f"Updated #{target_f_id}"

        if target_f_id:
            _sync_missing_assets_and_updates(
                assets,
                updates,
                str(target_f_id),
                existing_f,
                lambda fname: "file_mkwqf7mk" if any(ext in fname.lower() for ext in [".dwg", ".pdf", ".dxf", ".obj", ".stl", ".3dm", ".stp", ".step"]) else "file_mkrm23m5",
            )

    # ─────────────────────────────────────────────────────────────
    # 3. PROGETTAZIONE NEW (5089194104)
    # ─────────────────────────────────────────────────────────────
    if is_progettazione:
        prog_map = fetch_department_existing_items(BOARD_PROGETTAZIONE_NEW, "text_mm7nfz5s")
        existing_p = prog_map.get(commessa_code) or prog_map.get(item_name.lower())

        prog_values = {
            "text_mm7nfz5s": commessa_code,
            "text_mm7nknmn": nome_progetto,
        }
        if data_consegna:
            prog_values["date_mm7navx4"] = {"date": data_consegna}
        if specifiche:
            prog_values["long_text_mm7ntbap"] = {"text": specifiche}

        if not existing_p:
            prog_values["color_mkrm1ccx"] = {"label": "Non Disegnato"}
            prog_values["color_mkrmv6n9"] = {"label": "Da Fare"}
            prog_values["color_mkrmmwmw"] = {"label": "Da Fare"}
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
                "column_values": json.dumps(prog_values),
            })
            target_p_id = res_c.get("data", {}).get("create_item", {}).get("id")
            if target_p_id:
                results["progettazione"] = f"Created #{target_p_id}"
        else:
            target_p_id = str(existing_p["id"])
            monday_query(
                f"mutation ($val: JSON!) {{ change_multiple_column_values(board_id: {BOARD_PROGETTAZIONE_NEW}, item_id: {target_p_id}, column_values: $val) {{ id }} }}",
                {"val": json.dumps(prog_values)},
            )
            results["progettazione"] = f"Updated #{target_p_id}"

        if target_p_id:
            _sync_missing_assets_and_updates(
                assets,
                updates,
                str(target_p_id),
                existing_p,
                lambda _fname: "file_mm7nd89s",
            )

    # ─────────────────────────────────────────────────────────────
    # 4. AMMINISTRAZIONE NEW (5107455153)
    # ─────────────────────────────────────────────────────────────
    try:
        adm_map = fetch_department_existing_items(BOARD_AMMINISTRAZIONE_NEW, "text_mm8y3t4q")
        existing_adm = adm_map.get(commessa_code) or adm_map.get(item_name.lower())
        adm_stato_prod = "Fatto" if stato_progetto == "Fatto" else ("Bloccato" if stato_progetto == "Bloccato" else "In produzione")
        adm_values = {
            "text_mm8y3t4q": commessa_code,
            "text_mm8y9vxm": nome_progetto,
            "color_mm8ynr3s": {"label": adm_stato_prod},
            "text_mm8yeabv": priorita_label,
        }
        if data_consegna:
            adm_values["date_mm8ymb9n"] = {"date": data_consegna}
        referente_gp = (cols.get("dup__of_nome_referente_mkn3gf63", {}).get("text") or "").strip()
        if referente_gp:
            adm_values["text_mm8ynnhj"] = referente_gp
        email_gp = (cols.get("email_mkn1v37b", {}).get("text") or "").strip()
        if email_gp:
            adm_values["email_mm8yvmcy"] = {"email": email_gp, "text": email_gp}
        pag_gp = (cols.get("color_mknrnt50", {}).get("text") or "").strip()
        if pag_gp:
            adm_values["text_mm8y8eqg"] = pag_gp

        if existing_adm:
            target_adm_id = str(existing_adm["id"])
            monday_query(
                f"mutation ($val: JSON!) {{ change_multiple_column_values(board_id: {BOARD_AMMINISTRAZIONE_NEW}, item_id: {target_adm_id}, column_values: $val) {{ id }} }}",
                {"val": json.dumps(adm_values)},
            )
            results["amministrazione_new"] = f"Updated #{target_adm_id}"
        else:
            adm_values["color_mm8yt562"] = {"label": "DA FARE"}
            adm_values["color_mm8yk54d"] = {"label": "DA FARE"}
            adm_values["color_mm8yhrqg"] = {"label": "DA FARE"}
            adm_values["color_mm8yr340"] = {"label": "Non Consegnato"}
            adm_values["color_mm8y87rn"] = {"label": "DA FARE"}
            res_adm = monday_query(
                "mutation ($board_id: ID!, $item_name: String!, $column_values: JSON!) { create_item(board_id: $board_id, item_name: $item_name, column_values: $column_values) { id } }",
                {
                    "board_id": BOARD_AMMINISTRAZIONE_NEW,
                    "item_name": item_name,
                    "column_values": json.dumps(adm_values),
                },
            )
            target_adm_id = res_adm.get("data", {}).get("create_item", {}).get("id")
            if target_adm_id:
                results["amministrazione_new"] = f"Created #{target_adm_id}"
    except Exception as adm_err:
        logger.warning("Sincronizzazione AMMINISTRAZIONE NEW: %s", adm_err)

    return results


def sync_department_progress_to_gp_new(board_id: str, dept_item_id: str, column_id: str, new_label: str) -> dict:
    """
    Quando un operatore su TAGLIO E FRESA (5086546323), FINITURE (5088215890) o PROGETTAZIONE NEW (5089194104)
    aggiorna uno stato o carica un file CAD esecutivo, riporta l'avanzamento sulla commessa corrispondente
    in GESTIONE PROGETTI NEW (2136092569) e AMMINISTRAZIONE NEW (5107455153).
    """
    board_id = str(board_id)
    if board_id not in (BOARD_TAGLIO_FRESA, BOARD_FINITURE, BOARD_PROGETTAZIONE_NEW):
        return {"ok": False, "reason": "Board non di reparto"}

    q = f"""
    query {{
      items(ids: ["{dept_item_id}"]) {{
        id
        name
        assets {{ id name url public_url }}
        column_values {{ id text value column {{ title }} }}
      }}
    }}
    """
    items = monday_query(q).get("data", {}).get("items", [])
    if not items:
        return {"ok": False, "reason": "Item reparto non trovato"}

    dept_item = items[0]
    cols = {c["id"]: c for c in dept_item.get("column_values", [])}
    comm_col_id = "text_mm7nfz5s" if board_id == BOARD_PROGETTAZIONE_NEW else "text_mkvq6d5t"
    commessa_code = (cols.get(comm_col_id, {}).get("text") or "").strip()
    item_name = dept_item["name"].strip()

    # Cerca la commessa corrispondente in GESTIONE PROGETTI NEW (2136092569)
    gp_map = fetch_department_existing_items(BOARD_GESTIONE_PROGETTI_NEW, "text_mm51yk45")
    gp_item = gp_map.get(commessa_code) or gp_map.get(item_name.lower())
    if not gp_item:
        return {"ok": False, "reason": f"Commessa '{commessa_code or item_name}' non trovata in GESTIONE PROGETTI NEW"}

    gp_id = str(gp_item["id"])
    col_title = (cols.get(column_id, {}).get("column") or {}).get("title") or column_id
    val_text = new_label or (cols.get(column_id, {}).get("text") or "").strip()

    dept_badge = {
        BOARD_TAGLIO_FRESA: "✂️ [TAGLIO E FRESA]",
        BOARD_FINITURE: "🎨 [FINITURE]",
        BOARD_PROGETTAZIONE_NEW: "📐 [PROGETTAZIONE NEW]",
    }[board_id]

    # 1. Registra nota di avanzamento su GESTIONE PROGETTI NEW se c'è un cambio stato significativo
    if val_text:
        msg = f"{dept_badge} Aggiornamento su <b>{item_name}</b> ({commessa_code}): <b>{col_title}</b> ➔ <b>{val_text}</b>"
        monday_query(
            "mutation ($item_id: ID!, $body: String!) { create_update(item_id: $item_id, body: $body) { id } }",
            {"item_id": gp_id, "body": msg},
        )

    # 2. Se lo Stato di Produzione generale del reparto cambia, aggiorna lo Stato in GESTIONE PROGETTI NEW e AMMINISTRAZIONE NEW
    adm_updates = {}
    if column_id == "color_mkwq254a" and val_text:
        gp_status = None
        if val_text.lower() in ("in produzione", "in svolgimento"):
            gp_status = "In corso"
        elif val_text.lower() == "bloccato":
            gp_status = "Bloccato"
        elif val_text.lower() == "fatto":
            gp_status = "In corso"  # passa a Fatto se tutti i reparti attivi hanno Fatto
            gp_cols = {c["id"]: (c.get("text") or "").strip() for c in gp_item.get("column_values", [])}
            need_t = gp_cols.get("color_mkns43r0") == "SI" or gp_cols.get("color_mknsezab") == "SI"
            need_f = gp_cols.get("color_mknsghqz") == "SI"
            t_done = True
            f_done = True
            if need_t and board_id != BOARD_TAGLIO_FRESA:
                t_it = fetch_department_existing_items(BOARD_TAGLIO_FRESA, "text_mkvq6d5t").get(commessa_code)
                if t_it:
                    t_c = {c["id"]: (c.get("text") or "").strip() for c in t_it.get("column_values", [])}
                    t_done = t_c.get("color_mkwq254a") == "Fatto"
            if need_f and board_id != BOARD_FINITURE:
                f_it = fetch_department_existing_items(BOARD_FINITURE, "text_mkvq6d5t").get(commessa_code)
                if f_it:
                    f_c = {c["id"]: (c.get("text") or "").strip() for c in f_it.get("column_values", [])}
                    f_done = f_c.get("color_mkwq254a") == "Fatto"
            if t_done and f_done:
                gp_status = "Fatto"

        if gp_status:
            monday_query(
                f"mutation ($val: JSON!) {{ change_multiple_column_values(board_id: {BOARD_GESTIONE_PROGETTI_NEW}, item_id: {gp_id}, column_values: $val) {{ id }} }}",
                {"val": json.dumps({"color_mm45raj9": {"label": gp_status}})},
            )
            adm_stato = "Fatto" if gp_status == "Fatto" else ("Bloccato" if gp_status == "Bloccato" else "In produzione")
            adm_updates["color_mm8ynr3s"] = {"label": adm_stato}

    if column_id == "color_mkwqfakf" and val_text:
        adm_updates["color_mm8yhrqg"] = {"label": "APPROVATO" if "fatto" in val_text.lower() or "approv" in val_text.lower() else "DA FARE"}
    if column_id == "color_mkwqfr4j" and val_text:
        adm_updates["color_mm8yr340"] = {"label": "Consegnato" if "consegnato" in val_text.lower() and "non" not in val_text.lower() else "Non Consegnato"}

    if adm_updates:
        try:
            adm_map = fetch_department_existing_items(BOARD_AMMINISTRAZIONE_NEW, "text_mm8y3t4q")
            adm_it = adm_map.get(commessa_code) or adm_map.get(item_name.lower())
            if adm_it:
                monday_query(
                    f"mutation ($val: JSON!) {{ change_multiple_column_values(board_id: {BOARD_AMMINISTRAZIONE_NEW}, item_id: {adm_it['id']}, column_values: $val) {{ id }} }}",
                    {"val": json.dumps(adm_updates)},
                )
        except Exception as adm_e:
            logger.debug("Aggiornamento ritorno verso AMMINISTRAZIONE NEW: %s", adm_e)

    # 3. Se in PROGETTAZIONE NEW vengono caricati file CAD (Bozza o Esecutivo), propagali a GESTIONE PROGETTI NEW e ai reparti
    if board_id == BOARD_PROGETTAZIONE_NEW and dept_item.get("assets"):
        _sync_missing_assets_and_updates(
            dept_item.get("assets", []),
            [],
            gp_id,
            gp_item,
            lambda _fname: "file_mkn13rg5",
        )
        # Propaga anche ai reparti TAGLIO E FRESA / FINITURE se già creati
        for r_board in (BOARD_TAGLIO_FRESA, BOARD_FINITURE):
            r_it = fetch_department_existing_items(r_board, "text_mkvq6d5t").get(commessa_code)
            if r_it:
                _sync_missing_assets_and_updates(
                    dept_item.get("assets", []),
                    [],
                    str(r_it["id"]),
                    r_it,
                    lambda _fname: "file_mkwqmkbq",  # CAD Esecutivo
                )

    return {"ok": True, "gp_item_id": gp_id, "commessa": commessa_code, "updated_col": col_title, "value": val_text}


def sync_all_active_departments() -> dict:
    """
    1. Riconcilia prima le commesse accettate da NEW COMMERCIALE (2133436509) -> GESTIONE PROGETTI NEW (2136092569) & AMMINISTRAZIONE NEW (5107455153).
    2. Esegue la scansione di GESTIONE PROGETTI NEW e sincronizza tutti i progetti assegnati
       ai reparti TAGLIO E FRESA, FINITURE e PROGETTAZIONE NEW.
    """
    logger.info("🚀 Avvio riconciliazione automatica (NEW COMMERCIALE -> GESTIONE PROGETTI NEW -> Reparti)...")
    comm_reconcile = {}
    try:
        import onboard_project
        comm_reconcile = onboard_project.sync_all_accepted_commercial_to_gp_new()
    except Exception as e:
        logger.warning("Riconciliazione NEW COMMERCIALE -> GESTIONE PROGETTI NEW: %s", e)

    q = f"""
    query {{
      boards(ids: ["{BOARD_GESTIONE_PROGETTI_NEW}"]) {{
        items_page(limit: 500) {{
          items {{
            id
            name
            column_values(ids: ["text_mm51yk45", "color_mkns43r0", "color_mknsezab", "color_mknsghqz", "color_mkrkwak4"]) {{
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

    # Carica le mappe dei reparti UNA SOLA VOLTA per risparmiare il 98% delle chiamate API Monday
    taglio_map = fetch_department_existing_items(BOARD_TAGLIO_FRESA, "text_mkvq6d5t")
    fin_map = fetch_department_existing_items(BOARD_FINITURE, "text_mkvq6d5t")
    prog_map = fetch_department_existing_items(BOARD_PROGETTAZIONE_NEW, "text_mm7nfz5s")

    summary = {
        "commercial_reconciled": comm_reconcile,
        "scanned": len(items),
        "taglio_created": 0,
        "taglio_updated": 0,
        "finiture_created": 0,
        "finiture_updated": 0,
        "progettazione_created": 0,
        "progettazione_updated": 0,
    }

    for it in items:
        cols = {cv["id"]: (cv.get("text") or "").strip() for cv in it["column_values"]}
        is_t = cols.get("color_mkns43r0") == "SI"
        is_fr = cols.get("color_mknsezab") == "SI"
        is_fi = cols.get("color_mknsghqz") == "SI"
        is_pr = cols.get("color_mkrkwak4") == "SI"

        if not (is_t or is_fr or is_fi or is_pr):
            continue

        comm_code = cols.get("text_mm51yk45") or f"GP-{it['id']}"
        name_low = it["name"].strip().lower()

        missing_in_dept = False
        if (is_t or is_fr) and not (taglio_map.get(comm_code) or taglio_map.get(name_low)):
            missing_in_dept = True
        if is_fi and not (fin_map.get(comm_code) or fin_map.get(name_low)):
            missing_in_dept = True
        if is_pr and not (prog_map.get(comm_code) or prog_map.get(name_low)):
            missing_in_dept = True

        # Sincronizza nel loop di riconciliazione periodica SOLO se manca in almeno un reparto assegnato
        # (gli aggiornamenti in tempo reale avvengono già istantaneamente tramite webhook Monday)
        if not missing_in_dept:
            continue

        res = sync_project_to_departments(str(it["id"]))
        if "Created" in str(res.get("taglio_fresa")):
            summary["taglio_created"] += 1
        elif "Updated" in str(res.get("taglio_fresa")):
            summary["taglio_updated"] += 1

        if "Created" in str(res.get("finiture")):
            summary["finiture_created"] += 1
        elif "Updated" in str(res.get("finiture")):
            summary["finiture_updated"] += 1

        if "Created" in str(res.get("progettazione")):
            summary["progettazione_created"] += 1
        elif "Updated" in str(res.get("progettazione")):
            summary["progettazione_updated"] += 1

    logger.info(f"🏁 Riconciliazione completata: {summary}")
    return summary


if __name__ == "__main__":
    print(json.dumps(sync_all_active_departments(), indent=2, ensure_ascii=False))
