#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

VENV_DIR=".venv"
CHECKSUM_FILE="$VENV_DIR/req_checksum"
TEST_PID=""

cleanup() {
    if [ -n "$TEST_PID" ] && kill -0 "$TEST_PID" 2>/dev/null; then
        echo "Stopping test harness (pid $TEST_PID)..."
        kill "$TEST_PID" 2>/dev/null || true
        wait "$TEST_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# 1. Load dev.env (then .env overrides if present)
if [ -f dev.env ]; then
    set -a
    # shellcheck disable=SC1091
    source dev.env
    set +a
fi
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

# 2. Defaults
export IMESSAGE_HOST=${IMESSAGE_HOST:-127.0.0.1}
export IMESSAGE_PORT=${IMESSAGE_PORT:-8000}
export TEST_HOST=${TEST_HOST:-127.0.0.1}
export TEST_PORT=${TEST_PORT:-9000}
export FWD_URL=${FWD_URL:-http://${TEST_HOST}:${TEST_PORT}/webhook}
export LOG_LEVEL=${LOG_LEVEL:-DEBUG}

# 3. Virtualenv
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

if [ -f "requirements.txt" ]; then
    if command -v md5 >/dev/null 2>&1; then
        CURRENT_MD5=$(md5 -q requirements.txt 2>/dev/null || md5sum requirements.txt | awk '{print $1}')
    else
        CURRENT_MD5=$(md5sum requirements.txt | awk '{print $1}')
    fi

    if [ ! -f "$CHECKSUM_FILE" ] || [ "$CURRENT_MD5" != "$(cat "$CHECKSUM_FILE")" ]; then
        echo "Updating requirements..."
        pip install --upgrade pip
        pip install -r requirements.txt
        echo "$CURRENT_MD5" > "$CHECKSUM_FILE"
    else
        echo "Requirements are up to date."
    fi
fi

mkdir -p db

# 4. Test harness (FWD_URL target)
echo "Starting test harness at http://${TEST_HOST}:${TEST_PORT} (webhook: ${FWD_URL})..."
python3 test/server.py &
TEST_PID=$!
sleep 1

if ! kill -0 "$TEST_PID" 2>/dev/null; then
    echo "Test harness failed to start." >&2
    exit 1
fi

# 5. Gateway
echo "Starting gateway at http://${IMESSAGE_HOST}:${IMESSAGE_PORT}..."
echo "Open test UI: http://${TEST_HOST}:${TEST_PORT}"
python3 -m uvicorn app:app \
    --host "$IMESSAGE_HOST" \
    --port "$IMESSAGE_PORT" \
    --reload
