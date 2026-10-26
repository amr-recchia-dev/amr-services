#!/usr/bin/env python3
"""
webhook_server.py - Server Flask che riceve webhook da Monday.com
Quando "Avvia Esportazione" viene cliccato, archivia il progetto su Google Drive.
"""

from __future__ import annotations
import os
import json
import logging
import threading
import time
from collections import defaultdict
from datetime import datetime
from flask import Flask, request, jsonify, render_template, Response
from dotenv import load_dotenv
import drive_archiver
import step_time_tracker
import business_time
import department_syncer
import voice_agent
import user_auth
import timbra_log
import dispositivi

load_dotenv()

# Configurazione Sicurezza: PIN di accesso aziendale
AMR_ACCESS_PIN = os.getenv("AMR_ACCESS_PIN", "2026")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # nessun valore di riserva nel codice

def _admin_secret_ok(valore) -> bool:
    """True solo se la chiave admin è configurata e coincide (confronto a tempo costante)."""
    import hmac
    return bool(WEBHOOK_SECRET) and bool(valore) and hmac.compare_digest(str(valore), WEBHOOK_SECRET)

class SlidingWindowRateLimiter:
    """Rate limiter thread-safe in memoria con finestra scorrevole per IP."""
    def __init__(self):
        self._lock = threading.Lock()
        self._requests = defaultdict(list)

    def is_allowed(self, key: str, max_requests: int, window_seconds: int = 60) -> tuple[bool, int]:
        now = time.time()
        with self._lock:
            timestamps = [ts for ts in self._requests[key] if now - ts < window_seconds]
            if len(timestamps) >= max_requests:
                oldest = timestamps[0]
                retry_after = max(1, int(window_seconds - (now - oldest)))
                self._requests[key] = timestamps
                return False, retry_after
            timestamps.append(now)
            self._requests[key] = timestamps
            return True, 0

rate_limiter = SlidingWindowRateLimiter()

def get_client_ip() -> str:
    """Estrae l'indirizzo IP reale del client anche dietro Cloudflare / Render proxy."""
    return (
        request.headers.get("CF-Connecting-IP") or
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or
        request.remote_addr or
        "127.0.0.1"
    )

def get_current_user_from_request() -> dict:
    """Estrae e convalida il profilo utente a partire dal nome dipendente o dal PIN inviato via header, cookie, query o JSON."""
    import urllib.parse

    # 1. Ricerca per nome dipendente (da selezione rapida senza PIN)
    user_name = request.headers.get("X-AMR-USER") or request.cookies.get("amr_user_name") or request.args.get("user")
    if user_name:
        try:
            user_name_dec = urllib.parse.unquote(user_name).strip()
            u = user_auth.get_user_by_name(user_name_dec)
            if u:
                return u
        except Exception:
            pass

    # 2. Ricerca per PIN (retrocompatibilità se presente)
    provided_pin = (
        request.headers.get("X-AMR-PIN") or
        request.cookies.get("amr_pin") or
        request.args.get("pin")
    )
    if not provided_pin and request.is_json:
        data = request.get_json(silent=True) or {}
        provided_pin = data.get("pin")
        if data.get("user_name"):
            u = user_auth.get_user_by_name(data.get("user_name"))
            if u:
                return u

    if provided_pin:
        u = user_auth.get_user_by_pin(provided_pin)
        if u:
            return u

    # 3. Default aperto per reparto produzione se non specificato (nessun blocco all'ingresso)
    return {
        "name": "Operatore Produzione",
        "email": "produzione@amrrecchia.it",
        "department": "Reparto Produzione",
        "is_commercial": False,
        "label": "Reparto Produzione AMR"
    }

def check_pin_auth() -> bool:
    """Nessun blocco per l'assistente vocale: accesso aperto a tutti gli operatori."""
    return True

