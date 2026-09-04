#!/usr/bin/env python3
"""
onboard_project.py — Copia un progetto approvato da NEW COMMERCIALE a GESTIONE PROGETTI NEW
inclusi tutti i file allegati (campionature, disegni DWG, PDF) senza esporre i permessi.
"""

import os
import json
import requests
import tempfile
import mimetypes
from pathlib import Path
from dotenv import load_dotenv

# Carica .env usando un percorso assoluto rispetto al file
script_dir = Path(__file__).resolve().parent
load_dotenv(dotenv_path=script_dir / ".env")

MONDAY_TOKEN = os.getenv("MONDAY_API_TOKEN", "").strip()
if not MONDAY_TOKEN:
    print("⚠️ WARNING: MONDAY_API_TOKEN è vuoto! Verifica il file .env.", flush=True)
MONDAY_API = "https://api.monday.com/v2"
MONDAY_HEADERS = {
    "Authorization": MONDAY_TOKEN,
    "Content-Type": "application/json",
    "API-Version": "2024-10"
}

BOARD_COMMERCIALE = "2133436509"
BOARD_PROGETTI = "2136092569"

def _query(query: str, variables: dict = None) -> dict:
    print(f"GraphQL Query su {MONDAY_API}...", flush=True)
    r = requests.post(MONDAY_API, headers=MONDAY_HEADERS, json={"query": query, "variables": variables}, timeout=30)
    r.raise_for_status()
    res = r.json()
    if "errors" in res:
        raise RuntimeError(f"GraphQL Errors: {res['errors']}")
    return res.get("data", {})

def upload_file_to_monday(item_id: str, column_id: str, filepath: str) -> dict:
    headers = {
        "Authorization": MONDAY_TOKEN,
        "API-Version": "2024-10"
    }
    filename = Path(filepath).name
    mime_type, _ = mimetypes.guess_type(filepath)
    mime_type = mime_type or "application/octet-stream"
    
    print(f"  Caricamento file '{filename}' ({mime_type}) su Monday colonna '{column_id}'...", flush=True)
    query = f'mutation ($file: File!) {{ add_file_to_column (item_id: {item_id}, column_id: "{column_id}", file: $file) {{ id }} }}'
    
    payload = {"query": query}
    with open(filepath, "rb") as f:
        files = {
            "variables[file]": (filename, f, mime_type)
        }
        r = requests.post("https://api.monday.com/v2/file", headers=headers, data=payload, files=files, timeout=60)
        r.raise_for_status()
        res = r.json()
        if "errors" in res:
            print(f"  ❌ Errore caricamento file {filename}: {res['errors']}", flush=True)
        else:
            print(f"  ✅ File {filename} caricato con successo!", flush=True)
        return res

def download_file(url: str, dest_path: str) -> bool:
    try:
        r = requests.get(url, headers={"Authorization": MONDAY_TOKEN}, timeout=60, stream=True)
        if r.status_code == 200:
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
    except Exception as e:
        print(f"  ❌ Errore download file {url}: {e}", flush=True)
    return False

