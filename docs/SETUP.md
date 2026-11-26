# ORBION — Setup

> Full walkthrough for getting ORBION running on a small self-hosted server
> (a free-tier ARM VM is enough). No GPU required.

## 0. Prerequisites

- A Linux server (tested on Ubuntu 22.04/24.04, ARM64 and x86_64)
- Python 3.10+
- ~2 GB RAM minimum (4 GB+ recommended if you also run a local model)
- Optional: a Cloudflare account (for a public panel via Tunnel, free)

## 1. Install

```bash
git clone https://github.com/erdenyagici-cmd/ORBION.git
cd ORBION
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## 2. Configure

```bash
cp .env.example .env
chmod 600 .env
$EDITOR .env     # fill in tokens & provider keys
```

Minimum to boot:
- `PANEL_PASSWORD` + `PANEL_SESSION_KEY` (random strings)
- At least one LLM provider (`GEMINI_API_KEY` *or* `GROQ_API_KEY` *or* a local Ollama)
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` if you want the Telegram interface

The panel and protected endpoints are **fail-closed**: an unset token disables the
endpoint rather than opening it.

## 3. Run (foreground, to test)

```bash
python3 scripts/agent.py
```

Open `http://127.0.0.1:8090/operations` and log in with `PANEL_PASSWORD`.

## 4. Run as a service (systemd)

```ini
# /etc/systemd/system/orbion.service
[Unit]
Description=ORBION — self-hosted AI office
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=orbion
WorkingDirectory=/home/orbion/ORBION
ExecStart=/home/orbion/ORBION/.venv/bin/python scripts/agent.py
Restart=always
RestartSec=5
# Keep the core from starving other services when a local model runs:
CPUQuota=300%

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now orbion
sudo systemctl status orbion
```

## 5. Public panel via Cloudflare Tunnel (optional, free)

```bash
cloudflared tunnel login
cloudflared tunnel create orbion
cloudflared tunnel route dns orbion panel.example.com
# point the tunnel at http://127.0.0.1:8090
cloudflared tunnel run orbion
```

Set `PANEL_ALLOWED_ORIGINS=https://panel.example.com` so CORS is locked to your origin.

## 6. Telegram bot

1. Talk to `@BotFather` → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Send your bot a message, then set `TELEGRAM_CHAT_ID` to your numeric chat id.
   **The bot will not start with an empty `TELEGRAM_CHAT_ID`** (no first-come-first-served
   ownership — this is a deliberate security choice).

## 7. Local model (optional)

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:4b        # small & fast on CPU; 7B/14B if you have RAM
```

Set `OLLAMA_MODEL` accordingly. ORBION treats local Ollama as the *last* fallback and
**never** routes sensitive tasks to it.

## 8. Wear OS companion (optional)

See `wear/` for the Android project. You'll need Android Studio or the command-line
build tools. The watch talks to the server through a **dedicated, short-lived device
token** (`HEALTH_INGEST_TOKEN`), never the panel password.

## 9. Security checklist before exposing to the internet

- [ ] `PANEL_PASSWORD` and `PANEL_SESSION_KEY` are long and random
- [ ] `PANEL_ALLOWED_ORIGINS` set to your exact panel origin (no `*`)
- [ ] `HEALTH_INGEST_TOKEN` is distinct from the panel secret
- [ ] `ENABLE_SHELL_CMD=0`
- [ ] `.env` is `chmod 600` and never committed
- [ ] Cloudflare Tunnel in front; server ports not publicly open