# === Setup logging (compatibile con Railway/Render: stdout se in cloud) ===
log_handlers = [logging.StreamHandler()]
if os.getenv("LOG_TO_FILE", "").lower() in ("1", "true", "yes"):
    log_handlers.append(logging.FileHandler("webhook.log"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=log_handlers,
)
logger = logging.getLogger("webhook_server")

app = Flask(__name__)

# Stato dell'ultima esecuzione
last_status = {
    "last_trigger":    None,
    "last_item_id":    None,
    "last_result":     None,
    "running":         False,
    "total_archived":  0,
}


def run_archive_in_background(item_id: str):
    """Esegue l'archiviazione in un thread separato."""
    global last_status
    last_status["running"]      = True
    last_status["last_item_id"] = item_id
    last_status["last_trigger"] = datetime.now().isoformat()

    logger.info(f"🚀 Avvio archiviazione background per item {item_id}")
    try:
        result = drive_archiver.archive_item(item_id)
        last_status["last_result"] = result
        if result.get("success"):
            last_status["total_archived"] += 1
            logger.info(f"✅ Archiviazione completata: {result.get('folder_url')}")
        else:
            logger.error(f"❌ Archiviazione fallita: {result.get('error')}")
    except Exception as e:
        logger.error(f"❌ Errore archiviazione: {e}", exc_info=True)
        last_status["last_result"] = {"success": False, "error": str(e)}
    finally:
        last_status["running"] = False


@app.route("/webhook", methods=["POST"])
def webhook():
    """Endpoint principale che riceve i webhook da Monday.com."""
    data = request.get_json(force=True, silent=True) or {}
    logger.info(f"📨 Webhook ricevuto: {json.dumps(data)[:200]}")

    # Monday.com invia una challenge per verificare il webhook
    if "challenge" in data:
        logger.info("✅ Challenge Monday.com accettata")
        return jsonify({"challenge": data["challenge"]})

    # Estrai l'item ID dal payload
    event    = data.get("event", data)
    item_id  = (
        event.get("pulseId") or
        event.get("itemId") or
        event.get("pulse_id") or
        data.get("pulseId") or
        data.get("itemId")
    )

    if not item_id:
        logger.warning(f"⚠️  Nessun item ID nel payload: {data}")
        return jsonify({"status": "ignored", "reason": "no item_id"}), 200

    item_id = str(item_id)
    logger.info(f"📋 Item ID estratto: {item_id}")

    # Verifica che non sia già in esecuzione
    if last_status["running"]:
        logger.warning(f"⚠️  Archiviazione già in corso per {last_status['last_item_id']}, ignoro {item_id}")
        return jsonify({"status": "busy", "message": "Archiviazione già in corso"}), 200

    # Avvia archiviazione in background (risposta immediata a Monday.com)
    thread = threading.Thread(
        target=run_archive_in_background,
        args=(item_id,),
        daemon=True,
        name=f"archiver-{item_id}",
    )
    thread.start()

    return jsonify({
        "status":  "accepted",
        "item_id": item_id,
        "message": "Archiviazione avviata in background",
    }), 200


@app.route("/onboard", methods=["POST"])
def onboard_webhook():
    """Endpoint che riceve il webhook quando un preventivo viene accettato."""
    data = request.get_json(force=True, silent=True) or {}
    logger.info(f"📨 [ONBOARD] Webhook ricevuto: {json.dumps(data)[:200]}")

    # Monday.com invia una challenge per verificare il webhook
    if "challenge" in data:
        logger.info("✅ [ONBOARD] Challenge Monday.com accettata")
        return jsonify({"challenge": data["challenge"]})

    # Estrai l'item ID dal payload
    event = data.get("event", data)
    item_id = (
        event.get("pulseId") or
        event.get("itemId") or
        event.get("pulse_id") or
        data.get("pulseId") or
        data.get("itemId")
    )

    if not item_id:
        logger.warning(f"⚠️  [ONBOARD] Nessun item ID nel payload: {data}")
        return jsonify({"status": "ignored", "reason": "no item_id"}), 200

    item_id = str(item_id)
    logger.info(f"🚀 [ONBOARD] Avvio creazione progetto da preventivo commerciale {item_id}")

    def _run_onboard(id_str):
        import onboard_project
        try:
            onboard_project.onboard_item(id_str)
            logger.info(f"✅ [ONBOARD] Progetto creato con successo per item commerciale {id_str}")
        except Exception as err:
            logger.error(f"❌ [ONBOARD] Errore creazione progetto {id_str}: {err}", exc_info=True)

    thread = threading.Thread(
        target=_run_onboard,
        args=(item_id,),
        daemon=True,
        name=f"onboard-{item_id}"
    )
    thread.start()

    return jsonify({
        "status": "accepted",
        "item_id": item_id,
        "message": "Onboarding avviato in background"
    }), 200


@app.route("/health", methods=["GET"])
def health():
    """Health check — verifica che il server sia attivo."""
    return jsonify({
        "status":  "ok",
        "service": "AMR Drive Archiver Webhook",
        "running": last_status["running"],
        "total_archived": last_status["total_archived"],
    }), 200


dashboard_cache = {
    "data": None,
    "timestamp": 0
}


@app.route("/", methods=["GET"])
@app.route("/dashboard", methods=["GET"])
def dashboard_view():
    """Interfaccia Web della Dashboard Avanzamento Progetti AMR Recchia."""
    return render_template("dashboard.html")


@app.route("/vocale", methods=["GET"])
@app.route("/voice", methods=["GET"])
def voice_view():
    """Interfaccia Web dell'Assistente Vocale AMR Recchia per dipendenti e officina."""
    return render_template("voice.html")


@app.route("/timbrature", methods=["GET"])
@app.route("/presenze", methods=["GET"])
def timbrature_view():
    """Interfaccia Web del Portale Presenze & Ore Lavorate per i dipendenti AMR."""
    return render_template("timbrature.html")


@app.route("/api/presenze-data", methods=["GET", "POST"])
def api_presenze_data():
    """Restituisce il riepilogo ore, sessioni accoppiate e anomalie per un dipendente dato il PIN."""
    import presenze_service
    req_json = request.get_json(silent=True) or {} if request.is_json else {}
    pin = request.args.get("pin") or req_json.get("pin") or request.headers.get("X-AMR-PIN")
    month = request.args.get("month") or req_json.get("month")
    if not pin:
        return jsonify({"authenticated": False, "error": "PIN personale mancante"}), 400
    res = presenze_service.calculate_employee_report(pin, month)
    if not res.get("authenticated"):
        return jsonify(res), 401
    return jsonify(res), 200


@app.route("/api/presenze-admin", methods=["GET", "POST"])
def api_presenze_admin():
    """Restituisce la sintesi di tutti i dipendenti per l'amministrazione."""
    import presenze_service
    req_json = request.get_json(silent=True) or {} if request.is_json else {}
    pin = request.args.get("pin") or req_json.get("pin") or request.headers.get("X-AMR-PIN")
    month = request.args.get("month") or req_json.get("month")
    admin_pins = ["4921", "6198", "9276", "8359"]
    if pin not in admin_pins and not _admin_secret_ok(request.args.get("secret")):
        return jsonify({"error": "Accesso riservato all'amministrazione"}), 403
    return jsonify(presenze_service.get_all_employees_overview(month)), 200


# ── Timbratura con PIN (senza account Google) ───────────────────────────────
# La pagina /timbra parla solo con questo server; il server inoltra la richiesta
# all'endpoint Apps Script che scrive nel foglio "Registro delle timbrature".
AMR_TIMBRA_URL = os.getenv(
    "AMR_TIMBRA_URL",
    "https://script.google.com/macros/s/AKfycbxzWM7ClITTd3usI2qrIYKENQIiE2KGE9sWvEuGhBJAe40W_hcpn47rrOk0609-7nPr/exec",
)
AMR_TIMBRA_KEY = os.getenv("AMR_TIMBRA_KEY", "amr-tb-9X4qK7mW2zR8vN5c")


def _timbra_call(payload: dict) -> tuple[dict, int]:
    """Inoltra ad Apps Script e restituisce (dati, codice HTTP)."""
    import requests as _rq
    payload = dict(payload)
    payload["key"] = AMR_TIMBRA_KEY
    try:
        # Apps Script risponde con un redirect 302 → requests lo segue in GET (corretto)
        r = _rq.post(AMR_TIMBRA_URL, json=payload, timeout=40)
        data = r.json()
    except Exception as e:  # rete Google lenta o risposta non JSON
        logger.warning(f"[TIMBRA] Errore proxy Apps Script: {e}")
        return {"ok": False, "retry": True, "error": "Server timbrature non raggiungibile"}, 503
    if not data.get("ok"):
        code = 503 if data.get("retry") else (401 if "PIN" in str(data.get("error", "")) else 400)
        return data, code
    return data, 200


def _timbra_proxy(payload: dict):
    data, code = _timbra_call(payload)
    return jsonify(data), code


@app.route("/timbra", methods=["GET"])
def timbra_view():
    """Pagina di timbratura Entrata/Uscita con PIN + GPS (smartphone e tablet kiosk)."""
    return render_template("timbra.html")


@app.route("/timbra-sw.js", methods=["GET"])
def timbra_sw():
    """Service worker: apre la pagina anche senza rete (le timbrature restano in coda)."""
    resp = app.send_static_file("timbra-sw.js")
    resp.headers["Content-Type"] = "application/javascript"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


def _dip_key(pin: str) -> str:
    """Chiave del dipendente per l'associazione telefono (nome; se manca, impronta del PIN)."""
    try:
        u = user_auth.get_user_by_pin(pin)
        if u and u.get("name"):
            return u["name"]
    except Exception:
        pass
    return "pin:" + timbra_log._pin_hash(pin)


@app.route("/api/timbra/login", methods=["POST"])
def api_timbra_login():
    ip = get_client_ip()
    allowed, retry_after = rate_limiter.is_allowed(f"timbra_login_{ip}", max_requests=20, window_seconds=60)
    if not allowed:
        return jsonify({"ok": False, "error": "Troppi tentativi, attendi un minuto"}), 429
    body = request.get_json(silent=True) or {}
    pin = str(body.get("pin", "")).strip()
    if not pin.isdigit() or not (3 <= len(pin) <= 6):
        return jsonify({"ok": False, "error": "PIN non valido"}), 400
    data, code = _timbra_call({"action": "login", "pin": pin})
    if data.get("ok"):
        chk = dispositivi.controlla(_dip_key(pin), str(body.get("device_id", "")), bool(body.get("kiosk")),
                                    request.headers.get("User-Agent", ""), vincola=False)
        data["dispositivo"] = chk.get("stato")
        if chk.get("messaggio"):
            data["dispositivo_msg"] = chk["messaggio"]
    return jsonify(data), code


@app.route("/api/timbra/registra", methods=["POST"])
def api_timbra_registra():
    ip = get_client_ip()
    # Limite largo: la coda offline di un tablet kiosk può inviare molte timbrature insieme
    allowed, retry_after = rate_limiter.is_allowed(f"timbra_reg_{ip}", max_requests=60, window_seconds=60)
    if not allowed:
        return jsonify({"ok": False, "retry": True, "error": "Troppe richieste, riprovo a breve"}), 429
    body = request.get_json(silent=True) or {}
    pin = str(body.get("pin", "")).strip()
    azione = body.get("azione")
    if not pin.isdigit() or azione not in ("Entrata", "Uscita"):
        return jsonify({"ok": False, "error": "Dati non validi"}), 400
    payload = {
        "action": "timbra",
        "pin": pin,
        "azione": azione,
        "ts": body.get("ts"),
        "client_id": str(body.get("client_id", ""))[:64],
        "lat": body.get("lat"),
        "lng": body.get("lng"),
        "acc": body.get("acc"),
        "confermata": bool(body.get("confermata")),
    }
    logger.info(f"[TIMBRA] {azione} da {ip} (client_id={payload['client_id']})")
    # Copia di sicurezza su disco Render PRIMA di inviare a Google
    try:
        _u = user_auth.get_user_by_pin(pin)
        _nome = _u.get("name") if _u else None
    except Exception:
        _nome = None
    rid = timbra_log.registra_ricevuta(pin, _nome, azione, payload["ts"], payload["lat"],
                                       payload["lng"], payload["acc"], payload["client_id"], ip)

    # Telefono autorizzato per questo dipendente?
    dip = _dip_key(pin)
    dev = str(body.get("device_id", ""))
    kiosk = bool(body.get("kiosk"))
    ua = request.headers.get("User-Agent", "")
    chk = dispositivi.controlla(dip, dev, kiosk, ua, vincola=False)
    if not chk.get("ok"):
        stato = chk.get("stato")
        try:
            vecchia = (time.time() * 1000 - float(payload["ts"] or 0)) > 11 * 3600 * 1000
        except (TypeError, ValueError):
            vecchia = False
        if vecchia and stato != "tablet_non_attivo":
            stato = "scaduta"
            chk["messaggio"] = "Telefono non autorizzato in tempo: la timbratura non è stata registrata. Avvisa l'amministrazione."
        data = {"ok": False, "retry": False, "dispositivo": stato, "error": chk.get("messaggio", "Telefono non autorizzato")}
        timbra_log.registra_esito(rid, data, 403)
        logger.warning(f"[TIMBRA] Bloccata ({stato}) per {dip} da {ip}")
        return jsonify(data), 403

    data, code = _timbra_call(payload)
    timbra_log.registra_esito(rid, data, code)
    if data.get("ok") and chk.get("stato") == "nuovo":
        dispositivi.controlla(dip, dev, kiosk, ua, vincola=True)   # primo telefono: diventa il suo
        data["dispositivo"] = "associato"
    return jsonify(data), code


_TIMBRA_ADMIN_PINS = {"4921", "6198", "9276", "8359"}


@app.route("/api/timbra/dispositivi", methods=["POST"])
def api_timbra_dispositivi():
    """Gestione telefoni (solo PIN amministratori): elenco, approva, rifiuta, dissocia, attiva tablet condiviso."""
    ip = get_client_ip()
    allowed, retry_after = rate_limiter.is_allowed(f"timbra_disp_{ip}", max_requests=30, window_seconds=60)
    if not allowed:
        return jsonify({"ok": False, "error": "Troppe richieste, attendi un minuto"}), 429
    body = request.get_json(silent=True) or {}
    pin = str(body.get("pin", "")).strip()
    if pin not in _TIMBRA_ADMIN_PINS:
        return jsonify({"ok": False, "error": "Accesso riservato all'amministrazione"}), 403
    azione = body.get("azione", "lista")
    if azione == "lista":
        out = dispositivi.elenco()
        out["questo_condiviso"] = dispositivi.e_condiviso(str(body.get("device_id", "")))
        return jsonify({"ok": True, **out}), 200
    admin = _dip_key(pin)
    try:
        rid = int(body.get("id") or 0)
    except (TypeError, ValueError):
        rid = 0
    res = dispositivi.decidi(azione, rid, admin, str(body.get("device_id", "")), request.headers.get("User-Agent", ""))
    logger.info(f"[DISPOSITIVI] {admin}: {azione} id={rid} → {res}")
    return jsonify(res), (200 if res.get("ok") else 400)


@app.route("/api/timbra/sicurezza", methods=["POST"])
def api_timbra_sicurezza():
    """Registro di sicurezza (disco Render): stato o download CSV. Solo PIN amministratori."""
    ip = get_client_ip()
    allowed, retry_after = rate_limiter.is_allowed(f"timbra_sic_{ip}", max_requests=10, window_seconds=60)
    if not allowed:
        return jsonify({"ok": False, "error": "Troppe richieste, attendi un minuto"}), 429
    body = request.get_json(silent=True) or request.form or {}
    pin = str(body.get("pin", "")).strip()
    if pin not in _TIMBRA_ADMIN_PINS:
        return jsonify({"ok": False, "error": "Accesso riservato all'amministrazione"}), 403
    if body.get("formato") == "csv":
        dal = str(body.get("dal") or "")[:10] or None
        al = str(body.get("al") or "")[:10] or None
        csv_txt = timbra_log.esporta_csv(dal, al)
        nome = f"registro_sicurezza_timbrature_{datetime.now().strftime('%Y-%m-%d')}.csv"
        return Response(csv_txt, mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{nome}"'})
    return jsonify({"ok": True, **timbra_log.stato()}), 200


_PERMESSI_ACTIONS = {
    "lista": ("permessi_lista", ()),
    "richiedi": ("permessi_richiedi", ("tipo", "dal", "al", "ore", "note", "client_id")),
    "annulla": ("permessi_annulla", ("riga", "firma")),
    "admin_lista": ("permessi_admin_lista", ()),
    "decidi": ("permessi_decidi", ("riga", "firma", "esito", "nota")),
}


def _send_permesso_email_smtp(o: dict) -> None:
    """Invia l'email di nuova richiesta direttamente via SMTP autenticato Register.it."""
    try:
        if not o or not o.get("nome"):
            return
        import html as _html
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.utils import formataddr, formatdate, make_msgid

        sender_email = os.getenv("IMAP_USER", "ordini@amrrecchia.it")
        password = os.getenv("IMAP_PASSWORD", "")
        if not sender_email or not password:
            return
        recipients = ["amministrazione@amrrecchia.it", "ordini@amrrecchia.it"]
        nome = str(o.get("nome", ""))
        tipo = str(o.get("tipo", "Permesso"))
        dal = str(o.get("dal", ""))
        al = str(o.get("al") or dal)
        ore = str(o.get("ore") or "giornata intera")
        note = str(o.get("note") or "—")
        app_url = "https://amr-services.onrender.com/timbra"
        periodo = f"dal {dal} al {al}" if (al and al != dal) else f"il {dal}"
        subject = f"Nuova richiesta {tipo.lower()} – {nome} – {periodo}"

        testo = (
            "È stata inserita una nuova richiesta da approvare.\n\n"
            f"Dipendente: {nome}\n"
            f"Tipo: {tipo}\n"
            f"Dal: {dal}\n"
            f"Al: {al}\n"
            f"Ore richieste: {ore}\n"
            f"Note: {note}\n\n"
            f"Per approvare o rifiutare: apri l'app {app_url} con il tuo PIN e vai nella scheda \"Approva\".\n\n"
            "— App Timbrature AMR"
        )
        esc = _html.escape
        html_body = (
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1a1f36">'
            '<p>È stata inserita una <b>nuova richiesta</b> da approvare.</p>'
            '<table style="border-collapse:collapse;margin:10px 0">'
            f'<tr><td style="padding:6px 14px 6px 0;color:#556987">Dipendente</td><td style="padding:6px 0;font-weight:600;color:#001C3A">{esc(nome)}</td></tr>'
            f'<tr><td style="padding:6px 14px 6px 0;color:#556987">Tipo</td><td style="padding:6px 0;font-weight:600;color:#001C3A">{esc(tipo)}</td></tr>'
            f'<tr><td style="padding:6px 14px 6px 0;color:#556987">Dal</td><td style="padding:6px 0;font-weight:600;color:#001C3A">{esc(dal)}</td></tr>'
            f'<tr><td style="padding:6px 14px 6px 0;color:#556987">Al</td><td style="padding:6px 0;font-weight:600;color:#001C3A">{esc(al)}</td></tr>'
            f'<tr><td style="padding:6px 14px 6px 0;color:#556987">Ore richieste</td><td style="padding:6px 0;font-weight:600;color:#001C3A">{esc(ore)}</td></tr>'
            f'<tr><td style="padding:6px 14px 6px 0;color:#556987">Note</td><td style="padding:6px 0;font-weight:600;color:#001C3A">{esc(note)}</td></tr>'
            '</table>'
            f'<p><a href="{app_url}" style="display:inline-block;background:#5B8046;color:#fff;text-decoration:none;padding:10px 18px;border-radius:8px;font-weight:600">Apri l&#39;app e approva</a></p>'
            '<p style="color:#8a94a6;font-size:12px">Entra con il tuo PIN e vai nella scheda "Approva".<br>— App Timbrature AMR</p></div>'
        )
        msg = MIMEMultipart("alternative")
        msg["From"] = formataddr(("Timbrature AMR", sender_email))
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="amrrecchia.it")
        msg.attach(MIMEText(testo, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP_SSL("authsmtp.securemail.pro", 465, timeout=15) as s:
            s.login(sender_email, password)
            s.sendmail(sender_email, recipients, msg.as_string())
        logger.info(f"[PERMESSI] Email nuova richiesta inviata via SMTP per {nome} ({dal})")
    except Exception as e:
        logger.warning(f"[PERMESSI] Errore invio email SMTP nuova richiesta: {e}")


@app.route("/api/timbra/permessi", methods=["POST"])
def api_timbra_permessi():
    """Richieste ferie / permessi dalla app Timbrature (PIN), scritte nel foglio 'Registro Permessi'."""
    ip = get_client_ip()
    allowed, retry_after = rate_limiter.is_allowed(f"timbra_perm_{ip}", max_requests=30, window_seconds=60)
    if not allowed:
        return jsonify({"ok": False, "error": "Troppe richieste, attendi un minuto"}), 429
    body = request.get_json(silent=True) or {}
    pin = str(body.get("pin", "")).strip()
    op = _PERMESSI_ACTIONS.get(str(body.get("azione", "")))
    if not pin.isdigit() or not op:
        return jsonify({"ok": False, "error": "Dati non validi"}), 400
    action, fields = op
    payload = {"action": action, "pin": pin}
    for f in fields:
        v = body.get(f)
        payload[f] = v[:300] if isinstance(v, str) else v
    if action in ("permessi_richiedi", "permessi_annulla", "permessi_decidi"):
        logger.info(f"[PERMESSI] {action} da {ip}")
    data, code = _timbra_call(payload)
    if action == "permessi_richiedi" and code == 200 and data.get("ok") and data.get("richiesta"):
        threading.Thread(target=_send_permesso_email_smtp, args=(data["richiesta"],), daemon=True).start()
    return jsonify(data), code


@app.route("/api/employees", methods=["GET"])
def api_employees():
    """Restituisce l'elenco di tutti i dipendenti aziendali registrati per la selezione rapida con 1 tocco."""
    return jsonify({
        "employees": user_auth.get_all_employees()
    }), 200


@app.route("/api/verify-pin", methods=["GET", "POST"])
@app.route("/api/select-user", methods=["POST"])
def api_verify_pin():
    """Endpoint per selezionare il dipendente o verificare il PIN."""
    data = request.get_json(silent=True) or {}
    name = data.get("name") or request.args.get("name")
    if name:
        u = user_auth.get_user_by_name(name)
        if u:
            return jsonify({
                "valid": True,
                "message": f"Bentornato {u['name']}",
                "user": {
                    "name": u["name"],
                    "email": u["email"],
                    "department": u["department"],
                    "is_commercial": u.get("is_commercial", False),
                    "label": u.get("label", u["department"])
                }
            }), 200

    user = get_current_user_from_request()
    return jsonify({
        "valid": True,
        "message": f"Bentornato {user['name']}",
        "user": {
            "name": user["name"],
            "email": user["email"],
            "department": user["department"],
            "is_commercial": user.get("is_commercial", False),
            "label": user.get("label", user["department"])
        }
    }), 200


@app.route("/api/voice-command", methods=["POST"])
@app.route("/api/voice-command-audio", methods=["POST"])
def api_voice_command():
    """
    Riceve un comando vocale (trascritto via testo, multipart audio file o audio base64),
    trascrive l'audio fedelmente con Gemini Flash (se inviato come audio),
    interpreta l'intento e aggiorna istantaneamente la commessa su Monday.com.
    """
    ip = get_client_ip()
    is_audio = "audio" in request.endpoint or "audio" in request.files or (request.is_json and request.get_json(silent=True) and request.get_json(silent=True).get("audio_base64"))
    max_reqs = 10 if is_audio else 20
    allowed, retry_after = rate_limiter.is_allowed(f"voice_{ip}", max_requests=max_reqs, window_seconds=60)
    if not allowed:
        return jsonify({
            "success": False,
            "message": f"Troppe richieste vocali ravvicinate. Riprova tra {retry_after} secondi (Rate Limit)."
        }), 429

    # Controllo Autenticazione con PIN aziendale
    if not check_pin_auth():
        return jsonify({
            "success": False,
            "message": "Accesso non autorizzato. Inserisci il PIN aziendale per utilizzare l'assistente vocale.",
            "auth_required": True
        }), 401

    text = ""
    original_text = ""
    detected_lang = "it"

    # 1. Se inviato come JSON
    if request.is_json:
        data = request.get_json(silent=True) or {}
        text = data.get("text", "").strip()
        audio_b64 = data.get("audio_base64", "")
        mime_type = data.get("mime_type", "audio/webm")
        if not text and audio_b64:
            try:
                import base64
                raw_bytes = base64.b64decode(audio_b64)
                trans_res = voice_agent.transcribe_audio_with_gemini(raw_bytes, mime_type)
                if isinstance(trans_res, dict):
                    text = trans_res.get("text", "")
                    original_text = trans_res.get("transcription", text)
                    detected_lang = trans_res.get("language", "it")
                else:
                    text = str(trans_res or "").strip()
                    original_text = text
            except Exception as e:
                logger.error(f"Errore decodifica base64 audio: {e}")

    # 2. Se inviato come multipart/form-data con file audio (da MediaRecorder del browser)
    if not text and "audio" in request.files:
        audio_file = request.files["audio"]
        audio_bytes = audio_file.read()
        mime_type = audio_file.content_type or "audio/webm"
        logger.info(f"🎙️ Ricevuto file audio ({len(audio_bytes)} bytes, {mime_type}). Avvio trascrizione con Gemini...")
        
        # Salva ultimo audio per diagnostica
        try:
            with open("/tmp/last_voice_command.audio", "wb") as f:
                f.write(audio_bytes)
        except Exception:
            pass

        trans_res = voice_agent.transcribe_audio_with_gemini(audio_bytes, mime_type)
        if isinstance(trans_res, dict):
            text = trans_res.get("text", "")
            original_text = trans_res.get("transcription", text)
            detected_lang = trans_res.get("language", "it")
        else:
            text = str(trans_res or "").strip()
            original_text = text

    if not text:
        return jsonify({
            "success": False,
            "message": "Nessuna voce comprensibile rilevata nell'audio. Prova a parlare più vicino al microfono."
        }), 200

    # Se testo inviato manualmente e non ancora tradotto:
    if not original_text:
        tr_info = voice_agent.translate_arabic_to_italian_if_needed(text)
        original_text = tr_info.get("transcription", text)
        text = tr_info.get("text", text)
        detected_lang = tr_info.get("language", "it")

    curr_user = get_current_user_from_request()
    user_label = curr_user.get("name") if curr_user else "Anonimo"
    
    req_payload = request.get_json(silent=True) or {} if request.is_json else {}
    target_project_id = req_payload.get("project_id") or request.form.get("project_id")
    if target_project_id:
        target_project_id = str(target_project_id).strip()

    logger.info(f"📝 Testo per Voice Agent (lingua: {detected_lang}, utente: {user_label}, project_id: {target_project_id}): \"{text}\" [Originale: \"{original_text}\"]")
    try:
        result = voice_agent.process_voice_command(
            spoken_text=text,
            original_text=original_text,
            detected_lang=detected_lang,
            current_user=curr_user,
            project_id=target_project_id
        )
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"❌ Errore inatteso process_voice_command: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "transcription": original_text or text,
            "language": detected_lang,
            "message": f"Errore interno elaborazione: {e}"
        }), 200


@app.route("/api/voice-preview", methods=["POST"])
@app.route("/api/voice-preview-audio", methods=["POST"])
def api_voice_preview():
    """
    Riceve l'audio o il testo del comando vocale, effettua la trascrizione con Gemini
    e analizza l'intento/commessa SENZA scrivere o mutare nulla su Monday.com.
    Restituisce l'anteprima di sicurezza all'utente per eventuale conferma, cancellazione o modifica.
    """
    ip = get_client_ip()
    is_audio = "audio" in request.endpoint or "audio" in request.files or (request.is_json and request.get_json(silent=True) and request.get_json(silent=True).get("audio_base64"))
    max_reqs = 15 if is_audio else 25
    allowed, retry_after = rate_limiter.is_allowed(f"voice_prev_{ip}", max_requests=max_reqs, window_seconds=60)
    if not allowed:
        return jsonify({
            "success": False,
            "message": f"Troppe richieste ravvicinate. Riprova tra {retry_after} secondi."
        }), 429

    if not check_pin_auth():
        return jsonify({
            "success": False,
            "message": "Accesso non autorizzato. Inserisci il PIN aziendale.",
            "auth_required": True
        }), 401

    text = ""
    original_text = ""
    detected_lang = "it"

    # 1. Se inviato come JSON
    if request.is_json:
        data = request.get_json(silent=True) or {}
        text = data.get("text", "").strip()
        audio_b64 = data.get("audio_base64", "")
        mime_type = data.get("mime_type", "audio/webm")
        if not text and audio_b64:
            try:
                import base64
                raw_bytes = base64.b64decode(audio_b64)
                trans_res = voice_agent.transcribe_audio_with_gemini(raw_bytes, mime_type)
                if isinstance(trans_res, dict):
                    text = trans_res.get("text", "")
                    original_text = trans_res.get("transcription", text)
                    detected_lang = trans_res.get("language", "it")
                else:
                    text = str(trans_res or "").strip()
                    original_text = text
            except Exception as e:
                logger.error(f"Errore decodifica base64 audio preview: {e}")

    # 2. Se inviato come multipart con file audio
    if not text and "audio" in request.files:
        audio_file = request.files["audio"]
        audio_bytes = audio_file.read()
        mime_type = audio_file.content_type or "audio/webm"
        logger.info(f"🎙️ [PREVIEW] Audio ({len(audio_bytes)} bytes). Avvio trascrizione con Gemini...")

        trans_res = voice_agent.transcribe_audio_with_gemini(audio_bytes, mime_type)
        if isinstance(trans_res, dict):
            text = trans_res.get("text", "")
            original_text = trans_res.get("transcription", text)
            detected_lang = trans_res.get("language", "it")
        else:
            text = str(trans_res or "").strip()
            original_text = text

    if not text:
        return jsonify({
            "success": False,
            "message": "Nessuna voce comprensibile rilevata nell'audio. Prova a parlare più vicino al microfono."
        }), 200

    if not original_text:
        tr_info = voice_agent.translate_arabic_to_italian_if_needed(text)
        original_text = tr_info.get("transcription", text)
        text = tr_info.get("text", text)
        detected_lang = tr_info.get("language", "it")

    curr_user = get_current_user_from_request()
    logger.info(f"🔍 [PREVIEW] Testo: \"{text}\" [Originale: \"{original_text}\"] per {curr_user.get('name') if curr_user else 'Anonimo'}")
    try:
        preview_data = voice_agent.preview_voice_command(
            spoken_text=text,
            original_text=original_text,
            detected_lang=detected_lang,
            current_user=curr_user
        )
        return jsonify(preview_data), 200
    except Exception as e:
        logger.error(f"❌ Errore in preview_voice_command: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "transcription": original_text or text,
            "language": detected_lang,
            "message": f"Errore anteprima: {e}"
        }), 200


