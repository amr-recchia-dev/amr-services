#!/usr/bin/env python3
"""
Allinea lo stato reale delle commesse da GESTIONE PROGETTI (1865197409) a GESTIONE PROGETTI NEW (2136092569).
"""

import os, sys, json, time, requests
from dotenv import load_dotenv

load_dotenv()
token = os.getenv("MONDAY_API_TOKEN")

OLD_BOARD_ID = "1865197409"
NEW_BOARD_ID = "2136092569"

headers = {
    "Authorization": token,
    "API-Version": "2024-10",
    "Content-Type": "application/json"
}

STATUS_MAP = {
    "in produzione": "In corso",
    "Fatto": "Fatto",
    "Bloccato": "Bloccato",
    "non in produzione": "Da iniziare",
    "in attesa file": "Da iniziare",
    "conto lavoro": "In corso"
}

def get_items(board_id):
    items = []
    cursor = None
    while True:
        if cursor:
            q = f'query {{ next_items_page(cursor: "{cursor}", limit: 100) {{ cursor items {{ id name column_values {{ id text value type }} }} }} }}'
            data = requests.post("https://api.monday.com/v2", headers=headers, json={"query": q}).json().get("data", {}).get("next_items_page", {})
        else:
            q = f'query {{ boards(ids: ["{board_id}"]) {{ items_page(limit: 100) {{ cursor items {{ id name column_values {{ id text value type }} }} }} }} }}'
            data = requests.post("https://api.monday.com/v2", headers=headers, json={"query": q}).json().get("data", {}).get("boards", [{}])[0].get("items_page", {})
        batch = data.get("items", [])
        items.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    return items

def main():
    print("Fetching items from old GESTIONE PROGETTI...")
    old_items = get_items(OLD_BOARD_ID)
    print(f"Old items: {len(old_items)}")

    print("Fetching items from GESTIONE PROGETTI NEW...")
    new_items = get_items(NEW_BOARD_ID)
    print(f"New items: {len(new_items)}")

    new_by_name = {it["name"].strip().lower(): it for it in new_items}

    updated_count = 0
    for old_it in old_items:
        name = old_it["name"].strip()
        matched = new_by_name.get(name.lower())
        if not matched:
            continue

        new_id = matched["id"]
        old_cols = {cv["id"]: cv for cv in old_it["column_values"] if cv.get("text")}
        new_cols = {cv["id"]: cv for cv in matched["column_values"] if cv.get("text")}

        old_status = old_cols.get("project_status", {}).get("text")
        new_target_status = STATUS_MAP.get(old_status)

        current_new_status = new_cols.get("color_mm45raj9", {}).get("text")

        if new_target_status and new_target_status != current_new_status:
            print(f"Updating '{name}' (#{new_id}): '{current_new_status}' -> '{new_target_status}' (from old: '{old_status}')")
            
            mut = """
            mutation ($board_id: ID!, $item_id: ID!, $col_id: String!, $val: JSON!) {
              change_column_value(board_id: $board_id, item_id: $item_id, column_id: $col_id, value: $val) {
                id
              }
            }
            """
            val_json = json.dumps({"label": new_target_status})
            res = requests.post("https://api.monday.com/v2", headers=headers, json={
                "query": mut,
                "variables": {
                    "board_id": NEW_BOARD_ID,
                    "item_id": str(new_id),
                    "col_id": "color_mm45raj9",
                    "val": val_json
                }
            }).json()
            if res.get("data", {}).get("change_column_value"):
                updated_count += 1
            else:
                print(f"  Error updating: {res}")
            time.sleep(0.3)

    print(f"\n🎉 Successfully updated {updated_count} project statuses on GESTIONE PROGETTI NEW!")

if __name__ == "__main__":
    main()