def onboard_item(commercial_item_id: str) -> str:
    print(f"🚀 Avvio onboarding progetto da item commerciale: {commercial_item_id}", flush=True)
    
    # 1. Recupera dati dell'item commerciale
    q_get = """
    query GetCommercialItem($ids: [ID!]!) {
      items(ids: $ids) {
        id
        name
        column_values {
          id
          text
          value
          type
        }
        assets {
          id
          name
          url
          public_url
        }
      }
    }
    """
    data = _query(q_get, {"ids": [str(commercial_item_id)]})
    items = data.get("items", [])
    if not items:
        raise ValueError(f"Item commerciale {commercial_item_id} non trovato.")
    
    c_item = items[0]
    print(f"🔍 Item commerciale trovato: '{c_item['name']}'", flush=True)
    col_values = {c["id"]: c for c in c_item["column_values"]}
    
    # Estrai campi di origine
    nome_progetto = (col_values.get("testo_mkn1sqb4", {}).get("text") or "").strip() or c_item["name"]
    referente = (col_values.get("dup__of_nome_referente_mkn3gf63", {}).get("text") or "").strip()
    email = (col_values.get("email_mkn1v37b", {}).get("text") or "").strip()
    telefono = (col_values.get("telefono_mkn3gd8v", {}).get("text") or "").strip()
    richiesta = (col_values.get("testo_lungo_mkn1ydyz", {}).get("text") or "").strip()
    num_commessa = (col_values.get("text_mm51yvbk", {}).get("text") or "").strip()
    
    # Se il numero commessa non è compilato in NEW COMMERCIALE, lo usiamo come fallback
    if not num_commessa:
        num_commessa = f"COMM-{commercial_item_id}"
        
    print(f"📝 Dati estratti: Progetto='{nome_progetto}', Referente='{referente}', Commessa='{num_commessa}'", flush=True)
    
    # 2. Crea il nuovo item sulla board GESTIONE PROGETTI NEW
    q_create = """
    mutation CreateProject($board_id: ID!, $item_name: String!) {
      create_item (board_id: $board_id, item_name: $item_name) {
        id
      }
    }
    """
    res_create = _query(q_create, {
        "board_id": BOARD_PROGETTI,
        "item_name": c_item["name"]
    })
    project_item_id = res_create["create_item"]["id"]
    print(f"✅ Riga creata in GESTIONE PROGETTI NEW con ID: {project_item_id}", flush=True)
    
    # 3. Popola le colonne di GESTIONE PROGETTI NEW
    op_values = {
        "testo_mkn1sqb4": nome_progetto,
        "dup__of_nome_referente_mkn3gf63": referente,
        "email_mkn1v37b": {"email": email, "text": email} if email else "",
        "telefono_mkn3gd8v": {"phone": telefono, "countryShortName": "IT"} if telefono else "",
        "testo_lungo_mkn1ydyz": {"text": richiesta} if richiesta else "",
        "text_mm51yk45": num_commessa,
    }
    
    # Rimuovi chiavi vuote
    op_values = {k: v for k, v in op_values.items() if v}
    
    q_update = """
    mutation UpdateProjectColumns($column_values: JSON!) {
      change_multiple_column_values (board_id: """ + BOARD_PROGETTI + """, item_id: """ + project_item_id + """, column_values: $column_values) {
        id
      }
    }
    """
    _query(q_update, {"column_values": json.dumps(op_values)})
    print("📋 Colonne testuali e contatti copiate.", flush=True)
    
    # 4. Copia i file allegati ( assets )
    assets = c_item.get("assets", [])
    if assets:
        print(f"📎 Rilevati {len(assets)} file da copiare...", flush=True)
        with tempfile.TemporaryDirectory() as tmp_dir:
            for asset in assets:
                filename = asset["name"]
                url = asset["url"] or asset["public_url"]
                if not url:
                    continue
                
                local_path = Path(tmp_dir) / filename
                print(f"  ⬇️  Download temporaneo di {filename}...", flush=True)
                if download_file(url, str(local_path)):
                    print(f"  ⬆️  Upload di {filename} su Gestione Progetti...", flush=True)
                    # Carica nella colonna 'file_mkn13rg5' (Disegno/File) di GESTIONE PROGETTI NEW
                    upload_file_to_monday(project_item_id, "file_mkn13rg5", str(local_path))
                    print(f"  ✅ Copiato file: {filename}", flush=True)
                else:
                    print(f"  ⚠️  Impossibile copiare file: {filename}", flush=True)
    else:
        print("ℹ️ Nessun file allegato da copiare.", flush=True)
        
    print(f"🎉 Onboarding completato con successo! Item Operativo: {project_item_id}", flush=True)
    return project_item_id

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Uso: python3 onboard_project.py <commercial_item_id>", flush=True)
        sys.exit(1)
    
    onboard_item(sys.argv[1])