@app.route("/api/voice-feedback", methods=["POST"])
def api_voice_feedback():
    """
    Riceve il feedback o la correzione manuale dell'utente su una commessa
    (es. disambiguazione tra più commesse di Henoto o correzione nome).
    Registra l'apprendimento permanente nello store locale per migliorare la precisione futura.
    """
    import voice_learning
    data = request.get_json(silent=True) or {}
    phrase = data.get("phrase", "").strip()
    proj_id = data.get("project_id", "").strip()
    proj_name = data.get("project_name", "").strip()
    board_id = data.get("board_id", "").strip()

    if not phrase or not proj_id:
        return jsonify({"success": False, "message": "Parametri phrase o project_id mancanti"}), 400

    voice_learning.learn_project_alias(phrase, proj_id, proj_name, board_id)
    return jsonify({
        "success": True,
        "message": f"Associazione appresa con successo per '{proj_name}'"
    }), 200


@app.route("/api/dashboard-data", methods=["GET"])
def api_dashboard_data():
    """Restituisce i dati dei progetti da Monday.com in tempo reale (cache 30s) con rate limit e PIN auth."""
    ip = get_client_ip()
    allowed, retry_after = rate_limiter.is_allowed(f"dash_{ip}", max_requests=30, window_seconds=60)
    if not allowed:
        return jsonify({"error": f"Troppe richieste. Riprova tra {retry_after}s.", "rate_limited": True}), 429

    if not check_pin_auth():
        return jsonify({"error": "Accesso non autorizzato. Inserisci il PIN aziendale.", "auth_required": True}), 401

    import time
    import requests

    now = time.time()
    force_fresh = request.args.get("fresh", "").lower() in ("1", "true", "yes")
    if not force_fresh and dashboard_cache["data"] and (now - dashboard_cache["timestamp"] < 30):
        return jsonify({"projects": dashboard_cache["data"], "cached": True})

    token = os.getenv("MONDAY_API_TOKEN")
    query = """
    query {
      boards(ids: ["1865197409"]) {
        items_page(limit: 100) {
          items {
            id
            name
            state
            column_values {
              id
              text
            }
          }
        }
      }
    }
    """
    try:
        resp = requests.post(
            "https://api.monday.com/v2",
            json={"query": query},
            headers={"Authorization": token, "API-Version": "2024-10"},
            timeout=15,
        )
        items = resp.json().get("data", {}).get("boards", [{}])[0].get("items_page", {}).get("items", [])
        clean_projects = []
        for it in items:
            if it.get("state") != "active":
                continue
            cols = {cv.get("id"): cv.get("text") for cv in it.get("column_values", []) if cv.get("text")}
            clean_projects.append({
                "id": it["id"],
                "name": it["name"],
                "commessa": cols.get("testo_mkmnxqsk") or cols.get("text_mm51yk45", "COMM-" + it["id"]),
                "progetto": cols.get("lookup_mknz7srg") or cols.get("testo_mkn1sqb4", ""),
                "stato": cols.get("project_status") or cols.get("color_mm45raj9", "in produzione"),
                "consegna": cols.get("lookup_mknznap") or cols.get("date4", ""),
                "priorita": cols.get("lookup_mknzrga7") or cols.get("color_mknssm0t", "Normale"),
                "taglio": cols.get("lookup_mknzagn5") == "SI" or cols.get("color_mkns43r0") == "SI",
                "fresa": cols.get("lookup_mknzqq5j") == "SI" or cols.get("color_mknsezab") == "SI",
                "finitura": cols.get("lookup_mknz1bbx") == "SI" or cols.get("color_mknsghqz") == "SI",
                "esterna": False,
                "referente": cols.get("lookup_mknz8gq2") or cols.get("dup__of_nome_referente_mkn3gf63", ""),
                "drive_link": cols.get("file_mknpcqbv") or cols.get("link_mm45entc", "")
            })
        dashboard_cache["data"] = clean_projects
        dashboard_cache["timestamp"] = now
        return jsonify({"projects": clean_projects, "cached": False})
    except Exception as e:
        logger.error(f"Errore recupero dati dashboard: {e}")
        if dashboard_cache["data"]:
            return jsonify({"projects": dashboard_cache["data"], "cached": True, "error": str(e)})
        return jsonify({"projects": [], "error": str(e)}), 500


