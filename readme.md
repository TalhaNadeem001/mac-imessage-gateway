# MAC Server Imessage Gateway

**Features:**
- Send iMessages via REST API.
- Forward inbound messages to a webhook (e.g., ngrok endpoint).
- Automatic FaceTime call detection and Messages restart.
- Lightweight FastAPI server with async support.

---

## Requirements

- macOS
- Python 3.10+
- `imessage_monitor` library
- Python packages:
  - `fastapi`
  - `uvicorn`
  - `httpx`
  - `pydantic`

Install dependencies:

```bash
pip install fastapi uvicorn httpx pydantic imessage_monitor
