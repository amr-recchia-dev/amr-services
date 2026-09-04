#!/usr/bin/env python3
"""
webhook_server.py - Server Flask che riceve webhook da Monday.com
Quando "Avvia Esportazione" viene cliccato, archivia il progetto su Google Drive.
"""

import os
import json
import logging
import threading
from datetime import datetime
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv
import drive_archiver
import step_time_tracker
import business_time
import department_syncer
import voice_agent

load_dotenv()

# === Setup logging (compatibile con Railway: solo stdout se in cloud) ===
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


@app.route("/api/voice-command", methods=["POST"])
@app.route("/api/voice-command-audio", methods=["POST"])
def api_voice_command():
    """
    Riceve un comando vocale (trascritto via testo, multipart audio file o audio base64),
    trascrive l'audio fedelmente con Gemini Flash (se inviato come audio),
    interpreta l'intento e aggiorna istantaneamente la commessa su Monday.com.
    """
    text = ""

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
                text = voice_agent.transcribe_audio_with_gemini(raw_bytes, mime_type)
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

        text = voice_agent.transcribe_audio_with_gemini(audio_bytes, mime_type)

    if not text:
        return jsonify({
            "success": False,
            "message": "Nessuna voce comprensibile rilevata nell'audio. Prova a parlare più vicino al microfono."
        }), 200

    logger.info(f"📝 Testo finale per Voice Agent: \"{text}\"")
    try:
        result = voice_agent.process_voice_command(text)
        result["transcription"] = text
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"❌ Errore inatteso process_voice_command: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "transcription": text,
            "message": f"Errore interno elaborazione: {e}"
        }), 200




@app.route("/api/dashboard-data", methods=["GET"])
def api_dashboard_data():
    """Restituisce i dati dei progetti da Monday.com in tempo reale (cache 30s)."""
    import time
    import requests

    now = time.time()
    if dashboard_cache["data"] and (now - dashboard_cache["timestamp"] < 30):
        return jsonify({"projects": dashboard_cache["data"], "cached": True})

    token = os.getenv("MONDAY_API_TOKEN")
    query = """
    query {
      boards(ids: ["2136092569"]) {
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
            if it.get("state") == "deleted":
                continue
            cols = {cv.get("id"): cv.get("text") for cv in it.get("column_values", []) if cv.get("text")}
            clean_projects.append({
                "id": it["id"],
                "name": it["name"],
                "commessa": cols.get("text_mm51yk45", "COMM-" + it["id"]),
                "progetto": cols.get("testo_mkn1sqb4", ""),
                "stato": cols.get("color_mm45raj9", "Da iniziare"),
                "consegna": cols.get("date4", ""),
                "priorita": cols.get("color_mknssm0t", "Normale"),
                "taglio": cols.get("color_mkns43r0") == "SI",
                "fresa": cols.get("color_mknsezab") == "SI",
                "finitura": cols.get("color_mknsghqz") == "SI",
                "esterna": cols.get("color_mkyn8z12") == "SI",
                "referente": cols.get("dup__of_nome_referente_mkn3gf63", ""),
                "drive_link": cols.get("link_mm45entc", "")
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
    """Stato dettagliato dell'ultima archiviazione."""
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
    
    # 1. Se l'evento proviene da GESTIONE PROGETTI NEW (2136092569) -> Inoltro a schede Reparto (Taglio / Finiture)
    if board_id == "2136092569":
        logger.info(f"⚙️ Evento da GESTIONE PROGETTI NEW: avvio sincronizzazione con reparti per #{item_id}...")
        dept_res = department_syncer.sync_project_to_departments(item_id)
        return jsonify({"status": "processed", "type": "department_sync", "result": dept_res}), 200

    # 2. Se l'evento proviene da TAGLIO E FRESA (5086546323) o FINITURE (5088215890) -> Tracciamento Tempo Lavorativo
    res = step_time_tracker.handle_status_change(board_id, item_id, column_id, label)
    return jsonify({"status": "processed", "type": "step_time_tracking", "result": res}), 200


@app.route("/api/night-guardian/status", methods=["GET"])
@app.route("/api/night-guardian", methods=["GET", "POST"])
def night_guardian_status():
    """Restituisce lo stato del Guardiano Notturno e gli step attualmente in svolgimento."""
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


@app.route("/test/<item_id>", methods=["GET"])
def test_archive(item_id: str):
    """Endpoint di test per archiviare manualmente un item."""
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