@app.route("/status", methods=["GET"])
def status():
    """Stato dettagliato dell'ultima archiviazione protetto da PIN o admin secret."""
    if not check_pin_auth():
        admin_secret = request.headers.get("X-Admin-Secret") or request.args.get("secret")
        if not _admin_secret_ok(admin_secret):
            return jsonify({"error": "Unauthorized"}), 401
    return jsonify(last_status), 200


@app.route("/webhook/status-change", methods=["POST"])
@app.route("/monday/status-change", methods=["POST"])
def webhook_status_change():
    """
    Webhook ricezione cambio stato step di reparto da Monday.com.
    Traccia automaticamente l'orario di inizio ('In svolgimento') e
    calcola la durata lavorativa reale feriale (max 8h/giorno) al completamento ('Fatto').
    """
    data = request.get_json(silent=True) or {}

    # Risposta alla verifica webhook di Monday.com (challenge handshake)
    if "challenge" in data:
        logger.info(f"🤝 Handshake Monday webhook ricevuto con challenge: {data['challenge'][:15]}...")
        return jsonify({"challenge": data["challenge"]}), 200

    event = data.get("event", {})
    if not event:
        return jsonify({"status": "ignored", "reason": "No event payload"}), 200

    board_id = str(event.get("boardId", ""))
    item_id = str(event.get("pulseId", ""))
    column_id = str(event.get("columnId", ""))
    
    val = event.get("value", {})
    label = ""
    if isinstance(val, dict):
        label = val.get("label", {}).get("text", "")
        if not label:
            label = val.get("text", "")

    logger.info(f"📥 Ricevuto cambio stato Monday: Board {board_id}, Item #{item_id}, Colonna {column_id} -> '{label}'")
    
    # 1. Se l'evento proviene da GESTIONE PROGETTI NEW (2136092569) o NEW COMMERCIALE (2133436509) -> Inoltro a schede Reparto (Taglio / Finiture / Progettazione)
    if board_id in ("2136092569", "2133436509"):
        logger.info(f"⚙️ Evento da Board {board_id}: avvio sincronizzazione con reparti per #{item_id}...")
        dept_res = department_syncer.sync_project_to_departments(item_id)
        return jsonify({"status": "processed", "type": "department_sync", "result": dept_res}), 200

    # 2. Se l'evento proviene da TAGLIO E FRESA (5086546323) o FINITURE (5088215890) -> Tracciamento Tempo Lavorativo
    res = step_time_tracker.handle_status_change(board_id, item_id, column_id, label)
    return jsonify({"status": "processed", "type": "step_time_tracking", "result": res}), 200


