#!/bin/bash

set -e

SERVICE_NAME="com.macdev2.imessage-gateway"
PROJECT_NAME="mac-imessage-gateway"

USER_NAME=$(whoami)
HOME_DIR="$HOME"
CURRENT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICES_DIR="$HOME_DIR/services"
TARGET_DIR="$SERVICES_DIR/$PROJECT_NAME"
PLIST_DIR="$HOME_DIR/Library/LaunchAgents"
PLIST_FILE="$PLIST_DIR/$SERVICE_NAME.plist"

echo "== macOS FastAPI Service Installer =="
echo "User: $USER_NAME"
echo "Current directory: $CURRENT_DIR"
echo

# Check if we're inside a protected folder
if [[ "$CURRENT_DIR" == "$HOME_DIR/Documents"* ]] || \
   [[ "$CURRENT_DIR" == "$HOME_DIR/Desktop"* ]] || \
   [[ "$CURRENT_DIR" == "$HOME_DIR/Downloads"* ]]; then

    echo "Project is inside a protected folder."
    echo "Moving to: $TARGET_DIR"

    mkdir -p "$SERVICES_DIR"

    if [ -d "$TARGET_DIR" ]; then
        echo "Removing existing target directory..."
        rm -rf "$TARGET_DIR"
    fi

    cp -R "$CURRENT_DIR" "$TARGET_DIR"

    PROJECT_DIR="$TARGET_DIR"

    echo
    echo "Project copied successfully."
    echo "Please run this installer again from:"
    echo
    echo "  cd \"$TARGET_DIR\""
    echo "  ./install_service.sh"
    echo
    exit 0
else
    PROJECT_DIR="$CURRENT_DIR"
fi

echo "Using project directory:"
echo "  $PROJECT_DIR"
echo

# Check start.sh
if [ ! -f "$PROJECT_DIR/start.sh" ]; then
    echo "ERROR: start.sh not found"
    exit 1
fi

chmod +x "$PROJECT_DIR/start.sh"

mkdir -p "$PLIST_DIR"

cat > "$PLIST_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">

<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$SERVICE_NAME</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PROJECT_DIR/start.sh</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>StandardOutPath</key>
    <string>/tmp/imessage-gateway.log</string>

    <key>StandardErrorPath</key>
    <string>/tmp/imessage-gateway-error.log</string>
</dict>
</plist>
EOF

echo "LaunchAgent created:"
echo "  $PLIST_FILE"
echo

# Unload old service if loaded
launchctl bootout "gui/$(id -u)" "$PLIST_FILE" 2>/dev/null || true

# Load new service
launchctl bootstrap "gui/$(id -u)" "$PLIST_FILE"
launchctl enable "gui/$(id -u)/$SERVICE_NAME"
launchctl kickstart -k "gui/$(id -u)/$SERVICE_NAME"

sleep 2

echo
echo "=== Service Status ==="
launchctl print "gui/$(id -u)/$SERVICE_NAME" | grep -E "state|pid|last exit" || true

echo
echo "=== Done ==="
echo "Service: $SERVICE_NAME"
echo "Project: $PROJECT_DIR"
echo
echo "View logs:"
echo "  tail -f /tmp/imessage-gateway.log"
echo "  tail -f /tmp/imessage-gateway-error.log"
echo
echo "Restart service:"
echo "  launchctl kickstart -k gui/\$(id -u)/$SERVICE_NAME"
echo
echo "Check status:"
echo "  launchctl print gui/\$(id -u)/$SERVICE_NAME"