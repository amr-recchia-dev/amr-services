#!/usr/bin/env python3
"""
test_full_archive.py — Testa il flusso completo di archiviazione su Railway.
Simula esattamente il payload che Monday.com invia quando si clicca "Archivia Progetto".
"""
import requests, json

RAILWAY_URL = "https://ravishing-expression-production-8676.up.railway.app"
ITEM_ID = 2658572297  # Fiera Milano S.p.A.

print(f"=== Test archiviazione su Railway ===")
print(f"URL: {RAILWAY_URL}")
print(f"Item: Fiera Milano S.p.A. ({ITEM_ID})")
print()

# 1. Health check
print("1. Health check...")
r = requests.get(f"{RAILWAY_URL}/health", timeout=10)
print(f"   Status: {r.status_code} → {r.json()}")
print()

# 2. Trigger webhook
print("2. Triggering archive webhook...")
payload = {
    "event": {
        "type": "change_column_value",
        "pulseId": ITEM_ID,
        "boardId": 2136092569,
        "columnId": "button_mm45kae3",
        "columnTitle": "Archivia Progetto",
        "value": {"label": {"text": "Archivia Progetto"}},
        "previousValue": {}
    }
}
r2 = requests.post(f"{RAILWAY_URL}/webhook", json=payload, timeout=15)
print(f"   Status: {r2.status_code} → {r2.json()}")
print()
print("✅ Richiesta inviata! L'archiviazione gira in background su Railway.")
print("   Controlla il commento su Monday.com tra 15-30 secondi.")