@app.route("/api/night-guardian/status", methods=["GET"])
@app.route("/api/night-guardian", methods=["GET", "POST"])
def night_guardian_status():
    """Restituisce lo stato del Guardiano Notturno e gli step attualmente in svolgimento."""
    ip = get_client_ip()
    allowed, retry_after = rate_limiter.is_allowed(f"guard_{ip}", max_requests=30, window_seconds=60)
    if not allowed:
        return jsonify({"error": f"Rate limit superato. Riprova tra {retry_after}s."}), 429

    if not check_pin_auth():
        return jsonify({"error": "Accesso non autorizzato. Inserisci il PIN aziendale.", "auth_required": True}), 401

    is_off_hours = step_time_tracker.is_night_or_weekend()
    active_steps = step_time_tracker.get_active_tracked_steps()
    return jsonify({
        "night_guardian_active": is_off_hours,
        "mode": "FROZEN_NIGHT_WEEKEND" if is_off_hours else "ACTIVE_WORK_HOURS",
        "description": (
            "🌙 Guardiano Notturno ATTIVO: timer congelati, notti e weekend esclusi dal conteggio ore"
            if is_off_hours else
            "☀️ Orario Lavorativo ATTIVO: conteggio attivo su finestra 08:30-12:30 e 13:30-17:30 (max 8h/giorno)"
        ),
        "active_tracked_steps_count": len(active_steps),
        "active_steps": active_steps
    }), 200


