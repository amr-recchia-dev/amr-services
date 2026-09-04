#!/usr/bin/env python3
"""
drive_client.py - Client Google Drive REST API puro (nessuna libreria compilata C)
Usa OAuth 2.0 con Device Authorization Flow per autenticazione sicura.
"""

import os
import json
import time
import logging
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("drive_client")

# === Configurazione ===
CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"
TOKEN_FILE       = Path(__file__).parent / "drive_token.json"
DRIVE_ROOT       = os.getenv("DRIVE_ROOT_FOLDER_ID", "10JenGAh8Bq_7xO1r3qoJhBoqjrmRMV4b")
SCOPES           = ["https://www.googleapis.com/auth/drive"]

# === Endpoint Google OAuth ===
DEVICE_CODE_URL  = "https://oauth2.googleapis.com/device/code"
TOKEN_URL        = "https://oauth2.googleapis.com/token"
DRIVE_API        = "https://www.googleapis.com/drive/v3"
UPLOAD_API       = "https://www.googleapis.com/upload/drive/v3"

# === Estensioni per tipo cartella ===
EXT_DISEGNI  = {".dwg", ".dxf", ".step", ".stp", ".stl", ".iges", ".igs", ".3dm", ".sat"}
EXT_PDF      = {".pdf"}
EXT_FOTO     = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".heic", ".heif"}
EXT_FISCALI  = {".xml", ".p7m", ".xls", ".xlsx", ".csv"}


def _load_credentials():
    """
    Carica client_id e client_secret.
    Priorità: variabili d'ambiente → credentials.json locale.
    Compatibile sia con Mac locale che con Railway cloud.
    """
    # 1. Prova da variabili d'ambiente (Railway/cloud)
    env_id     = os.getenv("GOOGLE_CLIENT_ID", "")
    env_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    if env_id and env_secret:
        return env_id, env_secret
    # 2. Fallback: credentials.json locale (Mac)
    if CREDENTIALS_FILE.exists():
        with open(CREDENTIALS_FILE) as f:
            d = json.load(f)
        info = d.get("installed", d.get("web", {}))
        return info["client_id"], info["client_secret"]
    raise FileNotFoundError(
        "Credenziali Google non trovate. "
        "Imposta GOOGLE_CLIENT_ID e GOOGLE_CLIENT_SECRET nelle variabili d'ambiente, "
        "oppure posiziona credentials.json nella cartella del progetto."
    )


def _load_token():
    """Carica il token salvato da drive_token.json. Ritorna None se non esiste."""
    if not TOKEN_FILE.exists():
        return None
    with open(TOKEN_FILE) as f:
        return json.load(f)


def _save_token(token_data: dict):
    """Salva il token in drive_token.json."""
    token_data["saved_at"] = time.time()
    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)
    logger.info("Token salvato in drive_token.json")


def _refresh_access_token(refresh_token: str) -> dict:
    """Aggiorna l'access token usando il refresh token."""
    client_id, client_secret = _load_credentials()
    r = requests.post(TOKEN_URL, data={
        "client_id":     client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type":    "refresh_token",
    }, timeout=15)
    r.raise_for_status()
    return r.json()


# Cache in-memory del token (usata su cloud dove non si può scrivere file)
_token_cache = {"access_token": None, "expires_at": 0}


def get_access_token() -> str:
    """
    Ritorna un access token valido per Google Drive.
    Strategia (compatibile Mac locale + Railway cloud):
    1. Usa cache in-memory se non scaduta
    2. Prova da GOOGLE_REFRESH_TOKEN (env var) → rinnova via API
    3. Prova da drive_token.json locale → rinnova se scaduto
    """
    global _token_cache

    # 1. Usa cache se valida (margine 5 minuti)
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 300:
        return _token_cache["access_token"]

    # 2. Ottieni refresh_token: env var ha priorità (Railway), poi file locale
    refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN", "")
    if not refresh_token:
        token_file = _load_token()
        if token_file:
            refresh_token = token_file.get("refresh_token", "")

    if not refresh_token:
        raise DriveAuthRequired(
            "GOOGLE_REFRESH_TOKEN non configurato. "
            "Su Railway: aggiungi la variabile d'ambiente GOOGLE_REFRESH_TOKEN. "
            "In locale: esegui python3 setup_drive_oauth.py"
        )

    # 3. Rinnova l'access token
    logger.info("Rinnovo access token Google Drive...")
    try:
        new_token = _refresh_access_token(refresh_token)
        _token_cache["access_token"] = new_token["access_token"]
        _token_cache["expires_at"]   = time.time() + new_token.get("expires_in", 3600)
        # Aggiorna anche il file locale se esiste (Mac)
        if TOKEN_FILE.exists():
            local = _load_token() or {}
            local.update(new_token)
            _save_token(local)
        return _token_cache["access_token"]
    except Exception as e:
        raise DriveAuthRequired(f"Impossibile rinnovare il token Drive: {e}")


