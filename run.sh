#!/usr/bin/env bash
# =============================================================
# run.sh — Setup e avvio Agente Email → Monday.com AMR Recchia
# =============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
ENV_FILE="$SCRIPT_DIR/.env"

echo "═══════════════════════════════════════════════════════"
echo "  🤖 Agente Email → Monday.com  |  AMR Recchia"
echo "═══════════════════════════════════════════════════════"

# ── 1. Controlla Python ──────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "❌ Python3 non trovato. Installa Python 3.11+ da python.org"
    exit 1
fi
PYTHON_VERSION=$(python3 --version 2>&1)
echo "✅ Python: $PYTHON_VERSION"

# ── 2. Crea virtual environment ──────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    echo "📦 Creazione virtual environment..."
    python3 -m venv "$VENV_DIR"
    echo "✅ Virtual environment creato."
fi

# Attiva venv
source "$VENV_DIR/bin/activate"

# ── 3. Installa dipendenze ────────────────────────────────────
echo "📥 Installazione dipendenze..."
pip install --quiet --upgrade pip
pip install --quiet -r "$SCRIPT_DIR/requirements.txt"
echo "✅ Dipendenze installate."

# ── 4. Controlla .env ────────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
    echo ""
    echo "⚠️  File .env non trovato!"
    echo "   Copia .env.example in .env e compila le credenziali:"
    echo "   cp $SCRIPT_DIR/.env.example $ENV_FILE"
    echo "   nano $ENV_FILE"
    echo ""
    exit 1
fi

# Controlla variabili obbligatorie
source "$ENV_FILE" 2>/dev/null || true
MISSING=""
[ -z "$IMAP_PASSWORD" ]       && MISSING="$MISSING IMAP_PASSWORD"
[ -z "$MONDAY_API_TOKEN" ]    && MISSING="$MISSING MONDAY_API_TOKEN"
[ -z "$GEMINI_API_KEY" ]      && MISSING="$MISSING GEMINI_API_KEY"

if [ -n "$MISSING" ]; then
    echo ""
    echo "❌ Variabili .env mancanti:$MISSING"
    echo "   Modifica il file .env:"
    echo "   nano $ENV_FILE"
    echo ""
    exit 1
fi
echo "✅ Configurazione .env verificata."

# ── 5. Setup colonne board Monday.com (prima volta) ──────────
if [ "$1" == "--setup-board" ]; then
    echo ""
    echo "🔧 Configurazione colonne board Monday.com..."
    python3 "$SCRIPT_DIR/agent.py" --setup-board
    echo ""
fi

# ── 6. Esegui l'agente ───────────────────────────────────────
echo ""
echo "Modalità disponibili:"
echo "  Singola esecuzione: bash run.sh"
echo "  Loop daemon:        bash run.sh --daemon"
echo "  Test (dry-run):     bash run.sh --test"
echo "  Setup board:        bash run.sh --setup-board"
echo "  Statistiche DB:     bash run.sh --stats"
echo ""

if [ "$1" == "--cron" ]; then
    # ── Setup cron job ───────────────────────────────────────
    CRON_CMD="*/5 * * * * source $VENV_DIR/bin/activate && python3 $SCRIPT_DIR/agent.py >> $SCRIPT_DIR/agent.log 2>&1"
    echo "🕐 Configurazione cron job (ogni 5 minuti)..."
    (crontab -l 2>/dev/null | grep -v "email-agent"; echo "$CRON_CMD") | crontab -
    echo "✅ Cron job configurato!"
    echo ""
    crontab -l | grep "email-agent" || echo "(verificare con: crontab -l)"
    echo ""
    echo "Per rimuovere il cron job:"
    echo "  crontab -e  (rimuovi la riga con email-agent/agent.py)"

elif [ "$1" == "--remove-cron" ]; then
    echo "🗑️  Rimozione cron job..."
    (crontab -l 2>/dev/null | grep -v "email-agent") | crontab -
    echo "✅ Cron job rimosso."

elif [ "$1" == "--daemon" ]; then
    echo "🚀 Avvio in modalità DAEMON..."
    python3 "$SCRIPT_DIR/agent.py" --daemon

elif [ "$1" == "--test" ]; then
    echo "🧪 Avvio in modalità TEST (dry-run)..."
    python3 "$SCRIPT_DIR/agent.py" --test

elif [ "$1" == "--stats" ]; then
    python3 "$SCRIPT_DIR/agent.py" --stats

elif [ -z "$1" ] || [ "$1" == "--setup-board" ]; then
    echo "🚀 Avvio esecuzione singola..."
    python3 "$SCRIPT_DIR/agent.py"

else
    echo "❌ Argomento non riconosciuto: $1"
    echo "   Usa: bash run.sh [--daemon|--test|--cron|--remove-cron|--setup-board|--stats]"
    exit 1
fi
