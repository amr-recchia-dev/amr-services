#!/usr/bin/env python3
"""
AMR Recchia - Full Migration Script: COMMERCIALE (1865049112) -> NEW COMMERCIALE (2133436509)
Transfers all 210 items with complete data mapping, commessa codes, and file attachments.
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
logger = logging.getLogger("migration")

load_dotenv()
MONDAY_TOKEN = os.getenv("MONDAY_API_TOKEN") or "eyJhbGciOiJIUzI1NiJ9.eyJ0aWQiOjQ3MDE1OTc3MiwiYWFpIjoxMSwidWlkIjo3MTUzMzkxNCwiaWFkIjoiMjAyNS0wMi0xMFQxMTo0MDozMS4wMDBaIiwicGVyIjoibWU6d3JpdGUiLCJhY3RpZCI6Mjc3MTMxMDksInJnbiI6ImV1YzEifQ.I4K39oiYz10PfZ_5KSka5FEmEHPoOlma3GqhfPK8PCs"
MONDAY_API_URL = "https://api.monday.com/v2"

SOURCE_BOARD_ID = "1865049112"   # COMMERCIALE
TARGET_BOARD_ID = "2133436509"   # NEW COMMERCIALE

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
            logger.warning("Invalid JSON response on file upload: %s", res.stdout)
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
                  group {{ id title }}
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
                    group {{ id title }}
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
        logger.info("Fetched batch of %d items (total so far: %d)...", len(batch), len(items))
        if not cursor or not batch:
            break
    return items

def main():
    logger.info("=== STARTING AMR RECCHIA COMMERCIALE MIGRATION ===")
    
    # 1. Fetch Target board existing items
    target_items = fetch_all_items_with_assets(TARGET_BOARD_ID)
    commessa_to_target_id = {}
    for it in target_items:
        cols = {cv.get("id"): cv.get("text") for cv in it.get("column_values", []) if cv.get("text")}
        comm = cols.get("text_mm51yvbk")
        if comm:
            commessa_to_target_id[comm.strip()] = int(it["id"])
    
    logger.info("Found %d items in NEW COMMERCIALE (%d already have COMM codes).", len(target_items), len(commessa_to_target_id))
    
    # 2. Fetch Source board items
    source_items = fetch_all_items_with_assets(SOURCE_BOARD_ID)
    logger.info("Found %d items in COMMERCIALE to process.", len(source_items))
    
    # Process items (oldest to newest)
    items_to_migrate = list(reversed(source_items))
    
    migrated_count = 0
    skipped_count = 0
    error_count = 0
    files_transferred_count = 0
    
    for idx, item in enumerate(items_to_migrate, 1):
        old_id = str(item["id"])
        item_name = item["name"].strip()
        commessa_code = f"COMM-{old_id}"
        
        target_item_id = commessa_to_target_id.get(commessa_code)
        
        if not target_item_id:
            logger.info("[%d/%d] 🔄 Creazione item: '%s' (ID: %s | %s)...", idx, len(items_to_migrate), item_name, old_id, commessa_code)
            
            col_dict = {cv.get("id"): cv for cv in item.get("column_values", [])}
            target_column_values = {}
            
            # N° Commessa
            target_column_values["text_mm51yvbk"] = commessa_code
            
            # Nome Progetto (text)
            if "testo_mkn1sqb4" in col_dict and col_dict["testo_mkn1sqb4"].get("text"):
                target_column_values["testo_mkn1sqb4"] = col_dict["testo_mkn1sqb4"]["text"]
                
            # Referente (text)
            if "dup__of_nome_referente_mkn3gf63" in col_dict and col_dict["dup__of_nome_referente_mkn3gf63"].get("text"):
                target_column_values["dup__of_nome_referente_mkn3gf63"] = col_dict["dup__of_nome_referente_mkn3gf63"]["text"]
                
            # Email (email)
            if "email_mkn1v37b" in col_dict and col_dict["email_mkn1v37b"].get("text"):
                em_text = col_dict["email_mkn1v37b"]["text"].strip()
                if em_text and "@" in em_text:
                    target_column_values["email_mkn1v37b"] = {"email": em_text, "text": em_text}
                    
            # Telefono (phone)
            if "telefono_mkn3gd8v" in col_dict and col_dict["telefono_mkn3gd8v"].get("text"):
                phone_text = col_dict["telefono_mkn3gd8v"]["text"].strip()
                if phone_text:
                    target_column_values["telefono_mkn3gd8v"] = {"phone": phone_text, "countryShortName": "IT"}
                    
            # Richiesta / Testo lungo (long_text)
            if "testo_lungo_mkn1ydyz" in col_dict and col_dict["testo_lungo_mkn1ydyz"].get("text"):
                lt_text = col_dict["testo_lungo_mkn1ydyz"]["text"].strip()
                if lt_text:
                    target_column_values["testo_lungo_mkn1ydyz"] = {"text": lt_text}
                    
            # Stato preventivo (status)
            if "color_mkn4s77r" in col_dict and col_dict["color_mkn4s77r"].get("text"):
                st_text = col_dict["color_mkn4s77r"]["text"].strip()
                if st_text:
                    target_column_values["color_mkn4s77r"] = {"label": st_text}
                    
            # Data Preventivo (date)
            if "date_mknkb99e" in col_dict and col_dict["date_mknkb99e"].get("text"):
                d_text = col_dict["date_mknkb99e"]["text"].strip()
                if d_text:
                    target_column_values["date_mknkb99e"] = {"date": d_text}
                    
            # Disegno tecnico (status)
            if "color_mkrkwak4" in col_dict and col_dict["color_mkrkwak4"].get("text"):
                dt_text = col_dict["color_mkrkwak4"]["text"].strip()
                if dt_text:
                    target_column_values["color_mkrkwak4"] = {"label": dt_text}
                    
            # Preventivo Accettato (status)
            if "label_mkn37zp7" in col_dict and col_dict["label_mkn37zp7"].get("text"):
                pa_text = col_dict["label_mkn37zp7"]["text"].strip()
                if pa_text:
                    target_column_values["label_mkn37zp7"] = {"label": pa_text}
                    
            # Budget (numbers)
            if "numeri_mkn1w8z2" in col_dict and col_dict["numeri_mkn1w8z2"].get("text"):
                b_text = col_dict["numeri_mkn1w8z2"]["text"].strip()
                if b_text:
                    try:
                        target_column_values["numeri_mkn1w8z2"] = float(b_text.replace("€", "").replace(".", "").replace(",", ".").strip())
                    except:
                        pass
                        
            # Data consegna (date)
            if "date4" in col_dict and col_dict["date4"].get("text"):
                dc_text = col_dict["date4"]["text"].strip()
                if dc_text:
                    target_column_values["date4"] = {"date": dc_text}
                    
            # Priorità (status)
            if "color_mknssm0t" in col_dict and col_dict["color_mknssm0t"].get("text"):
                pr_text = col_dict["color_mknssm0t"]["text"].strip()
                if pr_text:
                    target_column_values["color_mknssm0t"] = {"label": pr_text}
                    
            # Pagamento (status)
            if "color_mknrnt50" in col_dict and col_dict["color_mknrnt50"].get("text"):
                pg_text = col_dict["color_mknrnt50"]["text"].strip()
                if pg_text:
                    target_column_values["color_mknrnt50"] = {"label": pg_text}
                    
            # DATI FISCALI (text)
            if "text_mkp5k1vg" in col_dict and col_dict["text_mkp5k1vg"].get("text"):
                df_text = col_dict["text_mkp5k1vg"]["text"].strip()
                if df_text:
                    target_column_values["text_mkp5k1vg"] = df_text
                    
            # Fresa (status)
            if "color_mknsezab" in col_dict and col_dict["color_mknsezab"].get("text"):
                fr_text = col_dict["color_mknsezab"]["text"].strip()
                if fr_text:
                    target_column_values["color_mknsezab"] = {"label": fr_text}
                    
            # Taglio (status)
            if "color_mkns43r0" in col_dict and col_dict["color_mkns43r0"].get("text"):
                tg_text = col_dict["color_mkns43r0"]["text"].strip()
                if tg_text:
                    target_column_values["color_mkns43r0"] = {"label": tg_text}
                    
            # Finitura (status)
            if "color_mknsghqz" in col_dict and col_dict["color_mknsghqz"].get("text"):
                fn_text = col_dict["color_mknsghqz"]["text"].strip()
                if fn_text:
                    target_column_values["color_mknsghqz"] = {"label": fn_text}
                    
            # People columns
            if "person" in col_dict and col_dict["person"].get("value"):
                try:
                    p_val = json.loads(col_dict["person"]["value"])
                    if p_val.get("personsAndTeams"):
                        target_column_values["person"] = {"personsAndTeams": p_val["personsAndTeams"]}
                except:
                    pass
                    
            if "multiple_person_mknmf28d" in col_dict and col_dict["multiple_person_mknmf28d"].get("value"):
                try:
                    op_val = json.loads(col_dict["multiple_person_mknmf28d"]["value"])
                    if op_val.get("personsAndTeams"):
                        target_column_values["multiple_person_mknmf28d"] = {"personsAndTeams": op_val["personsAndTeams"]}
                except:
                    pass
                    
            create_query = f'''mutation ($board_id: ID!, $item_name: String!, $column_values: JSON!) {{
              create_item (board_id: $board_id, item_name: $item_name, column_values: $column_values) {{
                id
                name
              }}
            }}'''
            
            vars_payload = {
                "board_id": TARGET_BOARD_ID,
                "item_name": item_name,
                "column_values": json.dumps(target_column_values)
            }
            
            res = monday_query(create_query, vars_payload)
            new_item_data = res.get("data", {}).get("create_item")
            
            if not new_item_data or not new_item_data.get("id"):
                logger.error("❌ Fallita creazione item per %s: %s", item_name, res)
                error_count += 1
                continue
                
            target_item_id = int(new_item_data["id"])
            commessa_to_target_id[commessa_code] = target_item_id
            migrated_count += 1
            logger.info("  ✅ Creato item #%d su NEW COMMERCIALE", target_item_id)
        else:
            logger.info("[%d/%d] ℹ️ Item già esistente #%d (%s | %s). Verifico allegati...", idx, len(items_to_migrate), target_item_id, item_name, commessa_code)
            skipped_count += 1
            
        # 3. Handle File Assets Transfer
        assets = item.get("assets", [])
        if assets:
            logger.info("  📎 Trasferimento %d file allegati per item #%d...", len(assets), target_item_id)
            for asset in assets:
                asset_name = asset.get("name")
                public_url = asset.get("public_url")
                if not public_url or not asset_name:
                    continue
                
                try:
                    f_resp = requests.get(public_url, timeout=45)
                    if f_resp.status_code == 200:
                        col_target = "file_mkn13rg5" if any(ext in asset_name.lower() for ext in [".dwg", ".dxf", ".png", ".jpg", ".jpeg", ".stp", ".step"]) else "file_mknpery4"
                        up_ok = upload_file_curl(target_item_id, col_target, asset_name, f_resp.content)
                        if up_ok:
                            files_transferred_count += 1
                            logger.info("    ✔️ File '%s' trasferito con successo!", asset_name)
                        else:
                            logger.warning("    ⚠️ File '%s' non caricato.", asset_name)
                except Exception as e:
                    logger.error("    ❌ Errore download/upload file %s: %s", asset_name, e)
                    
        time.sleep(0.3)
        
    logger.info("======================================================")
    logger.info("🎉 MIGRAZIONE COMPLETATA!")
    logger.info("📊 Totale Elaborati: %d", len(items_to_migrate))
    logger.info("✅ Nuovi Creati: %d", migrated_count)
    logger.info("⏭️ Già Esistenti Verificati: %d", skipped_count)
    logger.info("📎 File Trasferiti: %d", files_transferred_count)
    logger.info("❌ Errori: %d", error_count)
    logger.info("======================================================")

if __name__ == "__main__":
    main()
