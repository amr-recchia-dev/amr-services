"""
department_syncer.py - Inoltro e sincronizzazione automatica tra GESTIONE PROGETTI NEW
e le schede operative di reparto: TAGLIO E FRESA (5086546323) e FINITURE (5088215890).
"""

import os, json, time, tempfile, subprocess, logging, requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("department_syncer")

MONDAY_TOKEN = os.getenv("MONDAY_API_TOKEN")
MONDAY_API_URL = "https://api.monday.com/v2"

TAGLIO_BOARD_ID = "5086546323"
FINITURE_BOARD_ID = "5088215890"

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
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    try:
        resp = requests.post(MONDAY_API_URL, headers=headers, json=payload, timeout=25)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.error(f"Errore query Monday: {e}")
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
    except:
        return False
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

def sync_project_to_departments(item_id: str) -> dict:
    """
    Legge un progetto da GESTIONE PROGETTI NEW e lo inoltra/aggiorna
    sulle schede TAGLIO E FRESA e/o FINITURE in base ai reparti assegnati.
    """
    logger.info(f"🔄 Verifica inoltro reparto per progetto #{item_id}...")
    q = f"""
    query {{
      items(ids: ["{item_id}"]) {{
        id
        name
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
    item_name = it["name"].strip()
    cols = {cv["id"]: cv for cv in it["column_values"] if cv.get("text")}

    commessa_code = cols.get("text_mm51yk45", {}).get("text") or f"COMM-{item_id}"
    nome_progetto = cols.get("testo_mkn1sqb4", {}).get("text", "")
    data_consegna = cols.get("date4", {}).get("text", "")
    specifiche = cols.get("testo_lungo_mkn1ydyz", {}).get("text", "")
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

    is_taglio = cols.get("color_mkns43r0", {}).get("text") == "SI"
    is_fresa = cols.get("color_mknsezab", {}).get("text") == "SI"
    is_finitura = cols.get("color_mknsghqz", {}).get("text") == "SI"

    results = {"item_id": item_id, "commessa": commessa_code, "taglio_fresa": None, "finiture": None}

    # 1. GESTIONE TAGLIO E FRESA
    if is_taglio or is_fresa:
        # Controlla se già esiste
        q_check = f"""
        query {{
          boards(ids: ["{TAGLIO_BOARD_ID}"]) {{
            items_page(limit: 100) {{
              items {{ id name column_values(ids: ["text_mkvq6d5t"]) {{ text }} }}
            }}
          }}
        }}
        """
        taglio_items = monday_query(q_check).get("data", {}).get("boards", [{}])[0].get("items_page", {}).get("items", [])
        existing_t = None
        for t_it in taglio_items:
            t_comm = t_it.get("column_values", [{}])[0].get("text")
            if (t_comm and t_comm == commessa_code) or (t_it["name"].strip().lower() == item_name.lower()):
                existing_t = t_it
                break

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

        if not existing_t:
            mut = """
            mutation ($board_id: ID!, $group_id: String!, $item_name: String!, $column_values: JSON!) {
              create_item (board_id: $board_id, group_id: $group_id, item_name: $item_name, column_values: $column_values) {
                id
                name
              }
            }
            """
            res_c = monday_query(mut, {
                "board_id": TAGLIO_BOARD_ID,
                "group_id": "duplicate_of_questo_mese_mkmvm6x7",
                "item_name": item_name,
                "column_values": json.dumps(taglio_values)
            })
            new_id = res_c.get("data", {}).get("create_item", {}).get("id")
            if new_id:
                results["taglio_fresa"] = f"Created #{new_id}"
                # Trasferisci file
                for asset in it.get("assets", []):
                    try:
                        p_url = asset.get("public_url")
                        a_name = asset.get("name")
                        if p_url:
                            fb = requests.get(p_url, timeout=30).content
                            col_target = "file_mkwqf7mk" if any(ext in a_name.lower() for ext in [".dwg", ".pdf", ".dxf", ".obj"]) else "file_mkvqs303"
                            upload_file_curl(int(new_id), col_target, a_name, fb)
                    except:
                        pass
        else:
            results["taglio_fresa"] = f"Already exists #{existing_t['id']}"

    # 2. GESTIONE FINITURE
    if is_finitura:
        q_check = f"""
        query {{
          boards(ids: ["{FINITURE_BOARD_ID}"]) {{
            items_page(limit: 100) {{
              items {{ id name column_values(ids: ["text_mkvq6d5t"]) {{ text }} }}
            }}
          }}
        }}
        """
        fin_items = monday_query(q_check).get("data", {}).get("boards", [{}])[0].get("items_page", {}).get("items", [])
        existing_f = None
        for f_it in fin_items:
            f_comm = f_it.get("column_values", [{}])[0].get("text")
            if (f_comm and f_comm == commessa_code) or (f_it["name"].strip().lower() == item_name.lower()):
                existing_f = f_it
                break

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

        if not existing_f:
            mut = """
            mutation ($board_id: ID!, $group_id: String!, $item_name: String!, $column_values: JSON!) {
              create_item (board_id: $board_id, group_id: $group_id, item_name: $item_name, column_values: $column_values) {
                id
                name
              }
            }
            """
            res_c = monday_query(mut, {
                "board_id": FINITURE_BOARD_ID,
                "group_id": "duplicate_of_questo_mese_mkmvm6x7",
                "item_name": item_name,
                "column_values": json.dumps(fin_values)
            })
            new_id = res_c.get("data", {}).get("create_item", {}).get("id")
            if new_id:
                results["finiture"] = f"Created #{new_id}"
                # Trasferisci file
                for asset in it.get("assets", []):
                    try:
                        p_url = asset.get("public_url")
                        a_name = asset.get("name")
                        if p_url:
                            fb = requests.get(p_url, timeout=30).content
                            col_target = "file_mkwqf7mk" if any(ext in a_name.lower() for ext in [".dwg", ".pdf", ".dxf", ".obj"]) else "file_mkrm23m5"
                            upload_file_curl(int(new_id), col_target, a_name, fb)
                    except:
                        pass
        else:
            results["finiture"] = f"Already exists #{existing_f['id']}"

    return results