@app.route("/test/<item_id>", methods=["GET", "POST"])
def test_archive(item_id: str):
    """Endpoint di test per archiviare manualmente un item (protetto da admin secret)."""
    admin_secret = request.headers.get("X-Admin-Secret") or request.args.get("secret")
    if not _admin_secret_ok(admin_secret):
        return jsonify({"error": "Unauthorized"}), 403

    if last_status["running"]:
        return jsonify({"status": "busy"}), 200
    thread = threading.Thread(
        target=run_archive_in_background,
        args=(item_id,),
        daemon=True,
    )
    thread.start()
    return jsonify({"status": "started", "item_id": item_id}), 200



def start_email_agent_loop():
    """Loop in background per controllare le email ogni 5 minuti."""
    import time
    from agent import run_once
    from db import init_db
    
    logger.info("🤖 [CLOUD AGENT] Avvio thread demone agente email in background...")
    try:
        init_db()
    except Exception as e:
        logger.error(f"❌ Errore inizializzazione DB nel thread email: {e}")
        
    check_interval = int(os.getenv("CHECK_INTERVAL_MINUTES", "5")) * 60
    
    # Ritardo iniziale di 15 secondi per dare il tempo a gunicorn/Flask di avviarsi completamente
    time.sleep(15)
    
    while True:
        try:
            logger.info("🤖 [CLOUD AGENT] Scansione email in corso...")
            run_once(dry_run=False)
            logger.info("🤖 [CLOUD AGENT] Scansione email terminata con successo.")
        except Exception as e:
            logger.error(f"❌ [CLOUD AGENT] Errore critico nel loop dell'agente email: {e}", exc_info=True)
        
        logger.info(f"⏳ [CLOUD AGENT] Prossimo controllo tra {check_interval // 60} minuti...")
        time.sleep(check_interval)


