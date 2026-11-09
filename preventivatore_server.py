#!/usr/bin/env python3
"""
AMR OS — Server Dedicato Preventivatore 3D, Tempi KUKA/OSAI & Connettore Monday.com
====================================================================================
100% Python Standard Library (ThreadingHTTPServer) — Zero dipendenze esterne.
Espone il Preventivatore protetto da Login Whitelist (PBKDF2-SHA256) su porta 3095
(dietro reverse proxy Nginx HTTPS `https://zaffiroweb.com/amr-preventivatore/`).
Accesso riservato esclusivamente a:
- riccardo.g@zaffiroweb.com
- ordini@amrrecchia.it
- info@amrrecchia.it
"""

from __future__ import annotations
import os
import json
import logging
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from http.cookies import SimpleCookie
import amr_preventivatore_engine

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TPL_DIR = os.path.join(BASE_DIR, "templates")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [AMR-PREV] %(message)s")
logger = logging.getLogger("amr_preventivatore_server")


def render_login_html(error: str | None = None, login_action: str = "./login") -> bytes:
    path = os.path.join(TPL_DIR, "preventivatore_login.html")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("{{ login_action }}", login_action)
    if error:
        err_block = f'<div class="mb-5 bg-rose-950/80 border border-rose-500/50 text-rose-200 text-xs rounded-xl p-3.5 font-medium">⚠️ {error}</div>'
        html = html.replace("{% if error %}", "").replace("{% endif %}", "").replace("{{ error }}", error)
        # Ensure clean block
    else:
        import re
        html = re.sub(r"\{%\s*if\s+error\s*%\}.*?\{%\s*endif\s*%\}", "", html, flags=re.DOTALL)
    return html.encode("utf-8")


def render_app_html() -> bytes:
    path = os.path.join(TPL_DIR, "preventivatore.html")
    with open(path, "r", encoding="utf-8") as f:
        return f.read().encode("utf-8")


