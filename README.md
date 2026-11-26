<div align="center">

# ⬡ ORBION

**A self-hosted, multi-agent AI office.**
Zero cloud lock-in. Modular LLM providers. Your data stays on your box.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Self-hosted](https://img.shields.io/badge/self--hosted-%E2%9C%93-success)
![Zero-cost](https://img.shields.io/badge/LLM%20failover-Gemini%20%C2%B7%20Groq%20%C2%B7%20Cloudflare%20%C2%B7%20Ollama-informational)

*English · [Türkçe](README.tr.md) · [Deutsch](README.de.md) · [العربية](README.ar.md) · [中文](README.zh.md)*

</div>

---

## What is ORBION?

ORBION turns a single small server (a free-tier VM is enough) into a **personal AI office**:
a coordinator agent that delegates work to specialized agents, a live web console,
a Telegram interface, and even a **Wear OS watch face** that shows what your agents
are doing in real time.

It was built around a stubborn constraint: **run on free infrastructure, never leak
personal data, never depend on one AI vendor.**

```
                  ┌──────────────────────────────────────────┐
                  │              ORBION  CORE                │
                  │                                          │
  Telegram ─────▶ │  Coordinator ──▶ Agent Registry          │
  Web panel ────▶ │       │              │                   │
  Watch ────────▶ │       ▼              ▼                   │
                  │  LLM Pool      Tool/Action layer         │
                  │  (Gemini·Groq  (mail, files, research,   │
                  │   ·CF·Ollama)   scheduling, queue)       │
                  │       │              │                   │
                  │       ▼              ▼                   │
                  │   SQLite state / evidence (call_id)      │
                  └──────────────────────────────────────────┘
```

## Why it's different

| | Typical AI assistant | **ORBION** |
|---|---|---|
| Where it runs | Vendor cloud | **Your server** |
| AI vendor | Locked to one | **Hot-swappable pool** (Gemini · Groq · Cloudflare · local Ollama) |
| Cost | Per-token billing | **Free tiers + local fallback** |
| Proof of work | "I did it" | **`call_id` + status for every agent call** |
| Data | Uploaded to someone | **Stays on your box** |
| Presence | A chat window | **Live office, watch face, Telegram** |

## Features

- **Multi-agent orchestration** — a coordinator delegates to specialized agents
  (assistant, coder, mail archivist, researcher). Every call is logged with a
  `call_id`, status and duration, so "I did it" is always backed by evidence.
- **Provider-agnostic LLM pool** — OpenAI-compatible adapters with automatic
  failover: Gemini → Groq → Cloudflare Workers AI → local Ollama. When one provider
  is down or out of quota, ORBION transparently moves to the next. No single point
  of failure.
- **Zero-cost first** — deterministic code handles everything it can (no LLM call
  for classification, routing, counting). The LLM is a *last resort*, not the default.
- **Telegram interface** — natural conversation, plus a separate channel per agent.
- **Live web console** — a real-time "office" view of your agents, a systems panel
  with per-service limits, and a one-screen operational dashboard.
- **Wear OS companion** — a watch face with your agents arranged around the dial and
  a companion app that shows the live office; state lights update from the server.
- **Safety by design** — the assistant never fabricates citations, never claims it
  performed an action it didn't, and escalates honestly when a tool is unavailable.

## Architecture highlights

- **Single-process core, thread-isolated agents** — cheap to run on 2–4 vCPU.
- **SQLite with a strict evidence model** — `agent_calls`, `agent_tasks`,
  `agent_events`; every delegated task is traceable end-to-end.
- **Atomic job queue** — `claim / heartbeat / reclaim` with timeouts; a crashed
  worker never silently loses a job.
- **Fail-closed security** — missing tokens disable protected endpoints rather than
  opening them; no secrets in logs, no wildcard CORS.
- **Prompt-injection & SSRF aware** — external fetches reject private/loopback/
  cloud-metadata ranges; untrusted content is fenced before reaching the model.

---

## 🩺 Privacy-First Health Companion *(reference design — not shipped)*

ORBION's architecture was forged on a **health-critical** use case that we
deliberately **do not publish**. Health data is the most sensitive class of personal
data there is, and an open repo is the wrong place for it. But the design is worth
describing, because it shows the trust boundary ORBION is built around.

> **What it does (privately).** A dedicated health agent — written as a calm,
> PhD-level physician persona — manages a single living medical record for its owner,
> alongside a wearable data pipeline and an evidence-based research loop.

**Structured, source-tagged record**
Every entry is tagged by provenance — `USER_FACT`, `DEVICE_DATA`, `DOCTOR_NOTE`,
`RESEARCH_NOTE`, `PLAN`, `FOLLOW_UP`, `RESULT`, `DOCUMENT_FACT`,
`UNVERIFIED_EXTRACTION` — with full history. New information never overwrites old;
the timeline is preserved. The agent keeps *facts*, *observations* and *follow-ups*
strictly separate (a single coffee entry does not create a "caffeine problem").

**Evidence-based research, honestly labelled**
Pulls from WHO, PubMed, national medicines agencies and clinical guidelines; states
the strength of evidence (strong / moderate / mixed / limited); and **never invents
a citation**. If the research tool is unavailable, it says so instead of pretending
it searched.

**Wearable ingestion**
Galaxy Watch → self-hosted server: ECG waveform, heart rate, SpO₂, sleep and activity
flow in through a **dedicated, short-lived device token** — never the master panel
token. Raw signals are stored as measurement, explicitly *not* as diagnosis.

**Hard safety rails**
No diagnosis, no prescription, no dosage changes — the agent *informs*, the human
decides. Red-flag symptoms (chest pain, stroke signs, bleeding, self-harm) skip long
analysis and escalate to emergency services (112 in Türkiye). Medication questions get
real information with a firm "discuss this with your physician" — never "just take it."

**Isolation & zero leakage**
The health agent's memory is isolated from the general assistant. Raw health payloads
never reach office bubbles, generic logs, other agents, or a local small model.
External LLM calls carry only *generic* medical questions — personal values, names and
measurements never leave the box.

**Why it's not in this repo**
KVKK/GDPR-grade consent, audit trails and the medical-liability surface belong behind
a carefully reviewed boundary — not in a public clone. The full design (data isolation,
consent gates, audit log) is documented privately. This is the kind of rigor ORBION is
built to support.

---

## Quick start

```bash
git clone https://github.com/erdenyagici-cmd/orbion.git
cd orbion
cp .env.example .env        # fill in your provider keys & tokens
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 scripts/agent.py    # starts the core (bots + web console)
```

See **[docs/SETUP.md](docs/SETUP.md)** for the full walkthrough (systemd, Cloudflare
Tunnel, Telegram bot, Wear OS build).

## Security model

- Separate tokens per surface: panel, health-ingest, watch — **no shared master token**
  across trust boundaries.
- Fail-closed: an unset token disables its endpoint instead of opening it.
- No secrets in logs, argv, or commits; `.env` is git-ignored and `chmod 600`.
- External fetching is SSRF-guarded (private / loopback / metadata IPs rejected).
- Shell/exec features are **off by default**, behind an explicit opt-in flag.

## Status

Early but real. ORBION runs a live personal setup 24/7 on a free-tier ARM VM.
APIs and internal structure may change; the safety and evidence principles will not.

## License

MIT — see [LICENSE](LICENSE).

---

<div align="center"><sub>Built on a free-tier server, with a stubborn belief that your AI should be yours.</sub></div>
