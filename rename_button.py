#!/usr/bin/env python3
"""Rinomina la colonna button su Monday.com e verifica le etichette status."""
from dotenv import load_dotenv
load_dotenv()
import requests, json, os

h = {
    'Authorization': os.getenv('MONDAY_API_TOKEN'),
    'Content-Type': 'application/json',
    'API-Version': '2024-10'
}
BOARD_ID = "2136092569"

# 1. Rinomina la colonna button da "Cliccami"/"Click me" a "Archivia Progetto"
print("=== 1. Rinomino colonna button ===")
mutation_rename = """
mutation RenameCol($board_id: ID!, $col_id: String!, $title: String!) {
  change_column_title(board_id: $board_id, column_id: $col_id, title: $title) {
    id
    title
  }
}
"""
r = requests.post('https://api.monday.com/v2', headers=h,
    json={'query': mutation_rename, 'variables': {
        'board_id': BOARD_ID,
        'col_id': 'button_mm45kae3',
        'title': 'Archivia Progetto'
    }}, timeout=15)
resp = r.json()
if 'errors' in resp:
    print(f"❌ Errore rinomina: {resp['errors']}")
else:
    col = resp.get('data', {}).get('change_column_title', {})
    print(f"✅ Colonna rinominata: {col.get('title', 'N/D')} (ID: {col.get('id', 'N/D')})")

# 2. Verifica etichette status colonna Stato
print("\n=== 2. Etichette colonna Stato ===")
r2 = requests.post('https://api.monday.com/v2', headers=h,
    json={'query': '{ boards(ids: [' + BOARD_ID + ']) { columns { id title settings_str } } }'},
    timeout=15)
data = r2.json()
for col in data['data']['boards'][0]['columns']:
    if col['id'] == 'color_mm45raj9':
        settings = json.loads(col.get('settings_str', '{}'))
        labels = settings.get('labels', {})
        print(f"Colonna: {col['title']}")
        for k, v in sorted(labels.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 999):
            marker = " ← ARCHIVIATO" if v.lower() == 'archiviato' else ""
            print(f"  [{k}] '{v}'{marker}")
        found = any(v.lower() == 'archiviato' for v in labels.values())
        print(f"\n{'✅' if found else '⚠️ '} Etichetta Archiviato: {'TROVATA' if found else 'NON TROVATA — va aggiunta manualmente su Monday.com'}")
        break

print("\n=== Done ===")
