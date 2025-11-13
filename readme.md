iMessage HTTP API

A simple HTTP API to send iMessages on macOS using the imessage_monitor library.
Includes:

Sending iMessages via HTTP POST requests.

Forwarding incoming iMessages to a webhook.

Automatic FaceTime call detection and Messages restart.

Features

Send iMessages through a REST API.

Forward inbound messages to an external endpoint (e.g., ngrok URL).

Auto-decline FaceTime calls and send a pre-defined message.

Lightweight FastAPI server, fully async-friendly.

Requirements

macOS with imessage_monitor installed.

Python 3.10+.

FastAPI, uvicorn, httpx, pydantic.

Optional: ngrok for testing inbound message forwarding.

Install dependencies:

pip install fastapi uvicorn httpx pydantic imessage_monitor

Environment Variables
Variable Description
IMESSAGE_API_KEY API key for authorization (used in Authorization: Bearer <API_KEY> header)
IMESSAGE_HOST Host to bind the server to (default: 127.0.0.1)
IMESSAGE_PORT Port to run the server on (default: 8000)
API Usage
Send iMessage

POST /send

Headers:

Authorization: Bearer <API_KEY>
Content-Type: application/json

Body:

{
"to": "+1234567890",
"message": "Hello from API!"
}

Response:

{
"status": "ok",
"to": "+1234567890"
}

Running the Server
IMESSAGE_API_KEY=yourkey python3 app.py

The API will start at:

http://127.0.0.1:8000

Inbound Message Forwarding

Inbound messages are automatically forwarded to the URL defined in the code:

"https://cc839cc1352c.ngrok-free.app/sms/reply"

You can replace this with your own endpoint.

FaceTime Auto-Decline

Watches macOS logs for incoming FaceTime notifications.

Automatically restarts Messages to “decline” calls.

Sends a pre-defined message to a specific number.

Notes

Only works on macOS.

Ensure IMESSAGE_API_KEY is set; default "changeme" is unsafe.

Run with proper permissions; FaceTime and Messages must be allowed to be controlled via AppleScript.

For production, consider using launchd or systemd for reliable server startup.
