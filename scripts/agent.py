"""
ORBION — core skeleton.

A compact, dependency-light reference implementation of the architecture:
  • Agent Registry   — single source of truth for agents & their capabilities
  • Evidence model   — every delegated call gets a call_id + status + duration
  • Tool layer       — small, explicit capabilities (no magic)
  • HTTP panel       — read-only operational view (auth required)
  • LLM pool         — provider-agnostic failover (see llm_pool.py)

This is a skeleton: it shows HOW the pieces fit, with clean, generic code and
zero personal data. Drop your own agents/tools in `register_agents()`.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from llm_pool import LLMPool, load_providers

# ──────────────────────────────────────────────────────────────
#  Config (all via env — no secrets in code)
# ──────────────────────────────────────────────────────────────
PORT = int(os.environ.get("AGENT_PORT", "8090"))
DB_PATH = os.environ.get("DB_PATH", "data/orbion.db")
PANEL_PASSWORD = (os.environ.get("PANEL_PASSWORD") or "").strip()

# ──────────────────────────────────────────────────────────────
#  Storage (SQLite, additive schema, evidence-first)
# ──────────────────────────────────────────────────────────────
_lock = threading.Lock()


def db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _lock, db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_state(
                id TEXT PRIMARY KEY,
                state TEXT NOT NULL DEFAULT 'idle',
                updated_at REAL
            );
            CREATE TABLE IF NOT EXISTS agent_calls(
                call_id TEXT PRIMARY KEY,
                ts REAL NOT NULL,
                agent TEXT NOT NULL,
                cap TEXT NOT NULL,
                request TEXT,
                status TEXT NOT NULL,
                result TEXT,
                error TEXT,
                duration_ms INTEGER
            );
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                level TEXT,
                component TEXT,
                message TEXT
            );
            """
        )


def log(level: str, component: str, message: str) -> None:
    with _lock, db() as c:
        c.execute(
            "INSERT INTO events(ts, level, component, message) VALUES (?,?,?,?)",
            (time.time(), level, component, message[:500]),
        )


# ──────────────────────────────────────────────────────────────
#  Agent registry + tool layer
# ──────────────────────────────────────────────────────────────
@dataclass
class Agent:
    id: str
    name: str
    kind: str                      # "human" | "llm" | "code"
    capabilities: dict[str, Callable] = field(default_factory=dict)


REGISTRY: dict[str, Agent] = {}


def register(agent: Agent) -> None:
    REGISTRY[agent.id] = agent


def set_state(agent_id: str, state: str) -> None:
    with _lock, db() as c:
        c.execute(
            "INSERT INTO agent_state(id, state, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
            (agent_id, state, time.time()),
        )


def call_agent(agent_id: str, cap: str, request: dict | None = None) -> dict:
    """Delegate to an agent capability. Records evidence (call_id + status)."""
    agent = REGISTRY.get(agent_id)
    if not agent or cap not in agent.capabilities:
        return {"ok": False, "error": f"no such capability: {agent_id}.{cap}"}

    call_id = uuid.uuid4().hex[:12]
    t0 = time.time()
    with _lock, db() as c:
        c.execute(
            "INSERT INTO agent_calls(call_id, ts, agent, cap, request, status) "
            "VALUES (?,?,?,?,?,?)",
            (call_id, t0, agent_id, cap, json.dumps(request or {}, ensure_ascii=False), "running"),
        )
    set_state(agent_id, "busy")
    try:
        result = agent.capabilities[cap](**(request or {}))
        status, err, out = "ok", None, result
    except Exception as exc:  # noqa: BLE001 — recorded, not swallowed
        status, err, out = "error", type(exc).__name__, None
    finally:
        set_state(agent_id, "idle")
    dur = int((time.time() - t0) * 1000)
    with _lock, db() as c:
        c.execute(
            "UPDATE agent_calls SET status=?, result=?, error=?, duration_ms=? WHERE call_id=?",
            (status, json.dumps(out, ensure_ascii=False, default=str) if out else None,
             err, dur, call_id),
        )
    log("info" if status == "ok" else "error", agent_id, f"{cap} -> {status} ({dur}ms)")
    return {"ok": status == "ok", "call_id": call_id, "status": status,
            "result": out, "error": err, "duration_ms": dur}


# ──────────────────────────────────────────────────────────────
#  LLM pool + a coordinator
# ──────────────────────────────────────────────────────────────
POOL = LLMPool(load_providers())


def coordinator_reply(user_text: str) -> str:
    """The single front door. Answers in the user's language, honestly."""
    msgs = [
        {"role": "system", "content": (
            "You are the coordinator of a self-hosted AI office. Be concise, honest and "
            "warm. If you don't know, say so. Never claim you performed an action you "
            "did not. Answer in the user's language."
        )},
        {"role": "user", "content": user_text},
    ]
    text, meta = POOL.chat(msgs, consumer="general", max_tokens=600)
    if not meta.get("ok"):
        return f"(I can't reach any model right now: {meta.get('error')})"
    return text


def register_agents() -> None:
    """Register your agents here. This skeleton ships two examples."""
    register(Agent(id="assistant", name="Assistant", kind="llm", capabilities={
        "ask": lambda question="": coordinator_reply(question),
    }))
    register(Agent(id="sysinfo", name="System", kind="code", capabilities={
        "uptime": lambda: {"uptime_s": int(time.monotonic())},
    }))


# ──────────────────────────────────────────────────────────────
#  HTTP panel (read-only, auth required, no leaks)
# ──────────────────────────────────────────────────────────────
def _authed(handler: "Handler") -> bool:
    # fail-closed: no password configured => everything is locked
    if not PANEL_PASSWORD:
        return False
    import hmac
    got = handler.headers.get("X-Panel-Password") or ""
    return hmac.compare_digest(got, PANEL_PASSWORD)


class Handler(BaseHTTPRequestHandler):
    server_version = "ORBION"

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence default access log (no IPs in logs)
        pass

    def do_GET(self):
        if self.path == "/health":
            return self._send({"ok": True, "ts": time.time()})
        if not _authed(self):
            return self._send({"error": "unauthorized"}, 401)
        if self.path == "/ops/agents":
            with _lock, db() as c:
                states = {r["id"]: r["state"] for r in c.execute("SELECT id, state FROM agent_state")}
            return self._send({"agents": [
                {"id": a.id, "name": a.name, "kind": a.kind,
                 "state": states.get(a.id, "idle"),
                 "caps": list(a.capabilities.keys())}
                for a in REGISTRY.values()
            ]})
        if self.path == "/ops/models":
            return self._send({"providers": POOL.status()})
        if self.path == "/ops/calls":
            with _lock, db() as c:
                rows = [dict(r) for r in c.execute(
                    "SELECT call_id, agent, cap, status, duration_ms, ts "
                    "FROM agent_calls ORDER BY ts DESC LIMIT 50")]
            return self._send({"calls": rows})
        return self._send({"error": "not found"}, 404)


def main() -> None:
    init_db()
    register_agents()
    log("info", "boot", f"ORBION core up on :{PORT}")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"ORBION core listening on http://127.0.0.1:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
