#!/usr/bin/env python3
"""
check_and_deploy.py — Verifica etichette status e prepara info per il deploy.
"""
from dotenv import load_dotenv
load_dotenv()
import requests, json, os

h = {
    'Authorization': os.getenv('MONDAY_API_TOKEN'),
    'Content-Type': 'application/json',
    'API-Version': '2024-10'
}

print("=== Etichette colonna Stato (color_mm45raj9) ===")
r = requests.post('https://api.monday.com/v2', headers=h,
    json={'query': '{ boards(ids: [2136092569]) { columns { id title settings_str } } }'},
    timeout=15)
data = r.json()
for col in data['data']['boards'][0]['columns']:
    if col['id'] == 'color_mm45raj9':
        settings = json.loads(col.get('settings_str', '{}'))
        labels = settings.get('labels', {})
        print(f"Colonna: {col['title']}")
        for k, v in sorted(labels.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 999):
            print(f"  [{k}] '{v}'")
        print()
        if not any(v.lower() == 'archiviato' for v in labels.values()):
            print("⚠️  'Archiviato' NON esiste tra le etichette!")
            print("   Etichette esistenti:", list(labels.values()))
        else:
            idx = next(k for k,v in labels.items() if v.lower() == 'archiviato')
            print(f"✅ 'Archiviato' trovato all'indice {idx}")
        break

print("\n=== Verifica webhook attivi ===")
r2 = requests.post('https://api.monday.com/v2', headers=h,
    json={'query': '{ webhooks(board_id: 2136092569) { id event config } }'},
    timeout=15)
webhooks = r2.json().get('data', {}).get('webhooks', [])
for wh in webhooks:
    print(f"  [{wh['id']}] {wh['event']} → {wh.get('config','')[:60]}")

print("\n=== Done ===")
