#!/usr/bin/env python3
"""
AMR Recchia - Sync Script: NEW COMMERCIALE / COMMERCIALE -> GESTIONE PROGETTI NEW (2136092569)
Synchronizes all accepted quotes (Preventivo Accettato = SI) and active projects into GESTIONE PROGETTI NEW.
"""

import os
import sys
import time
import json
import logging
import tempfile
import subprocess
import requests
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("sync_progetti")

load_dotenv()
MONDAY_TOKEN = os.getenv("MONDAY_API_TOKEN") or "eyJhbGciOiJIUzI1NiJ9.eyJ0aWQiOjQ3MDE1OTc3MiwiYWFpIjoxMSwidWlkIjo3MTUzMzkxNCwiaWFkIjoiMjAyNS0wMi0xMFQxMTo0MDozMS4wMDBaIiwicGVyIjoibWU6d3JpdGUiLCJhY3RpZCI6Mjc3MTMxMDksInJnbiI6ImV1YzEifQ.I4K39oiYz10PfZ_5KSka5FEmEHPoOlma3GqhfPK8PCs"
MONDAY_API_URL = "https://api.monday.com/v2"

COMMERCIALE_ID = "1865049112"
NEW_COMMERCIALE_ID = "2133436509"
GESTIONE_PROGETTI_OLD_ID = "1865197409"
GESTIONE_PROGETTI_NEW_ID = "2136092569"