# Avvio del thread dell'agente email in background
email_thread = threading.Thread(
    target=start_email_agent_loop,
    daemon=True,
    name="email-agent-loop"
)
email_thread.start()


def start_board_sync_loop():
    """Loop continuo in background per mantenere perfettamente sincronizzate le board Monday.com."""
    logger.warning("⛔ [SYNC GUARDIAN] Sincronizzazione tra vecchie e nuove schede DISABILITATA.")
    return


def start_department_sync_loop():
    """
    Loop periodico di sicurezza (ogni 10 minuti) per inoltrare automaticamente
    tutti i progetti da GESTIONE PROGETTI NEW verso
    i reparti TAGLIO E FRESA, FINITURE e PROGETTAZIONE NEW.
    NON tocca MAI le vecchie board.
    """
    import time
    from department_syncer import sync_all_active_departments

    logger.info("🔄 [DEPT GUARDIAN] Avvio thread periodico inoltro reparti (ogni 10 min)...")
    time.sleep(45)

    while True:
        try:
            logger.info("🔄 [DEPT GUARDIAN] Controllo progetti assegnati ai reparti...")
            res = sync_all_active_departments()
            logger.info(f"🔄 [DEPT GUARDIAN] Inoltro reparti completato: {res}")
        except Exception as e:
            logger.error(f"❌ [DEPT GUARDIAN] Errore ciclo reparti: {e}", exc_info=True)

        time.sleep(600)