class PreventivatoreHandler(BaseHTTPRequestHandler):
    def _client_ip(self) -> str:
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def _get_auth_user(self):
        raw_cookie = self.headers.get("Cookie", "")
        c = SimpleCookie()
        try:
            c.load(raw_cookie)
        except Exception:
            return None
        morsel = c.get("amr_prev_session")
        token = morsel.value if morsel else ""
        return amr_preventivatore_engine.verify_session_token(token)

    def _send_security_headers(self):
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive, nosnippet")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")

    def _send_json(self, status: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, body: bytes, extra_headers: list[tuple[str, str]] | None = None):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str, extra_headers: list[tuple[str, str]] | None = None):
        self.send_response(302)
        self.send_header("Location", location)
        self._send_security_headers()
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()

    def _clean_path(self) -> str:
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path
        # Strip /amr-preventivatore prefix if passed through
        if p.startswith("/amr-preventivatore"):
            p = p[len("/amr-preventivatore"):]
        if not p:
            p = "/"
        return p

    def do_GET(self):
        p = self._clean_path()
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if p in ("/health", "/api/health"):
            return self._send_json(200, {"ok": True, "service": "amr-preventivatore"})

        if p in ("/logout", "/preventivatore/logout"):
            raw_cookie = self.headers.get("Cookie", "")
            c = SimpleCookie()
            try:
                c.load(raw_cookie)
                if "amr_prev_session" in c:
                    amr_preventivatore_engine.revoke_session_token(c["amr_prev_session"].value)
            except Exception:
                pass
            cookie_hdr = "amr_prev_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"
            return self._redirect("./login", [("Set-Cookie", cookie_hdr)])

        if p in ("/login", "/preventivatore/login"):
            if self._get_auth_user():
                return self._redirect("./")
            return self._send_html(200, render_login_html(None, "./login"))

        if p == "/api/preventivatore/monday/items":
            user = self._get_auth_user()
            if not user:
                return self._send_json(401, {"ok": False, "error": "Sessione non autorizzata"})
            try:
                items = amr_preventivatore_engine.fetch_monday_commercial_items(limit=60)
                return self._send_json(200, {"ok": True, "count": len(items), "items": items})
            except Exception as e:
                logger.error("Errore lettura Monday: %s", e, exc_info=True)
                return self._send_json(500, {"ok": False, "error": str(e)})

        if p == "/api/preventivatore/storico":
            user = self._get_auth_user()
            if not user:
                return self._send_json(401, {"ok": False, "error": "Sessione non autorizzata"})
            q = (qs.get("q", [""])[0] or "").strip()
            quotes = amr_preventivatore_engine.search_similar_historical_quotes(query=q, limit=100)
            return self._send_json(200, {"ok": True, "count": len(quotes), "quotes": quotes})

        # Main UI
        user = self._get_auth_user()
        if not user:
            return self._send_html(401, render_login_html(None, "./login"))
        return self._send_html(200, render_app_html())

    def do_POST(self):
        p = self._clean_path()
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(length) if length > 0 else b""

        if p in ("/login", "/preventivatore/login"):
            form = urllib.parse.parse_qs(raw_body.decode("utf-8", errors="replace"))
            email_addr = (form.get("email", [""])[0] or "").strip().lower()
            password = form.get("password", [""])[0] or ""
            ip = self._client_ip()

            allowed, retry_sec = amr_preventivatore_engine.check_rate_limit_login(f"{ip}:{email_addr}")
            if not allowed:
                return self._send_html(
                    429,
                    render_login_html(f"Troppi tentativi falliti. Riprova tra {retry_sec // 60 + 1} minuti.", "./login"),
                )

            if email_addr not in amr_preventivatore_engine.ALLOWED_USERS:
                amr_preventivatore_engine.record_failed_login(f"{ip}:{email_addr}")
                logger.warning("⛔ Tentativo login email NON in whitelist: '%s' (IP %s)", email_addr, ip)
                return self._send_html(
                    403,
                    render_login_html("Accesso negato: questo indirizzo email non è nella whitelist autorizzata.", "./login"),
                )

            user = amr_preventivatore_engine.verify_user_credentials(email_addr, password)
            if not user:
                amr_preventivatore_engine.record_failed_login(f"{ip}:{email_addr}")
                logger.warning("⛔ Password errata per '%s' (IP %s)", email_addr, ip)
                return self._send_html(
                    401,
                    render_login_html("Password non valida per l'account indicato.", "./login"),
                )

            amr_preventivatore_engine.clear_failed_login(f"{ip}:{email_addr}")
            tok = amr_preventivatore_engine.create_session_token(email_addr)
            logger.info("✅ Login eseguito con successo da '%s' (IP %s)", email_addr, ip)
            cookie_hdr = f"amr_prev_session={tok}; Max-Age=43200; Path=/; HttpOnly; SameSite=Lax; Secure"
            return self._redirect("./", [("Set-Cookie", cookie_hdr)])

        if p == "/api/preventivatore/chat":
            user = self._get_auth_user()
            if not user:
                return self._send_json(401, {"ok": False, "error": "Sessione non autorizzata"})
            try:
                body = json.loads(raw_body.decode("utf-8"))
                msg = (body.get("message") or "").strip()
                if not msg:
                    return self._send_json(400, {"ok": False, "error": "Messaggio vuoto"})
                history = body.get("history") or []
                quote_state = body.get("quote_state") or {}
                res = amr_preventivatore_engine.ask_amr_ai_assistant(
                    user_message=msg,
                    chat_history=history,
                    current_quote_state=quote_state,
                    author_user=user,
                )
                return self._send_json(200, res)
            except Exception as e:
                logger.error("Errore Chat AI Preventivatore: %s", e, exc_info=True)
                return self._send_json(500, {"ok": False, "error": str(e)})

        if p == "/api/preventivatore/monday/publish":
            user = self._get_auth_user()
            if not user:
                return self._send_json(401, {"ok": False, "error": "Sessione non autorizzata"})
            try:
                payload = json.loads(raw_body.decode("utf-8"))
                res = amr_preventivatore_engine.publish_quote_to_monday(payload, author_email=user["email"])
                return self._send_json(200, res)
            except Exception as e:
                logger.error("Errore scrittura Monday: %s", e, exc_info=True)
                return self._send_json(500, {"ok": False, "error": str(e)})

        if p == "/api/preventivatore/parse-machine":
            user = self._get_auth_user()
            if not user:
                return self._send_json(401, {"ok": False, "error": "Sessione non autorizzata"})
            try:
                body = json.loads(raw_body.decode("utf-8"))
                content = body.get("content", "")
                filename = body.get("filename", "")
                override = float(body.get("override_pct", 100.0))
                machine_type = body.get("machine_type", "auto").lower()
                if not content:
                    return self._send_json(400, {"ok": False, "error": "Contenuto vuoto"})
                is_kuka = (
                    machine_type == "kuka"
                    or filename.lower().endswith(".src")
                    or "$VEL.CP" in content.upper()
                    or "E6POS" in content.upper()
                )
                res = (
                    amr_preventivatore_engine.parse_kuka_src(content, override_pct=override)
                    if is_kuka
                    else amr_preventivatore_engine.parse_osai_gcode(content, override_pct=override)
                )
                return self._send_json(200, {"ok": True, **res})
            except Exception as e:
                return self._send_json(500, {"ok": False, "error": str(e)})

        return self._send_json(404, {"ok": False, "error": "Endpoint non trovato"})


if __name__ == "__main__":
    port = int(os.getenv("AMR_PREV_PORT", "3095"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), PreventivatoreHandler)
    logger.info("🚀 AMR OS Preventivatore Server in ascolto su http://127.0.0.1:%d", port)
    srv.serve_forever()
