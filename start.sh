#!/bin/bash

VENV_DIR=".venv"
CHECKSUM_FILE="$VENV_DIR/req_checksum"

# 1. Load from .env if it exists
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

# 2. Set DEFAULTS if variables are not already set
export IMESSAGE_HOST=${IMESSAGE_HOST:-127.0.0.1}
export IMESSAGE_PORT=${IMESSAGE_PORT:-8000}

# 3. Create .venv if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

# 4. Activate and check requirements
source "$VENV_DIR/bin/activate"

if [ -f "requirements.txt" ]; then
    CURRENT_MD5=$(md5 -q requirements.txt)
    
    if [ ! -f "$CHECKSUM_FILE" ] || [ "$CURRENT_MD5" != "$(cat "$CHECKSUM_FILE")" ]; then
        echo "Updating requirements..."
        pip install --upgrade pip
        pip install -r requirements.txt
        echo "$CURRENT_MD5" > "$CHECKSUM_FILE"
    else
        echo "Requirements are up to date."
    fi
fi

# 5. Run the app
echo "Launching app:app at $IMESSAGE_HOST:$IMESSAGE_PORT..."
python3 -m uvicorn app:app --host "$IMESSAGE_HOST" --port "$IMESSAGE_PORT" --reload