dept_thread = threading.Thread(
    target=start_department_sync_loop,
    daemon=True,
    name="dept-sync-loop"
)
dept_thread.start()


def start_timbra_localita_loop():
    """Compila la colonna 'Indirizzo / Luogo' (nome località) anche per le timbrature fatte
    da AppSheet: le sue scritture non attivano i trigger onEdit di Apps Script."""
    import requests as _rq
    time.sleep(90)
    while True:
        from zoneinfo import ZoneInfo
        ora = datetime.now(ZoneInfo("Europe/Rome")).hour
        if 5 <= ora <= 22:
            try:
                r = _rq.post(AMR_TIMBRA_URL, json={"key": AMR_TIMBRA_KEY, "action": "arricchisci", "righe": 80}, timeout=120)
                res = r.json()
                if res.get("compilate"):
                    logger.info(f"📍 [TIMBRA] Località compilate: {res.get('compilate')}")
            except Exception as e:
                logger.warning(f"📍 [TIMBRA] Arricchimento località non riuscito: {e}")
        time.sleep(600)


timbra_loc_thread = threading.Thread(
    target=start_timbra_localita_loop,
    daemon=True,
    name="timbra-localita-loop"
)
timbra_loc_thread.start()


if __name__ == "__main__":
    port = int(os.getenv("WEBHOOK_PORT", 8080))
    logger.info("=" * 55)
    logger.info("  🚀 AMR Drive Archiver — Webhook Server")
    logger.info(f"  📡 In ascolto su: http://localhost:{port}")
    logger.info(f"  📋 Endpoint webhook: POST /webhook")
    logger.info(f"  🏥 Health check:    GET  /health")
    logger.info(f"  📊 Status:          GET  /status")
    logger.info(f"  🔧 Test manuale:    GET  /test/<item_id>")
    logger.info("=" * 55)
    logger.info("")
    logger.info("⚠️  Per esporre il server a Monday.com, usa ngrok:")
    logger.info(f"   ngrok http {port}")
    logger.info("   Poi copia l'URL https:// nel webhook di Monday.com")
    logger.info("")
    app.run(host="0.0.0.0", port=port, debug=False)