def monday_query(query: str, variables: dict = None) -> dict:
    headers = {
        "Authorization": MONDAY_TOKEN,
        "API-Version": "2024-10",
        "Content-Type": "application/json"
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    
    for attempt in range(5):
        try:
            resp = requests.post(MONDAY_API_URL, headers=headers, json=payload, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                if "errors" in data:
                    logger.warning("Monday API returned errors: %s", data["errors"])
                return data
            elif resp.status_code in (429, 500, 502, 503, 504):
                wait_time = (attempt + 1) * 3
                logger.warning("Rate limit / server error %d. Waiting %d s...", resp.status_code, wait_time)
                time.sleep(wait_time)
            else:
                logger.error("HTTP error %d: %s", resp.status_code, resp.text)
                return {}
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
        try:
            data = json.loads(res.stdout)
            if "data" in data and data["data"].get("add_file_to_column"):
                return True
            else:
                logger.warning("File upload response for %s: %s", file_name, res.stdout)
                return False
        except:
            return False
    except Exception as e:
        logger.error("Error uploading file %s: %s", file_name, e)
        return False
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except:
                pass

def fetch_all_items_with_assets(board_id: str):
    items = []
    cursor = None
    logger.info("Fetching all items from board %s...", board_id)
    while True:
        if cursor:
            query = f'''query {{
              next_items_page(cursor: "{cursor}", limit: 100) {{
                cursor
                items {{
                  id
                  name
                  assets {{
                    id
                    name
                    url
                    public_url
                  }}
                  column_values {{
                    id
                    text
                    value
                    type
                  }}
                }}
              }}
            }}'''
            data = monday_query(query).get("data", {}).get("next_items_page", {})
        else:
            query = f'''query {{
              boards(ids: ["{board_id}"]) {{
                items_page(limit: 100) {{
                  cursor
                  items {{
                    id
                    name
                    assets {{
                      id
                      name
                      url
                      public_url
                    }}
                    column_values {{
                      id
                      text
                      value
                      type
                    }}
                  }}
                }}
              }}
            }}'''
            data = monday_query(query).get("data", {}).get("boards", [{}])[0].get("items_page", {})
        
        batch = data.get("items", [])
        items.extend(batch)
        cursor = data.get("cursor")
        logger.info("Fetched batch of %d items (total: %d)...", len(batch), len(items))
        if not cursor or not batch:
            break
    return items

def main():
    logger.info("=== STARTING SYNC TO GESTIONE PROGETTI NEW (2136092569) ===")
    
    # 1. Fetch Target board existing items
    target_progetti = fetch_all_items_with_assets(GESTIONE_PROGETTI_NEW_ID)
    existing_commesse = set()
    existing_names = set()
    for it in target_progetti:
        cols = {cv.get("id"): cv.get("text") for cv in it.get("column_values", []) if cv.get("text")}
        comm = cols.get("text_mm51yk45")
        if comm:
            existing_commesse.add(comm.strip())
        existing_names.add(it["name"].strip().lower())
    
    logger.info("Found %d items in GESTIONE PROGETTI NEW (%d with commessa codes).", len(target_progetti), len(existing_commesse))
    
    # 2. Fetch accepted items from COMMERCIALE
    old_comm_items = fetch_all_items_with_assets(COMMERCIALE_ID)
    accepted_old = [it for it in old_comm_items if any(cv.get("id") == "label_mkn37zp7" and cv.get("text") == "SI" for cv in it.get("column_values", []))]
    logger.info("Found %d accepted quotes in COMMERCIALE (1865049112).", len(accepted_old))
    
    # 3. Fetch accepted items from NEW COMMERCIALE
    new_comm_items = fetch_all_items_with_assets(NEW_COMMERCIALE_ID)
    accepted_new = [it for it in new_comm_items if any(cv.get("id") == "label_mkn37zp7" and cv.get("text") == "SI" for cv in it.get("column_values", []))]
    logger.info("Found %d accepted quotes in NEW COMMERCIALE (2133436509).", len(accepted_new))
    
    # Combine unique items to sync
    all_accepted_map = {}
    for it in accepted_old:
        comm_code = f"COMM-{it['id']}"
        all_accepted_map[comm_code] = it
        
    for it in accepted_new:
        cols = {cv.get("id"): cv.get("text") for cv in it.get("column_values", []) if cv.get("text")}
        comm_code = cols.get("text_mm51yvbk") or f"COMM-{it['id']}"
        if comm_code not in all_accepted_map:
            all_accepted_map[comm_code] = it
            
    items_to_sync = list(all_accepted_map.values())
    logger.info("Total unique accepted commesse to evaluate for GESTIONE PROGETTI NEW: %d", len(items_to_sync))
    
    created_count = 0
    skipped_count = 0
    files_count = 0
    
    for idx, item in enumerate(items_to_sync, 1):
        old_id = str(item["id"])
        item_name = item["name"].strip()
        col_dict = {cv.get("id"): cv for cv in item.get("column_values", [])}
        
        # Check commessa code
        comm_code = col_dict.get("text_mm51yvbk", {}).get("text") or f"COMM-{old_id}"
        
        if comm_code in existing_commesse:
            logger.info("[%d/%d] ⏭️ Progetto %s (%s) già presente in GESTIONE PROGETTI NEW. Salto.", idx, len(items_to_sync), comm_code, item_name)
            skipped_count += 1
            continue
            
        logger.info("[%d/%d] 🔄 Creazione Progetto: '%s' (%s)...", idx, len(items_to_sync), item_name, comm_code)
        
        proj_column_values = {}
        
        # N° Commessa on GESTIONE PROGETTI NEW is 'text_mm51yk45'
        proj_column_values["text_mm51yk45"] = comm_code
        
        # Stato
        proj_column_values["color_mm45raj9"] = {"label": "Da iniziare"}
        
        # Nome Progetto
        if "testo_mkn1sqb4" in col_dict and col_dict["testo_mkn1sqb4"].get("text"):
            proj_column_values["testo_mkn1sqb4"] = col_dict["testo_mkn1sqb4"]["text"]
            
        # Referente
        if "dup__of_nome_referente_mkn3gf63" in col_dict and col_dict["dup__of_nome_referente_mkn3gf63"].get("text"):
            proj_column_values["dup__of_nome_referente_mkn3gf63"] = col_dict["dup__of_nome_referente_mkn3gf63"]["text"]
            
        # Email
        if "email_mkn1v37b" in col_dict and col_dict["email_mkn1v37b"].get("text"):
            em_text = col_dict["email_mkn1v37b"]["text"].strip()
            if em_text and "@" in em_text:
                proj_column_values["email_mkn1v37b"] = {"email": em_text, "text": em_text}
                
        # Telefono
        if "telefono_mkn3gd8v" in col_dict and col_dict["telefono_mkn3gd8v"].get("text"):
            ph_text = col_dict["telefono_mkn3gd8v"]["text"].strip()
            if ph_text:
                proj_column_values["telefono_mkn3gd8v"] = {"phone": ph_text, "countryShortName": "IT"}
                
        # Richiesta / Testo lungo
        if "testo_lungo_mkn1ydyz" in col_dict and col_dict["testo_lungo_mkn1ydyz"].get("text"):
            lt_text = col_dict["testo_lungo_mkn1ydyz"]["text"].strip()
            if lt_text:
                proj_column_values["testo_lungo_mkn1ydyz"] = {"text": lt_text}
                
        # Data consegna
        if "date4" in col_dict and col_dict["date4"].get("text"):
            dc_text = col_dict["date4"]["text"].strip()
            if dc_text:
                proj_column_values["date4"] = {"date": dc_text}
                
        # Priorità
        if "color_mknssm0t" in col_dict and col_dict["color_mknssm0t"].get("text"):
            pr_text = col_dict["color_mknssm0t"]["text"].strip()
            if pr_text:
                proj_column_values["color_mknssm0t"] = {"label": pr_text}
                
        # Disegno tecnico
        if "color_mkrkwak4" in col_dict and col_dict["color_mkrkwak4"].get("text"):
            dt_text = col_dict["color_mkrkwak4"]["text"].strip()
            if dt_text:
                proj_column_values["color_mkrkwak4"] = {"label": dt_text}
                
        # Pagamento
        if "color_mknrnt50" in col_dict and col_dict["color_mknrnt50"].get("text"):
            pg_text = col_dict["color_mknrnt50"]["text"].strip()
            if pg_text:
                proj_column_values["color_mknrnt50"] = {"label": pg_text}
                
        # Fresa
        if "color_mknsezab" in col_dict and col_dict["color_mknsezab"].get("text"):
            fr_text = col_dict["color_mknsezab"]["text"].strip()
            if fr_text:
                proj_column_values["color_mknsezab"] = {"label": fr_text}
                
        # Taglio
        if "color_mkns43r0" in col_dict and col_dict["color_mkns43r0"].get("text"):
            tg_text = col_dict["color_mkns43r0"]["text"].strip()
            if tg_text:
                proj_column_values["color_mkns43r0"] = {"label": tg_text}
                
        # Finitura
        if "color_mknsghqz" in col_dict and col_dict["color_mknsghqz"].get("text"):
            fn_text = col_dict["color_mknsghqz"]["text"].strip()
            if fn_text:
                proj_column_values["color_mknsghqz"] = {"label": fn_text}
                
        # Lavorazione Esterna
        if "color_mkyn8z12" in col_dict and col_dict["color_mkyn8z12"].get("text"):
            le_text = col_dict["color_mkyn8z12"]["text"].strip()
            if le_text:
                proj_column_values["color_mkyn8z12"] = {"label": le_text}
                
        # Fornitori
        if "dropdown_mknxwdw3" in col_dict and col_dict["dropdown_mknxwdw3"].get("value"):
            try:
                drop_val = json.loads(col_dict["dropdown_mknxwdw3"]["value"])
                if drop_val.get("ids"):
                    proj_column_values["dropdown_mknxwdw3"] = {"ids": drop_val["ids"]}
            except:
                pass
                
        # Create Project item on GESTIONE PROGETTI NEW
        create_query = f'''mutation ($board_id: ID!, $item_name: String!, $column_values: JSON!) {{
          create_item (board_id: $board_id, item_name: $item_name, column_values: $column_values) {{
            id
            name
          }}
        }}'''
        
        vars_payload = {
            "board_id": GESTIONE_PROGETTI_NEW_ID,
            "item_name": item_name,
            "column_values": json.dumps(proj_column_values)
        }
        
        res = monday_query(create_query, vars_payload)
        new_item_data = res.get("data", {}).get("create_item")
        
        if not new_item_data or not new_item_data.get("id"):
            logger.error("❌ Fallita creazione progetto per %s: %s", item_name, res)
            continue
            
        new_proj_id = int(new_item_data["id"])
        created_count += 1
        existing_commesse.add(comm_code)
        logger.info("  ✅ Creato Progetto #%d su GESTIONE PROGETTI NEW", new_proj_id)
        
        # Transfer Assets
        assets = item.get("assets", [])
        if assets:
            logger.info("  📎 Trasferimento %d file allegati su Progetto #%d...", len(assets), new_proj_id)
            for asset in assets:
                asset_name = asset.get("name")
                public_url = asset.get("public_url")
                if not public_url or not asset_name:
                    continue
                try:
                    f_resp = requests.get(public_url, timeout=45)
                    if f_resp.status_code == 200:
                        col_target = "file_mkn13rg5" if any(ext in asset_name.lower() for ext in [".dwg", ".dxf", ".png", ".jpg", ".jpeg", ".stp", ".step", ".3ds", ".obj"]) else "file_mknpery4"
                        up_ok = upload_file_curl(new_proj_id, col_target, asset_name, f_resp.content)
                        if up_ok:
                            files_count += 1
                            logger.info("    ✔️ File '%s' trasferito con successo!", asset_name)
                except Exception as e:
                    logger.error("    ❌ Errore trasferimento file %s: %s", asset_name, e)
                    
        time.sleep(0.3)
        
    logger.info("======================================================")
    logger.info("🎉 SINCRONIZZAZIONE PROGETTI COMPLETATA!")
    logger.info("📊 Totale Commesse Accettate: %d", len(items_to_sync))
    logger.info("✅ Nuovi Progetti Creati: %d", created_count)
    logger.info("⏭️ Già Esistenti (Saltati): %d", skipped_count)
    logger.info("📎 File Trasferiti: %d", files_count)
    logger.info("======================================================")

if __name__ == "__main__":
    main()