class DriveAuthRequired(Exception):
    pass


def _headers() -> dict:
    return {"Authorization": f"Bearer {get_access_token()}"}


# ===================== OPERAZIONI DRIVE =====================

def find_folder(name: str, parent_id: str):  # -> Optional[str]
    """Cerca una cartella per nome dentro parent_id. Ritorna l'ID o None."""
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and '{parent_id}' in parents and trashed=false"
    r = requests.get(f"{DRIVE_API}/files", headers=_headers(), params={
        "q": q, "fields": "files(id,name)", "spaces": "drive"
    }, timeout=15)
    r.raise_for_status()
    files = r.json().get("files", [])
    return files[0]["id"] if files else None


def create_folder(name: str, parent_id: str) -> str:
    """Crea una cartella in parent_id e ritorna il suo ID."""
    body = {
        "name":     name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents":  [parent_id],
    }
    r = requests.post(f"{DRIVE_API}/files", headers={**_headers(), "Content-Type": "application/json"},
                      json=body, timeout=15)
    r.raise_for_status()
    folder_id = r.json()["id"]
    logger.info(f"📁 Cartella creata: '{name}' → {folder_id}")
    return folder_id


def get_or_create_folder(name: str, parent_id: str) -> str:
    """Cerca o crea una cartella per nome dentro parent_id."""
    folder_id = find_folder(name, parent_id)
    if folder_id:
        logger.debug(f"📁 Cartella trovata: '{name}' → {folder_id}")
        return folder_id
    return create_folder(name, parent_id)


def upload_file(file_path, parent_id: str, filename: str = None) -> str:
    """
    Carica un file su Google Drive nella cartella parent_id.
    Ritorna l'ID del file caricato.
    """
    file_path = Path(file_path)
    filename  = filename or file_path.name

    # Determina il tipo MIME
    import mimetypes
    mime_type, _ = mimetypes.guess_type(str(file_path))
    mime_type = mime_type or "application/octet-stream"

    # Metadata
    metadata = json.dumps({"name": filename, "parents": [parent_id]})

    with open(file_path, "rb") as f:
        file_data = f.read()

    # Upload multipart
    boundary = "amr_boundary_2026"
    body = (
        f"--{boundary}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{metadata}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode() + file_data + f"\r\n--{boundary}--".encode()

    r = requests.post(
        f"{UPLOAD_API}/files?uploadType=multipart",
        headers={
            **_headers(),
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        data=body,
        timeout=120,
    )
    r.raise_for_status()
    file_id = r.json()["id"]
    logger.info(f"⬆️  File caricato: '{filename}' → {file_id}")
    return file_id


def upload_bytes(content: bytes, filename: str, parent_id: str, mime_type: str = "text/plain") -> str:
    """Carica bytes direttamente su Drive senza file locale."""
    metadata = json.dumps({"name": filename, "parents": [parent_id]})
    boundary = "amr_bytes_boundary"
    body = (
        f"--{boundary}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{metadata}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--".encode()

    r = requests.post(
        f"{UPLOAD_API}/files?uploadType=multipart",
        headers={
            **_headers(),
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        data=body,
        timeout=60,
    )
    r.raise_for_status()
    file_id = r.json()["id"]
    logger.info(f"⬆️  Contenuto caricato: '{filename}' → {file_id}")
    return file_id


def get_folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def get_subfolder_for_file(filename: str) -> str:
    """Determina la sottocartella in base all'estensione del file."""
    ext = Path(filename).suffix.lower()
    if ext in EXT_DISEGNI:
        return "Disegni"
    elif ext in EXT_PDF:
        return "PDF"
    elif ext in EXT_FOTO:
        return "Foto"
    elif ext in EXT_FISCALI:
        return "Dati Fiscali"
    else:
        return "Documenti"


# ======================== TEST ========================

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if "--test" in sys.argv:
        print("🔍 Test connessione Google Drive...")
        try:
            token = get_access_token()
            print(f"✅ Token valido: {token[:20]}...")

            # Test: crea cartella di prova nella root
            test_id = get_or_create_folder("_TEST_AMR_ARCHIVER", DRIVE_ROOT)
            print(f"✅ Cartella test creata/trovata: {get_folder_url(test_id)}")

            # Test upload testo
            fid = upload_bytes(b"Test archiviazione AMR Recchia - OK!", "test.txt", test_id)
            print(f"✅ File test caricato: {fid}")
            print("✅ Tutto funziona! Sistema di archiviazione pronto.")
        except DriveAuthRequired as e:
            print(f"⚠️  {e}")
        except Exception as e:
            print(f"❌ Errore: {e}")
