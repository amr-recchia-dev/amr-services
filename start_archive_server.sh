#!/bin/bash
# start_archive_server.sh - Avvia il server webhook per l'archiviazione Drive

PROJ_DIR="/Users/shallo/Documents/Antigravity/email-agent"
VENV_PY="${PROJ_DIR}/.venv/bin/python3"
SERVER_SCRIPT="${PROJ_DIR}/webhook_server.py"
LOG_FILE="${PROJ_DIR}/webhook.log"
PID_FILE="${PROJ_DIR}/webhook.pid"
PORT=8080

echo "======================================================"
echo "  🚀 AMR Drive Archiver — Avvio Server Webhook"
echo "======================================================"

# Controlla se Flask è installato
if ! ${VENV_PY} -c "import flask" 2>/dev/null; then
    echo "⏳ Installo Flask..."
    ${PROJ_DIR}/.venv/bin/pip install flask --quiet
    echo "✅ Flask installato"
fi

# Controlla se il token Drive è presente
if [ ! -f "${PROJ_DIR}/drive_token.json" ]; then
    echo ""
    echo "⚠️  Token Google Drive non trovato!"
    echo "   Esegui prima: ${VENV_PY} ${PROJ_DIR}/setup_drive_oauth.py"
    echo ""
    read -p "Vuoi eseguire l'autenticazione adesso? (s/n): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Ss]$ ]]; then
        ${VENV_PY} ${PROJ_DIR}/setup_drive_oauth.py
    else
        echo "ℹ️  Avvio comunque il server (autenticazione richiesta al primo utilizzo)"
    fi
fi

# Ferma il server esistente se in esecuzione
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if ps -p "$OLD_PID" > /dev/null 2>&1; then
        echo "🛑 Fermo server precedente (PID: $OLD_PID)..."
        kill "$OLD_PID"
        sleep 1
    fi
    rm -f "$PID_FILE"
fi

# Avvia il server in background
echo ""
echo "▶️  Avvio webhook server sulla porta $PORT..."
${VENV_PY} ${SERVER_SCRIPT} >> ${LOG_FILE} 2>&1 &
SERVER_PID=$!
echo $SERVER_PID > "$PID_FILE"
sleep 2

# Verifica che il server sia partito
if ps -p "$SERVER_PID" > /dev/null 2>&1; then
    echo "✅ Server avviato (PID: $SERVER_PID)"
    echo ""
    echo "======================================================"
    echo "  📡 Server attivo su: http://localhost:${PORT}"
    echo "  🏥 Health:  http://localhost:${PORT}/health"
    echo "  📊 Status:  http://localhost:${PORT}/status"
    echo "======================================================"
    echo ""
    echo "⚠️  PROSSIMO STEP — Esponi il server con ngrok:"
    echo ""
    echo "   1. Installa ngrok: https://ngrok.com/download"
    echo "   2. Esegui: ngrok http ${PORT}"
    echo "   3. Copia l'URL https (es: https://abc123.ngrok.io)"
    echo "   4. Vai su Monday.com → GESTIONE PROGETTI NEW"
    echo "      → Automatizza → Gestisci → Modifica automazione"
    echo "      → 'Avvia Esportazione' → incolla l'URL + /webhook"
    echo ""
    echo "   URL webhook completo: https://TUO-NGROK-URL/webhook"
    echo ""
    echo "📄 Log in tempo reale: tail -f ${LOG_FILE}"
else
    echo "❌ Server non avviato correttamente. Controlla il log:"
    tail -20 "$LOG_FILE"
fi
