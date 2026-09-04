import os
import sys
import json
import requests
from dotenv import load_dotenv

load_dotenv()
MONDAY_TOKEN = os.getenv("MONDAY_API_TOKEN") or "eyJhbGciOiJIUzI1NiJ9.eyJ0aWQiOjQ3MDE1OTc3MiwiYWFpIjoxMSwidWlkIjo3MTUzMzkxNCwiaWFkIjoiMjAyNS0wMi0xMFQxMTo0MDozMS4wMDBaIiwicGVyIjoibWU6d3JpdGUiLCJhY3RpZCI6Mjc3MTMxMDksInJnbiI6ImV1YzEifQ.I4K39oiYz10PfZ_5KSka5FEmEHPoOlma3GqhfPK8PCs"

def get_board_items(board_id):
    items = []
    cursor = None
    while True:
        if cursor:
            query = """query ($cursor: String!) {
              next_items_page(cursor: $cursor, limit: 100) {
                cursor
                items {
                  id
                  name
                  column_values {
                    id
                    text
                  }
                }
              }
            }"""
            r = requests.post("https://api.monday.com/v2", json={"query": query, "variables": {"cursor": cursor}}, headers={"Authorization": MONDAY_TOKEN, "API-Version": "2024-10"}).json()
            data = r.get("data", {}).get("next_items_page", {})
        else:
            query = """query ($board_id: [ID!]) {
              boards(ids: $board_id) {
                items_page(limit: 100) {
                  cursor
                  items {
                    id
                    name
                    column_values {
                      id
                      text
                    }
                  }
                }
              }
            }"""
            r = requests.post("https://api.monday.com/v2", json={"query": query, "variables": {"board_id": [board_id]}}, headers={"Authorization": MONDAY_TOKEN, "API-Version": "2024-10"}).json()
            data = r.get("data", {}).get("boards", [{}])[0].get("items_page", {})
        
        batch = data.get("items", [])
        items.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    return items

real_comm = get_board_items("1865049112")
real_prog = get_board_items("1865197409")

new_comm = get_board_items("2133436509")
new_prog = get_board_items("2136092569")

real_comm_names = {it["name"].strip().lower() for it in real_comm}
real_prog_names = {it["name"].strip().lower() for it in real_prog}
real_comm_ids = {"comm-" + str(it["id"]).lower() for it in real_comm}

# Also recent emails that were legitimate incoming quotes:
recent_legit_emails = {
    "ds mechatronics",
    "volpaghese",
    "extreme srl",
    "bussola & ralph international srl",
    "pardgroup",
    "linealight"
}

print("=== 1. COMMERCIALE REALE (1865049112) ===")
print("Totale elementi reali in COMMERCIALE:", len(real_comm))

print("\n=== 2. GESTIONE PROGETTI REALE (1865197409) ===")
print("Totale elementi reali in GESTIONE PROGETTI:", len(real_prog))

print("\n=== 3. ANALISI NEW COMMERCIALE (2133436509) ===")
print("Totale elementi attuali in NEW COMMERCIALE:", len(new_comm))

fake_new_comm = []
for it in new_comm:
    name_clean = it["name"].strip().lower()
    cols = {cv.get("id"): cv.get("text") for cv in it.get("column_values", []) if cv.get("text")}
    comm = cols.get("text_mm51yvbk", "").strip().lower()
    
    # Check if legitimate
    is_real = (comm in real_comm_ids) or (name_clean in real_comm_names) or (name_clean in recent_legit_emails)
    if not is_real:
        fake_new_comm.append(it)

print(f"-> RILEVATI {len(fake_new_comm)} ELEMENTI TEST/FINTI SU NEW COMMERCIALE:")
for it in fake_new_comm:
    cols = {cv.get("id"): cv.get("text") for cv in it.get("column_values", []) if cv.get("text")}
    print(f"   [ID: {it['id']:12}] {it['name']:35} | Commessa: {cols.get('text_mm51yvbk', '')}")

print("\n=== 4. ANALISI GESTIONE PROGETTI NEW (2136092569) ===")
print("Totale elementi attuali in GESTIONE PROGETTI NEW:", len(new_prog))

fake_new_prog = []
for it in new_prog:
    name_clean = it["name"].strip().lower()
    cols = {cv.get("id"): cv.get("text") for cv in it.get("column_values", []) if cv.get("text")}
    comm = cols.get("text_mm51yk45", "").strip().lower()
    
    is_real = (comm in real_comm_ids) or (name_clean in real_comm_names) or (name_clean in real_prog_names) or (name_clean in recent_legit_emails)
    if not is_real:
        fake_new_prog.append(it)

print(f"-> RILEVATI {len(fake_new_prog)} ELEMENTI TEST/FINTI SU GESTIONE PROGETTI NEW:")
for it in fake_new_prog:
    cols = {cv.get("id"): cv.get("text") for cv in it.get("column_values", []) if cv.get("text")}
    print(f"   [ID: {it['id']:12}] {it['name']:35} | Commessa: {cols.get('text_mm51yk45', '')}")
