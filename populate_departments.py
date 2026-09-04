#!/usr/bin/env python3
"""
Popola le schede di reparto TAGLIO E FRESA (5086546323) e FINITURE (5088215890)
con tutte le commesse assegnate da GESTIONE PROGETTI NEW (2136092569).
"""

import os, sys, json, time, tempfile, subprocess, requests
from dotenv import load_dotenv

load_dotenv()
MONDAY_TOKEN = os.getenv("MONDAY_API_TOKEN")
MONDAY_API_URL = "https://api.monday.com/v2"

PROGETTI_BOARD_ID = "2136092569"
TAGLIO_BOARD_ID = "5086546323"
FINITURE_BOARD_ID = "5088215890"

headers = {
    "Authorization": MONDAY_TOKEN,
    "API-Version": "2024-10",
    "Content-Type": "application/json"
}

PRIORITY_MAP = {
    "normale": "MEDIA",
    "media": "MEDIA",
    "alta": "ALTA",
    "urgente": "MOLTO ALTA",
    "molto alta": "MOLTO ALTA",
    "bassa": "BASSA"
}

def monday_query(query: str, variables: dict = None) -> dict:
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    for attempt in range(5):
        try:
            resp = requests.post(MONDAY_API_URL, headers=headers, json=payload, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                if "errors" in data:
                    print("Monday API Error:", data["errors"])
                return data
            time.sleep((attempt + 1) * 2)
        except Exception as e:
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
    except:
        return False
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

def fetch_items(board_id: str):
    items = []
    cursor = None
    while True:
        if cursor:
            q = f'''query {{
              next_items_page(cursor: "{cursor}", limit: 100) {{
                cursor
                items {{
                  id
                  name
                  group {{ id title }}
                  assets {{ id name public_url }}
                  column_values {{ id text value type }}
                }}
              }}
            }}'''
            data = monday_query(q).get("data", {}).get("next_items_page", {})
        else:
            q = f'''query {{
              boards(ids: ["{board_id}"]) {{
                items_page(limit: 100) {{
                  cursor
                  items {{
                    id
                    name
                    group {{ id title }}
                    assets {{ id name public_url }}
                    column_values {{ id text value type }}
                  }}
                }}
              }}
            }}'''
            data = monday_query(q).get("data", {}).get("boards", [{}])[0].get("items_page", {})
        batch = data.get("items", [])
        items.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    return items

def main():
    print("=== POPOLAMENTO SCHEDE REPARTO DA GESTIONE PROGETTI NEW ===")
    
    progetti = fetch_items(PROGETTI_BOARD_ID)
    print(f"Letti {len(progetti)} progetti da GESTIONE PROGETTI NEW.")

    existing_taglio = fetch_items(TAGLIO_BOARD_ID)
    existing_finiture = fetch_items(FINITURE_BOARD_ID)

    taglio_commesse = set()
    for it in existing_taglio:
        cols = {cv["id"]: cv["text"] for cv in it["column_values"] if cv.get("text")}
        comm = cols.get("text_mkvq6d5t") or it["name"].strip()
        taglio_commesse.add(comm)

    finiture_commesse = set()
    for it in existing_finiture:
        cols = {cv["id"]: cv["text"] for cv in it["column_values"] if cv.get("text")}
        comm = cols.get("text_mkvq6d5t") or it["name"].strip()
        finiture_commesse.add(comm)

    print(f"TAGLIO E FRESA contiene {len(existing_taglio)} elementi.")
    print(f"FINITURE contiene {len(existing_finiture)} elementi.")

    taglio_created = 0
    finiture_created = 0

    for idx, it in enumerate(progetti, 1):
        item_id = str(it["id"])
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

        # ----------------------------------------------------
        # A. ASSEGNAZIONE A TAGLIO E FRESA
        # ----------------------------------------------------
        if (is_taglio or is_fresa) and commessa_code not in taglio_commesse and item_name not in taglio_commesse:
            print(f"[{idx}/{len(progetti)}] ✂️ Creazione su TAGLIO E FRESA: '{item_name}' ({commessa_code})...")
            
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

            mut = """
            mutation ($board_id: ID!, $group_id: String!, $item_name: String!, $column_values: JSON!) {
              create_item (board_id: $board_id, group_id: $group_id, item_name: $item_name, column_values: $column_values) {
                id
                name
              }
            }
            """
            res = monday_query(mut, {
                "board_id": TAGLIO_BOARD_ID,
                "group_id": "duplicate_of_questo_mese_mkmvm6x7",
                "item_name": item_name,
                "column_values": json.dumps(taglio_values)
            })
            new_t_data = res.get("data", {}).get("create_item")
            if new_t_data and new_t_data.get("id"):
                new_t_id = new_t_data["id"]
                taglio_created += 1
                taglio_commesse.add(commessa_code)
                print(f"   ✅ Creato item #{new_t_id} su TAGLIO E FRESA")
                
                # Transfer assets
                for asset in it.get("assets", []):
                    try:
                        p_url = asset.get("public_url")
                        a_name = asset.get("name")
                        if p_url:
                            fb = requests.get(p_url, timeout=30).content
                            col_target = "file_mkwqf7mk" if any(ext in a_name.lower() for ext in [".dwg", ".pdf", ".dxf", ".obj"]) else "file_mkvqs303"
                            if upload_file_curl(int(new_t_id), col_target, a_name, fb):
                                print(f"     📎 Allegato '{a_name}' trasferito!")
                    except Exception as ex:
                        pass
            time.sleep(0.4)

        # ----------------------------------------------------
        # B. ASSEGNAZIONE A FINITURE
        # ----------------------------------------------------
        if is_finitura and commessa_code not in finiture_commesse and item_name not in finiture_commesse:
            print(f"[{idx}/{len(progetti)}] 🎨 Creazione su FINITURE: '{item_name}' ({commessa_code})...")
            
            finiture_values = {
                "text_mkvq39sw": nome_progetto,
                "text_mkvq6d5t": commessa_code,
                "color_mkwqwf0x": {"label": priorita_label},
                "color_mkwq254a": {"label": stato_prod}
            }
            if specifiche:
                finiture_values["text_mkwqxf4n"] = specifiche
                finiture_values["text_mky1yd9m"] = specifiche
            if data_consegna:
                finiture_values["date_mkwq7a73"] = {"date": data_consegna}

            mut = """
            mutation ($board_id: ID!, $group_id: String!, $item_name: String!, $column_values: JSON!) {
              create_item (board_id: $board_id, group_id: $group_id, item_name: $item_name, column_values: $column_values) {
                id
                name
              }
            }
            """
            res = monday_query(mut, {
                "board_id": FINITURE_BOARD_ID,
                "group_id": "duplicate_of_questo_mese_mkmvm6x7",
                "item_name": item_name,
                "column_values": json.dumps(finiture_values)
            })
            new_f_data = res.get("data", {}).get("create_item")
            if new_f_data and new_f_data.get("id"):
                new_f_id = new_f_data["id"]
                finiture_created += 1
                finiture_commesse.add(commessa_code)
                print(f"   ✅ Creato item #{new_f_id} su FINITURE")
                
                # Transfer assets
                for asset in it.get("assets", []):
                    try:
                        p_url = asset.get("public_url")
                        a_name = asset.get("name")
                        if p_url:
                            fb = requests.get(p_url, timeout=30).content
                            col_target = "file_mkwqf7mk" if any(ext in a_name.lower() for ext in [".dwg", ".pdf", ".dxf", ".obj"]) else "file_mkrm23m5"
                            if upload_file_curl(int(new_f_id), col_target, a_name, fb):
                                print(f"     📎 Allegato '{a_name}' trasferito!")
                    except Exception as ex:
                        pass
            time.sleep(0.4)

    print("\n=======================================================")
    print(f"🎉 COMPLETATO!")
    print(f"✂️ Nuovi creati su TAGLIO E FRESA: {taglio_created}")
    print(f"🎨 Nuovi creati su FINITURE: {finiture_created}")
    print("=======================================================")

if __name__ == "__main__":
    main()
