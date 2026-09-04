#!/usr/bin/env python3
"""
setup_drive_oauth.py - Autorizzazione Google Drive (una tantum)
Usa OAuth 2.0 con redirect localhost (standard per Desktop App).
Apre il browser, l'utente autorizza, il token viene salvato in drive_token.json.
"""

import os
import json
import time
import socket
import threading
import webbrowser
import urllib.parse
import requests
from pathlib import Path
from dotenv import load_dotenv
from http.server import HTTPServer, BaseHTTPRequestHandler

load_dotenv()

CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"
TOKEN_FILE       = Path(__file__).parent / "drive_token.json"
TOKEN_URL        = "https://oauth2.googleapis.com/token"
AUTH_URL         = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPES           = "https://www.googleapis.com/auth/drive"
REDIRECT_PORT    = 8765  # Porta locale per il redirect

auth_code = None  # Condiviso tra il server HTTP e il main thread


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Handler HTTP che cattura il codice di autorizzazione dal redirect."""

    def do_GET(self):
        global auth_code
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"""
            <html><body style="font-family:sans-serif;text-align:center;padding:50px">
            <h1>&#x2705; Autorizzazione completata!</h1>
            <p>Google Drive &egrave; ora connesso ad AMR Drive Archiver.</p>
            <p>Puoi chiudere questa finestra e tornare al terminale.</p>
            </body></html>
            """)
        else:
            error = params.get("error", ["sconosciuto"])[0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<html><body><h1>Errore: {error}</h1></body></html>".encode())

    def log_message(self, format, *args):
        pass  # Silenzia i log del server HTTP


def main():
    global auth_code

    print("=" * 60)
    print("  🔐 Autorizzazione Google Drive per AMR Drive Archiver")
    print("=" * 60)

    # Carica client_id e client_secret da .env (priorità) o da credentials.json
    client_id     = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")

    if not client_id or not client_secret or client_secret.startswith("****"):
        if CREDENTIALS_FILE.exists():
            with open(CREDENTIALS_FILE) as f:
                d = json.load(f)
            info = d.get("installed", d.get("web", {}))
            client_id     = info.get("client_id", client_id)
            client_secret = info.get("client_secret", client_secret)

    if not client_id or not client_secret or client_secret.startswith("****"):
        print("\n❌ Credenziali non trovate. Verifica GOOGLE_CLIENT_ID e GOOGLE_CLIENT_SECRET nel file .env")
        return

    print(f"\n📋 Client ID: {client_id[:40]}...")
    print(f"🔑 Client Secret: {client_secret[:6]}...")

    redirect_uri = f"http://localhost:{REDIRECT_PORT}"

    # Costruisci l'URL di autorizzazione
    params = {
        "client_id":     client_id,
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "scope":         SCOPES,
        "access_type":   "offline",  # ottieni refresh_token
        "prompt":        "consent",  # forza il consenso per ottenere refresh_token
    }
    auth_link = AUTH_URL + "?" + urllib.parse.urlencode(params)

    # Avvia server HTTP locale in background
    server = HTTPServer(("localhost", REDIRECT_PORT), OAuthCallbackHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    print(f"\n🌐 Il browser si apre per l'autorizzazione...")
    time.sleep(1)
    webbrowser.open(auth_link)

    print("⏳ Aspetto che tu autorizzi l'accesso a Google Drive nel browser...")
    print("   (Se il browser non si apre, vai su:)")
    print(f"   {auth_link[:80]}...")

    # Attendi il codice di autorizzazione (max 5 minuti)
    deadline = time.time() + 300
    while auth_code is None and time.time() < deadline:
        time.sleep(0.5)
        print(".", end="", flush=True)

    server.shutdown()
    print()

    if auth_code is None:
        print("\n❌ Timeout: autorizzazione non completata entro 5 minuti.")
        return

    print(f"\n✅ Codice ricevuto! Scambio per access token...")

    # Scambia il codice con access_token + refresh_token
    r = requests.post(TOKEN_URL, data={
        "code":          auth_code,
        "client_id":     client_id,
        "client_secret": client_secret,
        "redirect_uri":  redirect_uri,
        "grant_type":    "authorization_code",
    }, timeout=15)
    r.raise_for_status()
    token_data = r.json()

    if "access_token" not in token_data:
        print(f"\n❌ Errore risposta token: {token_data}")
        return

    token_data["saved_at"] = time.time()
    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)

    print(f"\n✅ Token salvato in: {TOKEN_FILE}")
    print(f"   Access token:  {token_data['access_token'][:30]}...")
    print(f"   Refresh token: {'✅ presente' if 'refresh_token' in token_data else '❌ assente'}")
    print(f"   Scade tra:     {token_data.get('expires_in', '?')}s")
    print("\n🎉 Google Drive pronto! Ora avvia il webhook server:")
    print(f"   bash start_archive_server.sh")


if __name__ == "__main__":
    main()
