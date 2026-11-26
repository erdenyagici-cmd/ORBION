"""
Evren Otomasyon Ajan Servisi  (notebook YOK, bagimlilik YOK)
--------------------------------------------------------------
Sadece Python standart kutuphanesi kullanir:
  imaplib, sqlite3, http.server, urllib, json, threading, email

Gorevler:
  1) Gmail IMAP'i periyodik tarar
  2) Tier 0: kural tabanli filtre (LLM maliyeti yok)
  3) Tier 1: Ollama ile siniflandirma/ozet (yerel, 0 TL)
  4) Tier 2: Evren API ile taslak yanit (gunluk kota korumali)
  5) Panel icin HTTP API sunar (8090)

Calistirma:
  python scripts\\agent.py
"""

import base64
import email
import hashlib
import hmac
import imaplib
from email.header import decode_header, make_header
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
DB_FILE = ROOT / "data" / "app.db"
LOG_FILE = ROOT / "logs" / "agent.log"

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import actions as _actions_mod
import telegram_bot as _tg_mod
import identity as ident
import dev_bot as _dev_mod
import projects as _proj
import web_research as _web

# ------------------------------------------------------------------ env
def load_env(path: Path) -> dict:
    cfg = {}
    if not path.exists():
        return cfg
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
    return cfg

CFG = load_env(ENV_FILE)
PROV_CFG = load_env(ROOT / ".env.providers")

# T21: gunluk limitler (deger degil, üst sinir). Sir icermez.
LIMIT_KV_WRITE = int(CFG.get("LIMIT_KV_WRITE", "1000") or 1000)
LIMIT_R2_GB = float(CFG.get("LIMIT_R2_GB", "10") or 10)
LIMIT_WAI_NEURON = float(CFG.get("LIMIT_WAI_NEURON", "10000") or 10000)
LIMIT_GMAIL_SEND = int(CFG.get("LIMIT_GMAIL_SEND", "500") or 500)
LIMIT_ORACLE_OCPU = float(CFG.get("LIMIT_ORACLE_OCPU", "4") or 4)
LIMIT_ORACLE_MEM = float(CFG.get("LIMIT_ORACLE_MEM", "24") or 24)
LIMIT_DISK_GB = float(CFG.get("LIMIT_DISK_GB", "200") or 200)
LIMIT_WARN_PCT = 90.0
LIMIT_CRIT_PCT = 95.0
LIMIT_ALERT_DEDUP_SEC = 6 * 3600

EV_BASE = CFG.get("EVREN_BASE_URL", "").rstrip("/")
_KEYS = []
for _suffix in ("", "_BACKUP", "_3", "_4", "_5"):
    _k = CFG.get(f"EVREN_API_KEY{_suffix}", "").strip()
    if _k and _k not in _KEYS:
        _KEYS.append(_k)
EV_KEY = _KEYS[0] if _KEYS else ""
EV_KEY_BACKUP = _KEYS[1] if len(_KEYS) > 1 else ""
EV_HDR = CFG.get("EVREN_AUTH_HEADER", "X-API-Key")
EV_MODEL = CFG.get("EVREN_MODEL", "deepseek-v4.1-flash")
EV_LIMIT = int(CFG.get("EVREN_DAILY_TOKEN_LIMIT", "15000") or 15000)
_AYLA_LAST_EVT = {"at": 0.0}

OL_BASE = CFG.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OL_MODEL = CFG.get("OLLAMA_MODEL", "qwen3.5:4b")

GM_USER = CFG.get("GMAIL_ADDRESS", "").strip()
GM_PASS = CFG.get("GMAIL_APP_PASSWORD", "").strip()
IMAP_HOST = CFG.get("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(CFG.get("IMAP_PORT", "993") or 993)

API_PORT = int(CFG.get("AGENT_PORT", "8090") or 8090)
HOST_BIND = CFG.get("AGENT_BIND", "localhost")
PANEL_TOKEN = CFG.get("PANEL_TOKEN", "")
POLL_SEC = int(CFG.get("MAIL_POLL_SECONDS", "180") or 180)

# ------------------------------------------------------------------ log
# ============ HASSAS VERI MASKELEME (Madde 15) ============
_SECRET_PATTERNS = [
    (re.compile(r"evren_" r"llm_[A-Za-z0-9_\-]{10,}"), "evren_" r"llm_[REDACTED]"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b"), "[TG_TOKEN_REDACTED]"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer|basic)\s+[\w\-\.=~+/]{8,}"),
     r"\1\2 [REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|psk|"
                r"app_password|encryption_key)\s*[=:]\s*)[^\s&,;\"']+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(cookie\s*[:=]\s*)[^\n]+"), r"\1[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"), "sk-[REDACTED]"),
    (re.compile(r"\bcfat_[A-Za-z0-9]{20,}\b"), "cfat_[REDACTED]"),
    (re.compile(r"\bcfk_[A-Za-z0-9]{20,}\b"), "cfk_[REDACTED]"),
    # ortam degiskeni sizintisi
    (re.compile(r"(?i)\b([A-Z_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD))=(\S+)"),
     r"\1=[REDACTED]"),
]

# Transactional gizli veri (mail gövdesi, TCKN, IBAN, kart)
_PII_PATTERNS = [
    (re.compile(r"\b\d{11}\b"), "[TCKN]"),
    (re.compile(r"\bTR\d{2}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{2}\b", re.I), "[IBAN]"),
    (re.compile(r"\b(?:\d[ -]*?){13,16}\b"), "[KART]"),
]


def _mask_only(text: str, cap: int = 8000) -> str:
    """Hassas veriyi maskele ama metni KISALTMA (gorev detayi gibi uzun alanlar icin)."""
    if not text:
        return ""
    out = str(text)
    for rx, rep in _SECRET_PATTERNS:
        try:
            out = rx.sub(rep, out)
        except Exception:
            pass
    for rx, rep in _PII_PATTERNS:
        try:
            out = rx.sub(rep, out)
        except Exception:
            pass
    return out[:cap] if cap and len(out) > cap else out


def _safe_msg(text: str) -> str:
    """Panel/log'a yazilmadan once hassas veriyi maskele."""
    if not text:
        return ""
    out = str(text)
    for rx, rep in _SECRET_PATTERNS:
        try:
            out = rx.sub(rep, out)
        except Exception:
            pass
    for rx, rep in _PII_PATTERNS:
        try:
            out = rx.sub(rep, out)
        except Exception:
            pass
    if len(out) > 500:
        out = out[:500] + "…[kisaltildi]"
    return out


# Ayni (component,message) tekrarini events tablosunda kisitla (log seli onleme)
_LOG_LAST = {}
_LOG_LOCK = threading.Lock()
_LOG_REPEAT_SEC = 300


def log(level: str, component: str, message: str, meta: str = ""):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    message = _safe_msg(message)
    meta = _safe_msg(meta)
    # ayni satiri 5 dk icinde tekrar yazma (dosyaya yine yazilir)
    _k = (component, level, message[:80])
    now_m = time.time()
    with _LOG_LOCK:
        _last = _LOG_LAST.get(_k, 0)
        _dup = (now_m - _last) < _LOG_REPEAT_SEC
        if not _dup:
            _LOG_LAST[_k] = now_m
            if len(_LOG_LAST) > 500:
                _LOG_LAST.clear()
    if _dup and level not in ("error",):
        return
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{ts} | {level} | {component} | {message}\n")
    except Exception:
        pass
    conn = None
    try:
        conn = db()
        c = conn.cursor()
        c.execute("INSERT INTO events(ts,level,component,message,meta) VALUES(?,?,?,?,?)",
                  (ts, level, component, message, meta))
        c.execute("DELETE FROM events WHERE id NOT IN "
                  "(SELECT id FROM events ORDER BY id DESC LIMIT 500)")
        if level == "error":
            row = c.execute("SELECT id,count FROM errors WHERE component=? AND message=? AND resolved=0",
                            (component, message)).fetchone()
            if row:
                c.execute("UPDATE errors SET count=count+1, ts=? WHERE id=?", (ts, row[0]))
            else:
                c.execute("INSERT INTO errors(ts,component,message,count,resolved) VALUES(?,?,?,1,0)",
                          (ts, component, message))
        conn.commit()
    except Exception as exc:
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
        print("log db error:", exc)
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass

# ------------------------------------------------------------------ alerts (onemli olaylar)
ALERT_LEVELS = ("critical", "warn", "info")


def alert(severity: str, source: str, code: str, message: str,
          detail: str = "", dedup_sec: int = 600):
    """Anlamli sistem olayi -> alerts tablosu (dedup'li). Event seli uretmez."""
    sev = (severity or "warn").strip().lower()
    if sev not in ALERT_LEVELS:
        sev = "warn"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = _safe_msg(message)[:300]
    det = _safe_msg(detail)[:800]
    code = (code or "")[:40]
    src = (source or "")[:40]
    try:
        with db() as c:
            row = c.execute(
                "SELECT id,last_ts,count,resolved FROM alerts "
                "WHERE source=? AND code=? AND resolved=0 ORDER BY id DESC LIMIT 1",
                (src, code)).fetchone()
            if row:
                try:
                    from datetime import datetime as _dt
                    last = _dt.strptime(row["last_ts"], "%Y-%m-%d %H:%M:%S")
                    gap = (datetime.now() - last).total_seconds()
                except Exception:
                    gap = 999999
                # Ayni kosul KESINTISIZ suruyorsa: yalniz last_ts guncelle.
                # count = "kac kez yeniden ortaya cikti", "kac dakikadir acik" DEGIL.
                if gap < dedup_sec:
                    c.execute("UPDATE alerts SET last_ts=?, message=?, detail=? "
                              "WHERE id=?", (ts, msg, det, row["id"]))
                    return row["id"]
                # kosul dedup penceresinden SONRA tekrar ortaya cikti ->
                # eski kaydi kapat, artan sayacla yenisini ac
                c.execute("UPDATE alerts SET resolved=1, resolved_ts=? WHERE id=?",
                          (ts, row["id"]))
                recur = int(row["count"] or 1) + 1
            else:
                # ayni kodun gecmis (kapanmis) kaydi varsa tekrar sayisini tasi
                prev = c.execute(
                    "SELECT count FROM alerts WHERE source=? AND code=? "
                    "ORDER BY id DESC LIMIT 1", (src, code)).fetchone()
                recur = (int(prev["count"] or 1) + 1) if prev else 1
            cur = c.execute(
                "INSERT INTO alerts(first_ts,last_ts,severity,source,code,message,detail,count) "
                "VALUES(?,?,?,?,?,?,?,?)", (ts, ts, sev, src, code, msg, det, recur))
            c.execute("DELETE FROM alerts WHERE resolved=1 AND id NOT IN "
                      "(SELECT id FROM alerts ORDER BY id DESC LIMIT 300)")
            aid = cur.lastrowid
        log("warn" if sev != "critical" else "error", src,
            f"[{sev}] {msg}")
        return aid
    except Exception as exc:
        print("alert error:", exc)
        return None


def alert_resolve(source: str, code: str):
    """Kosul duzeldiginde acik alarmi kapat + 'duzeldi' kaydi."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with db() as c:
            cur = c.execute("UPDATE alerts SET resolved=1, resolved_ts=? "
                            "WHERE source=? AND code=? AND resolved=0",
                            (ts, (source or "")[:40], (code or "")[:40]))
            return cur.rowcount
    except Exception:
        return 0


def changelog(actor: str, area: str, summary: str):
    """Degisiklik gunlugu (teshis icin)."""
    try:
        with db() as c:
            c.execute("INSERT INTO changelog(ts,actor,area,summary) VALUES(?,?,?,?)",
                      (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       (actor or "")[:30], (area or "")[:40], _safe_msg(summary)[:300]))
    except Exception:
        pass


# ------------------------------------------------------------------ gorev defteri (Ayla/Evren)
TASK_STATUSES = ("BEKLIYOR", "YAPILIYOR", "OWNER_PENDING", "BITTI", "BASARISIZ", "IPTAL", "REVIZYON")
LOCK_TTL_SEC = 2 * 86400   # 2 gun: bu sureden eski kilit gecersiz


def task_add(title, detail="", owner="ayla", priority=1, source="telegram"):
    try:
        with db() as c:
            cur = c.execute(
                "INSERT INTO agent_tasks(ts,updated_at,title,detail,owner,status,priority,source) "
                "VALUES(?,?,?,?,?,'BEKLIYOR',?,?)",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 _safe_msg(title)[:200], _mask_only(detail, 8000),
                 (owner or "ayla")[:20], int(priority or 1), (source or "")[:20]))
            return cur.lastrowid
    except Exception as exc:
        print("task_add:", exc)
        return None


# E4: kullanici mesaji gorev/proje niyeti tasiyorsa ve model uretemezse
# ham mesaji kaybetme; deftere BEKLIYOR olarak yaz.
_INTENT_RE = re.compile(
    r"(?i)\b(ba[gğ]la|ba[gğ]lant|kur|kurulum|yap|d[uü]zelt|ekle|hallet|ayarla|"
    r"olu[şs]tur|entegre|entegrasyon|aktar|d[oö]n[uü][şs]t[uü]r|g[oö]rev|proje|"
    r"g[uü]ncelle|de[gğ]i[şs]tir|kald[ıi]r|ilet|devreye)\b")


def _salvage_intent(text, reason=""):
    """Model basarisiz olsa bile gorev/proje niyetli ham mesaji deftere yaz.

    Donus: olusan/var olan task id veya None (niyet yok/eklenemedi)."""
    t = (text or "").strip()
    if not t or not _INTENT_RE.search(t):
        return None
    try:
        with db() as c:
            row = c.execute(
                "SELECT id FROM agent_tasks WHERE detail=? AND status IN "
                "('BEKLIYOR','YAPILIYOR','OWNER_PENDING','REVIZYON') "
                "ORDER BY id DESC LIMIT 1", (_mask_only(t, 8000),)).fetchone()
        if row:
            return row["id"]
        title = t.splitlines()[0][:120]
        tid = task_add(title, detail=t, owner="evren", priority=2, source="salvage")
        if tid:
            log("warn", "ayla",
                f"model basarisiz; ham mesaj goreve kaydedildi #{tid} ({_safe_msg(reason)[:60]})")
        return tid
    except Exception:
        return None


def task_set(tid, status=None, result=None, owner=None):
    sets, args = [], []
    if status:
        st = status.upper()
        if st not in TASK_STATUSES:
            return False
        sets.append("status=?")
        args.append(st)
        if st in ("BITTI", "BASARISIZ", "IPTAL"):
            sets.append("closed_ts=?")
            args.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if result is not None:
        sets.append("result=?")
        args.append(_safe_msg(result)[:4000])
    if owner:
        sets.append("owner=?")
        args.append(owner[:20])
    if not sets:
        return False
    sets.append("updated_at=?")
    args.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    args.append(int(tid))
    try:
        with db() as c:
            c.execute("UPDATE agent_tasks SET " + ",".join(sets) + " WHERE id=?", args)
        if status and status.upper() == "OWNER_PENDING":
            try:
                notify_owner_pending(int(tid), result or "")
            except Exception:
                pass
        return True
    except Exception:
        return False


def task_list(status=None, owner=None, limit=40):
    q = "SELECT id,ts,title,owner,status,priority,result FROM agent_tasks"
    cond, args = [], []
    if status:
        cond.append("status=?"); args.append(status.upper())
    if owner:
        cond.append("owner=?"); args.append(owner)
    if cond:
        q += " WHERE " + " AND ".join(cond)
    q += (" ORDER BY CASE status WHEN 'YAPILIYOR' THEN 0 WHEN 'BEKLIYOR' THEN 1 "
          "ELSE 2 END, priority ASC, id ASC LIMIT ?")
    args.append(int(limit))
    try:
        with db() as c:
            return [{"id": r["id"], "ts": r["ts"], "title": r["title"],
                     "owner": r["owner"], "status": r["status"],
                     "priority": r["priority"], "result": r["result"]}
                    for r in c.execute(q, args).fetchall()]
    except Exception:
        return []


def task_next(owner, priority_max=9):
    """Siradaki bekleyen gorev (priority <= priority_max)."""
    try:
        with db() as c:
            row = c.execute(
                "SELECT id,title,detail,priority FROM agent_tasks "
                "WHERE status='BEKLIYOR' AND owner=? AND priority<=? "
                "ORDER BY priority ASC, id ASC LIMIT 1",
                (owner, int(priority_max))).fetchone()
            return dict(row) if row else None
    except Exception:
        return None


def task_is_brief_eksik(tid):
    """Gorev BRIEF_EKSIK etiketli mi? (BASARISIZ + 'BRIEF_EKSIK:' sonuc)."""
    try:
        with db() as c:
            r = c.execute("SELECT status,result FROM agent_tasks WHERE id=?",
                          (int(tid),)).fetchone()
        return bool(r and r["status"] == "BASARISIZ"
                    and (r["result"] or "").startswith("BRIEF_EKSIK"))
    except Exception:
        return False


def task_retry(tid):
    """Basarisiz gorevi tekrar kuyruga al. BRIEF_EKSIK ise ATLA (dogru brief gerekir).
    Donus: (ok, mesaj)."""
    try:
        with db() as c:
            r = c.execute("SELECT status,result FROM agent_tasks WHERE id=?",
                          (int(tid),)).fetchone()
        if not r:
            return False, "gorev yok"
        if r["status"] != "BASARISIZ":
            return False, f"yalniz BASARISIZ yeniden denenir (durum={r['status']})"
        if (r["result"] or "").startswith("BRIEF_EKSIK"):
            return False, "BRIEF_EKSIK atlandi (once duzgun brief gerekir)"
        with db() as c:
            c.execute("UPDATE agent_tasks SET status='BEKLIYOR', closed_ts=NULL, "
                      "updated_at=? WHERE id=?",
                      (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(tid)))
        return True, "yeniden kuyruga alindi"
    except Exception as exc:
        return False, _safe_msg(str(exc))[:80]


# ------------------------------------------------------------------ kilit (dosya alani)
def lock_acquire(owner, area, note=""):
    """Alani kilitle. Baskasi tutuyorsa False. Suresi gecmis kilit serbest."""
    now = time.time()
    area = (area or "").replace("\\", "/").strip("/")[:120]
    if not area:
        return False, "alan bos"
    try:
        with db() as c:
            c.execute("DELETE FROM agent_lock WHERE expires_ts < ?", (now,))
            row = c.execute("SELECT owner,note,ts FROM agent_lock WHERE area=?",
                            (area,)).fetchone()
            if row:
                if row["owner"] == owner:
                    c.execute("UPDATE agent_lock SET ts=?, expires_ts=? WHERE area=?",
                              (now, now + LOCK_TTL_SEC, area))
                    return True, "yenilendi"
                return False, f"{row['owner']} kullaniyor"
            c.execute("INSERT INTO agent_lock(owner,area,note,ts,expires_ts) "
                      "VALUES(?,?,?,?,?)", (owner, area, (note or "")[:200], now,
                                            now + LOCK_TTL_SEC))
            return True, "alindi"
    except Exception as exc:
        return False, f"db: {type(exc).__name__}"


def lock_release(owner, area):
    area = (area or "").replace("\\", "/").strip("/")[:120]
    try:
        with db() as c:
            cur = c.execute("DELETE FROM agent_lock WHERE area=? AND owner=?",
                            (area, owner))
            return cur.rowcount > 0
    except Exception:
        return False


def lock_release_all(owner):
    try:
        with db() as c:
            cur = c.execute("DELETE FROM agent_lock WHERE owner=?", (owner,))
            return cur.rowcount
    except Exception:
        return 0


def lock_list():
    now = time.time()
    try:
        with db() as c:
            rows = c.execute("SELECT owner,area,note,ts,expires_ts FROM agent_lock "
                             "ORDER BY ts DESC").fetchall()
        out = []
        for r in rows:
            age = int(now - (r["ts"] or now))
            out.append({"owner": r["owner"], "area": r["area"],
                        "note": r["note"] or "", "age_sec": age,
                        "stale": (r["expires_ts"] or 0) < now})
        return out
    except Exception:
        return []


def lock_check(area):
    """Alan su an kimin elinde? None = serbest, 'stale' = gecersiz kilit."""
    now = time.time()
    area = (area or "").replace("\\", "/").strip("/")[:120]
    try:
        with db() as c:
            r = c.execute("SELECT owner,expires_ts FROM agent_lock WHERE area=?",
                          (area,)).fetchone()
        if not r:
            return None
        if (r["expires_ts"] or 0) < now:
            return "stale"
        return r["owner"]
    except Exception:
        return None


# ------------------------------------------------------------------ db
class _ClosingConnection(sqlite3.Connection):
    """`with db() as c` blogu bitince hem commit/rollback hem de baglantiyi KAPAT.

    sqlite3.Connection'in kendi context manager'i baglantiyi kapatmaz; her db()
    cagrisi acik fd birakir. Bu alt sinif fd sizintisini onler."""

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()
        return False


def db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False,
                           factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS mails(
          id TEXT PRIMARY KEY, received_at TEXT, sender TEXT, subject TEXT,
          label TEXT, summary TEXT, draft TEXT, status TEXT,
          tokens_used INTEGER DEFAULT 0, tier INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS token_usage(day TEXT PRIMARY KEY, used INTEGER);
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT,
          component TEXT, message TEXT, meta TEXT);
        CREATE TABLE IF NOT EXISTS errors(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, component TEXT,
          message TEXT, count INTEGER, resolved INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS model_state(
          id INTEGER PRIMARY KEY CHECK(id=1), active TEXT, updated_at TEXT);
        INSERT OR IGNORE INTO model_state(id, active, updated_at)
          VALUES(1, 'evren', datetime('now'));
        CREATE TABLE IF NOT EXISTS agent_state(
          id TEXT PRIMARY KEY, state TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS escalation(
          id INTEGER PRIMARY KEY AUTOINCREMENT, prompt TEXT, task TEXT,
          cirak_raw TEXT, evren_reply TEXT, tokens INTEGER, ts TEXT);
        CREATE TABLE IF NOT EXISTS delegation(
          id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT, label TEXT,
          source TEXT, ts TEXT);
        CREATE TABLE IF NOT EXISTS agent_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, event_type TEXT,
          source_agent TEXT, target_agent TEXT, task_id TEXT, status TEXT,
          summary TEXT);
        CREATE INDEX IF NOT EXISTS idx_agent_events_task ON agent_events(task_id);
        CREATE TABLE IF NOT EXISTS chat_history(
          id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, role TEXT,
          content TEXT, ts TEXT);
        CREATE TABLE IF NOT EXISTS settings(
          key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS token_usage_consumer(
          day TEXT, consumer TEXT, used INTEGER DEFAULT 0,
          PRIMARY KEY(day, consumer));
        CREATE TABLE IF NOT EXISTS reminders(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts_due REAL NOT NULL,
          text TEXT, channel TEXT DEFAULT 'telegram', done INTEGER DEFAULT 0,
          created_ts REAL);
        CREATE TABLE IF NOT EXISTS alerts(
          id INTEGER PRIMARY KEY AUTOINCREMENT, first_ts TEXT, last_ts TEXT,
          severity TEXT, source TEXT, code TEXT, message TEXT, detail TEXT,
          count INTEGER DEFAULT 1, resolved INTEGER DEFAULT 0, resolved_ts TEXT);
        CREATE INDEX IF NOT EXISTS idx_alerts_res ON alerts(resolved, severity);
        CREATE TABLE IF NOT EXISTS changelog(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT,
          area TEXT, summary TEXT);
        CREATE TABLE IF NOT EXISTS agent_tasks(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, updated_at TEXT,
          title TEXT, detail TEXT, owner TEXT, status TEXT DEFAULT 'BEKLIYOR',
          priority INTEGER DEFAULT 1, source TEXT, result TEXT,
          due_ts REAL, closed_ts TEXT);
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON agent_tasks(status, priority);
        CREATE TABLE IF NOT EXISTS agent_lock(
          id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, area TEXT,
          note TEXT, ts REAL, expires_ts REAL);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_lock_area ON agent_lock(area);
        CREATE TABLE IF NOT EXISTS dev_jobs(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, chat_id TEXT,
          message TEXT, status TEXT DEFAULT 'pending', result TEXT,
          worker TEXT, started_at TEXT, finished_at TEXT, updated_at TEXT,
          task_id INTEGER);
        CREATE INDEX IF NOT EXISTS idx_dev_jobs_status ON dev_jobs(status);
        CREATE TABLE IF NOT EXISTS tg_messages(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, chat_id TEXT,
          direction TEXT, msg_id INTEGER, reply_to INTEGER,
          agent TEXT, topic TEXT, call_id TEXT, text TEXT);
        CREATE INDEX IF NOT EXISTS idx_tg_msg ON tg_messages(chat_id, msg_id);
        CREATE INDEX IF NOT EXISTS idx_tg_dir ON tg_messages(direction, ts);
        CREATE TABLE IF NOT EXISTS agent_calls(
          id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT, ts TEXT,
          agent TEXT, cap TEXT, request TEXT, status TEXT,
          result TEXT, error TEXT, duration_ms INTEGER, source TEXT);
        CREATE INDEX IF NOT EXISTS idx_calls_agent ON agent_calls(agent, ts);
        CREATE TABLE IF NOT EXISTS research_log(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, agent TEXT, cap TEXT,
          query TEXT, sources TEXT, summary TEXT, status TEXT);
        CREATE INDEX IF NOT EXISTS idx_research_ts ON research_log(ts);
        """)
    # --- kucuk migration: dev_jobs.task_id (eski DB) ---
    try:
        with db() as c:
            _cols = [r[1] for r in c.execute("PRAGMA table_info(dev_jobs)").fetchall()]
            if "task_id" not in _cols:
                c.execute("ALTER TABLE dev_jobs ADD COLUMN task_id INTEGER")
            # G2: eklemeli (additive) kolonlar — DROP/DELETE yok
            if "due_ts" not in _cols:
                c.execute("ALTER TABLE dev_jobs ADD COLUMN due_ts REAL")
            if "heartbeat_at" not in _cols:
                c.execute("ALTER TABLE dev_jobs ADD COLUMN heartbeat_at REAL")
            if "attempts" not in _cols:
                c.execute("ALTER TABLE dev_jobs ADD COLUMN attempts INTEGER DEFAULT 0")
    except Exception:
        pass


# ------------------------------------------------------------------ token butcesi
def tokens_used_today() -> int:
    with db() as c:
        row = c.execute("SELECT used FROM token_usage WHERE day=?", (date.today().isoformat(),)).fetchone()
        return row[0] if row else 0

def add_tokens(n: int, consumer: str = "system"):
    if n <= 0:
        return
    with db() as c:
        c.execute("INSERT INTO token_usage(day,used) VALUES(?,?) "
                  "ON CONFLICT(day) DO UPDATE SET used=used+?",
                  (date.today().isoformat(), n, n))
        try:
            c.execute("INSERT INTO token_usage_consumer(day,consumer,used) VALUES(?,?,?) "
                      "ON CONFLICT(day,consumer) DO UPDATE SET used=used+?",
                      (date.today().isoformat(), consumer or "system", n, n))
        except Exception:
            pass

def active_model() -> str:
    with db() as c:
        row = c.execute("SELECT active FROM model_state WHERE id=1").fetchone()
        return row[0] if row else "evren"

def set_active_model(mid: str):
    with db() as c:
        c.execute("UPDATE model_state SET active=?, updated_at=datetime('now') WHERE id=1", (mid,))
    log("ok", "panel", f"Model degistirildi -> {mid}")

# telegram ve actions modulleri bu modulun fonksiyonlarina erisir
actions = _actions_mod

# ------------------------------------------------------------------ HTTP yardimci
def http_post_json(url: str, payload: dict, headers: dict, timeout: int = 90):
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json"}
    h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def http_get_json(url: str, headers: dict, timeout: int = 15):
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

# ------------------------------------------------------------------ Evren gercek kota
_EV_QUOTA_CACHE = {"at": 0, "data": None}

def evren_quota(force: bool = False) -> dict:
    """Evren /v1/quota -> gercek gunluk kota. 60 sn onbellek."""
    now = time.time()
    if not force and _EV_QUOTA_CACHE["data"] and now - _EV_QUOTA_CACHE["at"] < 60:
        return _EV_QUOTA_CACHE["data"]
    if not _KEYS:
        return {}
    key = _active_key() or _KEYS[0]
    try:
        d = http_get_json(f"{EV_BASE}/v1/quota", {EV_HDR: key}, timeout=15)
        _EV_QUOTA_CACHE["at"] = now
        _EV_QUOTA_CACHE["data"] = d
        return d
    except Exception as exc:
        log("warn", "quota", f"Evren quota alinamadi: {exc}")
        return _EV_QUOTA_CACHE["data"] or {}

# ------------------------------------------------------------------ Tier 1: Ollama
def ollama_chat(prompt: str, model: str = None, max_tokens: int = 120,
                identity: str = "") -> str:
    """identity="" -> ic isler (siniflandirma/ozet). Sohbet icin ident.system_prompt gecir."""
    model = model or OL_MODEL
    full = f"{identity}\n\n---\n\n{prompt}" if identity else prompt
    try:
        res = http_post_json(f"{OL_BASE}/api/generate", {
            "model": model, "prompt": full, "stream": False,
            "think": False,
            "options": {"num_predict": max_tokens, "temperature": 0.2},
        }, {}, timeout=180)
        txt = (res.get("response") or "").strip()
        if not txt:
            # bazi surumler think alanini yok sayar; dusunme metnini kullan
            txt = (res.get("thinking") or "").strip()
            # "Thinking Process:" blogunu at, son paragrafi al
            if "Thinking Process" in txt:
                txt = txt.split("\n\n")[-1].strip()
        return txt
    except Exception as exc:
        log("error", "tier1", f"Ollama hatasi: {exc}")
        return ""


# ------------------------------------------------------------------ sohbet gecmisi
HISTORY_LIMIT = 10   # geriye donuk hatirlanacak mesaj sayisi

def add_history(channel: str, role: str, content: str):
    try:
        with db() as c:
            c.execute("INSERT INTO chat_history(channel,role,content,ts) "
                      "VALUES(?,?,?,datetime('now'))",
                      (channel or "default", role, (content or "")[:4000]))
            # kanal basina 60 mesajdan fazlasini sil
            c.execute("DELETE FROM chat_history WHERE channel=? AND id NOT IN "
                      "(SELECT id FROM chat_history WHERE channel=? "
                      "ORDER BY id DESC LIMIT 60)",
                      (channel or "default", channel or "default"))
    except Exception as exc:
        log("error", "history", f"yazma: {exc}")

def get_history(channel: str, limit: int = HISTORY_LIMIT) -> list:
    """Son N mesaj: [{role, content}, ...] (eskiden yeniye)."""
    try:
        with db() as c:
            rows = c.execute(
                "SELECT role,content FROM chat_history WHERE channel=? "
                "ORDER BY id DESC LIMIT ?", (channel or "default", limit)).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    except Exception as exc:
        log("error", "history", f"okuma: {exc}")
        return []

def ollama_messages(messages: list, model: str = None,
                    max_tokens: int = 400) -> str:
    """Ollama /api/chat ile mesaj dizisi (gecmis destekli)."""
    model = model or OL_MODEL
    try:
        res = http_post_json(f"{OL_BASE}/api/chat", {
            "model": model, "messages": messages, "stream": False,
            "think": False,
            "options": {"num_predict": max_tokens, "temperature": 0.2},
        }, {}, timeout=240)
        msg = (res.get("message") or {})
        txt = (msg.get("content") or "").strip()
        if not txt:
            txt = (msg.get("thinking") or "").strip()
            if "Thinking Process" in txt:
                txt = txt.split("\n\n")[-1].strip()
        return txt
    except Exception as exc:
        log("error", "tier1", f"Ollama chat hatasi: {exc}")
        return ""
    except Exception as exc:
        log("error", "tier1", f"Ollama hatasi: {exc}")
        return ""

def set_agent(aid: str, state: str):
    with db() as c:
        c.execute("INSERT INTO agent_state(id,state,updated_at) VALUES(?,?,datetime('now')) "
                  "ON CONFLICT(id) DO UPDATE SET state=?, updated_at=datetime('now')",
                  (aid, state, state))


# ------------------------------------------------------------------ ajan gecis event'leri
# Panel/ofis kimlikleri kanonik kabul edilir; identity/roles takma adlari eslenir.
_AGENT_ALIAS = {
    "ayla": "filtre",
    "kurye": "postaci",
    "arsivci": "arsiv",
    "yerel": "cirak",
}

def _norm_agent(aid: str) -> str:
    """Bilinen ajan takma adini panel/ofis kimligine cevir (uydurma yok)."""
    a = (aid or "").strip().lower()
    return _AGENT_ALIAS.get(a, a)

def new_task_id(prefix: str = "task") -> str:
    """Benzersiz gorev/korelasyon kimligi."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"

def agent_event(event_type: str, source_agent: str, target_agent: str,
                status: str, task_id: str = None, summary: str = "") -> int:
    """
    Gercek ajanlar arasi gecisi yapisal olarak kaydeder.
    summary _safe_msg ile maskelenir ve 200 karakterle sinirlanir.
    Donus: benzersiz event id (basarisizsa None).
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe_summary = _safe_msg((summary or ""))[:200]
    try:
        with db() as c:
            cur = c.execute(
                "INSERT INTO agent_events(ts,event_type,source_agent,target_agent,"
                "task_id,status,summary) VALUES(?,?,?,?,?,?,?)",
                (ts, event_type, _norm_agent(source_agent), _norm_agent(target_agent),
                 task_id or "", status, safe_summary))
            c.execute("DELETE FROM agent_events WHERE id NOT IN "
                      "(SELECT id FROM agent_events ORDER BY id DESC LIMIT 2000)")
            return cur.lastrowid
    except Exception as exc:
        print("agent_event error:", exc)
        return None


_TG_BOT = None

def _tg_send(text: str):
    """Telegram gonderim cekirdegi. Donus: True/False.
    Gonderim basarisizsa (sessiz) bir kez alerts'e yazar (dedup)."""
    ok = False
    try:
        bot = globals().get("_TG_BOT")
        chat = (CFG.get("TELEGRAM_CHAT_ID") or "").strip()
        if bot and chat:
            res = bot.send(chat, text)
            ok = bool(res) if res is not None else True
        else:
            ok = False
    except Exception as exc:
        log("warn", "notify", f"telegram gonderilemedi: {_safe_msg(type(exc).__name__)}")
        ok = False
    if not ok:
        try:
            alert("warn", "notify", "tg_send_fail",
                  "Telegram gonderimi basarisiz", "bot/chat yok veya API hatasi",
                  dedup_sec=900)
        except Exception:
            pass
    return ok


def notify_user(text: str):
    """Telegram ile kullaniciya bildirim. Donus: True/False. Hata sessiz ama alarmli."""
    try:
        return _tg_send(text)
    except Exception as exc:
        log("warn", "notify", f"telegram gonderilemedi: {exc}")
        return False


def _tg_dedup(key: str, text: str, window: int = 900):
    """Ayni anahtar icin `window` sn icinde tek bildirim. Donus: gonderildi mi."""
    try:
        with db() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?",
                            ("tgdedup_" + key,)).fetchone()
        now = time.time()
        if row and row["value"]:
            try:
                if now - float(row["value"]) < window:
                    return False
            except Exception:
                pass
        sent = _tg_send(text)
        try:
            with db() as c:
                c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          ("tgdedup_" + key, str(now)))
        except Exception:
            pass
        return sent
    except Exception:
        return _tg_send(text)


def notify_owner_pending(tid: int, reason: str = ""):
    """Bir gorev OWNER_PENDING durumuna gectiginde TEK bildirim (gorev basina dedup)."""
    title = ""
    try:
        with db() as c:
            r = c.execute("SELECT title FROM agent_tasks WHERE id=?", (int(tid),)).fetchone()
            title = (r["title"] if r else "") or ""
    except Exception:
        pass
    body = f"\U000023F8 <b>OWNER_PENDING</b> \u2014 Gorev <b>#{tid}</b>\n"
    if title:
        body += _safe_msg(title)[:120] + "\n"
    if reason:
        body += f"<i>{_safe_msg(reason)[:160]}</i>\n"
    body += "\nDurum: yalniz the owner's yapabilecegi adim bekleniyor."
    return _tg_dedup(f"owner_pending_{tid}", body, window=6 * 3600)


def delegate(target: str, label: str, source: str = "panel"):
    """Sef'in bir ajana gorev devrettigini kaydeder (panel animasyonu icin)."""
    try:
        with db() as c:
            c.execute("INSERT INTO delegation(target,label,source,ts) VALUES(?,?,?,datetime('now'))",
                      (target, (label or "")[:160], source))
            c.execute("DELETE FROM delegation WHERE id NOT IN "
                      "(SELECT id FROM delegation ORDER BY id DESC LIMIT 50)")
    except Exception:
        pass
    # Gercek Asistan -> ajan devri (Telegram akisi); panel de ayni koordinatoru temsil eder.
    try:
        if _norm_agent(target) != "asistan":
            agent_event("delegated", "asistan", target, "transferred",
                        new_task_id("deleg"), f"gorev iletildi ({source})")
    except Exception:
        pass

# ------------------------------------------------------------------ Cirak + yukseltme
UNCERTAIN = [
    "bilmiyorum", "emin değilim", "emin degilim", "anlayamadım", "anlayamadim",
    "üzgünüm", "uzgunum", "cannot", "i don't know", "not sure", "as an ai",
    "yardımcı olamıyorum", "yardimci olamiyorum", "hata oluştu", "hata olustu",
]

def _cirak_weak(text: str) -> bool:
    t = (text or "").strip().lower()
    if len(t) < 12:
        return True
    return any(u in t for u in UNCERTAIN)

def ask_cirak(prompt: str, max_tokens: int = 400, task: str = "chat",
              force: str = None, channel: str = None,
              task_id: str = None) -> dict:
    """
    Cirak (Ollama) dener. Zayif/bos cevap verirse Evren'e yukseltir.
    force: None=otomatik | 'cirak'=sadece cirak | 'evren'=sadece evren
    channel: verilirse son 10 mesajlik gecmis kullanilir ve kaydedilir.
    Donus: {reply, tier, escalated, cirak_raw}
    """
    log("info", "cirak-call", f"is alindi ({task})")

    # Sohbet gecmisi (yalnizca chat/route/free islerinde)
    hist = []
    if channel and task in ("chat", "route", "free"):
        hist = get_history(channel, HISTORY_LIMIT)

    # --- Zorunlu hedef: SADECE CIRAK
    if force == "cirak":
        set_agent("cirak", "busy")
        if hist:
            msgs = [{"role": "system", "content": ident.system_prompt("cirak")}] + hist + \
                   [{"role": "user", "content": prompt}]
            raw = ollama_messages(msgs, max_tokens=max_tokens)
        else:
            raw = ollama_chat(prompt, max_tokens=max_tokens,
                              identity=ident.system_prompt("cirak"))
        set_agent("cirak", "idle")
        if not raw:
            raw = "Cirak cevap veremedi. (Evren'e yonlendirilmedi - zorunlu cirak modu)"
        if channel:
            add_history(channel, "user", prompt)
            add_history(channel, "assistant", raw)
        return {"reply": raw, "tier": 1, "escalated": False, "cirak_raw": raw}

    # --- Zorunlu hedef: SADECE EVREN
    if force == "evren":
        if active_model() == "off":
            return {"reply": "Evren kapali (model=off).", "tier": 0,
                    "escalated": False, "cirak_raw": ""}
        if not _KEYS:
            return {"reply": "Evren API anahtari yok.", "tier": 0,
                    "escalated": False, "cirak_raw": ""}
        if not evren_pool_available():
            return {"reply": "Evren havuzunda kullanilabilir saglikli anahtar yok.",
                    "tier": 0, "escalated": False, "cirak_raw": ""}
        set_agent("filtre", "busy"); set_agent("sefler", "busy")
        ev_msgs = [{"role": "system", "content": ident.system_prompt("evren")}]
        if hist:
            ev_msgs += hist
        ev_msgs.append({"role": "user", "content": prompt})
        sol, tok = evren_chat(ev_msgs, max_tokens=max_tokens)
        set_agent("filtre", "idle"); set_agent("sefler", "idle")
        if sol:
            log("ok", "route", f"Evren (zorunlu) yanitladi ({tok} tok)")
            if channel:
                add_history(channel, "user", prompt)
                add_history(channel, "assistant", sol)
            return {"reply": sol, "tier": 2, "escalated": False, "cirak_raw": ""}
        return {"reply": "Evren yanit veremedi (429/503 olabilir).",
                "tier": 0, "escalated": False, "cirak_raw": ""}

    # --- Otomatik: cirak -> gerekirse evren
    set_agent("cirak", "busy")
    if hist:
        msgs = [{"role": "system", "content": ident.system_prompt("cirak")}] + hist + \
               [{"role": "user", "content": prompt}]
        raw = ollama_messages(msgs, max_tokens=max_tokens)
    else:
        raw = ollama_chat(prompt, max_tokens=max_tokens,
                          identity=ident.system_prompt("cirak"))
    set_agent("cirak", "idle")

    result = {"reply": raw, "tier": 1, "escalated": False, "cirak_raw": raw}
    if not _cirak_weak(raw):
        if channel:
            add_history(channel, "user", prompt)
            add_history(channel, "assistant", raw)
        return result

    # ---- Yukseltme: Cirak takildi -> Evren cozum uretir
    if active_model() == "off" or not _KEYS or not evren_pool_available():
        log("warn", "escalate",
            f"Cirak takildi ama Evren yok (model={active_model()}, "
            f"havuz={'hazir' if _KEYS else 'bos'}).")
        return result

    log("warn", "escalate", f"Cirak takildi -> Evren'e soruluyor ({task})")
    esc_tid = task_id or new_task_id("esc")
    agent_event("escalate", "cirak", "evren", "started", esc_tid,
                f"cirak takildi, evrene yukseltildi ({task})")
    set_agent("filtre", "busy"); set_agent("sefler", "busy")
    sys_msg = (
        ident.system_prompt("evren") + "\n\n"
        "Cirak (kucuk yerel model) su gorevi basaramadi. "
        "Gorevi SEN coz ve SONUCU dogrudan ver. Aciklama/ozur yazma, sadece istenen cikti. "
        "Ciktiyi ciraga aynen uygulatacagiz."
    )
    sol, tok = evren_chat([
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": prompt},
    ], max_tokens=min(max_tokens + 200, 900))
    set_agent("filtre", "idle"); set_agent("sefler", "idle")

    if sol:
        log("ok", "escalate", f"Evren cozum uretti ({tok} tok) -> ciraga verildi")
        agent_event("escalate", "evren", "cirak", "completed", esc_tid,
                    f"evren cozum uretti, ciraga dondu ({tok} tok)")
        with db() as c:
            c.execute("INSERT INTO escalation(prompt,task,cirak_raw,evren_reply,tokens,ts) "
                      "VALUES(?,?,?,?,?,datetime('now'))",
                      (prompt[:2000], task, (raw or "")[:1000], sol[:4000], tok))
        if channel:
            add_history(channel, "user", prompt)
            add_history(channel, "assistant", sol)
        return {"reply": sol, "tier": 2, "escalated": True, "cirak_raw": raw}

    log("error", "escalate", "Evren de cevap veremedi")
    agent_event("escalate", "evren", "cirak", "error", esc_tid,
                "evren cozum uretemedi")
    if channel:
        add_history(channel, "user", prompt)
    return result


# ------------------------------------------------------------------ Ajan yukseltme (tumu)
def ask_evren_escalate(agent_id: str, task: str, error: str,
                       context: str = "", max_tokens: int = 500,
                       task_id: str = None) -> str:
    """
    Herhangi bir ajan hata aldiginda Evren'den cozum ister.
    Donus: Evren'in uygulanabilir talimati (metin). Bos ise cozum yok.
    """
    if active_model() == "off" or not _KEYS:
        return ""
    esc_tid = task_id or new_task_id("esc")
    agent_event("escalate", agent_id, "evren", "started", esc_tid,
                f"ajan hatasi, evrene yukseltildi ({_norm_agent(agent_id)})")
    prompt = (
        f"Sen Evren'sin, bulut beyin ve kurtarici ajansin.\n"
        f"Ajan '{agent_id}' su gorevi yaparken hata aldi.\n\n"
        f"GOREV: {task}\n"
        f"HATA: {error}\n"
        f"BAGLAM: {context[:1500]}\n\n"
        f"Ajana ne yapmasi gerektigini NET ve UYGULANABILIR sekilde yaz. "
        f"Kisa tut (max 5 madde). Kullaniciya degil, ajana hitap et."
    )
    set_agent("evren", "busy")
    try:
        sol, tok = evren_chat([
            {"role": "system", "content": ident.system_prompt("evren")},
            {"role": "user", "content": prompt},
        ], max_tokens=max_tokens)
    finally:
        set_agent("evren", "idle")
    if sol:
        try:
            with db() as c:
                c.execute("INSERT INTO escalation(prompt,task,cirak_raw,evren_reply,tokens,ts) "
                          "VALUES(?,?,?,?,?,datetime('now'))",
                          (prompt[:2000], f"escalate:{agent_id}",
                           (error or "")[:1000], sol[:4000], tok))
        except Exception:
            pass
        log("ok", "escalate", f"{agent_id} hatasi -> Evren cozum uretti ({tok} tok)")
        agent_event("escalate", "evren", agent_id, "completed", esc_tid,
                    f"evren cozum uretti ({tok} tok)")
    else:
        agent_event("escalate", "evren", agent_id, "error", esc_tid,
                    "evren cozum uretemedi")
    return sol or ""


def agent_run_with_escalation(agent_id: str, task: str, fn):
    """
    fn() calistir; hata/False donerse Evren'den cozum iste, tekrar dene.
    fn -> (ok: bool, result) dondurmeli.
    Donus: (ok, result, escalated)
    """
    try:
        ok, res = fn()
    except Exception as exc:
        ok, res = False, f"exception: {exc}"
    if ok:
        return True, res, False
    sol = ask_evren_escalate(agent_id, task, str(res)[:500])
    if sol:
        return False, res, True
    return False, res, False


# ------------------------------------------------------------------ Evren -> Cirak
def evren_delegate_to_cirak(task: str, max_tokens: int = 400,
                            task_id: str = None) -> str:
    """
    Evren'in agir/basit isleri Cirak'a devretmesi.
    Cirak raporu SADECE Evren'e doner (the owner gormez).
    """
    del_tid = task_id or new_task_id("deleg")
    try:
        log("info", "delegate", f"evren -> cirak: {task[:60]}")
        agent_event("delegated", "evren", "cirak", "started", del_tid,
                    "evren ciraga is verdi")
        set_agent("cirak", "busy")
        raw = ollama_chat(task, max_tokens=max_tokens,
                          identity=ident.system_prompt("cirak"))
        set_agent("cirak", "idle")
        if not raw:
            agent_event("delegated", "cirak", "evren", "error", del_tid,
                        "cirak rapor veremedi")
            return "(cirak rapor veremedi)"
        log("ok", "delegate", f"cirak raporu alindi ({len(raw)} karakter)")
        agent_event("delegated", "cirak", "evren", "completed", del_tid,
                    f"cirak raporu evrene dondu ({len(raw)} karakter)")
        return raw
    except Exception as exc:
        log("error", "delegate", f"evren->cirak: {exc}")
        agent_event("delegated", "cirak", "evren", "error", del_tid,
                    "cirak hatasi")
        return f"(cirak hatasi: {exc})"


def evren_with_cirak(task: str, max_tokens: int = 700) -> str:
    """
    Evren once planlar; gerekirse Cirak'a is verir; nihai cevabi Evren verir.
    Donus: the owner gidecek metin (Evren imzali).
    """
    # 1) Evren plan kurar: cirak'a ne yaptirmali?
    plan, _ = evren_chat([
        {"role": "system", "content": ident.system_prompt("evren")},
        {"role": "user", "content": (
            "Asagidaki gorevi analiz et. Eger is basit/tekrarlanabilir ise "
            "(ozet, siniflandirma, cikarim, kisa hesap) SADECE su satiri yaz: "
            "CIRAK: <cirak'a verilecek net gorev>\n"
            "Eger is senin (Evren) cozmen gereken karmasik bir isse "
            "SADECE su satiri yaz: EVREN\n\n"
            f"GOREV: {task}")},
    ], max_tokens=200)
    plan = (plan or "").strip()
    if plan.upper().startswith("CIRAK"):
        subtask = plan.split(":", 1)[1].strip() if ":" in plan else task
        rap = evren_delegate_to_cirak(subtask, max_tokens=350)
        # 2) Evren, Cirak raporunu the owner icin duzenler
        final, _ = evren_chat([
            {"role": "system", "content": ident.system_prompt("evren")},
            {"role": "user", "content": (
                f"the owner sunu istedi: {task}\n\n"
                f"Cirak'in raporu: {rap}\n\n"
                "Bu raporu the owner 1-3 cumleyle ozetle. Cirak adini anma, "
                "islini sen yapmissin gibi sun.")},
        ], max_tokens=max_tokens)
        return (final or rap)
    # karmasik is -> Evren kendisi
    final, _ = evren_chat([
        {"role": "system", "content": ident.system_prompt("evren")},
        {"role": "user", "content": task},
    ], max_tokens=max_tokens)
    return final or ""


# ------------------------------------------------------------------ Tier 2: Evren
# ========== COKLU API KEY HAVUZU (Apiman) ==========
# Apiman: deterministik API/kota yoneticisi. LLM degil; sohbet uretmez, token tuketmez,
# Telegram komutu yurutmez, EVREN kararlarina karismaz. Yalniz key havuzunu yonetir.
# Uc EVREN anahtari BAGIMSIZ gercek kaynaktir; tek mantiksal havuz olarak yonetilir.
# Secim istek basina ve thread-safe. Sticky: saglikli oldukca ayni anahtarda kalinir.
_KEY_LOCK = threading.RLock()
_KEY_PREF = {"idx": None}          # tercih edilen (sticky) anahtar indeksi
_KEY_META = {}                     # idx -> durum sozlugu
_KEY_LAST_EVENT = {}               # (event_type, idx) -> monotonic ts (spam koruma)

# Guvenli gecis: kalan < tahmini ihtiyac + rezerv ise o anahtara yeni is verilmez.
_KEY_RESERVE = int(CFG.get("EVREN_KEY_RESERVE", "10000") or 10000)
# Ilave rezerv orani (0 = kapali / mevcut davranis korunur). Yapilandirilabilir.
_POOL_RESERVE_RATIO = float(CFG.get("EVREN_POOL_RESERVE_RATIO", "0") or 0)

_KEY_STATE_TR = {
    "healthy": "sağlıklı", "degraded": "kotalı", "cooldown": "beklemede",
    "exhausted": "tükendi", "invalid": "geçersiz", "unknown": "bilinmiyor",
}


def _key_label(idx):
    return f"EVREN-{idx + 1}"


def _mask_key(k):
    """Maskele: yalniz son 4 karakter. Ham anahtar asla panel/event/log'a donmez."""
    if not k or len(k) <= 4:
        return "••••"
    return "••••" + k[-4:]


def _key_meta(idx):
    m = _KEY_META.get(idx)
    if m is None:
        m = {"state": "unknown", "cooldown_until": 0.0, "exhausted_until": 0.0,
             "last_success": 0.0, "last_error": "", "fail_count": 0, "invalid": False}
        _KEY_META[idx] = m
    return m


def _load_key_idx():
    with _KEY_LOCK:
        if _KEY_PREF["idx"] is not None:
            return _KEY_PREF["idx"]
    try:
        with db() as c:
            row = c.execute("SELECT value FROM settings WHERE key='evren_key_idx'").fetchone()
            idx = int(row["value"]) if row and row["value"] else 0
    except Exception:
        idx = 0
    if idx < 0 or idx >= len(_KEYS):
        idx = 0
    with _KEY_LOCK:
        _KEY_PREF["idx"] = idx
    return idx


def _save_key_idx(idx):
    with _KEY_LOCK:
        _KEY_PREF["idx"] = idx
    try:
        with db() as c:
            c.execute("INSERT INTO settings(key,value) VALUES('evren_key_idx',?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(idx),))
    except Exception:
        pass


def _active_key():
    if not _KEYS:
        return ""
    return _KEYS[_load_key_idx()]


# ---- Anahtar basina gercek kota (60 sn cache) ----
_QUOTA_CACHE = {}


def key_quota(idx):
    """Belirli anahtarin /v1/quota verisi (60 sn cache). Anahtar degeri loglanmaz."""
    if idx < 0 or idx >= len(_KEYS):
        return {}
    now = time.time()
    c = _QUOTA_CACHE.get(idx)
    if c and now - c["at"] < 60:
        return c["data"]
    try:
        d = http_get_json(f"{EV_BASE}/v1/quota", {EV_HDR: _KEYS[idx]}, timeout=12)
        _QUOTA_CACHE[idx] = {"at": now, "data": d}
        return d
    except Exception as exc:
        log("warn", "quota", f"{_key_label(idx)} kota alinamadi: {_safe_msg(str(exc))[:120]}")
        return (c["data"] if c else {})


def _remaining_from_quota(q):
    """Kalan kapasite; bilinmiyorsa None."""
    if not q:
        return None
    left = q.get("daily_remaining_tokens")
    if left is not None:
        try:
            return int(left)
        except Exception:
            return None
    try:
        cap, used = q.get("cap"), q.get("used_tokens")
        if cap is not None and used is not None:
            return int(cap) - int(used)
    except Exception:
        return None
    return None


def _reset_epoch(q):
    """daily_reset_at -> epoch (basarisizsa 0)."""
    s = (q or {}).get("daily_reset_at")
    if not s:
        return 0.0
    try:
        return datetime.strptime(str(s).replace("Z", "").replace("T", " ")[:19],
                                 "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return 0.0


def _emit_key_event(etype, idx, summary, throttle=30):
    """Yalniz anlamli durum degisikliginde, spam yapmadan guvenli event uret."""
    now = time.monotonic()
    k = (etype, idx)
    if now - _KEY_LAST_EVENT.get(k, 0) < throttle:
        return None
    _KEY_LAST_EVENT[k] = now
    try:
        return agent_event(etype, "apiman", "evren", "info",
                           new_task_id("key"), summary)
    except Exception:
        return None


def _key_display_state(idx, q=None):
    """Kavramsal durum: healthy/degraded/cooldown/exhausted/invalid/unknown."""
    m = _key_meta(idx)
    now = time.time()
    if m["invalid"]:
        return "invalid"
    if now < m["cooldown_until"]:
        return "cooldown"
    if now < m["exhausted_until"]:
        return "exhausted"
    if q is None:
        q = key_quota(idx)
    rem = _remaining_from_quota(q)
    if rem is None:
        return "unknown"
    if rem <= 0:
        return "exhausted"
    if rem < _KEY_RESERVE:
        return "degraded"
    return "healthy"


def _eligible(idx, now):
    m = _key_meta(idx)
    if m["invalid"] or now < m["cooldown_until"] or now < m["exhausted_until"]:
        return False
    return True


# Kota/limit kaynakli hata metinleri (TR + EN). Kod ne olursa olsun bu metin
# gorulurse key "exhausted" sayilir -> sonraki key denenir.
_QUOTA_RX = re.compile(
    r"(?i)(daily[^.]{0,30}(token|quota|limit|usage)|quota|exceeded|insufficient|"
    r"insufficient_quota|budget|out of credit|capacity|exhaust|no remaining|"
    r"a[sş][ıi]ld[ıi]|kota|limitiniz|yetersiz)")


def _is_quota_error(body: str) -> bool:
    """Provider govdesi kota/limit kaynakli mi?"""
    if not body:
        return False
    return bool(_QUOTA_RX.search(body))


def select_key(need_tokens=0, consumer="evren"):
    """
    Istek basina thread-safe saglikli anahtar secimi (sticky).
    Kalan < need + rezerv ise o anahtara yeni is verilmez; sonraki denenir.
    Devam eden istek asla yarida kesilmez; yalniz yeni istek bu secimi kullanir.
    Donus: idx veya None.
    """
    if not _KEYS:
        return None
    need = max(int(need_tokens or 0), 0)
    now = time.time()
    pref = _load_key_idx()
    order = [(pref + i) % len(_KEYS) for i in range(len(_KEYS))]
    unknown_fallback = None
    for idx in order:
        with _KEY_LOCK:
            if not _eligible(idx, now):
                continue
        q = key_quota(idx)
        rem = _remaining_from_quota(q)
        if rem is None:
            if unknown_fallback is None:
                unknown_fallback = idx
            continue
        reserve = _KEY_RESERVE + int(need * _POOL_RESERVE_RATIO)
        if rem <= 0:
            with _KEY_LOCK:
                m = _key_meta(idx)
                reset = _reset_epoch(q)
                m["exhausted_until"] = max(reset, now + 60)
                m["state"] = "exhausted"
            _emit_key_event("key_exhausted", idx,
                            f"{_key_label(idx)} tükendi; reset bekleniyor", throttle=60)
            continue
        if rem < need + reserve:
            with _KEY_LOCK:
                _key_meta(idx)["state"] = "degraded"
            _emit_key_event("key_low", idx,
                            f"{_key_label(idx)} kotası azalıyor", throttle=300)
            continue
        with _KEY_LOCK:
            _mm = _key_meta(idx)
            _mm["prev_state"] = _mm["state"]
            _mm["state"] = "healthy"
            changed = (_KEY_PREF["idx"] != idx)
            _KEY_PREF["idx"] = idx
        if changed:
            try:
                with db() as c:
                    c.execute("INSERT INTO settings(key,value) VALUES('evren_key_idx',?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (str(idx),))
            except Exception:
                pass
            _emit_key_event("key_switched", idx,
                            f"{_key_label(idx)} aktif; sağlıklı hat", throttle=5)
        return idx
    if unknown_fallback is not None:
        with _KEY_LOCK:
            changed = (_KEY_PREF["idx"] != unknown_fallback)
            _KEY_PREF["idx"] = unknown_fallback
            _key_meta(unknown_fallback)["state"] = "unknown"
        if changed:
            _emit_key_event("key_switched", unknown_fallback,
                            f"{_key_label(unknown_fallback)} denendi (durum bilinmiyor)",
                            throttle=5)
        return unknown_fallback
    return None


def evren_pool_available():
    """Havuzda yeni is icin kullanilabilir saglikli anahtar var mi? (yerel sayac engellemez)"""
    try:
        return select_key(_KEY_RESERVE, "evren") is not None
    except Exception:
        return bool(_KEYS)


def quota_all():
    """Tum anahtarlarin gercek kota + durum ozeti (panel icin). Ham anahtar donmez."""
    cur = _load_key_idx()
    out = []
    total_rem = 0
    total_lim = 0
    healthy = 0
    for i, k in enumerate(_KEYS):
        q = key_quota(i)
        st = _key_display_state(i, q)
        m = _key_meta(i)
        rem = _remaining_from_quota(q)
        lim = q.get("daily_limit_tokens")
        used = q.get("daily_used_tokens")
        if rem is not None:
            total_rem += rem
        if lim:
            try:
                total_lim += int(lim)
            except Exception:
                pass
        if st == "healthy":
            healthy += 1
        cooldown_left = max(0, int(m["cooldown_until"] - time.time()))
        out.append({
            "index": i,
            "label": _key_label(i),
            "masked": _mask_key(k),
            "active": i == cur,
            "ok": bool(q),
            "state": st,
            "state_label": _KEY_STATE_TR.get(st, st),
            "used": used,
            "remaining": rem,
            "limit": lim,
            "reset": q.get("daily_reset_at"),
            "cooldown_left": cooldown_left,
            "last_success": (datetime.fromtimestamp(m["last_success"]).strftime("%Y-%m-%d %H:%M:%S")
                             if m["last_success"] else None),
            "last_error": m["last_error"] or None,
            # geriye uyumlu alanlar
            "daily_used": used,
            "daily_limit": lim,
            "daily_remaining": rem,
            "window_used": q.get("used_tokens"),
            "window_cap": q.get("cap"),
            "window_remaining": ((q.get("cap") or 0) - (q.get("used_tokens") or 0))
                                if q.get("cap") is not None else None,
            "credits": q.get("remaining_cr"),
            "level": q.get("level"),
        })
    return {"keys": out, "active": cur, "count": len(_KEYS),
            "pool": {"total_remaining": total_rem, "total_limit": total_lim,
                     "healthy": healthy, "count": len(_KEYS)}}


def auto_switch_if_low(threshold=None):
    """Aktif anahtar yeni is icin yetersizse saglikli anahtara gec (sticky korunur)."""
    if len(_KEYS) < 2:
        return False
    need = _KEY_RESERVE if threshold is None else int(threshold)
    before = _load_key_idx()
    sel = select_key(need, "evren")
    return bool(sel is not None and sel != before)


# ------------------------------------------------------------------ Apiman: kota izleyici
# Tek gorevi: kotalari takip et, azalan aktif anahtari dolu olanla degistir, raporla.
APIMAN_SEC = int(CFG.get("APIMAN_CHECK_SECONDS", "300") or 300)
APIMAN_LOW_PCT = float(CFG.get("APIMAN_LOW_PCT", "20") or 20)
APIMAN_CRIT_PCT = float(CFG.get("APIMAN_CRIT_PCT", "10") or 10)
APIMAN_MIN_GAIN_PCT = float(CFG.get("APIMAN_MIN_GAIN_PCT", "30") or 30)
APIMAN_FLAP_SEC = int(CFG.get("APIMAN_FLAP_SECONDS", "600") or 600)
_APIMAN_STATE = {"last_switch": 0.0, "last_report": 0.0}


def _pct(rem, lim):
    try:
        if not lim:
            return None
        return (float(rem) / float(lim)) * 100.0
    except Exception:
        return None


def _apiman_pick(cur, keys, pcts, cur_p):
    """Geçilecek hedef anahtari sec. Donus: (index, reason) veya (None, '')."""
    cands = [k for k in keys
             if k["index"] != cur and pcts.get(k["index"]) is not None]
    if not cands:
        return None, ""
    best = max(cands, key=lambda k: pcts.get(k["index"]) or -1)
    bp = pcts.get(best["index"]) or 0
    if cur_p < APIMAN_CRIT_PCT and bp > cur_p:
        return best["index"], f"aktif %{cur_p:.0f} kritik -> %{bp:.0f}"
    if cur_p < APIMAN_LOW_PCT and bp >= cur_p + APIMAN_MIN_GAIN_PCT:
        return best["index"], f"aktif %{cur_p:.0f} dusuk -> hedef %{bp:.0f}"
    return None, ""


def apiman_check_once(notify=True):
    """Kotalari gozle; gerekirse aktif anahtari degistir. Donus: ozet dict.
    Panel isigi icin busy/idle sarmalayici."""
    try:
        set_agent("apiman", "busy")
    except Exception:
        pass
    try:
        return _apiman_check_impl(notify=notify)
    except Exception:
        raise
    finally:
        try:
            set_agent("apiman", "idle")
        except Exception:
            pass


def _apiman_check_impl(notify=True):
    """Kotalari gozle (ic uygulama)."""
    out = {"action": "none", "from": None, "to": None,
           "pct": {}, "reason": "", "report": ""}
    try:
        qa = quota_all()
    except Exception as exc:
        log("warn", "apiman", f"kota okunamadi: {_safe_msg(str(exc))[:80]}")
        return out
    keys = qa.get("keys") or []
    if len(keys) < 2:
        return out
    cur = qa.get("active", 0)
    pcts = {k["index"]: _pct(k.get("remaining"), k.get("limit")) for k in keys}
    out["pct"] = {k["label"]: (round(pcts[k["index"]], 1)
                               if pcts.get(k["index"]) is not None else None)
                  for k in keys}
    cur_p = pcts.get(cur)
    cur_label = next((k["label"] for k in keys if k["index"] == cur), f"#{cur}")
    out["from"] = cur_label
    if cur_p is None:
        return out

    # hepsi kritik -> kurtarilacak sey yok, alarm
    vals = [p for p in pcts.values() if p is not None]
    if vals and all(p < APIMAN_CRIT_PCT for p in vals):
        alert("critical", "apiman", "pool_low",
              "Tum API anahtarlari kritik seviyede",
              " | ".join(f"{k['label']}=%{pcts[k['index']]:.0f}" for k in keys),
              dedup_sec=1800)
        if notify and time.time() - _APIMAN_STATE["last_report"] > 1800:
            _APIMAN_STATE["last_report"] = time.time()
            notify_user("\u26A0\ufe0f <b>Apiman</b>: T\u00fcm anahtarlar %"
                        f"{APIMAN_CRIT_PCT:.0f} alt\u0131nda. "
                        + ", ".join(f"{l}=%{p:.0f}" for l, p in out["pct"].items()
                                    if p is not None)
                        + ". De\u011fi\u015ftirilecek taze anahtar yok.")
        out["action"] = "alarm"
        return out
    try:
        alert_resolve("apiman", "pool_low")
    except Exception:
        pass

    target, reason = _apiman_pick(cur, keys, pcts, cur_p)
    if target is None:
        return out

    # flap korumasi
    now = time.time()
    if now - _APIMAN_STATE["last_switch"] < APIMAN_FLAP_SEC:
        log("warn", "apiman", "gecis flap korumasi ile ertelendi")
        return out

    to_label = next((k["label"] for k in keys if k["index"] == target), f"#{target}")
    to_p = pcts.get(target)
    try:
        _save_key_idx(target)
    except Exception:
        return out
    _APIMAN_STATE["last_switch"] = now
    out["action"] = "switched"
    out["to"] = to_label
    out["reason"] = reason
    log("warn", "apiman", f"{cur_label} -> {to_label} ({reason})")
    _emit_key_event("key_switched", target,
                    f"{to_label} aktif (%{to_p:.0f}); neden: {reason}", throttle=0)
    parts = ", ".join(f"{l}=%{p:.0f}" for l, p in out["pct"].items() if p is not None)
    out["report"] = (f"\U0001F511 <b>Apiman</b>: {cur_label} -> <b>{to_label}</b> "
                     f"({reason}). Kesinti yok.\nAnahtarlar: {parts}\n"
                     f"Havuz: {qa['pool']['total_remaining']/1e6:.1f}M / "
                     f"{qa['pool']['total_limit']/1e6:.0f}M")
    if notify:
        try:
            notify_user(out["report"])
        except Exception:
            pass
    return out


def apiman_loop():
    """5 dk'da bir kota denetimi (acilista 60 sn sonra bir kez)."""
    time.sleep(60)
    while True:
        try:
            apiman_check_once()
            _hb("apiman")
        except Exception as exc:
            log("error", "apiman", f"dongu: {_safe_msg(str(exc))[:90]}")
        time.sleep(APIMAN_SEC)


def key_status():
    """Panel icin: tum anahtarlar + aktif indeks."""
    cur = _load_key_idx()
    out = []
    for i, k in enumerate(_KEYS):
        out.append({"index": i, "label": _key_label(i), "masked": _mask_key(k),
                    "active": i == cur, "state": _key_display_state(i)})
    return {"keys": out, "active": cur, "count": len(_KEYS)}


def switch_key(idx=None):
    """Manuel anahtar degisimi. Donus: True/False. Sadece gecerli anahtara izin verir."""
    if len(_KEYS) < 2:
        return False
    cur = _load_key_idx()
    if idx is None:
        idx = (cur + 1) % len(_KEYS)
    if idx < 0 or idx >= len(_KEYS):
        return False
    if idx == cur:
        return False
    if _key_meta(idx)["invalid"]:
        return False
    _save_key_idx(idx)
    with _KEY_LOCK:
        _key_meta(idx)["state"] = _key_display_state(idx)
    log("warn", "tier2", f"Evren anahtari degistirildi -> {_key_label(idx)}")
    _emit_key_event("key_switched", idx, f"{_key_label(idx)} manuel aktif", throttle=0)
    notify_user(f"🔑 <b>Apiman</b>: API hattı → <b>{_key_label(idx)}</b>")
    return True


def _switch_key():
    """Geriye uyumluluk: otomatik sonraki anahtara gec."""
    return switch_key(None)


def _evren_pool_chat(messages: list, max_tokens: int = 600,
                     consumer: str = "evren", timeout: int = 120) -> tuple:
    """Yalniz EVREN (evren-llmapi) anahtar havuzu. Bossa/biterse ("",0).
    T23: bu fonksiyon sadece havuzu dener; dis saglayici failover'i
    evren_chat() orkestre eder."""
    if not _KEYS:
        return "", 0
    is_evren = (consumer == "evren")
    if is_evren:
        set_agent("evren", "busy")
    try:
        attempts = max(1, len(_KEYS))
        tried = set()
        last_err = ""
        for _ in range(attempts):
            idx = select_key(max_tokens, consumer)
            if idx is None or idx in tried:
                break
            tried.add(idx)
            key = _KEYS[idx]
            try:
                res = http_post_json(f"{EV_BASE}/v1/chat/completions", {
                    "model": EV_MODEL, "messages": messages, "max_tokens": max_tokens,
                }, {EV_HDR: key}, timeout=timeout)
            except urllib.error.HTTPError as exc:
                code = exc.code
                body = ""
                try:
                    body = _safe_msg(exc.read().decode("utf-8", "ignore")[:500])
                except Exception:
                    body = ""
                last_err = f"HTTP {code}"
                quota_err = _is_quota_error(body)
                # --- 1) KOTA/LIMIT: koddan bagimsiz -> exhausted, sonraki key ---
                if quota_err:
                    reset = 0.0
                    try:
                        reset = _reset_epoch(key_quota(idx))
                    except Exception:
                        reset = 0.0
                    until = max(reset, time.time() + 1800)   # en az 30 dk
                    with _KEY_LOCK:
                        m = _key_meta(idx)
                        m["exhausted_until"] = until
                        m["state"] = "exhausted"
                        m["last_error"] = f"quota ({last_err})"
                    _QUOTA_CACHE.pop(idx, None)   # bayat "bol kota" gorunumunu sil
                    _emit_key_event("key_exhausted", idx,
                                    f"{_key_label(idx)} kotası tükendi; "
                                    f"reset {datetime.fromtimestamp(until).strftime('%H:%M')}",
                                    throttle=0)
                    alert("warn", "apiman", f"key_exhausted_{idx}",
                          f"{_key_label(idx)} kotası tükendi",
                          f"{last_err}: {body[:250]}", dedup_sec=1800)
                    log("warn", "tier2",
                        f"{_key_label(idx)} kota hatasi ({last_err}) -> exhausted, "
                        f"sonraki key deneniyor")
                    continue
                if code in (401, 403):
                    with _KEY_LOCK:
                        m = _key_meta(idx)
                        m["invalid"] = True
                        m["state"] = "invalid"
                        m["last_error"] = last_err
                    _emit_key_event("key_invalid", idx,
                                    f"{_key_label(idx)} geçersiz; havuzdan çıkarıldı",
                                    throttle=0)
                    log("warn", "tier2", f"{_key_label(idx)} HTTP {code} -> gecersiz")
                    continue
                if code == 429:
                    try:
                        ra = int(exc.headers.get("Retry-After") or 60)
                    except Exception:
                        ra = 60
                    ra = max(5, min(ra, 3600))
                    with _KEY_LOCK:
                        m = _key_meta(idx)
                        m["cooldown_until"] = time.time() + ra
                        m["state"] = "cooldown"
                        m["last_error"] = last_err
                    key_quota(idx)
                    _emit_key_event("key_cooldown", idx,
                                    f"{_key_label(idx)} geçici beklemede (429)", throttle=20)
                    log("warn", "tier2", f"{_key_label(idx)} HTTP 429 -> cooldown {ra}s")
                    continue
                if 500 <= code < 600:
                    with _KEY_LOCK:
                        m = _key_meta(idx)
                        m["cooldown_until"] = time.time() + 20
                        m["state"] = "cooldown"
                        m["last_error"] = last_err
                    _emit_key_event("key_cooldown", idx,
                                    f"{_key_label(idx)} geçici beklemede (servis)",
                                    throttle=60)
                    log("warn", "tier2", f"{_key_label(idx)} HTTP {code} -> kisa cooldown")
                    continue
                # taninmayan hata: key'i kisa sure dinlendir, sonraki key'i dene
                with _KEY_LOCK:
                    m = _key_meta(idx)
                    m["cooldown_until"] = time.time() + 60
                    m["state"] = "cooldown"
                    m["last_error"] = f"{last_err}: {body[:80]}"
                log("error", "tier2",
                    f"{_key_label(idx)} HTTP {code}: {body[:120]} -> sonraki key")
                continue
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = _safe_msg(str(exc))[:120]
                with _KEY_LOCK:
                    m = _key_meta(idx)
                    m["cooldown_until"] = time.time() + 15
                    m["state"] = "cooldown"
                    m["last_error"] = last_err
                _emit_key_event("key_cooldown", idx,
                                f"{_key_label(idx)} geçici beklemede (ağ)", throttle=60)
                log("warn", "tier2", f"{_key_label(idx)} ag/zaman asimi -> kisa cooldown")
                continue
            # basarili
            txt = (res.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            tok = int((res.get("usage") or {}).get("total_tokens") or 0)
            add_tokens(tok, consumer)
            with _KEY_LOCK:
                m = _key_meta(idx)
                prev = m.get("prev_state", m["state"])
                m["last_success"] = time.time()
                m["fail_count"] = 0
                m["last_error"] = ""
                m["state"] = "healthy"
            if prev in ("cooldown", "exhausted", "degraded"):
                _emit_key_event("key_recovered", idx,
                                f"{_key_label(idx)} tekrar kullanılabilir", throttle=0)
            try:
                alert_resolve("apiman", f"key_exhausted_{idx}")
                alert_resolve("apiman", "all_exhausted")
            except Exception:
                pass
            comp = "evren-call" if consumer == "evren" else f"{consumer}-call"
            log("info", comp, f"ok ({tok} tok, {_key_label(idx)})")
            return txt.strip(), tok
        if last_err:
            # E3: yalniz havuz GERCEKTEN kullanilamazsa alarm; tek cagri
            # basarisizligi (gecici ag/timeout) kota saglikliyken alarm uretmesin.
            try:
                _usable = _wd_key_pool_state()["usable"]
            except Exception:
                _usable = 0
            if _usable > 0:
                try:
                    alert_resolve("apiman", "all_exhausted")
                except Exception:
                    pass
            else:
                alert("critical", "apiman", "all_exhausted",
                      "Havuzda kullanılabilir anahtar kalmadı",
                      f"denenen={len(tried)} son_hata={last_err}", dedup_sec=900)
            log("warn", "tier2",
                f"Evren havuzunda uygun anahtar kalmadi ({last_err})")
        return "", 0
    except Exception as exc:
        log("error", "tier2", f"Evren hatasi: {_safe_msg(str(exc))[:160]}")
        return "", 0
    finally:
        if is_evren:
            set_agent("evren", "idle")


# ==================================================================
# T23 — COK-SAGLAYICI HAVUZ (evren -> gemini -> groq -> cloudflare)
# ------------------------------------------------------------------
# Amac: evren-llmapi kapansa bile ajanlar UCRETSIZ saglayicilarla calissin.
# Hepsi OpenAI-uyumlu /chat/completions. Sir degerleri ASLA loglanmaz.
# Bulut havuzu daima onceliklidir; ollama/yerel yalniz degrade.
# ==================================================================
_PROVIDER_USAGE_LOCK = threading.Lock()
# Bazi saglayicilar (Groq) UA'siz istekleri Cloudflare ile 403'ler (code 1010).
_PROVIDER_UA = "EvrenAgent/1.0 (+https://evren.example)"


def _provider_usage_file():
    return ROOT / "data" / "provider_usage.json"


def _provider_usage_read():
    today = datetime.now().date().isoformat()
    try:
        d = json.loads(_provider_usage_file().read_text(encoding="utf-8"))
        if d.get("date") == today:
            return d.get("counts", {}) or {}
    except Exception:
        pass
    return {}


def _provider_usage_bump(pid):
    with _PROVIDER_USAGE_LOCK:
        counts = _provider_usage_read()
        counts[pid] = int(counts.get(pid) or 0) + 1
        f = _provider_usage_file()
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps({"date": datetime.now().date().isoformat(),
                                       "counts": counts}), encoding="utf-8")
            os.replace(tmp, f)
        except Exception:
            pass
        return counts[pid]


def _provider_limit(pid):
    """Gunluk istek limiti .env'den (LIMIT_*). Aşırıya kacmamak icin muhafazakar."""
    defaults = {"gemini": 1400, "groq": 1300, "cloudflare": 9000}
    try:
        return int(CFG.get(f"LIMIT_{pid.upper()}_REQ",
                           str(defaults.get(pid, 1000))) or defaults.get(pid, 1000))
    except Exception:
        return defaults.get(pid, 1000)


def _provider_blocked(pid):
    """%95 limit -> saglayiciyi atla (limit ASLA asilmaz)."""
    lim = _provider_limit(pid)
    used = int(_provider_usage_read().get(pid) or 0)
    return used >= int(lim * LIMIT_CRIT_PCT / 100.0)


def _providers():
    """Yapilandirilmis dis saglayicilar, oncelik sirasiyla. Sir icermez."""
    out = []
    gkey = (PROV_CFG.get("GEMINI_API_KEY") or "").strip()
    gbase = (PROV_CFG.get("GEMINI_BASE_URL") or "").strip().rstrip("/")
    gmodel = (PROV_CFG.get("GEMINI_MODEL") or "").strip()
    if gkey and gbase and gmodel:
        out.append({"id": "gemini", "name": "Gemini", "base": gbase,
                    "key": gkey, "model": gmodel})
    rkey = (PROV_CFG.get("GROQ_API_KEY") or "").strip()
    rbase = (PROV_CFG.get("GROQ_BASE_URL") or "").strip().rstrip("/")
    rmodel = (PROV_CFG.get("GROQ_MODEL") or "").strip()
    if rkey and rbase and rmodel:
        out.append({"id": "groq", "name": "Groq", "base": rbase,
                    "key": rkey, "model": rmodel})
    cftok = (CFG.get("CLOUDFLARE_API_TOKEN") or "").strip()
    cfacct = (CFG.get("CF_ACCOUNT_ID") or "").strip()
    cfmodel = (CFG.get("CF_AI_MODEL") or "@cf/meta/llama-3.1-8b-instruct").strip()
    if cftok and cfacct:
        out.append({"id": "cloudflare", "name": "Cloudflare Workers AI",
                    "base": (f"https://api.cloudflare.com/client/v4/accounts/"
                             f"{cfacct}/ai/v1"),
                    "key": cftok, "model": cfmodel})
    return out


def _provider_chat(p, messages, max_tokens, timeout):
    """Tek saglayiciya OpenAI-uyumlu cagri. (metin, tok). Hata -> exception.
    Not: reasoning modelleri (gpt-oss vb.) butceyi dusunme icin harcayabilir;
    bu yuzden en az 256 token birakilir, aksi halde content bos donebilir."""
    mt = max(int(max_tokens or 0), 256)
    res = http_post_json(p["base"] + "/chat/completions", {
        "model": p["model"], "messages": messages,
        "max_tokens": mt,
    }, {"Authorization": "Bearer " + p["key"],
        "User-Agent": _PROVIDER_UA}, timeout=timeout)
    choice = (res.get("choices") or [{}])[0]
    msg = choice.get("message", {}) or {}
    txt = msg.get("content") or ""
    if not txt:
        txt = msg.get("reasoning") or ""
    tok = int((res.get("usage") or {}).get("total_tokens") or 0)
    return txt.strip(), tok


def provider_chat(messages, max_tokens=600, consumer="evren", timeout=60):
    """Dis saglayici failover: sirali dene. (metin, tok, provider_id).
    Hicbiri calismazsa ("",0,"")."""
    for p in _providers():
        _lim = _provider_limit(p["id"])
        _used = int(_provider_usage_read().get(p["id"]) or 0)
        if _lim and _used >= int(_lim * LIMIT_WARN_PCT / 100.0) \
                and _used < _lim:
            _pct = round(100.0 * _used / _lim, 1)
            log("warn", "provider", f"{p['name']} gunluk limit %{_pct} (uyari)")
        if _provider_blocked(p["id"]):
            log("warn", "provider", f"{p['name']} gunluk limit %95 -> atlandi")
            continue
        try:
            txt, tok = _provider_chat(p, messages, max_tokens, timeout)
        except urllib.error.HTTPError as exc:
            # 401/403/429/kota/5xx: sonraki saglayici
            log("warn", "provider",
                f"{p['name']} HTTP {exc.code} -> sonraki saglayici")
            continue
        except Exception as exc:
            log("warn", "provider",
                f"{p['name']} hata ({type(exc).__name__}) -> sonraki saglayici")
            continue
        if not txt:
            continue
        n = _provider_usage_bump(p["id"])
        comp = "evren-call" if consumer == "evren" else f"{consumer}-call"
        log("info", comp, f"ok ({tok} tok, {p['name']} #{n})")
        return txt, tok, p["id"]
    return "", 0, ""


def provider_status():
    """Panel icin saglayici durum ozeti (sir yok, sadece sayi/id)."""
    out = []
    for p in _providers():
        used = int(_provider_usage_read().get(p["id"]) or 0)
        lim = _provider_limit(p["id"])
        out.append({"id": p["id"], "name": p["name"], "model": p["model"],
                    "used": used, "limit": lim,
                    "pct": round(100.0 * used / lim, 1) if lim else 0.0})
    return out


def evren_chat(messages: list, max_tokens: int = 600, consumer: str = "evren",
               timeout: int = 120) -> tuple:
    """(metin, kullanilan_token) dondurur.
    T23 sirasi: evren havuzu -> gemini -> groq -> cloudflare.
    consumer: tuketici bazli muhasebe ayrimi (evren | system)."""
    # K5: "cirak" secimi yalniz sohbet/yerel tuketicileri etkiler.
    if active_model() == "cirak":
        return "", 0
    # 1) Evren anahtar havuzu (varsa). T23: dis saglayici varsa evren'i cok
    #    uzun beklemeyiz (yavas/olu evren failover'i geciktirmesin).
    _ev_to = min(timeout, 45) if _providers() else timeout
    txt, tok = _evren_pool_chat(messages, max_tokens=max_tokens,
                                consumer=consumer, timeout=_ev_to)
    if txt:
        return txt, tok
    # 2) Dis saglayicilar (ucretsiz yedekler) — evren kapansa da calisir
    txt, tok, pid = provider_chat(messages, max_tokens=max_tokens,
                                  consumer=consumer,
                                  timeout=min(timeout, 60))
    if txt:
        return txt, tok
    # 3) Tum bulut basarisiz. Yalniz sohbet (evren) icin yerel degrade.
    if consumer == "evren" and llm_degrade_active():
        _local = ollama_messages(messages, max_tokens=max_tokens)
        if _local:
            log("warn", "tier2", "bulut yok -> yerel Ollama (degrade)")
            return _local, 0
    return "", 0


# Her 60 sn calisir. Yalniz gercek kosullari kontrol eder, LLM kullanmaz.
# Kritik durum -> alerts (critical) + Telegram bildirimi (dedup'li).
WD_INTERVAL = int(CFG.get("WATCHDOG_INTERVAL", "60") or 60)

# G2: calisan is zaman asimi / reclaim
DEV_JOB_TIMEOUT_SEC = int(CFG.get("DEV_JOB_TIMEOUT", "1800") or 1800)
DEV_JOB_MAX_ATTEMPTS = int(CFG.get("DEV_JOB_MAX_ATTEMPTS", "2") or 2)


def dev_jobs_reclaim():
    """Takili (running) isleri kurtar: heartbeat eskiyse pending'e geri al,
    deneme sinirinda error + bildirim. Donus: (reclaimed, failed)."""
    now = time.time()
    limit = DEV_JOB_TIMEOUT_SEC * 1.5
    reclaimed = failed = 0
    try:
        with db() as c:
            rows = c.execute(
                "SELECT id,attempts,heartbeat_at,started_at FROM dev_jobs "
                "WHERE status='running'").fetchall()
        for r in rows:
            ref = r["heartbeat_at"]
            if not ref and r["started_at"]:
                try:
                    ref = datetime.strptime(r["started_at"], "%Y-%m-%d %H:%M:%S").timestamp()
                except Exception:
                    ref = None
            if not ref:
                ref = now  # bilinmiyorsa hemen reclaim etme, bir tur bekle
            if now - ref <= limit:
                continue
            att = int(r["attempts"] or 0)
            if att >= DEV_JOB_MAX_ATTEMPTS:
                with db() as c:
                    c.execute("UPDATE dev_jobs SET status='error', updated_at=datetime('now'), "
                              "finished_at=datetime('now') WHERE id=? AND status='running'",
                              (r["id"],))
                failed += 1
                try:
                    alert("warn", "devworker", "job_timeout",
                          f"Is #{r['id']} zaman asimi (deneme siniri)", 
                          f"attempts={att}", dedup_sec=900)
                    _tg_dedup(f"job_timeout_{r['id']}",
                              f"\u26A0\uFE0F <b>Is #{r['id']}</b> zaman asimina ugradi, "
                              f"deneme siniri ({att}) doldu.", window=6 * 3600)
                except Exception:
                    pass
            else:
                with db() as c:
                    c.execute("UPDATE dev_jobs SET status='pending', updated_at=datetime('now'), "
                              "heartbeat_at=NULL WHERE id=? AND status='running'", (r["id"],))
                reclaimed += 1
                log("warn", "devworker", f"is #{r['id']} takildi -> kuyruga geri alindi")
    except Exception as exc:
        log("warn", "devworker", f"reclaim: {_safe_msg(str(exc))[:100]}")
    return reclaimed, failed



def _wd_telegram(text):
    """Sadece kritik alarmlarda patrona mesaj. Gonderim hatasi alarmi _tg_send'de."""
    try:
        return _tg_send(text)
    except Exception:
        return False


def _wd_key_pool_state():
    """Havuzda kullanilabilir key var mi? (ag cagrisi yok, sadece meta+cache)"""
    if not _KEYS:
        return {"total": 0, "usable": 0, "exhausted": 0, "invalid": 0, "cooldown": 0}
    now = time.time()
    ex = inv = cd = usable = 0
    for i in range(len(_KEYS)):
        m = _key_meta(i)
        if m.get("invalid"):
            inv += 1
        elif now < (m.get("exhausted_until") or 0):
            ex += 1
        elif now < (m.get("cooldown_until") or 0):
            cd += 1
        else:
            usable += 1
    return {"total": len(_KEYS), "usable": usable, "exhausted": ex,
            "invalid": inv, "cooldown": cd}


def _opencode_alive():
    """Evren calisma ortami canli mi? tmux 'evren' oturumu VEYA opencode prosesi."""
    ok = False
    try:
        import subprocess
        try:
            r = subprocess.run(["tmux", "has-session", "-t", "evren"],
                               capture_output=True, timeout=8)
            if r.returncode == 0:
                ok = True
        except Exception:
            pass
        if not ok:
            try:
                r = subprocess.run(["pgrep", "-x", "opencode"],
                                   capture_output=True, timeout=8)
                if r.returncode == 0 and (r.stdout or b"").strip():
                    ok = True
            except Exception:
                pass
    except Exception:
        return False
    return ok


_OPENCODE_PROC_CACHE = {"at": 0.0, "ok": False}


def _opencode_process_alive(ttl=10):
    """E2: GERCEK opencode prosesi calisiyor mu? (panel 'evren' durumu icin).

    tmux oturumu bos bir kabuk olabilir; burada prosesin kendisine bakariz.
    Sonuc `ttl` sn cache'lenir (panel her poll'de subprocess acmasin)."""
    now = time.time()
    if now - _OPENCODE_PROC_CACHE["at"] < ttl:
        return _OPENCODE_PROC_CACHE["ok"]
    ok = False
    try:
        import subprocess
        r = subprocess.run(["pgrep", "-x", "opencode"],
                           capture_output=True, timeout=8)
        if r.returncode == 0 and (r.stdout or b"").strip():
            ok = True
        else:
            # opencode farkli argv[0] ile calisiyorsa tmux pane komutuna bak
            r = subprocess.run(["tmux", "list-panes", "-t", "evren", "-F",
                                "#{pane_current_command}"],
                               capture_output=True, timeout=8)
            out = (r.stdout or b"").decode("utf-8", "ignore")
            if r.returncode == 0 and "opencode" in out:
                ok = True
    except Exception:
        ok = False
    _OPENCODE_PROC_CACHE["at"] = now
    _OPENCODE_PROC_CACHE["ok"] = ok
    return ok



def _active_evren_work():
    """Devam eden Evren isi var mi? (kuyruk veya YAPILIYOR gorev)"""
    try:
        with db() as c:
            j = c.execute("SELECT COUNT(*) FROM dev_jobs WHERE status IN "
                          "('pending','running')").fetchone()[0]
            t = c.execute("SELECT COUNT(*) FROM agent_tasks WHERE status='YAPILIYOR' "
                          "AND owner='evren'").fetchone()[0]
        return int(j or 0) + int(t or 0)
    except Exception:
        return 0


def _reset_stale_busy():
    """E1: N dk'dan uzun suredir busy/waiting kalan ajanlari idle'a cek (kalici).

    set_agent eski updated_at'i guncellemedigi icin model timeout/cokme sonrasi
    'busy' takili kalabiliyordu. Donus: sifirlanan ajan id'leri."""
    reset = []
    try:
        import calendar as _cal
        now = time.time()
        aged = []
        with db() as c:
            rows = c.execute("SELECT id,state,updated_at FROM agent_state "
                             "WHERE state IN ('busy','waiting')").fetchall()
            for r in rows:
                try:
                    up = time.strptime(str(r["updated_at"])[:19], "%Y-%m-%d %H:%M:%S")
                    age = now - _cal.timegm(up)   # updated_at UTC yazilir
                except Exception:
                    continue
                if age > _BUSY_TTL_SEC:
                    c.execute("UPDATE agent_state SET state='idle', updated_at=datetime('now') "
                              "WHERE id=?", (r["id"],))
                    reset.append(r["id"])
                    aged.append((r["id"], r["state"], int(age)))
        # log() ayri bir yazma baglantisi acar; kilit catismasi olmamasi icin
        # islem bittikten SONRA yaz.
        for aid, prev, age in aged:
            log("warn", "watchdog", f"bayat {prev} -> idle: {aid} ({age} sn)")
    except Exception:
        pass
    return reset


def watchdog_once():
    """Tek tur kontrol. Donus: uretilen/kapatilan alarm sayilari."""
    out = {"raised": [], "resolved": []}

    def raise_(sev, src, code, msg, det=""):
        aid = alert(sev, src, code, msg, det)
        if aid:
            out["raised"].append((sev, code))

    def resolve_(src, code):
        if alert_resolve(src, code):
            out["resolved"].append((src, code))

    # --- 1) Veritabani ---
    try:
        with db() as c:
            c.execute("SELECT 1").fetchone()
        resolve_("watchdog", "db_down")
    except Exception as exc:
        raise_("critical", "watchdog", "db_down", "Veritabanı erişilemiyor",
               type(exc).__name__)

    # --- 2) API havuzu: hic kullanilabilir key yok ---
    try:
        st = _wd_key_pool_state()
        if st["total"] and st["usable"] == 0:
            raise_("critical", "watchdog", "pool_empty",
                   f"API havuzunda kullanılabilir anahtar yok "
                   f"(tükendi:{st['exhausted']} geçersiz:{st['invalid']} "
                   f"beklemede:{st['cooldown']})")
        else:
            resolve_("watchdog", "pool_empty")
            # E3: havuzda kullanilabilir anahtar varsa 'all_exhausted' alarmi
            # (gecici tek cagri hatasindan kalmis olabilir) kapanir.
            if st["usable"] > 0:
                resolve_("apiman", "all_exhausted")
    except Exception:
        pass

    # --- 3) Donguler (heartbeat) ---
    now = time.time()
    checks = [
        ("mail", "mail_loop", "watchdog", POLL_SEC * 3 + 180,
         "Mail döngüsü durdu"),
    ]
    for hb_key, code, src, limit, msg in checks:
        hb = _hb_get(hb_key)
        if not hb:
            continue   # henuz hic calismamis -> alarm uretme
        age = now - hb
        if age > limit:
            raise_("critical", src, code, msg,
                   f"{int(age)} sn sinyal yok (limit {int(limit)} sn)")
        else:
            resolve_(src, code)

    # --- 4) Disk ---
    try:
        import shutil as _sh
        u = _sh.disk_usage("/")
        pct = round(u.used / u.total * 100, 1)
        if pct >= 92:
            raise_("critical", "watchdog", "disk_full",
                   f"Disk %{pct} dolu", f"bos={round(u.free/1e9,1)}GB")
        elif pct >= 85:
            raise_("warn", "watchdog", "disk_high", f"Disk %{pct} dolu")
            resolve_("watchdog", "disk_full")
        else:
            resolve_("watchdog", "disk_full")
            resolve_("watchdog", "disk_high")
    except Exception:
        pass

    # --- 5) dev worker (yalniz hic sinyal yoksa ve is bekliyorsa) ---
    try:
        hb = _hb_get("devworker")
        with db() as c:
            pend = c.execute("SELECT COUNT(*) FROM dev_jobs WHERE status='pending'"
                             ).fetchone()[0]
        if pend and not hb:
            raise_("warn", "watchdog", "devworker_off",
                   f"{pend} iş kuyrukta, PC worker bağlı değil")
        elif hb and (now - hb) > 300 and pend:
            raise_("warn", "watchdog", "devworker_off",
                   f"{pend} iş kuyrukta, worker sinyali eski")
        else:
            resolve_("watchdog", "devworker_off")
    except Exception:
        pass

    # --- 5b) Evren/OpenCode calisma ortami sessiz cokme (restart dahil) ---
    try:
        active = _active_evren_work()
        if active and not _opencode_alive():
            raise_("critical", "watchdog", "opencode_down",
                   "Evren/OpenCode calisma ortami yok ama aktif is bekliyor",
                   f"aktif is={active} (tmux/opencode yok)")
        else:
            resolve_("watchdog", "opencode_down")
    except Exception:
        pass

    # --- 5c) Takili calisan isleri kurtar (G2) ---
    try:
        dev_jobs_reclaim()
    except Exception:
        pass

    # --- 5d) E1: takili busy/waiting durumlarini idle'a cek ---
    try:
        _reset_stale_busy()
    except Exception:
        pass

    # --- 6) Kritik alarmlari Telegram'a bildir (dedup: 15 dk) ---
    try:
        with db() as c:
            crit = c.execute(
                "SELECT id,source,code,message,count,last_ts FROM alerts "
                "WHERE resolved=0 AND severity='critical'").fetchall()
        # E3: kota/havuz saglikliyken tükenme alarmini TEKRAR bildirme.
        try:
            _pool_ok = _wd_key_pool_state()["usable"] > 0
        except Exception:
            _pool_ok = False
        _pool_codes = {"all_exhausted", "pool_empty", "pool_low"}
        for a in crit:
            if _pool_ok and a["code"] in _pool_codes:
                continue
            # T21: limit alarmlarini guard 6 saat dedup ile kendisi gonderir.
            # Cozulmus alarmlar zaten SELECT disinda (resolved=0).
            if a["source"] == "limits":
                continue
            dkey = f"{a['source']}_{a['code']}"
            seen = _hb_get(f"wd_notified_{dkey}")
            if now - seen < 1800:   # T21: alarm dedup penceresi 30 dk
                continue
            _wd_telegram(f"🔴 <b>EVREN alarm</b>: {_safe_msg(a['message'])[:180]}\n"
                         f"<i>{a['code']} · x{a['count']} · Ayla bakıyor</i>")
            _hb(f"wd_notified_{dkey}")
    except Exception:
        pass

    return out


# ---------------- EVREN KOPRUSU: task -> dev_jobs -> sonuc ----------------
EVREN_BRIDGE_SEC = int(CFG.get("EVREN_BRIDGE_SECONDS", "20") or 20)


def evren_bridge_once():
    """owner=evren BEKLIYOR task -> opencode dev_jobs. Sonuc gelince task kapat."""
    out = {"dispatched": [], "closed": []}
    # a) tamamlanan opencode isleri -> task'i kapat
    try:
        with db() as c:
            rows = c.execute(
                "SELECT j.id, j.task_id, j.status, j.result FROM dev_jobs j "
                "JOIN agent_tasks t ON t.id=j.task_id "
                "WHERE j.task_id IS NOT NULL AND j.status IN ('done','error') "
                "AND t.status='YAPILIYOR'").fetchall()
        for r in rows:
            st = "BITTI" if r["status"] == "done" else "BASARISIZ"
            task_set(r["task_id"], status=st,
                     result=(r["result"] or "")[:3000])
            out["closed"].append(r["task_id"])
            # --- DONUS YOLU: is bitince Ayla patrona tek satir rapor gecsin ---
            try:
                _title = ""
                with db() as _c:
                    _t = _c.execute("SELECT title FROM agent_tasks WHERE id=?",
                                    (r["task_id"],)).fetchone()
                _title = (_t["title"] if _t else "") or f"gorev #{r['task_id']}"
                _res = (r["result"] or "").strip()
                _head = ("\u2705" if st == "BITTI" else "\u274C")
                _body = _res[:600] if _res else "(sonuc bos)"
                _safe_t = _safe_msg(_title)[:90]
                notify_user(
                    f"{_head} <b>Ayla \u2014 Evren raporu</b>\n"
                    f"Gorev: <b>#{r['task_id']}</b> {_safe_t}\n"
                    f"Durum: <b>{'BITTI' if st == 'BITTI' else 'BASARISIZ'}</b>\n\n"
                    f"{_body}")
                log("ok", "evren-bridge",
                    f"gorev #{r['task_id']} raporu patrona iletildi ({st})")
            except Exception as exc:
                log("warn", "evren-bridge",
                    f"rapor gonderilemedi: {type(exc).__name__}")
    except Exception as exc:
        log("warn", "evren-bridge", f"kapatma: {type(exc).__name__}")

    # b) bekleyen evren gorevlerini kuyruga aktar (tek seferde bir tane)
    try:
        pending = [t for t in task_list(status="BEKLIYOR", owner="evren", limit=5)]
        if pending:
            t = pending[0]
            # ayni task icin zaten dev job var mi?
            with db() as c:
                dup = c.execute("SELECT id FROM dev_jobs WHERE task_id=? "
                                "AND status IN ('pending','running')",
                                (t["id"],)).fetchone()
            if not dup:
                _det = ""
                try:
                    with db() as _c:
                        _r = _c.execute("SELECT detail FROM agent_tasks WHERE id=?",
                                        (t["id"],)).fetchone()
                    _det = (_r["detail"] if _r else "") or ""
                except Exception:
                    _det = ""
                msg = t["title"] + ("\n\n" + _det if _det else "")
                with db() as c:
                    cur = c.execute(
                        "INSERT INTO dev_jobs(ts,chat_id,message,status,updated_at,task_id) "
                        "VALUES(datetime('now'),'evren-bridge',?,'pending',datetime('now'),?)",
                        (f"GOREV #{t['id']}: {msg}"[:12000], t["id"]))
                    jid = cur.lastrowid
                task_set(t["id"], status="YAPILIYOR",
                         result=f"opencode iş #{jid} kuyruğa alındı")
                out["dispatched"].append((t["id"], jid))
    except Exception as exc:
        log("warn", "evren-bridge", f"atama: {type(exc).__name__}")
    return out


def evren_bridge_loop():
    time.sleep(30)
    while True:
        try:
            r = evren_bridge_once()
            if r["dispatched"]:
                log("ok", "evren-bridge",
                    "gonderildi: " + ",".join(f"task{t}->job{j}" for t, j in r["dispatched"]))
            if r["closed"]:
                log("ok", "evren-bridge", "kapatildi: " + ",".join(str(x) for x in r["closed"]))
        except Exception as exc:
            log("error", "evren-bridge", f"dongu: {_safe_msg(str(exc))[:100]}")
        time.sleep(EVREN_BRIDGE_SEC)


def watchdog_loop():
    time.sleep(45)
    while True:
        try:
            _hb("watchdog")
            r = watchdog_once()
            if r["raised"]:
                log("warn", "watchdog",
                    "alarm: " + ",".join(c for _, c in r["raised"]))
            if r["resolved"]:
                log("ok", "watchdog",
                    "duzeldi: " + ",".join(c for _, c in r["resolved"]))
        except Exception as exc:
            log("error", "watchdog", f"tur hatasi: {_safe_msg(str(exc))[:120]}")
        time.sleep(WD_INTERVAL)


# ---------------- AYLA: tek kapi (persona + defter + kuyruk) ----------------
AYLA_CHANNEL = "ayla"
_A_CACHE = {}
_A_TASK_RX = re.compile(r"\[\[\s*GOREV\s*:\s*([a-zA-Z_]+)\s*\|([^\]|]*)(?:\|([^\]]*))?\]\]")

# T20: son GÖSTERİLEN mail listesi (bağlam). "bunu sil/şu maili sil" bununla çözülür.
_LAST_MAIL_CTX = {"items": [], "at": 0.0, "days": 7}
_LAST_MAIL_CTX_TTL = 7200   # 2 saatten eski bağlamla yanlış mail silinmesin


def set_last_mail_ctx(items, days=7):
    """Panel/Telegram'da gösterilen son mail listesini bağlam olarak sakla.
    items: [{subject, from, date, msg_id, uid}, ...] (en yeni ilk)."""
    try:
        clean = []
        for it in (items or [])[:15]:
            clean.append({
                "subject": (it.get("subject") or "")[:160],
                "from": (it.get("from") or "")[:120],
                "date": it.get("date") or "",
                "msg_id": it.get("msg_id") or "",
                "uid": it.get("uid") or "",
            })
        _LAST_MAIL_CTX["items"] = clean
        _LAST_MAIL_CTX["at"] = time.time()
        _LAST_MAIL_CTX["days"] = int(days or 7)
        return len(clean)
    except Exception:
        return 0


def last_mail_ctx():
    """Son gösterilen mail listesi + yaşı (saniye). Boşsa items=[]."""
    try:
        age = time.time() - float(_LAST_MAIL_CTX.get("at") or 0)
    except Exception:
        age = 1e9
    return {"items": list(_LAST_MAIL_CTX.get("items") or []), "age": age,
            "days": _LAST_MAIL_CTX.get("days", 7)}


def _ayla_file(name):
    p = ROOT / "agents" / ("_shared" if name == "CORE.md" else "ayla") / name
    try:
        st = p.stat()
        c = _A_CACHE.get(name)
        if c and c[0] == st.st_mtime_ns:
            return c[1]
        txt = p.read_text(encoding="utf-8")
        _A_CACHE[name] = (st.st_mtime_ns, txt)
        return txt
    except Exception:
        return ""


def ayla_persona():
    parts = [_ayla_file("IDENTITY.md"), _ayla_file("CORE.md"), _ayla_file("COMMAND.md")]
    return "\n\n---\n\n".join(x.strip() for x in parts if x.strip())


def ayla_context(max_tasks=8):
    """Kisa, gercek durum baglami (LLM'e gider)."""
    lines = []
    try:
        pend = task_list(status="BEKLIYOR", limit=max_tasks)
        work = task_list(status="YAPILIYOR", limit=max_tasks)
        if work:
            lines.append("Şu an YAPILIYOR:")
            for t in work:
                lines.append(f"  #{t['id']} {t['title']} ({t['owner']})")
        if pend:
            lines.append("Bekleyen:")
            for t in pend:
                lines.append(f"  #{t['id']} {t['title']} ({t['owner']}, P{t['priority']})")
        if not work and not pend:
            lines.append("Bekleyen görev yok.")
    except Exception:
        pass
    try:
        lk = [l for l in lock_list() if not l["stale"]]
        if lk:
            lines.append("Kilitli alanlar: " + ", ".join(f"{l['area']}({l['owner']})" for l in lk[:5]))
    except Exception:
        pass
    try:
        with db() as c:
            rows = c.execute("SELECT severity,message FROM alerts WHERE resolved=0 "
                             "ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END LIMIT 3"
                             ).fetchall()
        if rows:
            lines.append("Açık alarmlar: " + "; ".join(f"[{r['severity']}] {r['message']}" for r in rows))
    except Exception:
        pass
    try:
        st = _wd_key_pool_state()
        if st["total"]:
            lines.append(f"API havuzu: {st['usable']}/{st['total']} kullanılabilir.")
    except Exception:
        pass
    # gelistirme kuyrugu + son hareketler (is yuku sorularina zemin)
    try:
        w = workload_snapshot()
        dev = w.get("dev_pending", 0) + w.get("dev_running", 0)
        if dev:
            lines.append(f"Gelistirme kuyrugu: {dev} is "
                         f"(bekleyen {w.get('dev_pending', 0)}, "
                         f"calisan {w.get('dev_running', 0)}).")
        a = activity_snapshot()
        if a.get("open_tasks"):
            lines.append("Su an calisilan: " + "; ".join(
                f"#{t['id']} {t['title'][:40]} ({t['owner']})"
                for t in a["open_tasks"][:4]))
        if a.get("agents"):
            busy = [x for x in a["agents"]
                    if x["state"] not in ("idle", "off", "offline")]
            lines.append("Mesgul ajanlar: " +
                         (", ".join(f"{x['id']}={x['state']}" for x in busy[:6])
                          or "yok (hepsi bosta)"))
        if a.get("events"):
            lines.append("Son hareketler: " + "; ".join(
                f"{e['src']}->{e['tgt']} {e['status']}" for e in a["events"][:4]))
    except Exception:
        pass
    lines.append("Is yuku / gorev listesi / kim ne yapiyor sorularinda YUKARIDAKI "
                 "gercek veriyi kullan; 'erisimim yok' deme.")
    # proje defteri (kalici): aktif proje varsa detay, yoksa acik projeler
    try:
        act = setting_get(_ACTIVE_PROJ_KEY, "")
        ctx_p = ("\n\n" + _proj.proj_context(slug=act) if act and _proj.proj_exists(act)
                 else "\n\n" + _proj.proj_context())
        return "CANLI DURUM (gerçek):\n" + "\n".join(lines) + ctx_p
    except Exception:
        pass
    return "CANLI DURUM (gerçek):\n" + "\n".join(lines)


def _brief_ok(owner, title, detail):
    """Evren'e giden is brief formunda mi? (baslik + yeterli detay + kabul izi)"""
    if owner != "evren":
        return True, ""
    if not title or len(title.strip()) < 8:
        return False, "baslik cok kisa"
    d = (detail or "").strip()
    if len(d) < 40:
        return False, f"detay yetersiz ({len(d)} kr; en az 40)"
    low = d.lower()
    blob = (title + " " + d).lower()
    has_req = any(w in blob for w in ("gereksinim", "gereksinimler", "istek",
                                      "yapılacak", "yapilacak", "kapsam",
                                      "adım", "adim", "- ", "1."))
    has_accept = any(w in blob for w in ("kabul", "doğrula", "dogrula", "test",
                                         "beklenen", "çıktı", "cikti", "bitiş",
                                         "bitis"))
    if not has_req:
        return False, "gereksinim listesi yok"
    if not has_accept:
        return False, "kabul kriteri yok"
    return True, ""


def _ayla_parse_tasks(text):
    """[[GOREV:evren|baslik|detay]] markerlarini ayikla ve kuyruga yaz.
    Evren gorevleri icin brief zorunlu: eksikse YAZILMAZ (durust hata)."""
    created = []
    rejected = []

    def _rep(m):
        owner = (m.group(1) or "").strip().lower()
        title = (m.group(2) or "").strip()
        detail = (m.group(3) or "").strip()
        if owner not in ("evren", "ayla", "cirak") or not title:
            return ""
        ok, why = _brief_ok(owner, title, detail)
        if not ok:
            rejected.append((owner, title, why))
            # BRIEF_EKSIK = BASARISIZ + etiket: sessizce yutma, acik durum olarak kaydet
            try:
                tid = task_add(("BRIEF_EKSIK: " + title)[:200], detail or "",
                               owner=owner, priority=2, source="ayla-brief")
                if tid:
                    task_set(tid, status="BASARISIZ", result=f"BRIEF_EKSIK: {why}")
            except Exception:
                pass
            log("warn", "ayla", f"brief reddedildi ({owner}): {why}")
            return ""
        try:
            tid = task_add(title, detail, owner=owner, priority=1, source="ayla")
            if tid:
                created.append((tid, owner, title))
        except Exception:
            pass
        return ""

    out = _A_TASK_RX.sub(_rep, text or "")
    try:
        globals()["_AYLA_LAST_REJECTED"] = rejected
    except Exception:
        pass
    return out.strip(), created


_A_PROJ_RX = re.compile(
    r"\[\[\s*PROJE\s*:\s*([a-zA-Z_]+)\s*\|([^\]|]*)(?:\|([^\]]*))?\]\]")


def _ayla_parse_projects(text):
    """[[PROJE:not|ad|metin]] / [[PROJE:yeni|ad|ilk not]] / [[PROJE:mimari|ad]]
    / [[PROJE:ilet|ad]] markerlarini isle. Donus: (temiz_metin, olaylar[])."""
    events = []

    def _rep(m):
        act = (m.group(1) or "").strip().lower()
        ref = (m.group(2) or "").strip()
        arg = (m.group(3) or "").strip()
        try:
            if act == "yeni":
                if not ref:
                    return ""
                meta = _proj.proj_create(ref, note=arg, source="ayla")
                if meta:
                    proj_active_set(meta["slug"])
                    events.append(("yeni", meta["name"]))
                return ""
            slug = _proj.proj_find(ref) if ref else proj_resolve("")
            if act == "not":
                if not slug:
                    return ""
                _proj.proj_add_note(slug, arg or ref, kind="not")
                proj_active_set(slug)
                events.append(("not", _proj.proj_get(slug)["name"]))
                return ""
            if act == "mimari":
                if not slug:
                    return ""
                spec, err = ayla_project_spec(slug)
                events.append(("mimari", _proj.proj_get(slug)["name"], err))
                return ""
            if act == "ilet":
                if not slug:
                    return ""
                tid, err = ayla_project_transfer(slug)
                events.append(("ilet", _proj.proj_get(slug)["name"], tid, err))
                return ""
        except Exception:
            return ""
        return ""

    out = _A_PROJ_RX.sub(_rep, text or "")
    return out.strip(), events


def _ayla_model_state():
    """Ayla'nin BULUT modeli kullanilabilir mi? Donus: (ok, neden_tr).
    T2/G3: model yoksa ezber/canned metne DUSULMEZ; neden acikca soylenir."""
    if active_model() == "off":
        return False, "model kapalı (off)"
    # K5: cirak modunda Ayla bilerek yerel modeli kullanir (bulut denenmez).
    if active_model() == "cirak":
        return False, "çırak modu (yerel)"
    if _KEYS and evren_pool_available():
        return True, ""
    # T23: evren havuzu bossa/bitmişse ucretsiz saglayicilar devrede mi?
    if _providers():
        return True, ""
    if not _KEYS:
        return False, "bulut sağlayıcı tanımlı değil"
    return False, "anahtar havuzunda kullanılabilir hat kalmadı (kota/limit)"


def _ayla_honest_model_error(reason):
    """Dürüst tek satır: model gerçekten yok. Görev içeriği UYDURULMAZ."""
    tail = f" ({reason})" if reason else ""
    return (f"Şu an modele ulaşamıyorum the owner{tail}. "
            f"Birazdan tekrar deneyin; uydurma cevap vermeyeceğim.")


def _ayla_local_reply(user_text, persona, max_tokens=500):
    """Yerel yedek (Ollama) — yalniz sohbet. Dönen metin etiketlenir.
    T2/G3: yedeğe düşüldüğü panelde/telegramda açıkça 'yerel yedek' olarak görünür."""
    try:
        if active_model() == "off":
            return ""
        out = ollama_chat(
            user_text, max_tokens=max_tokens,
            identity=(persona or "") + "\n\n[Bu yanıt YEREL YEDEK modelden geliyor; "
                                       "bulut modeline ulaşılamadı.]")
        return (out or "").strip()
    except Exception:
        return ""


# T8: Modelin gerçek araç çağrısı markeri. Model karar verir; biz GERÇEK yeteneği çağırırız.
_AYLA_CAGRI_RX = re.compile(r"\[\[\s*CAGRI\s*:\s*(\{.*?\})\s*\]\]", re.S)


def _ayla_exec_cagri(payload, user_text):
    """[[CAGRI:{"agent":..,"cap":..,"args":{..}}]] -> ajana_sor (call_id'li)."""
    try:
        obj = json.loads(payload)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    agent_id = obj.get("agent") or obj.get("ajan") or ""
    cap = obj.get("cap") or obj.get("yetenek") or ""
    cargs = obj.get("args") or {}
    if not isinstance(cargs, dict):
        cargs = {}
    if not agent_id:
        return None
    if not cap:
        cap = cap_tahmin(agent_id, user_text)
    if not cap:
        return None
    # F3: model "denetle" markeri uydursa bile açık denetim komutu yoksa çalıştırma.
    if cap == "denetle" and not _AUDIT_CMD_RX.search(user_text or ""):
        return None
    try:
        return ajana_sor(agent_id, cap, text=user_text, args=cargs, source="ayla_model")
    except Exception:
        return None


def ayla_reply(user_text, max_tokens=900, channel=None):
    """Ayla'nın model cevabı. Donus: (metin, olusan_gorevler).
    T2/G3: bulut modeli yoksa/boşsa sessizce eski metne düşmez; yerel yedek varsa
    açıkça etiketlenir, yoksa dürüst hata döner."""
    _ch = channel or AYLA_CHANNEL
    ok, reason = _ayla_model_state()
    if not ok:
        loc = _ayla_local_reply(user_text, ayla_persona())
        if loc:
            return ("[yerel yedek · Ollama]\n" + loc, [])
        _salvage_intent(user_text, reason)
        return (_ayla_honest_model_error(reason), [])
    try:
        set_agent("filtre", "busy"); set_agent("sefler", "busy")
        persona = ayla_persona()
        if not persona:
            return ("Talimat dosyalarıma ulaşamadım the owner.", [])
        # hafiza: son 24 mesaj (proje konusmalari kopmasin)
        hist = get_history(_ch, 12)  # G3: ham gecmis 24->12; aktif proje baglami korunur
        sys_msg = (persona + "\n\n" + ayla_context() + "\n\n"
                   "GÖREV MARKERİ (yalnız Evren'e KOD işi): cevabının SONUNA, "
                   "kullanıcıya görünmeyen tek satır ekle:\n"
                   "[[GOREV:evren|başlık|Gereksinimler: 1) ... 2) ... 3) ... Kabul: ...]]\n"
                   "ZORUNLU: detay en az 40 karakter olmalı; numaralı gereksinim listesi ve "
                   "'Kabul:' kriteri yoksa görev REDDEDİLİR. "
                   "Kendin yapabileceğin iş için marker ekleme. Uydurma görev açma.\n\n"
                   "PROJE DEFTERİ MARKERLERİ (kullanıcıya görünmez; konuşulanları kalıcı kaydet):\n"
                   "- Yeni proje: [[PROJE:yeni|proje adı|ilk not]]\n"
                   "- Nota ekle: [[PROJE:not|proje adı veya boş|not metni]]\n"
                   "- Mimari çıkar: [[PROJE:mimari|proje adı]]\n"
                   "- Evren'e ilet: [[PROJE:ilet|proje adı]]\n"
                   "Kullanıcı bir projeden bahsedip gereksinim/karar verdikçe MUTLAKA "
                   "[[PROJE:not|...]] ile kaydet; 'toparla/mimariyi kur' derse mimari, "
                   "'evrene ilet' derse ilet. Proje adı boş bırakılırsa aktif proje kullanılır.\n"
                   "Kullanıcı sadece proje konuşuyorsa (aksiyon istemediyse) mimari/ilet markerı KOYMA.\n\n"
                   "ARAÇ ÇAĞRISI (GERÇEK İŞ; kullanıcıya görünmez): Bir ajanın yeteneği gerekiyorsa\n"
                   "cevabının sonuna tek satır ekle:\n"
                   "[[CAGRI:{\"agent\":\"arsivci\",\"cap\":\"temizle\",\"args\":{\"months\":1}}]]\n"
                   "MAIL SİLME KURALI (T20): 'en son maili sil'/'son maili sil' -> "
                   "arsivci.son_sil (count=1; 'son 3 mail' -> 3). 'bunu sil'/'şu maili sil' "
                   "(son gösterdiğin mail) veya 'X konulu maili sil' -> arsivci.konu_sil "
                   "(args: {konu} ya da {msg_id}); hedef belirsizse marker KOYMA, kullanıcıya "
                   "'hangi maili?' diye sor. 'N aydan eski'/'eski mailler' -> arsivci.temizle "
                   "(months=N). 'tüm kutu/hepsini' -> arsivci.arsivle. Hiçbiri KALICI silmez; "
                   "hepsi Cop'e taşır.\n"
                   "DENETİM KURALI (F3): ayla.denetle / apiman.denetle YALNIZ kullanıcı AÇIKÇA "
                   "'denetle', 'kontrol et', 'teftiş' derse çağrılır. Selam, sohbet, soru sorma, "
                   "araştırma, proje konuşması ve 'ne yapıyor/ne durumda' gibi durum sorularında "
                   "denetle markerı KOYMA; durum sorularını CANLI DURUM verisiyle kendin anlat. "
                   "Veri yoksa dürüstçe 'bilmiyorum' de.\n"
                   "Ajanlar/yetenekler: apiman(kota,denetle), arsivci(kutu,gereksizler,temizle,arsivle,gereksiz_temizle,son_sil,konu_sil), kurye(mail_gonder), evren(kuyruk,brief), ayla(denetle,evren_gorev,onay,api_duzelt,web_arastir,hatirlat,hatirlat_listele).\n"
                   "HATIRLATMA: 'X hatirlat' / 'X zamaninda hatirlat' derse "
                   "[[CAGRI:{\"agent\":\"ayla\",\"cap\":\"hatirlat\",\"args\":{\"ne\":\"...\",\"ne_zaman\":\"...\"}}]] koy "
                   "(yerel zamanli hatirlatma; Google Calendar YOK). 'hatirlatmalarim' -> ayla.hatirlat_listele. "
                   "'Hatirlatma/takvim aracim yok' DEME.\n"
                   "MAIL GONDERME: Mail gonderme yeteneğin VAR (kurye.mail_gonder, gercek SMTP). "
                   "Kullanici 'raporu mail at/gonder' derse [[CAGRI:{\"agent\":\"kurye\",\"cap\":\"mail_gonder\",\"args\":{\"subject\":\"...\",\"body\":\"...\"}}]] koy. "
                   "Varsayilan alici the owner's kendisidir; dis adres icin the owner onayi gerekir. "
                   "'Mail gonderme yetenegim yok' DEME.\n"
                   "gönderilmez; genel soru metni kullan. Kaynak yoksa uydurma.\n"
                   "Sohbet/duygu/proje sorusu için marker KOYMA. Mail işi olmayana mail özeti verme.\n"
                   "Gerçek iş yaptıysan 'ilettim/yaptım' de; marker yoksa 'yapamadım' de ve nedenini söyle.\n"
                   "the owner prompt/görev listesi yazmaz; gereksinimi toparla, brief'i SEN yaz, Evren'e aç, takip et, kanıt iste.")
        msgs = [{"role": "system", "content": sys_msg}] + hist + \
               [{"role": "user", "content": user_text}]
        budget = max(int(max_tokens or 0), 3000)
        txt, _tok = evren_chat(msgs, max_tokens=budget, consumer="ayla")
        if not txt:
            txt, _tok = evren_chat(msgs, max_tokens=6000, consumer="ayla")
        set_agent("filtre", "idle"); set_agent("sefler", "idle")
        if not txt:
            # T2/G3: bulut boş döndü -> yerel yedek (etiketli) veya dürüst hata.
            loc = _ayla_local_reply(user_text, persona)
            if loc:
                return ("[yerel yedek · Ollama]\n" + loc, [])
            _salvage_intent(user_text, "model boş yanıt verdi")
            return (_ayla_honest_model_error("model boş yanıt verdi"), [])
        # T8: modelin gerçek araç çağrılarını çalıştır (call_id'li, kanıtlı)
        _calls = []
        for _m in _AYLA_CAGRI_RX.finditer(txt):
            _r = _ayla_exec_cagri(_m.group(1), user_text)
            if _r:
                _calls.append(_r)
        if _calls:
            txt = _AYLA_CAGRI_RX.sub("", txt).strip()
        body, _pev = _ayla_parse_projects(txt)
        body, created = _ayla_parse_tasks(body)
        for _r in _calls:
            try:
                body = (body + chr(10) + chr(10) + cap_metni(_r)).strip()
            except Exception:
                pass
        if _pev:
            try:
                extra = []
                for ev in _pev:
                    if ev[0] == "yeni":
                        extra.append(f"🗂 Yeni proje açıldı: <b>{ev[1]}</b>")
                    elif ev[0] == "not":
                        extra.append(f"📝 <b>{ev[1]}</b> defterine not eklendi")
                    elif ev[0] == "mimari":
                        extra.append(f"🏗 <b>{ev[1]}</b> mimarisi "
                                     + ("hazır." if not ev[2] else f"üretilemedi: {ev[2]}"))
                    elif ev[0] == "ilet":
                        if ev[3]:
                            extra.append(f"⚠ <b>{ev[1]}</b> iletilemedi: {ev[3]}")
                        else:
                            extra.append(f"📤 <b>{ev[1]}</b> Evren'e iletildi "
                                         f"(görev #{ev[2]})")
                if extra:
                    body = (body + chr(10) + chr(10) + chr(10).join(extra)).strip()
            except Exception:
                pass
        # Brief eksikligi: Ayla "ilettim" demez; acik hata olarak bildirir.
        try:
            _rej = globals().get("_AYLA_LAST_REJECTED") or []
            if _rej:
                _rl = []
                for _o, _t, _w in _rej[:4]:
                    _rl.append(f"\u26A0 Brief eksik ({_o}): {_safe_msg(_t)[:60]} \u2014 {_w}. "
                               f"BASARISIZ/BRIEF_EKSIK olarak kaydedildi; duzgun brief ile tekrar ilet.")
                body = (body + chr(10) + chr(10) + chr(10).join(_rl)).strip()
        except Exception:
            pass
        add_history(_ch, "user", user_text)
        add_history(_ch, "assistant", body)
        # Ofis: the owner (Şef) -> Ayla konusmasi. Gercek event; balonlar ondan olusur.
        now = time.time()
        try:
            if now - _AYLA_LAST_EVT.get("ayla_talk", 0) > 15:
                _AYLA_LAST_EVT["ayla_talk"] = now
                agent_event("ayla_reply", "asistan", "filtre", "completed",
                            new_task_id("ayla"), "Ayla yanitladi")
        except Exception:
            pass
        # Ayla bir ajana is verdiyse: Ayla -> o ajan (ofiste yurur, konusur)
        for t in created:
            try:
                agent_event("delegated", "filtre", t[1], "transferred",
                            new_task_id("deleg"), f"Ayla -> {t[1]}")
            except Exception:
                pass
        return body, created
    except Exception as exc:
        log("error", "ayla", f"yanit: {_safe_msg(type(exc).__name__)}")
        _salvage_intent(user_text, f"işlem hatası: {type(exc).__name__}")
        return (_ayla_honest_model_error("işlem hatası"), [])
    finally:
        # E1: model hatasi/timeout/erken return olsa bile busy ASLA takili kalmasin.
        try:
            set_agent("filtre", "idle"); set_agent("sefler", "idle")
        except Exception:
            pass


# ------------------------------------------------------------------ proje defteri (Ayla)
_ACTIVE_PROJ_KEY = "ayla_active_project"


def _now_stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def setting_get(key, default=None):
    try:
        with db() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    except Exception:
        return default


def setting_set(key, value):
    try:
        with db() as c:
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, "" if value is None else str(value)))
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ Telegram mesaj defteri
# Her gelen/giden mesaj buraya yazilir. Amac:
#   1) offset kaliciligi -> restart'ta eski mesajlar tekrar cevaplanmasin
#   2) "kim cevapladi" izi -> reply ile dogru ajana yonlendirme (P2)
#   3) denetim -> gelen sayisi = giden sayisi dogrulanabilsin
_TG_OFFSET_KEY = "tg_offset"


def tg_offset_load():
    try:
        return int(setting_get(_TG_OFFSET_KEY, "0") or 0)
    except Exception:
        return 0


def tg_offset_save(offset):
    try:
        return setting_set(_TG_OFFSET_KEY, int(offset))
    except Exception:
        return False


def tg_log(chat_id, direction, msg_id=None, reply_to=None, agent=None,
           topic=None, call_id=None, text=""):
    """Gelen/giden tek mesaji deftere yaz. Hata firlatmaz."""
    try:
        with db() as c:
            cur = c.execute(
                "INSERT INTO tg_messages(ts,chat_id,direction,msg_id,reply_to,"
                "agent,topic,call_id,text) VALUES(?,?,?,?,?,?,?,?,?)",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 str(chat_id or ""), direction, msg_id, reply_to,
                 agent, topic, call_id, (text or "")[:4000]))
            # son 5000 kayit kalsin (sinirsiz buyumesin)
            if cur.lastrowid and cur.lastrowid % 500 == 0:
                c.execute("DELETE FROM tg_messages WHERE id < ?",
                          (cur.lastrowid - 5000,))
        return True
    except Exception:
        return False


def tg_seen(msg_id, chat_id=None):
    """Bu telegram message_id daha once islendi mi? (restart/cift teslim korumasi)"""
    try:
        with db() as c:
            if chat_id is None:
                row = c.execute("SELECT 1 FROM tg_messages WHERE direction='in' "
                                "AND msg_id=? LIMIT 1", (msg_id,)).fetchone()
            else:
                row = c.execute("SELECT 1 FROM tg_messages WHERE direction='in' "
                                "AND msg_id=? AND chat_id=? LIMIT 1",
                                (msg_id, str(chat_id))).fetchone()
        return bool(row)
    except Exception:
        return False


def tg_lookup_agent_by_msg(msg_id, chat_id=None):
    """Verilen giden mesaj id'sinin hangi ajana ait oldugunu bul (reply yonlendirmesi).
    T3/G1: mesajin call_id'si varsa agent_calls'tan GERCEK ajan cozulur (Ayla imzali
    olsa bile cevabi ureten ajan dogru bulunur)."""
    try:
        with db() as c:
            if chat_id is None:
                row = c.execute(
                    "SELECT t.agent,t.topic,t.call_id,call.agent AS call_agent "
                    "FROM tg_messages t LEFT JOIN agent_calls call "
                    "ON call.call_id=t.call_id "
                    "WHERE t.direction='out' AND t.msg_id=? "
                    "ORDER BY t.id DESC LIMIT 1", (msg_id,)).fetchone()
            else:
                row = c.execute(
                    "SELECT t.agent,t.topic,t.call_id,call.agent AS call_agent "
                    "FROM tg_messages t LEFT JOIN agent_calls call "
                    "ON call.call_id=t.call_id "
                    "WHERE t.direction='out' AND t.msg_id=? AND t.chat_id=? "
                    "ORDER BY t.id DESC LIMIT 1",
                    (msg_id, str(chat_id))).fetchone()
        if not row:
            return None
        d = dict(row)
        # gercek cagri ajanini tercih et
        if d.get("call_agent"):
            d["agent"] = d["call_agent"]
        return d
    except Exception:
        return None


def proj_active_get():
    slug = setting_get(_ACTIVE_PROJ_KEY, "")
    if not slug:
        return None
    return _proj.proj_get(slug)


def proj_active_set(slug):
    return setting_set(_ACTIVE_PROJ_KEY, slug or "")


def proj_resolve(ref=""):
    """Isaret edilen projeyi coz: verilen ad -> yoksa aktif proje -> tek acik proje."""
    if ref:
        slug = _proj.proj_find(ref)
        if slug:
            return slug
    act = setting_get(_ACTIVE_PROJ_KEY, "")
    if act and _proj.proj_exists(act):
        return act
    opens = _proj.proj_open()
    if len(opens) == 1:
        return opens[0]["slug"]
    return None


def ayla_project_report(slug=None):
    """Proje durum raporu (LLM'siz). slug yoksa acik projelerin listesi."""
    if not slug:
        opens = _proj.proj_open()
        if not opens:
            return ("\U0001F5C2 <b>Ayla</b>\n\nAçık proje yok. "
                    "Yeni proje: <code>/proje yeni &lt;ad&gt;</code>")
        act = setting_get(_ACTIVE_PROJ_KEY, "")
        lines = ["\U0001F5C2 <b>Ayla — açık projeler</b>", ""]
        for m in opens[:12]:
            mark = " \u2b50" if m["slug"] == act else ""
            lines.append(f"\u2022 <b>{m['name']}</b>{mark} — {m.get('status')} "
                         f"({len(m.get('notes') or [])} not"
                         f"{', mimari var' if m.get('spec') else ''})")
        lines.append("")
        lines.append("Detay: <code>/proje &lt;ad&gt;</code> · "
                     "devam: <code>/proje devam &lt;ad&gt;</code>")
        return "\n".join(lines)
    meta = _proj.proj_get(slug)
    if not meta:
        return "\U0001F5C2 <b>Ayla</b>\n\nProje bulunamadı."
    lines = [f"\U0001F5C2 <b>{meta['name']}</b> — {meta.get('status')}",
             f"oluşturuldu: {meta.get('created')} · son: {meta.get('updated')}"]
    notes = meta.get("notes") or []
    if notes:
        lines.append("")
        lines.append("<b>Notlar</b>")
        for n in notes[-8:]:
            lines.append(f"\u2022 <i>{str(n.get('ts'))[5:16]}</i> {_safe_msg(n.get('text'))[:160]}")
        if len(notes) > 8:
            lines.append(f"… ve {len(notes) - 8} not daha")
    else:
        lines.append("\nHenüz not yok. Konuşarak ekleyebilirsin.")
    if meta.get("spec"):
        lines.append("")
        lines.append(f"\U0001F3D7 Mimari hazır ({meta['spec'].get('chars', 0)} karakter). "
                     f"Görmek için: <code>/proje mimari {meta['name']}</code>")
    if meta.get("tasks"):
        lines.append(f"\U0001F4CC Bağlı görev: " +
                     ", ".join(f"#{t}" for t in meta["tasks"][:6]))
    return "\n".join(lines)


def _spec_prompt(meta, notes_text):
    return (
        "Sen Ayla'sın; the ownerün (the owner) proje isteklerini toparlayıp "
        "Evren'e (kıdemli yazılım ajanı) uygulanabilir bir MİMARİ/SPEC "
        "metni hazırlıyorsun.\n\n"
        "KURALLAR:\n"
        "- Yalnızca aşağıdaki proje notlarındaki bilgiyi kullan. Uydurma.\n"
        "- Eksik kritik bilgi varsa 'VARSAYIM:' başlığı altında makul varsayım "
        "olarak belirt, soru sorma.\n"
        "- Türkçe, Markdown, net ve uygulanabilir yaz.\n"
        "- Kod yazma; plan ve mimari yaz.\n"
        "- KISA tut: her başlıkta en fazla 3-5 madde, toplam ~400 kelime.\n"
        "- Uzun paragraf yazma; yalnız madde işaretleri kullan.\n"
        "- Gereksiz açıklama ve tekrar yok.\n\n"
        "ŞU BAŞLIKLARI KULLAN:\n"
        "# {name}\n"
        "## 1. Amaç\n## 2. Kapsam (neler var / neler yok)\n"
        "## 3. Fonksiyonel Gereksinimler\n## 4. Fonksiyonel Olmayan "
        "(performans, güvenlik, uyumluluk)\n## 5. Teknoloji Seçimi\n"
        "## 6. Mimari ve Bileşenler\n## 7. Dosya/Klasör Yapısı\n"
        "## 8. Uygulama Adımları (sıralı, Evren için)\n"
        "## 9. Kabul Kriterleri\n## 10. Varsayımlar\n\n"
        "PROJE NOTLARI:\n" + notes_text
    ).replace("{name}", meta.get("name", "Proje"))


def _spec_looks_complete(txt):
    """Mimari metni yeterli mi? (bolum + uzunluk kontrolu; kesik yaniti reddet).
    NOT: model muhakeme (reasoning) tokeni harcadigi icin cikti bazen bos/kisa
    donebilir; burada yalnizca 'icerik var mi' bakiyoruz. Son karakter kontrolu
    YAPILMAZ (madde isaretinde biten gecerli spec'i reddetmesin)."""
    if not txt:
        return False
    if len(txt) < 700:
        return False
    low = txt.lower()
    markers = sum(1 for m in ("amac", "amaç", "kapsam", "gereksinim", "mimari",
                              "kabul", "adim", "adım", "teknoloji", "bilesen",
                              "bileşen", "dosya", "varsayım", "varsayim")
                  if m in low)
    return markers >= 2


def _spec_try_budgets(base):
    """Dusunme tokenlari icin taban butce + artan yedekler."""
    b = max(int(base or 0), 2000)
    seq = [b, b + 1200]
    out, seen = [], set()
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def ayla_project_spec(slug, max_tokens=2000):
    """Proje notlarindan mimari/spec uret ve kaydet. Donus: (spec_md, err).
    Model 'dusunme' tokenlari butceyi yiyebilir -> eksik/bos yanitta daha buyuk
    butceyle yeniden denenir."""
    meta = _proj.proj_get(slug)
    if not meta:
        return "", "proje bulunamadı"
    notes = _proj.proj_notes_text(slug, limit_chars=9000)
    if not (meta.get("notes") or []):
        return "", "projede hiç not yok; önce konuşup not ekleyelim"
    if not _KEYS:
        return "", "model havuzuna ulaşılamıyor"
    msgs = [{"role": "system", "content": _spec_prompt(meta, notes)},
            {"role": "user", "content": "Mimariyi yukarıdaki formatta üret."}]
    try:
        set_agent("filtre", "busy"); set_agent("sefler", "busy")
        best = ""
        last_empty = False
        for budget in _spec_try_budgets(max_tokens):
            try:
                out, _tok = evren_chat(msgs, max_tokens=budget, consumer="ayla",
                                       timeout=260)
            except Exception:
                out = ""
            out = (out or "").strip()
            last_empty = not out
            if len(out) > len(best):
                best = out
            if _spec_looks_complete(best):
                break
            log("warn", "proje",
                f"mimari yetersiz ({'bos' if not out else str(len(out)) + ' kr'}) "
                f"butce={budget} -> artiriliyor")
        set_agent("filtre", "idle"); set_agent("sefler", "idle")
        if not best:
            return "", ("model boş yanıt verdi (düşünme tokenları bütçeyi yedi). "
                        "Birazdan tekrar dene: /proje mimari")
        if not _spec_looks_complete(best):
            return "", (f"model eksik/kısa yanıt verdi ({len(best)} karakter). "
                        f"Tekrar dene: /proje mimari")
        saved = _proj.proj_save_spec(slug, best)
        log("ok", "proje", f"mimari hazir: {slug} ({len(best)} kr)")
        return saved or best, ""
    except Exception as exc:
        try:
            set_agent("filtre", "idle"); set_agent("sefler", "idle")
        except Exception:
            pass
        return "", _safe_msg(str(exc))[:120]


def ayla_project_transfer(slug, auto_spec=True):
    """Projeyi Evren'e gorev olarak ilet. Donus: (task_id, err)."""
    meta = _proj.proj_get(slug)
    if not meta:
        return None, "proje bulunamadı"
    spec = _proj.proj_get_spec(slug)
    if not spec.strip() and auto_spec:
        spec, err = ayla_project_spec(slug)
        if err:
            return None, "mimari üretilemedi: " + err
    if not spec.strip():
        return None, "mimari yok; önce /proje mimari"
    title = f"[Proje] {meta['name']} — mimariye göre uygula"
    detail = (f"PROJE: {meta['name']} (slug: {slug})\n"
              f"Kaynak defter: agents/ayla/projeler/{slug}/PROJE.md\n"
              f"Mimari: agents/ayla/projeler/{slug}/MIMARI.md\n\n"
              f"--- MİMARİ / SPEC ---\n{spec}")
    # Ayni proje icin acik (bekleyen/yapilan) gorev varsa: yenisini acma, GUNCELLE.
    reuse = None
    try:
        with db() as c:
            for t in (meta.get("tasks") or []):
                r = c.execute("SELECT id,status FROM agent_tasks WHERE id=?",
                              (int(t),)).fetchone()
                if r and r["status"] in ("BEKLIYOR", "YAPILIYOR"):
                    reuse = r["id"]
                    break
    except Exception:
        reuse = None

    if reuse:
        try:
            with db() as c:
                c.execute("UPDATE agent_tasks SET title=?, detail=?, updated_at=? "
                          "WHERE id=?",
                          (title, _mask_only(detail, 8000), _now_stamp(), reuse))
            _proj.proj_link_task(slug, reuse)
            _proj.proj_set_status(slug, "evrene_iletildi")
            proj_active_set(slug)
            log("ok", "proje", f"mevcut gorev guncellendi: {slug} -> #{reuse}")
            return reuse, ""
        except Exception:
            pass

    tid = task_add(title, detail, owner="evren", priority=1, source="ayla")
    if not tid:
        return None, "görev oluşturulamadı"
    _proj.proj_link_task(slug, tid)
    _proj.proj_set_status(slug, "evrene_iletildi")
    proj_active_set(slug)
    log("ok", "proje", f"Evren'e iletildi: {slug} -> gorev #{tid}")
    return tid, ""


# ------------------------------------------------------------------ Ajan kayıt defteri + çağrı katmanı
# Her ajanin GERCEKTEN yapabildikleri burada tanimli. Ayla yalnizca buradaki
# yetenekleri cagirabilir; liste disindakine "yapamiyorum" der. Her cagri
# agent_calls'a yazilir (call_id + status) -> "ilettim" iddiasi kanit ister.

def cap_kurye_mail_gonder(text="", args=None, call_id=""):
    """V1: Ayla -> Kurye uzerinden GERCEK SMTP mail gonderir.
    GUVENLIK: varsayilan alici the owner's kendisi (GMAIL_ADDRESS). Dis adrese
    gonderim OWNER ONAYI ister -> args.onay==True olmadan dis adrese GONDERMEZ."""
    a = args or {}
    self_addr = (CFG.get("GMAIL_ADDRESS") or "").strip()
    raw = (text or "").strip()
    # alici: args.to veya metindeki ilk e-posta
    to = (a.get("to") or "").strip()
    if not to:
        m = re.search(r"([\w.\-+]+@[\w.\-]+\.\w+)", raw)
        if m:
            to = m.group(1)
    to = to or self_addr
    onay = bool(a.get("onay") or a.get("confirm"))
    if to.lower() != self_addr.lower() and not onay:
        return (f"Dis adrese (<code>{_safe_msg(to)[:60]}</code>) gonderim icin "
                f"the owner onayi gerekli. Onaylarsaniz ayni istegi 'onayliyorum' "
                f"diyerek tekrarlayin; aksi halde raporu size ({self_addr}) gonderebilirim.")
    subject = (a.get("subject") or a.get("konu") or "").strip()
    body = (a.get("body") or a.get("icerik") or "").strip()
    if not subject:
        mm = re.search(r"(?:konu|baslik)\s*[:=]\s*(.+?)(?:\n|$)", raw, re.I)
        subject = (mm.group(1).strip() if mm else "EVREN raporu")[:120]
    if not body:
        body = re.sub(r"(?i).*?(?:mail|e-?posta)\w*\s*(?:at|g[oö]nder)\w*"
                      r"\s*[:\-]?\s*", "", raw).strip() or raw
    if len(body) < 3:
        return "Mail icerigi bos; gonderilecek bir rapor/metin belirtin."
    try:
        r = _actions_mod.send_mail(CFG, to, subject, body)
    except Exception as exc:
        return f"Mail gonderilemedi: {type(exc).__name__}"
    if not r.get("ok"):
        return f"Mail gonderilemedi: {_safe_msg(str(r.get('error')))[:120]}"
    log("ok", "kurye", f"mail gonderildi (call_id={call_id})")
    return (f"Mail gonderildi -> <code>{_safe_msg(to)[:60]}</code>\n"
            f"Konu: {_safe_msg(subject)[:100]}\n"
            f"<i>{_safe_msg(body)[:200]}</i>")


_TR_MONTHS = {
    "ocak": 1, "şubat": 2, "subat": 2, "mart": 3, "nisan": 4, "mayıs": 5,
    "mayis": 5, "haziran": 6, "temmuz": 7, "ağustos": 8, "agustos": 8,
    "eylül": 9, "eylul": 9, "ekim": 10, "kasım": 11, "kasim": 11,
    "aralık": 12, "aralik": 12,
}


def parse_when(text):
    """Basit TR tarih/zaman ifadesi -> epoch (yerel). Bulamazsa None.
    Destek: '1 dk/saat sonra', 'bugun/yarin HH:MM', 'DD Ay [YYYY] HH:MM',
    'HH:MM' (gecmisse yarin)."""
    try:
        low = (text or "").lower()
        now = datetime.now()
        m = re.search(r"(\d+)\s*(dk|dakika)\s*sonra", low)
        if m:
            return (now + timedelta(minutes=int(m.group(1)))).timestamp()
        m = re.search(r"(\d+)\s*saat\s*sonra", low)
        if m:
            return (now + timedelta(hours=int(m.group(1)))).timestamp()

        def _at(base, hh, mm):
            try:
                return base.replace(hour=int(hh), minute=int(mm or 0),
                                    second=0, microsecond=0)
            except Exception:
                return None
        m = re.search(r"yarin\s*(?:saat\s*)?(\d{1,2})[:.](\d{2})", low)
        if m:
            d = _at(now + timedelta(days=1), m.group(1), m.group(2))
            return d.timestamp() if d else None
        m = re.search(r"bug[uü]n\s*(?:saat\s*)?(\d{1,2})[:.](\d{2})", low)
        if m:
            d = _at(now, m.group(1), m.group(2))
            return d.timestamp() if d else None
        m = re.search(r"(\d{1,2})\s*(" + "|".join(_TR_MONTHS) + r")\s*"
                      r"(\d{4})?\s*(?:saat\s*)?(\d{1,2})[:.](\d{2})", low)
        if m:
            mon = _TR_MONTHS.get(m.group(2))
            year = int(m.group(3)) if m.group(3) else now.year
            try:
                d = datetime(year, mon, int(m.group(1)),
                             int(m.group(4)), int(m.group(5)))
            except Exception:
                return None
            if not m.group(3) and d < now - timedelta(days=1):
                d = d.replace(year=year + 1)
            return d.timestamp()
        m = re.search(r"(?:saat\s*)?(\d{1,2})[:.](\d{2})", low)
        if m:
            d = _at(now, m.group(1), m.group(2))
            if d and d < now:
                d = d + timedelta(days=1)
            return d.timestamp() if d else None
        return None
    except Exception:
        return None


def _reminder_clean_text(text, when_text=""):
    """Zaman ifadesini metinden ayikla; kalan = hatirlatma konusu."""
    t = (text or "").strip()
    for pat in (
        r"(?i)\b(?:yarin|bug[uü]n)\b(?:\s*saat\s*)?\d{1,2}[:.]\d{2}",
        r"(?i)\d{1,2}\s*(?:" + "|".join(_TR_MONTHS) + r")\s*\d{0,4}"
        r"\s*(?:saat\s*)?\d{0,2}[:.]?\d{0,2}",
        r"(?i)\d+\s*(?:dk|dakika|saat)\s*sonra",
        r"(?i)\b(?:saat\s*)?\d{1,2}[:.]\d{2}\b",
        r"(?i)\bhat[iı]rlat(?:ma)?\b", r"(?i)\bhat[iı]rlat\b",
        r"(?i)\bne\s*zaman\b",
    ):
        t = re.sub(pat, " ", t)
    t = re.sub(r"\s+", " ", t).strip(" ,.:-")
    return t or (when_text or "hatirlatma")


def cap_ayla_hatirlat(text="", args=None, call_id=""):
    """V2: yerel hatirlatma olustur. args: {ne, ne_zaman} veya metinden parse."""
    a = args or {}
    ne = (a.get("ne") or "").strip()
    ne_zaman = (a.get("ne_zaman") or a.get("zaman") or "").strip()
    due = parse_when(ne_zaman or text)
    if due is None:
        return ("Hatirlatma zamani anlasilamadi. Ornek: "
                "'28 Kasim 10:00 doktor randevusu hatirlat' veya '1 saat sonra su ...'.")
    if not ne:
        ne = _reminder_clean_text(text, ne_zaman)
    with db() as c:
        c.execute("INSERT INTO reminders(ts_due,text,channel,done,created_ts) "
                  "VALUES(?,?,?,0,?)",
                  (float(due), ne[:500], "telegram", time.time()))
    when = datetime.fromtimestamp(due).strftime("%d.%m.%Y %H:%M")
    log("ok", "ayla", f"hatirlatma eklendi ({when})")
    return f"Hatirlatma kuruldu: <b>{_safe_msg(ne)[:120]}</b>\nZaman: <b>{when}</b>"


def cap_ayla_hatirlat_listele(text="", args=None, call_id=""):
    """V2: yaklasan hatirlatmalari listele (salt okuma)."""
    with db() as c:
        rows = c.execute("SELECT id,ts_due,text FROM reminders WHERE done=0 "
                         "ORDER BY ts_due ASC LIMIT 30").fetchall()
    if not rows:
        return "Planli hatirlatma yok."
    lines = ["<b>Yaklasan hatirlatmalar</b>"]
    for r in rows:
        when = datetime.fromtimestamp(r["ts_due"]).strftime("%d.%m.%Y %H:%M")
        lines.append(f"• {when} — {_safe_msg(r['text'])[:100]}")
    return "\n".join(lines)


def reminder_due_once():
    """Vakit gelen hatirlatmalari Telegram'a at ve done=1 yap."""
    now = time.time()
    sent = 0
    with db() as c:
        rows = c.execute("SELECT id,text FROM reminders WHERE done=0 AND ts_due<=? "
                         "ORDER BY ts_due ASC LIMIT 20", (now,)).fetchall()
    for r in rows:
        msg = "\u23F0 <b>Hat\u0131rlatma</b>\n" + _safe_msg(r["text"] or "")[:400]
        ok = False
        try:
            ok = _tg_send(msg)
        except Exception:
            ok = False
        if ok:
            with db() as c:
                c.execute("UPDATE reminders SET done=1 WHERE id=?", (r["id"],))
            sent += 1
    return sent


REMINDER_SEC = int(CFG.get("REMINDER_SECONDS", "60") or 60)


def reminder_loop():
    """Her dakika vadesi gelen hatirlatmalari kontrol et."""
    time.sleep(30)
    while True:
        try:
            reminder_due_once()
        except Exception as exc:
            log("error", "ayla", f"hatirlatma dongusu: {exc}")
        time.sleep(REMINDER_SEC)


AGENT_REGISTRY = {
    "apiman": {
        "name": "Apiman", "title": "API & Kota",
        "caps": {
            "kota": {"desc": "Anahtar/kota durumunu raporlar (salt okuma)",
                     "fn": "cap_apiman_kota", "readonly": True},
            "denetle": {"desc": "Kotayi denetler, gerekirse anahtar degistirir",
                        "fn": "cap_apiman_denetle", "readonly": False},
        },
    },
    "cirak": {
        "name": "Cirak", "title": "Yerel Isci / Yedek",
        "caps": {
            "yedek": {"desc": "Yedek durumunu denetler (salt okuma)",
                      "fn": "cap_cirak_yedek", "readonly": True},
            "durum": {"desc": "Kendi durumunu/heartbeat'ini raporlar",
                      "fn": "cap_cirak_durum", "readonly": True},
        },
    },
    "arsivci": {
        "name": "Arsivci", "title": "Mail Arsiv",
        "caps": {
            "kutu": {"desc": "Klasor doluluk ozeti (salt okuma)",
                     "fn": "cap_arsivci_kutu", "readonly": True},
            "gereksizler": {"desc": "Gereksizler klasoru durumu (salt okuma)",
                            "fn": "cap_arsivci_gereksizler", "readonly": True},
            "temizle": {"desc": "N aydan eski mailleri Cop'e tasir (yazma)",
                        "fn": "cap_arsivci_temizle", "readonly": False, "needs": "months"},
            "arsivle": {"desc": "TÜM gelen kutusunu arsivler (silmez, geri alinabilir)",
                        "fn": "cap_arsivci_arsivle", "readonly": False},
            "gereksiz_temizle": {"desc": "Reklam/spam maillerini 'Gereksizler'e tasir (silmez)",
                                 "fn": "cap_arsivci_gereksiz_temizle", "readonly": False},
            "son_sil": {"desc": "En son gelen (veya en yeni N) maili Cop'e tasir (silmez); "
                                "opsiyonel gonderen/konu filtresi",
                        "fn": "cap_arsivci_son_sil", "readonly": False},
            "konu_sil": {"desc": "Belirli bir maili konu eslesmesi veya Message-ID ile Cop'e "
                                 "tasir (silmez); baglam yoksa hangi mail diye sorar",
                         "fn": "cap_arsivci_konu_sil", "readonly": False},
        },
    },
    "evren": {
        "name": "Evren", "title": "Kod Ajan",
        "caps": {
            "kuyruk": {"desc": "Gorev kuyrugu + worker durumu (salt okuma)",
                       "fn": "cap_evren_kuyruk", "readonly": True},
            "brief": {"desc": "Isi brief olarak kuyruga yazar (yazma)",
                      "fn": "cap_evren_brief", "readonly": False, "needs": "text"},
        },
    },
    # V1: Kurye = gercek SMTP mail gonderimi (Ayla uzerinden).
    "kurye": {
        "name": "Kurye", "title": "Postaci (SMTP)",
        "caps": {
            "mail_gonder": {"desc": "the owner'e (veya onayli dis adrese) gercek e-posta "
                                    "gonderir; rapor/ozet iletilir",
                            "fn": "cap_kurye_mail_gonder", "readonly": False,
                            "needs": "text"},
        },
    },
    # T8/T8b: Ayla artik yonetici asistan + gercek yurutucu.
    "ayla": {
        "name": "Ayla", "title": "Yonetici Asistan / Denetci",
        "caps": {
            "denetle": {"desc": "Ajanlari gercek veriyle denetler; sorun/eksik listesi",
                        "fn": "cap_ayla_denetle", "readonly": True},
            "evren_gorev": {"desc": "Evren'e gercek gorev + yazilmis brief acar (task_id)",
                            "fn": "cap_ayla_evren_gorev", "readonly": False,
                            "needs": "text"},
            "onay": {"desc": "Gorev kanitini denetler: ONAY/REVIZYON",
                     "fn": "cap_ayla_onay", "readonly": False, "needs": "text"},
            "api_duzelt": {"desc": "Apiman yetmezse API isini guvenle ustlenir + Evren'e cozum gorevi",
                           "fn": "cap_ayla_api_duzelt", "readonly": False},
            "web_arastir": {"desc": "Web'de genel konu arastirir; kaynakli ozet (salt okuma)",
                            "fn": "cap_ayla_web_arastir", "readonly": True},
            "hatirlat": {"desc": "Yerel hatirlatma kurar (ne + ne zaman)",
                         "fn": "cap_ayla_hatirlat", "readonly": False, "needs": "text"},
            "hatirlat_listele": {"desc": "Yaklasan hatirlatmalari listeler (salt okuma)",
                                 "fn": "cap_ayla_hatirlat_listele", "readonly": True},
        },
    },
}

_AGENT_ALIAS_REG = {"filtre": "ayla", "ayla": "ayla",
                    "kurye": "kurye", "postaci": "kurye"}


def _reg_norm(aid):
    a = (aid or "").strip().lower()
    return _AGENT_ALIAS_REG.get(a, a)


def ajan_yetenekleri(aid=None):
    """Bir ajanin (veya tum ajanlarin) gercek yetenek listesi."""
    if aid:
        a = _reg_norm(aid)
        reg = AGENT_REGISTRY.get(a)
        if not reg:
            return {"agent": a, "known": False, "caps": []}
        return {"agent": a, "name": reg["name"], "title": reg["title"],
                "known": True,
                "caps": [{"cap": k, **v} for k, v in reg["caps"].items()]}
    out = []
    for a, reg in AGENT_REGISTRY.items():
        out.append({"agent": a, "name": reg["name"], "title": reg["title"],
                    "caps": [{"cap": k, **v} for k, v in reg["caps"].items()]})
    return out


def ajan_yetenek_metni():
    """Telegram icin okunur yetenek listesi."""
    lines = ["\U0001F9F0 <b>Kim ne yapabiliyor</b>", ""]
    for reg in ajan_yetenekleri():
        lines.append(f"<b>{reg['name']}</b> — {reg['title']}")
        for c in reg["caps"]:
            tag = "" if c["readonly"] else " \u270F\uFE0F"
            lines.append(f"  \u2022 <code>{c['cap']}</code>{tag} {c['desc']}")
        lines.append("")
    lines.append("<i>\u270F\uFE0F = kay\u0131t de\u011fi\u015ftirir (onay gerekebilir)</i>")
    return "\n".join(lines)


def _call_log(call_id, aid, cap, request, status, result="", error="",
              duration_ms=0, source="ayla"):
    try:
        with db() as c:
            c.execute("INSERT INTO agent_calls(call_id,ts,agent,cap,request,status,"
                      "result,error,duration_ms,source) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (call_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       aid, cap, (request or "")[:1000], status,
                       (result or "")[:4000], (error or "")[:500],
                       int(duration_ms or 0), source))
    except Exception:
        pass


def ajana_sor(aid, cap="", text="", args=None, source="ayla", timeout=60):
    """Ajan cagri katmani. Donus: dict(call_id, agent, cap, ok, result, error, ms).
    'ilettim' iddiasi icin ok=True + call_id sart."""
    a = _reg_norm(aid)
    cid = "c" + uuid.uuid4().hex[:12]
    t0 = time.time()
    # T16: araştırma çağrılarında ham soru DB'ye yazılmaz (kişisel veri sızmasın).
    req_log = text
    if cap in ("arastir", "derin_arastir", "web_arastir"):
        _q = _research_question(text, args)
        _okq, _cleanq, _ = _web.sanitize_query(_q)
        req_log = _cleanq if _okq else "[REDACTED:kisisel veri]"
    reg = AGENT_REGISTRY.get(a)
    if not reg:
        err = f"bilinmeyen ajan: {aid}"
        _call_log(cid, a, cap, req_log, "error", error=err, source=source)
        return {"call_id": cid, "agent": a, "cap": cap, "ok": False,
                "result": "", "error": err, "ms": 0}

    # cap secimi: verilmezse tek readonly yetenek varsa onu sec
    if not cap:
        ros = [k for k, v in reg["caps"].items() if v.get("readonly")]
        cap = ros[0] if len(ros) == 1 else list(reg["caps"])[0]
    cdef = reg["caps"].get(cap)
    if not cdef:
        err = f"{reg['name']} icin '{cap}' yetenegi yok"
        _call_log(cid, a, cap, req_log, "error", error=err, source=source)
        return {"call_id": cid, "agent": a, "cap": cap, "ok": False,
                "result": "", "error": err, "ms": 0}
    if cdef.get("needs") == "text" and not (text or "").strip():
        err = f"{reg['name']}.{cap} icin metin gerekli"
        _call_log(cid, a, cap, req_log, "error", error=err, source=source)
        return {"call_id": cid, "agent": a, "cap": cap, "ok": False,
                "result": "", "error": err, "ms": 0}

    fn = globals().get(cdef["fn"])
    if not callable(fn):
        err = f"yetenek kodu yok: {cdef['fn']}"
        _call_log(cid, a, cap, req_log, "error", error=err, source=source)
        return {"call_id": cid, "agent": a, "cap": cap, "ok": False,
                "result": "", "error": err, "ms": 0}

    ok = False
    try:
        set_agent(a, "busy")
    except Exception:
        pass
    try:
        res = fn(text=text, args=args or {}, call_id=cid)
        ok = True
        err = ""
    except Exception as exc:
        res = ""
        ok = False
        err = _safe_msg(f"{type(exc).__name__}: {exc}")[:200]
    finally:
        try:
            set_agent(a, "idle" if ok else "error")
        except Exception:
            pass
    ms = int((time.time() - t0) * 1000)
    result = res if isinstance(res, str) else str(res)
    result_log = (result[:800] if cap in ("arastir", "derin_arastir", "web_arastir")
                  else result)
    _call_log(cid, a, cap, req_log, "ok" if ok else "error",
              result=result_log, error=err, duration_ms=ms, source=source)
    if ok:
        try:
            _emit_key_event  # noqa: F821 (dokunma; sadece import dogrulama)
        except Exception:
            pass
        try:
            agent_event("call_ok", a, "filtre", "completed", cid,
                        f"{reg['name']}.{cap}")
        except Exception:
            pass
    return {"call_id": cid, "agent": a, "cap": cap, "ok": ok,
            "result": result, "error": err, "ms": ms,
            "name": reg["name"]}


_AGENT_WORDS = {
    # Turkce ek alabilir: doktora, apimana, ciraga, evrenin ...
    "apiman": (r"\bap[iı]man\w*", "Apiman"),
    "cirak": (r"\b[cç][ıi]?ra[kğg]\w*", "Cirak"),
    "arsivci": (r"\bar[sş]ivci\w*", "Arsivci"),
    "evren": (r"\bevren\w*", "Evren"),
    "ayla": (r"\bayla\w*", "Ayla"),
    "kurye": (r"\bkurye\w*|\bpostac[ıi]\w*", "Kurye"),
}


def ajan_bul_pos(text):
    """Metinde gecen ilk ajan kimligi ve konumu. Donus: (aid, konum) veya None."""
    low = (text or "").lower()
    best = None
    for aid, (rx, _n) in _AGENT_WORDS.items():
        m = re.search(rx, low)
        if m and (best is None or m.start() < best[1]):
            best = (aid, m.start())
    return best


def ajan_bul(text):
    """Metinde gecen ajan kimligini bul (deterministik). Donus: aid veya None."""
    b = ajan_bul_pos(text)
    return b[0] if b else None


def cap_tahmin(aid, text):
    """Ajan + metne gore en uygun yetenegi sec (LLM'siz, guvenli varsayilan: readonly)."""
    low = (text or "").lower()
    a = _reg_norm(aid)
    if a == "apiman":
        if re.search(r"(de[gğ]i[sş]tir|denetle|kontrol\s*et\s*ve|otomatik)", low):
            return "denetle"
        return "kota"
    if a == "cirak":
        if re.search(r"yedek|backup", low):
            return "yedek"
        return "durum"
    if a == "arsivci":
        # "tüm/hepsini sil-boşalt-temizle" = tüm kutuyu arşivle (ayrı iş)
        if re.search(r"(t[uü]m|hepsi|hepsini|b[uü]t[uü]n|komple)\w*.*?"
                     r"(sil|bo[sş]alt|temizle)|"
                     r"(sil|bo[sş]alt|temizle)\w*.*?(t[uü]m|hepsi|hepsini|b[uü]t[uü]n)",
                     low):
            return "arsivle"
        # N aydan eski / eski mailler -> toplu temizlik KORUNUR
        if re.search(r"\d+\s*(ay|ayl[ıi]k|g[uü]n)|aydan\s*eski|eski\s*mail", low):
            return "temizle"
        # E2/T20: tek/belirli mail silme (N-ay temizliği DEĞİL)
        if re.search(r"(bunu|şunu|sunu|bu\s*mail[i]?|şu\s*mail[i]?|belirli|"
                     r"konulu|konusu)", low) and re.search(
                r"(sil|kald[ıi]r|ta[şs][ıi]|ç[oö]pe)", low):
            return "konu_sil"
        if re.search(r"(en\s*son|son|en\s*yeni|en\s*g[uü]ncel)\s*"
                     r"(gelen\s*)?(\d+\s*)?mail", low) and re.search(
                r"(sil|kald[ıi]r|ta[şs][ıi]|ç[oö]pe)", low):
            return "son_sil"
        if re.search(r"temizle|sil", low):
            return "temizle"
        if re.search(r"gereksiz", low):
            return "gereksizler"
        return "kutu"
    if a == "evren":
        if re.search(r"ilet|brief|g[oö]rev\s*(ver|ekle)|kuyru[gğ]a\s*(yaz|at)", low):
            return "brief"
        return "kuyruk"
    if a == "kurye":
        return "mail_gonder"
    if a == "ayla":
        if re.search(r"hat[iı]rlat", low):
            if re.search(r"listele|neler|hangileri|g[oö]ster|ne\s*var", low):
                return "hatirlat_listele"
            return "hatirlat"
        if re.search(r"onay|reviz|kabul\s*et", low):
            return "onay"
        if re.search(r"api|anahtar|kota|havuz", low) and re.search(
                r"üstlen|ustlen|yapam|düzelt|duzelt|çöz|coz", low):
            return "api_duzelt"
        if re.search(r"evren|g[oö]rev|brief|i[şs]\s*(ver|a[cç])", low):
            return "evren_gorev"
        if _RESEARCH_RX.search(low):
            return "web_arastir"
        # F3: varsayılan denetle DEĞİL; yalnız açık denetim komutu.
        if _AUDIT_CMD_RX.search(low):
            return "denetle"
        return ""
    reg = AGENT_REGISTRY.get(a) or {}
    caps = reg.get("caps") or {}
    for k, v in caps.items():
        if v.get("readonly"):
            return k
    return next(iter(caps), "")


def cap_args_tahmin(aid, cap, text):
    """Yetenek parametrelerini metinden cikar."""
    low = (text or "").lower()
    args = {}
    if cap == "temizle":
        m = re.search(r"(\d+)\s*(ay|ayl[ıi]k)", low)
        args["months"] = int(m.group(1)) if m else 1
    if cap in ("son_sil", "konu_sil"):
        m = re.search(r"(\d+)\s*(?:adet\s*)?mail", low)
        if m:
            try:
                args["count"] = max(1, min(int(m.group(1)), 50))
            except Exception:
                pass
        mf = re.search(r"([\w.\-+]+@[\w.\-]+\.\w+)", low)
        if mf:
            args["from"] = mf.group(1)
        mk = re.search(r"(?:konu|konusu|ba[şs]l[ıi][ğg][ıi])\s*[:=]?\s*[\"']?(.+?)[\"']?$",
                       text or "", re.I)
        if mk:
            args["konu"] = mk.group(1).strip()
        mi = re.search(r"message[-\s]?id\s*[:=]\s*<?([^\s>]+)>?", text or "", re.I)
        if mi:
            args["msg_id"] = mi.group(1).strip()
    if _reg_norm(aid) == "ayla" and cap == "onay":
        m = re.search(r"#?\s*(\d+)", text or "")
        if m:
            args["task_id"] = int(m.group(1))
        if re.search(r"revizyon", low):
            args["karar"] = "REVIZYON"
        elif re.search(r"onay", low):
            args["karar"] = "ONAY"
    return args


_INTENT_RX = re.compile(
    r"(s[oö]yle|sor|baks[ıi]n|kontrol|ilet|dan[ıi][sş]|raporla|"
    r"ne\s*yap[ıi]yor|durum|nedir|nas[ıi]l|istiyorum|isterim|"
    r"onayla|revizyon|denetle|üstlen|ustlen|g[oö]rev\s*(ver|a[cç])|"
    r"ne\s*yiyeyim|ne\s*yemeliyim|öner|oner|tavsiye|yorumla|analiz)", re.I)

# F3: "denetle" YALNIZ açık denetim komutunda çalışır. Durum soruları
# ("ne yapıyor/ne durumda") sabit denetim raporuna DEĞİL, gerçek veriyle modele gider.
_AUDIT_CMD_RX = re.compile(
    r"(denetle|denetim|tefti[sş]|\baudit\b|kontrol\s+et|check\s+et)", re.I)

_STATUS_Q_RX = re.compile(
    r"(ayla|apiman|ar[sş]ivci|[cç][ıi]?ra[kğg]|evren|ekip|ajanlar|sistem)\w*"
    r".{0,30}(ne\s*durumda|durumu\s*ne|durum(u)?\b|ne\s*yap[ıi]yor|"
    r"neyle\s*u[gğ]ra[sş]|nas[ıi]l\s*gidiyor)", re.I)

# T16: araştırma isteği (isim/sıfat hâli "araştırması" tek başına niyet sayılmaz)
_RESEARCH_RX = re.compile(
    r"(\bara[sş]t[ıi]r\b|ara[sş]t[ıi]rma\s*yap|derin\s*ara\b|web\s*ara\b|"
    r"internet\s*ara\b|pubmed|\bwho\b|literat[üu]r|makale\s*ara|"
    r"kaynak\s*(ara|bul)|g[üu]ncel\s*(bilgi|kaynak))", re.I)


def ajan_metinden_calistir(text, source="ayla", require_intent=True):
    """'Apiman'a soyle kotalara baksin' -> gercek cagri.
    Donus: dict(ok, agent, cap, result, call_id, error, skipped) veya None."""
    pos = ajan_bul_pos(text)
    if not pos:
        return None
    aid, at = pos
    low = (text or "").lower()
    # Ayla = cevaplayıcı. "Ayla <sohbet/iş>" modelle konuşmaktır; yalnız Ayla'nın
    # gerçek yürütücü fiilleri (denetle/onay/görev aç/api üstlen) araç çağrısıdır.
    if _reg_norm(aid) == "ayla":
        if not (_AUDIT_CMD_RX.search(low) or _RESEARCH_RX.search(low) or re.search(
                r"(onay|reviz|g[oö]rev|"
                r"(g[oö]rev|i[şs])[a-zçğıöşü]*\s*(ver|a[cç]|ekle)|"
                r"brief|[üu]stlen|ustlen|\bapi\b)", low)):
            return None
    if require_intent:
        addressed = at <= 25
        has_intent = bool(_INTENT_RX.search(low))
        # Evren haric: cumle basinda isimle hitap -> ajan cagrisi sayilir
        if _reg_norm(aid) == "evren":
            if not has_intent:
                return None
        elif not (has_intent or addressed):
            return None
    cap = cap_tahmin(aid, text)
    if not cap:
        return None
    # F3 regresyon kapisi: denetle yalnız açık denetim komutunda.
    if cap == "denetle" and not _AUDIT_CMD_RX.search(low):
        return None
    args = cap_args_tahmin(aid, cap, text)
    body = text
    res = ajana_sor(aid, cap, text=body, args=args, source=source)
    res["skipped"] = False
    return res


def cap_metni(res):
    """Cagri sonucunu patrona okunur tek bloka cevir."""
    if not res:
        return ""
    name = res.get("name") or res.get("agent")
    if not res.get("ok"):
        return f"\u26A0 {name}: yapamadim — {res.get('error') or 'bilinmeyen hata'}"
    body = (res.get("result") or "").strip()
    return (f"\U0001F916 <b>{name}</b> ({res.get('cap')}):\n{body}\n"
            f"<i>call {res.get('call_id', '')[:10]} \u00b7 {res.get('ms', 0)} ms</i>")


def ajan_cagri_dogrula(call_id):
    """Bir cagrinin gercekten basarili oldugunu DB'den dogrula."""
    if not call_id:
        return {"ok": False, "error": "call_id yok"}
    try:
        with db() as c:
            row = c.execute("SELECT agent,cap,status,result,ts FROM agent_calls "
                            "WHERE call_id=? ORDER BY id DESC LIMIT 1",
                            (call_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "kayit bulunamadi"}
        return {"ok": row["status"] == "ok", "agent": row["agent"],
                "cap": row["cap"], "result": row["result"], "ts": row["ts"]}
    except Exception as exc:
        return {"ok": False, "error": _safe_msg(str(exc))[:100]}


def dev_worker_online(max_age=300):
    """PC'deki opencode worker'i son 5 dk icinde sinyal verdi mi?"""
    try:
        hb = _hb_get("devworker")
        return bool(hb and (time.time() - hb) <= max_age)
    except Exception:
        return False


# ---------------- yetenek uygulamalari (yalniz registry'den cagrilir) ----------------
def cap_apiman_kota(text="", args=None, call_id=""):
    qa = quota_all()
    parts = []
    for k in qa["keys"]:
        lim = k.get("limit") or 0
        rem = k.get("remaining")
        pct = (rem / lim * 100) if (lim and rem is not None) else None
        mark = " \u2b50" if k.get("active") else ""
        parts.append(f"{k['label']}{mark}=%{pct:.0f}" if pct is not None
                     else f"{k['label']}{mark}=?")
    pool = qa["pool"]
    return (f"Anahtarlar: {', '.join(parts)}\n"
            f"Havuz: {pool['total_remaining']/1e6:.1f}M / "
            f"{pool['total_limit']/1e6:.0f}M "
            f"({pool['healthy']}/{pool['count']} saglikli)")


def cap_apiman_denetle(text="", args=None, call_id=""):
    out = apiman_check_once(notify=False)
    if out["action"] == "switched":
        return f"Gecis yapildi: {out['from']} -> {out['to']} ({out['reason']})"
    if out["action"] == "alarm":
        return "Tum anahtarlar kritik seviyede; degistirilecek taze anahtar yok."
    pct = ", ".join(f"{l}=%{p:.0f}" for l, p in out["pct"].items() if p is not None)
    return f"Degisiklik gerekmedi. {pct}"






# ------------------------------------------------------------------ T16: web araştırma
def _research_question(text="", args=None):
    a = args or {}
    q = (a.get("question") or a.get("query") or a.get("soru") or text or "").strip()
    return q


def _research_log(agent, cap, query, sources, summary, status):
    """Yalnız temizlenmiş genel sorgu + kaynak URL'leri + özet; kişisel veri YOK."""
    try:
        with db() as c:
            c.execute("INSERT INTO research_log(ts,agent,cap,query,sources,summary,status) "
                      "VALUES(datetime('now'),?,?,?,?,?,?)",
                      (agent, cap, query,
                       json.dumps(_web.source_refs(sources), ensure_ascii=False),
                       (summary or "")[:2000], status))
    except Exception:
        pass


def _web_synthesize(agent, question, sources, deep=False):
    """Kaynak özetlerinden modelle kaynaklı rapor. Model yoksa boş (deterministik özet)."""
    if not _KEYS or not sources:
        return ""
    try:
        blocks = []
        for i, s in enumerate(sources[:10], 1):
            blocks.append(f"[{i}] {s.get('name')} ({s.get('url')})\n"
                          f"{(s.get('snippet') or '')[:1000]}")
        sys_msg = (
            "Sen kaynaklı araştırma raporu yazıyorsun. YALNIZ aşağıdaki kaynak "
            "özetlerini kullan; uydurma kaynak/URL/veri ÜRETME. Türkçe yaz. "
            "Tanı koyma; 'tanı değildir, bilgi amaçlıdır' çerçevesinde kal. "
            + ("Kaynaklar arasında çelişki varsa açıkça belirt; yoksa 'belirgin "
               "çelişki yok' de. Daha uzun ve çok kaynaklı ol." if deep
               else "Kısa, maddeli ve kaynak numaralarına atıflı özet ver."))
        user = f"Soru: {question}\n\nKaynaklar:\n" + "\n\n".join(blocks)
        # Hız: tek deneme + kısa zaman aşımı; model gelmezse deterministik kaynak
        # özeti kullanılır (panel/Telegram uzun süre bloklanmaz).
        txt, _tok = evren_chat(
            [{"role": "system", "content": sys_msg},
             {"role": "user", "content": user}],
            max_tokens=(1100 if deep else 700),
            consumer="ayla",
            timeout=35)
        return (txt or "").strip()
    except Exception:
        return ""


def _run_web_research(agent, cap, text, args, deep=False):
    q = _research_question(text, args)
    res = _web.collect(q, deep=deep)
    if not res.get("ok"):
        err = res.get("error") or "bilinmeyen"
        # Log'a ham soru YAZILMAZ; yalnız neden.
        try:
            log("warn", agent, f"{cap}: {err} (dis aramaya gonderilmedi)")
        except Exception:
            pass
        return _web.format_report(res, deep=deep)
    summary = (_web_synthesize(agent, res["query"], res["sources"], deep=deep)
               or _web.deterministic_summary(res["sources"], deep=deep))
    report = _web.format_report(res, summary=summary, deep=deep)
    _research_log(agent, cap, res["query"], res["sources"], summary, "ok")
    try:
        log("info", agent, f"{cap} query={res['query'][:120]} "
                          f"sources={len(res['sources'])}")
    except Exception:
        pass
    return report






def cap_ayla_web_arastir(text="", args=None, call_id=""):
    return _run_web_research("ayla", "web_arastir", text, args, deep=False)




def cap_cirak_yedek(text="", args=None, call_id=""):
    st = _load_backup_state()
    try:
        files = sorted(BACKUP_DIR.glob("evren-*.tar.gz.gpg"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return "Hic yedek dosyasi yok (~/evren-backups bos)."
        newest = files[0]
        age_h = (time.time() - newest.stat().st_mtime) / 3600.0
        size_mb = newest.stat().st_size / 1048576
        return (f"Son yedek: {newest.name} | {size_mb:.2f} MB | "
                f"{age_h:.1f} saat once | toplam {len(files)} arsiv")
    except Exception as exc:
        return f"Yedek durumu okunamadi: {_safe_msg(str(exc))[:80]}"


def cap_cirak_durum(text="", args=None, call_id=""):
    bits = []
    for name in ("mail", "watchdog", "spam", "apiman", "devworker"):
        try:
            hb = _hb_get(name)
            bits.append(f"{name}={'yok' if not hb else str(int(time.time()-hb))+'sn'}")
        except Exception:
            pass
    return "Donguler: " + ", ".join(bits)


def cap_arsivci_kutu(text="", args=None, call_id=""):
    st = _actions_mod.mailbox_stats(CFG)
    if not st:
        return "Klasor bilgisi alinamadi."
    top = sorted(st.items(), key=lambda x: -x[1])[:6]
    return "Klasorler: " + ", ".join(f"{k}={v}" for k, v in top)


def cap_arsivci_gereksizler(text="", args=None, call_id=""):
    st = spam_status()
    return (f"Gereksizler: {st.get('folder_count')} mail | "
            f"son tarama: {st.get('last_ts') or 'henuz yok'} | "
            f"toplam tasinan: {st.get('total_moved', 0)}")


def cap_arsivci_temizle(text="", args=None, call_id=""):
    months = int((args or {}).get("months") or 1)
    r = _actions_mod.cleanup_older(CFG, months=months)
    n = int((r or {}).get("moved") or 0)
    err = int((r or {}).get("errors") or 0)
    if err:
        log("warn", "arsivci", f"cleanup: {n} tasindi, {err} hata (silinmedi)")
    if not n:
        if err:
            return (f"Tasima basarisiz: {err} mail kopyalanamadi, hicbiri silinmedi.")
        return f"{months} aydan eski mail yok."
    log("warn", "arsivci", f"{n} mail Cop'e tasindi ({months} ay)")
    return (f"{n} mail Cop kutusuna tasindi ({months}+ ay eski)."
            + (f" ({err} mail kopyalanamadi; silinmedi.)" if err else ""))


def _mail_move_text(r, count):
    items = (r or {}).get("items") or []
    if not items:
        return "Eşleşen mail bulunamadı; hiçbir şey taşınmadı."
    lines = [f"{len(items)} mail Cop kutusuna taşındı (kalıcı silinmedi)."]
    for it in items[:10]:
        lines.append(f"• <b>{_safe_msg(it.get('subject') or '(konusuz)')[:80]}</b> "
                     f"— <i>{_safe_msg(it.get('from') or '')[:60]}</i>")
    return "\n".join(lines)


def cap_arsivci_son_sil(text="", args=None, call_id=""):
    """En son gelen (veya en yeni N) maili Cop'e tasir. Filtre: from/konu."""
    a = args or {}
    try:
        count = int(a.get("count") or 1)
    except Exception:
        count = 1
    r = _actions_mod.trash_mail(CFG, count=count, frm=a.get("from"),
                                subject=a.get("konu"))
    if not r.get("moved"):
        if not r.get("ok"):
            return "Silme başarısız: " + str(r.get("error") or "bilinmeyen hata") + \
                   ". Hiçbir mail silinmedi."
        return _mail_move_text(r, count)
    log("warn", "arsivci", f"son_sil: {r.get('moved', 0)} mail Cop'e tasindi")
    out = _mail_move_text(r, count)
    if r.get("errors"):
        out += f"\n({r['errors']} mail kopyalanamadi; silinmedi.)"
    return out


def cap_arsivci_konu_sil(text="", args=None, call_id=""):
    """Belirli maili konu/Message-ID ile Cop'e tasir. Bağlam yoksa sorar."""
    a = args or {}
    subject = (a.get("konu") or a.get("subject") or "").strip()
    msg_id = (a.get("msg_id") or a.get("id") or "").strip()
    # Bağlam çözümü: açık hedef yoksa son gösterilen mail listesinin en yenisi.
    if not subject and not msg_id:
        ctx = last_mail_ctx()
        items = ctx.get("items") or []
        if not items or ctx.get("age", 1e9) > _LAST_MAIL_CTX_TTL:
            return ("Hangi maili silmemi istediğinizi anlayamadım. Önce mail özetini "
                    "gösterin ya da konu/ID belirtin; yanlış maili silmeyeceğim.")
        msg_id = items[0].get("msg_id") or ""
        if not msg_id:
            subject = items[0].get("subject") or ""
    r = _actions_mod.trash_mail(CFG, count=1, subject=subject or None,
                                msg_id=msg_id or None)
    if not r.get("moved"):
        if not r.get("ok"):
            return "Silme başarısız: " + str(r.get("error") or "bilinmeyen hata") + \
                   ". Hiçbir mail silinmedi."
        hedef = msg_id or subject
        return (f"'{_safe_msg(hedef)[:80]}' ile eşleşen mail bulunamadı; "
                f"hiçbir şey taşınmadı.")
    log("warn", "arsivci", f"konu_sil: {r.get('moved', 0)} mail Cop'e tasindi")
    out = _mail_move_text(r, 1)
    if r.get("errors"):
        out += f"\n({r['errors']} mail kopyalanamadi; silinmedi.)"
    return out


def cap_arsivci_arsivle(text="", args=None, call_id=""):
    """TUM gelen kutusunu arsivle (KALICI SILME YOK, geri alinabilir).
    Yetenek yalniz arsivlemedir; 'tum kutuyu kalici sil' icin durust ret verilir."""
    r = _actions_mod.archive_all_inbox(CFG)
    if not r.get("ok"):
        return ("Arşivleme başarısız: " + str(r.get("error") or "bilinmeyen hata") +
                ". Hiçbir mail silinmedi.")
    if r.get("mode") == "bos":
        return "Gelen kutusu zaten boş; arşivlenecek mail yok."
    return (f"{r.get('moved', 0)} mail ARŞİVLENDİ (silinmedi; geri alınabilir — "
            f"Gmail › Tüm Postalar). Not: tüm kutuyu KALICI silme yetkim yok; "
            f"kalıcı silme istersen çöp kutusunu ayrıca boşaltman gerekir.")


def cap_arsivci_gereksiz_temizle(text="", args=None, call_id=""):
    """Reklam/spam maillerini 'Gereksizler'e tasir (KALICI SILME YOK)."""
    lbl = (args or {}).get("label") or "Gereksiz"
    r = ayla_label_marketing(lbl)
    if not r.get("ok"):
        return ("Gereksiz temizleme başarısız: " + str(r.get("error") or "bilinmeyen") +
                ". Hiçbir mail silinmedi.")
    return (f"{r.get('labeled', 0)} reklam/spam mail '{lbl}' klasörüne taşındı "
            f"(silinmedi). Kişisel/kritik {r.get('skipped_personal', 0)} mail korundu.")


def cap_evren_kuyruk(text="", args=None, call_id=""):
    try:
        with db() as c:
            rows = c.execute(
                "SELECT status,COUNT(*) n FROM agent_tasks "
                "WHERE status IN ('BEKLIYOR','YAPILIYOR') GROUP BY status").fetchall()
            jobs = c.execute("SELECT status,COUNT(*) n FROM dev_jobs "
                             "WHERE status IN ('pending','running','started') "
                             "GROUP BY status").fetchall()
        t = ", ".join(f"{r['status']}={r['n']}" for r in rows) or "acik gorev yok"
        j = ", ".join(f"{r['status']}={r['n']}" for r in jobs) or "dev kuyrugu bos"
        w = "worker BAGLI" if dev_worker_online() else "worker KAPALI"
        return f"Gorevler: {t} | Dev: {j} | {w}"
    except Exception as exc:
        return f"Kuyruk okunamadi: {_safe_msg(str(exc))[:80]}"


def cap_evren_brief(text="", args=None, call_id=""):
    text = (text or "").strip()
    if not text:
        return "Brief metni bos."
    if not dev_worker_online():
        return ("Kuyruga YAZILMADI: PC worker kapali. Is kaydi acilmadi; "
                "worker acilinca iletebilirim.")
    title = (args or {}).get("title") or text.split("\n")[0][:90]
    tid = task_add(title, text, owner="evren", priority=1, source="ayla")
    if not tid:
        raise RuntimeError("gorev olusturulamadi")
    return f"Brief kuyruga yazildi (gorev #{tid}). Worker bagli, is alinacak."


# ---------------------------------------------------------------- T8/T8b: Ayla yurutucu yetenekler
_EVIDENCE_RX = re.compile(
    r"(commit\s*[0-9a-f]{7,40}|\b[0-9a-f]{7,40}\b|https?://\S+|\.png\b|"
    r"call[_ ]?id|c[0-9a-z]{8,}|curl\b|HTTP\s*200|exit\s*0|sha256|"
    r"DEPLOY_OK|test\w*\s*(ge[cç]|ok)|ekran g[oö]r[uü]nt[uü]s[uü])", re.I)


def _ayla_task_row(tid):
    try:
        with db() as c:
            r = c.execute("SELECT id,title,detail,owner,status,priority,result "
                          "FROM agent_tasks WHERE id=?", (int(tid),)).fetchone()
        return dict(r) if r else None
    except Exception:
        return None


def cap_ayla_denetle(text="", args=None, call_id=""):
    """Ajanlari GERCEK veriyle denetle. Donus: sorun/eksik listesi."""
    target = (args or {}).get("agent") or ""
    if not target:
        # metindeki ajanlardan Ayla disindaki ilkini sec (Ayla = denetleyen)
        low = (text or "").lower()
        best = None
        for aid, (rx, _n) in _AGENT_WORDS.items():
            if aid == "ayla":
                continue
            m = re.search(rx, low)
            if m and (best is None or m.start() < best[1]):
                best = (aid, m.start())
        target = best[0] if best else ""
    target = _reg_norm(target) if target else ""
    issues, info = [], []
    try:
        with db() as c:
            states = {r["id"]: r["state"] for r in c.execute(
                "SELECT id,state FROM agent_state").fetchall()}
            alerts = c.execute("SELECT severity,source,message FROM alerts "
                               "WHERE resolved=0 ORDER BY CASE severity "
                               "WHEN 'critical' THEN 0 ELSE 1 END LIMIT 10").fetchall()
            calls = c.execute(
                "SELECT agent,status,COUNT(*) n FROM agent_calls "
                "WHERE ts >= datetime('now','-1 day') GROUP BY agent,status"
            ).fetchall()
            errs = c.execute(
                "SELECT agent,cap,error,ts FROM agent_calls "
                "WHERE status!='ok' AND ts >= datetime('now','-1 day') "
                "ORDER BY id DESC LIMIT 8").fetchall()
            open_t = c.execute(
                "SELECT id,title,owner,status,priority FROM agent_tasks "
                "WHERE status IN ('BEKLIYOR','YAPILIYOR') ORDER BY id DESC LIMIT 8"
            ).fetchall()
            stale = c.execute(
                "SELECT id,status,updated_at FROM agent_tasks "
                "WHERE status='YAPILIYOR' AND updated_at < datetime('now','-3 day')"
            ).fetchall()
        want = {target} if target else set()
        for r in alerts:
            if want and _reg_norm(r["source"]) not in want:
                continue
            issues.append(f"alarm[{r['severity']}] {r['source']}: {_safe_msg(r['message'])[:80]}")
        counts = {}
        for r in calls:
            counts.setdefault(r["agent"], {})[r["status"]] = r["n"]
        for a, d in counts.items():
            if want and _reg_norm(a) not in want:
                continue
            bad = d.get("error", 0)
            info.append(f"{a}: ok={d.get('ok',0)} error={bad}")
            if bad:
                issues.append(f"{a}: son 24s {bad} başarısız çağrı")
        for r in errs:
            if want and _reg_norm(r["agent"]) not in want:
                continue
            else:
                issues.append(f"{r['agent']}.{r['cap']}: {_safe_msg(r['error'] or '')[:60]}")
        for r in open_t:
            if want and _reg_norm(r["owner"]) not in want and _reg_norm(r["owner"]) != "evren":
                continue
            info.append(f"açık görev #{r['id']} {r['owner']}/{r['status']}: {r['title'][:40]}")
        for r in stale:
            issues.append(f"görev #{r['id']} 3+ gündür YAPILIYOR (takılı olabilir)")
        for a, st in states.items():
            if want and _reg_norm(a) not in want:
                continue
            if st in ("error",):
                issues.append(f"{a}: durum=error")
        try:
            for name in ("mail", "watchdog", "apiman"):
                hb = _hb_get(name)
                if hb and (time.time() - hb) > 900:
                    issues.append(f"döngü {name}: {int(time.time()-hb)}sn sessiz")
        except Exception:
            pass
    except Exception as exc:
        return "Denetim okunamadı: " + _safe_msg(str(exc))[:100]
    scope = target or "tüm ekip"
    head = f"Denetim ({scope}) — gerçek veri:"
    body = []
    if info:
        body.append("Durum: " + "; ".join(info[:10]))
    if issues:
        body.append("Sorun/eksik: " + "; ".join(issues[:12]))
    else:
        body.append("Sorun/eksik: yok (açık alarm/hata izi bulunmadı).")
    return head + "\n" + "\n".join(body)


def cap_ayla_evren_gorev(text="", args=None, call_id=""):
    """Evren'e GERCEK gorev + Ayla'nin yazdigi brief. task_id doner. Uydurma yok."""
    a = args or {}
    raw = (text or "").strip()
    _clean = raw
    if ":" in raw:
        _pre, _post = raw.split(":", 1)
        if re.search(r"(?i)evren|g[oö]rev|i[şs]|ver|a[cç]", _pre):
            _clean = _post.strip() or raw
    title = (a.get("title") or (_clean.split("\n")[0][:90] if _clean else "")).strip()
    if not title:
        return "Görev açılmadı: başlık/istek yok (uydurma görev açmam)."
    detail = (a.get("detail") or "").strip()
    if not detail:
        detail = raw or title
    low = detail.lower()
    if "gereksinim" not in low:
        detail += ("\n\nGereksinimler: 1) İstenen davranışı uygula 2) Mevcut davranışı bozma "
                   "3) Geri alınabilir/kanıtlı olsun")
    if "kabul" not in low:
        detail += ("\nKabul: çalışan çıktı + commit/endpoint/ekran görüntüsü kanıtı; "
                   "kanıtsız iş ONAYLANMAZ.")
    tid = task_add(title, detail, owner="evren", priority=1, source="ayla")
    if not tid:
        return "Görev açılamadı (DB hatası). Hiçbir iş uydurulmadı."
    try:
        agent_event("ayla_task", "ayla", "evren", "started", str(tid),
                    "Ayla Evren'e brief açtı")
    except Exception:
        pass
    w = "worker bağlı" if dev_worker_online() else "worker şu an kapalı (kuyrukta bekler)"
    return (f"Evren'e görev açıldı: #{tid} — {title}\n"
            f"Brief yazıldı (gereksinim + kabul + kanıt şartı). {w}.\n"
            f"Durum: BEKLIYOR.")


def cap_ayla_onay(text="", args=None, call_id=""):
    """Gorev kanitini denetle: ONAY / REVIZYON. Kanit yoksa ONAY VERMEZ."""
    a = args or {}
    tid = a.get("task_id") or a.get("id")
    if not tid:
        m = re.search(r"#?\s*(\d+)", text or "")
        tid = m.group(1) if m else None
    if not tid:
        return "Onay için görev numarası gerekli (ör. #12). ONAY verilmedi."
    row = _ayla_task_row(tid)
    if not row:
        return f"#{tid} bulunamadı; ONAY verilmedi."
    karar = (a.get("karar") or "").upper()
    if not karar:
        kl = (text or "").lower()
        karar = "ONAY" if ("onay" in kl and "reviz" not in kl) else "REVIZYON"
    gerekce = a.get("gerekce") or ""
    result = (row.get("result") or "")
    blob = (result + " " + (row.get("detail") or ""))
    ev = _EVIDENCE_RX.search(result) or _EVIDENCE_RX.search(blob)
    has_commit = bool(re.search(r"\b[0-9a-f]{7,40}\b", result or ""))
    if karar == "ONAY":
        if row.get("status") not in ("BITTI", "YAPILIYOR") or not result.strip():
            why = "sonuç/kanıt kaydı yok"
            task_set(tid, status="REVIZYON",
                     result=(result + f"\n[AYLA REVIZYON] {why}; kanıt ekle.").strip())
            return (f"#{tid} için ONAY YOK — {why}. REVIZYON'a alındı. "
                    f"Gerekli: commit/çıktı/ekran görüntüsü/endpoint kanıtı.")
        if not ev:
            why = "kanıt izi (commit/çıktı/URL/görüntü/call) bulunamadı"
            task_set(tid, status="REVIZYON",
                     result=(result + f"\n[AYLA REVIZYON] {why}.").strip())
            return (f"#{tid} için ONAY YOK — {why}. REVIZYON'a alındı; "
                    f"somut kanıt olmadan yüzeysel 'tamam' kabul edilmez.")
        task_set(tid, status="BITTI", result=(result + "\n[AYLA ONAY] kanıt doğrulandı.").strip())
        return (f"#{tid} ONAYLANDI. Kanıt: {_safe_msg(ev.group(0))[:60]}"
                + (" (commit)" if has_commit else "") + ".")
    # REVIZYON
    why = gerekce or "kanıt yetersiz/eksik"
    task_set(tid, status="REVIZYON",
             result=(result + f"\n[AYLA REVIZYON] {why}").strip())
    return f"#{tid} REVIZYON'a alındı: {why}. Eksikleri tamamlayıp kanıtla dön."


def cap_ayla_api_duzelt(text="", args=None, call_id=""):
    """Apiman yetmediginde Ayla guvenle ustlenir (anahtar DEGERI gormez) + Evren'e cozum gorevi."""
    try:
        out = apiman_check_once(notify=False)
        if out.get("action") == "switched":
            durum = f"Apiman geçiş yaptı: {out.get('from')} -> {out.get('to')} ({out.get('reason')})"
        elif out.get("action") == "alarm":
            durum = "Apiman tüm anahtarlar kritik; taze anahtar yok."
        else:
            pct = ", ".join(f"{k}=%{v:.0f}" for k, v in (out.get("pct") or {}).items()
                            if v is not None)
            durum = f"Apiman değişiklik yapmadı. {pct}".strip()
    except Exception as exc:
        durum = "Apiman çalıştırılamadı: " + _safe_msg(str(exc))[:80]
    req = (text or "").strip() or "API anahtar havuzu davranışını düzelt"
    title = "API havuzu davranışını düzelt (Ayla tespiti)"
    detail = (f"İstek: {req}\n"
              f"Apiman sonucu: {durum}\n"
              "Gereksinimler: 1) Anahtar seçimi/cooldown/exhausted geçişlerini incele "
              "2) Hatalı davranışı düzelt 3) Anahtar DEĞERİ log/çıktıya yazılmasın "
              "4) Geri alınabilir olsun\n"
              "Kabul: test çıktısı + commit kanıtı; anahtar değeri görünmez.")
    tid = task_add(title, detail, owner="evren", priority=1, source="ayla")
    return (f"{durum}\nAyla üstlendi (anahtar değeri görünmez). "
            f"Kalıcı çözüm için Evren'e görev açıldı: #{tid}.")


def ayla_task_table(limit=15):
    """'Durum' komutu icin defter tablosu (LLM'siz, deterministik)."""
    rows = task_list(limit=limit)
    if not rows:
        return "Görev defteri boş the owner. Yeni iş verdiğinizde burada görünür."
    icon = {"BEKLIYOR": "⏳", "YAPILIYOR": "🔧", "BITTI": "✅",
            "BASARISIZ": "❌", "IPTAL": "⏹"}
    lines = ["<b>Görev Defteri</b>"]
    for t in rows:
        lines.append(f"{icon.get(t['status'], '•')} <b>#{t['id']}</b> {t['title']} "
                     f"— <i>{t['owner']}</i> · {t['status']}")
    try:
        lk = [l for l in lock_list() if not l["stale"]]
        if lk:
            lines.append("")
            lines.append("<b>Kilitli alanlar</b>: " +
                         ", ".join(f"{l['area']} ({l['owner']})" for l in lk[:5]))
    except Exception:
        pass
    return "\n".join(lines)


def workload_snapshot():
    """Evren/ekibin CANLI is yuku: acik gorevler, dev kuyrugu, alarmlar.
    Deterministik; LLM'e sormadan gercek DB'den okur."""
    out = {"open": [], "by_status": {}, "dev_pending": 0, "dev_running": 0,
           "alerts": [], "locks": []}
    try:
        with db() as c:
            for r in c.execute(
                    "SELECT status, COUNT(*) n FROM agent_tasks GROUP BY status").fetchall():
                out["by_status"][r["status"]] = r["n"]
            rows = c.execute(
                "SELECT id,title,owner,status,priority FROM agent_tasks "
                "WHERE status IN ('BEKLIYOR','YAPILIYOR') "
                "ORDER BY CASE status WHEN 'YAPILIYOR' THEN 0 ELSE 1 END, "
                "priority ASC, id ASC LIMIT 30").fetchall()
            out["open"] = [{"id": r["id"], "title": r["title"] or "-",
                            "owner": r["owner"] or "-", "status": r["status"],
                            "priority": r["priority"]} for r in rows]
            try:
                out["dev_pending"] = c.execute(
                    "SELECT COUNT(*) FROM dev_jobs WHERE status='pending'").fetchone()[0]
                out["dev_running"] = c.execute(
                    "SELECT COUNT(*) FROM dev_jobs WHERE status IN ('running','started')"
                ).fetchone()[0]
            except Exception:
                pass
            out["alerts"] = [{"severity": r["severity"], "message": r["message"],
                              "count": r["count"]} for r in c.execute(
                "SELECT severity,message,count FROM alerts WHERE resolved=0 "
                "ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END LIMIT 5"
            ).fetchall()]
    except Exception as exc:
        out["error"] = _safe_msg(str(exc))[:120]
    try:
        out["locks"] = [l for l in lock_list() if not l.get("stale")]
    except Exception:
        pass
    return out


def workload_open_count():
    """Sadece acik (bekleyen + yapilan) toplam is sayisi."""
    try:
        with db() as c:
            n = c.execute("SELECT COUNT(*) FROM agent_tasks "
                          "WHERE status IN ('BEKLIYOR','YAPILIYOR')").fetchone()[0]
            d = c.execute("SELECT COUNT(*) FROM dev_jobs "
                          "WHERE status IN ('pending','running','started')").fetchone()[0]
            return int(n) + int(d)
    except Exception:
        return None


# ---- Niyet tespiti: "ne kadar is", "gorev listesi", "su an ne yapiyor" ... ----
_ACT_PAT = re.compile(
    r"(evren|ekip|ajanlar|herkes|sistem|ofis|asistan)\b.{0,30}"
    r"(ne\s*yap|neyle|ne\s*ile|me[sş]gul|u[gğ]ra[sş]|[cç]al[ıi][sş])"
    r"|(şu\s*an|su\s*an|simdi|şimdi)\s*(ne\s*yap|ne\s*oluyor|durum)"
    r"|ne\s*yap[ıi]yor"
    r"|(neler|ne)\s*oluyor"
    r"|canl[ıi]\s*durum"
    r"|aktif\s*(i[sş]|g[oö]rev)"
    r"|kim\s*(ne\s*yap|çal[ıi][sş]|calis|me[sş]gul|u[gğ]ra[sş])", re.I)

_QUEUE_PAT = re.compile(
    r"(g[oö]rev|i[sş]|kuyruk|is)\b.{0,25}"
    r"(liste|kuyruk|kald|durum|y[uü]k|say[ıi]|aded|toplam|neler|var\s*m[ıi])"
    r"|(ne\s*kadar).{0,20}(i[sş]|g[oö]rev|kuyruk|y[uü]k)"
    r"|(kalan|a[cç][ıi]k|bekleyen|s[ıi]rada|siradaki).{0,20}(i[sş]|g[oö]rev|kuyruk)"
    r"|i[sş]\s*y[uü]k"
    r"|g[oö]rev\s*defteri"
    r"|yap[ıi]lacaklar"
    r"|(i[sş]ler|g[oö]revler)(i|in)?\b.{0,20}(ne|neler|nas[ıi]l|durum|var)", re.I)


def workload_intent(text):
    """'activity' (su an ne yapiyor) | 'queue' (is yuku/liste) | None."""
    low = (text or "").lower()
    # F3: açık denetim komutu ayrı yeteneğe gider; durum sorusu modele (gerçek veri).
    if _AUDIT_CMD_RX.search(low):
        return None
    if _ACT_PAT.search(low) or _STATUS_Q_RX.search(low):
        return "activity"
    if _QUEUE_PAT.search(low):
        return "queue"
    return None


def activity_snapshot():
    """Sistemin SU AN ne yaptigi: ajan durumlari, son olaylar, calisan dev isleri."""
    out = {"agents": [], "events": [], "dev_running": [], "open_tasks": []}
    try:
        with db() as c:
            for r in c.execute("SELECT id,state,updated_at FROM agent_state ORDER BY id"):
                out["agents"].append({"id": r["id"], "state": r["state"] or "idle",
                                      "updated_at": r["updated_at"]})
            for r in c.execute(
                    "SELECT ts,event_type,source_agent,target_agent,status,summary "
                    "FROM agent_events ORDER BY id DESC LIMIT 8"):
                out["events"].append({"ts": r["ts"], "type": r["event_type"],
                                      "src": r["source_agent"], "tgt": r["target_agent"],
                                      "status": r["status"],
                                      "summary": _safe_msg(r["summary"] or "")[:100]})
            try:
                for r in c.execute(
                        "SELECT id,message,worker,started_at FROM dev_jobs "
                        "WHERE status IN ('running','started') ORDER BY id DESC LIMIT 5"):
                    out["dev_running"].append({"id": r["id"],
                                               "msg": _safe_msg(r["message"] or "")[:80],
                                               "worker": r["worker"],
                                               "started_at": r["started_at"]})
            except Exception:
                pass
            out["open_tasks"] = [{"id": r["id"], "title": r["title"] or "-",
                                  "owner": r["owner"] or "-"}
                                 for r in c.execute(
                "SELECT id,title,owner FROM agent_tasks "
                "WHERE status='YAPILIYOR' ORDER BY id").fetchall()]
    except Exception as exc:
        out["error"] = _safe_msg(str(exc))[:120]
    return out


def ayla_workload_report(mode="queue"):
    """'Ne kadar is kaldi?' / 'su an ne yapiyor?' -> gercek rapor (LLM'siz)."""
    if mode == "activity":
        return ayla_activity_report()
    w = workload_snapshot()
    if w.get("error"):
        return (f"\U0001F935 <b>Ayla</b>\n\nKuyruk okunamadi: "
                f"<code>{w['error']}</code>")
    open_rows = w["open"]
    dev = w["dev_pending"] + w["dev_running"]
    total = len(open_rows) + dev
    lines = ["\U0001F935 <b>Ayla — canli is durumu</b>", ""]
    if total == 0:
        lines.append("Evren'in <b>bekleyen isi yok</b>; kuyruk temiz. "
                     "Yeni is verdiginizde burada gorunur.")
    else:
        lines.append(f"Evren'in toplam <b>{total}</b> acik isi var.")
    by = w["by_status"]
    if by:
        bits = [f"{k}: {by[k]}" for k in ("BEKLIYOR", "YAPILIYOR", "BITTI",
                                          "BASARISIZ", "IPTAL") if k in by]
        if bits:
            lines.append("Kayitlar — " + ", ".join(bits))
    if dev:
        lines.append(f"Gelistirme kuyrugu: <b>{dev}</b> "
                     f"(bekleyen {w['dev_pending']}, calisan {w['dev_running']})")
    if open_rows:
        lines.append("")
        lines.append("<b>Acik isler</b>")
        icon = {"YAPILIYOR": "\U0001F527", "BEKLIYOR": "\u23F3"}
        for t in open_rows[:12]:
            lines.append(f"{icon.get(t['status'], '\u2022')} <b>#{t['id']}</b> "
                         f"{_safe_msg(t['title'])[:70]} — <i>{t['owner']}</i>")
        if len(open_rows) > 12:
            lines.append(f"... ve {len(open_rows) - 12} is daha")
    if w["alerts"]:
        lines.append("")
        lines.append("<b>Acik alarm</b>")
        for a in w["alerts"]:
            lines.append(f"\u26A0 {a['severity']}: {_safe_msg(a['message'])[:100]}")
    if w["locks"]:
        lines.append("")
        lines.append("<b>Kilitli alanlar</b>: " +
                     ", ".join(f"{l['area']} ({l['owner']})" for l in w["locks"][:5]))
    return "\n".join(lines)


def ayla_activity_report():
    """'Evren su an ne yapiyor?' -> canli aktivite raporu (deterministik)."""
    a = activity_snapshot()
    w = workload_snapshot()
    if a.get("error"):
        return (f"\U0001F935 <b>Ayla</b>\n\nAktivite okunamadi: "
                f"<code>{a['error']}</code>")
    lines = ["\U0001F935 <b>Ayla — su an ne oluyor</b>", ""]
    # 1) calisan isler
    if a["open_tasks"]:
        lines.append("<b>Su an uzerinde calisilan</b>")
        for t in a["open_tasks"][:6]:
            lines.append(f"\U0001F527 <b>#{t['id']}</b> {_safe_msg(t['title'])[:70]} "
                         f"— <i>{t['owner']}</i>")
    else:
        lines.append("Su an calisan bir gorev yok.")
    # 2) dev isleri
    if a["dev_running"]:
        lines.append("")
        lines.append("<b>Gelistirme kuyrugu (calisan)</b>")
        for d in a["dev_running"][:4]:
            lines.append(f"\U0001F6E0 #{d['id']} {d['msg']} "
                         f"({d.get('worker') or '-'})")
    # 3) ajan durumlari
    if a["agents"]:
        busy = [x for x in a["agents"] if x["state"] not in ("idle", "off", "offline")]
        lines.append("")
        if busy:
            lines.append("<b>Mesgul ajanlar</b>: " +
                         ", ".join(f"{x['id']}={x['state']}" for x in busy[:8]))
        else:
            lines.append("<b>Tum ajanlar bosta</b> (yeni is bekliyor).")
    # 4) son olaylar
    if a["events"]:
        lines.append("")
        lines.append("<b>Son hareketler</b>")
        for e in a["events"][:6]:
            arrow = f"{e['src']} -> {e['tgt']}" if e.get("tgt") else e.get("src")
            lines.append(f"\u2022 {str(e['ts'])[11:16]} {arrow} "
                         f"({e['status']}) {e['summary'][:55]}")
    # 5) kuyruk ozeti (kisa)
    dev = w["dev_pending"] + w["dev_running"]
    total = len(w["open"]) + dev
    lines.append("")
    if total:
        lines.append(f"Bekleyen is: <b>{total}</b>. Detay icin: "
                     f"'evrenin gorev listesi'")
    else:
        lines.append("Bekleyen is yok; kuyruk temiz.")
    if w["alerts"]:
        lines.append("")
        lines.append("<b>Acik alarm</b>: " +
                     "; ".join(f"{x['severity']}" for x in w["alerts"][:3]))
    return "\n".join(lines)


def ayla_alert_summary():
    """Kritik alarm varsa kisa ozet (yoksa bos)."""
    try:
        with db() as c:
            rows = c.execute("SELECT severity,message,count FROM alerts "
                             "WHERE resolved=0 ORDER BY CASE severity WHEN 'critical' "
                             "THEN 0 ELSE 1 END LIMIT 3").fetchall()
        if not rows:
            return ""
        out = ["⚠️ <b>Açık alarm</b>"]
        for r in rows:
            out.append(f"• [{r['severity']}] {_safe_msg(r['message'])[:120]} (x{r['count']})")
        return "\n".join(out)
    except Exception:
        return ""


# ------------------------------------------------------------------ Tier 0
NOISE_PATTERNS = [
    r"noreply", r"no-reply", r"do-not-reply", r"newsletter", r"bulten", r"b\xfclten",
    r"bildirim", r"notification", r"promosyon", r"kampanya", r"indirim",
    r"black\s*friday", r"cyber\s*monday", r"f[ıi]rsat", r"f[ıi]rsatlar",
    r"ho[şs]\s*geldin", r"welcome\s*to", r"hesab[ıi]n[ıi]z\s*(olu[şs]turuldu|haz[ıi]r|a[çc][ıi]ld[ıi])",
    r"unsubscribe", r"abonelik", r"e-?b[üu]lten", r"kargo", r"teslimat",
    r"%\s*\d+\s*indirim", r"\d+\s*tl\s*(indirim|hediye)", r"ücretsiz\s*tesli",
    r"^\s*re:\s*$",
]
NOISE_DOMAINS = ["github.com", "trendyol.com", "hepsiburada.com", "n11.com",
                 "youtube.com", "linkedin.com", "twitter.com", "x.com",
                 "em.nightcafe.studio", "nightcafe.studio", "yemeksepeti.com",
                 "shopify.com", "openrouter.ai", "runpod.io", "getir.com",
                 "instagram.com", "facebookmail.com", "mail.google.com",
                 "aliexpress.com", "amazon.com", "temu.com"]

def tier0_noise(sender: str, subject: str) -> bool:
    s = (sender or "").lower()
    sub = (subject or "").lower()
    for d in NOISE_DOMAINS:
        if d in s:
            return True
    for p in NOISE_PATTERNS:
        if re.search(p, s, re.I) or re.search(p, sub, re.I):
            return True
    return False

SENSITIVE = [
    (re.compile(r"\b\d{11}\b"), "[TCKN]"),
    (re.compile(r"\bTR\d{2}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{2}\b", re.I), "[IBAN]"),
    (re.compile(r"\b(?:\d[ -]*?){13,16}\b"), "[KART]"),
]

def mask(text: str) -> str:
    for rx, rep in SENSITIVE:
        text = rx.sub(rep, text)
    return text

# ------------------------------------------------------------------ IMAP
def imap_fetch(since_days: int = 2, limit: int = 25) -> list:
    if not GM_USER or not GM_PASS:
        log("warn", "imap", "Gmail bilgileri eksik, tarama atlandi")
        return []
    set_agent("postaci", "busy")
    out = []
    try:
        m = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        m.login(GM_USER, GM_PASS)
        m.select("INBOX", readonly=False)
        typ, ids = m.search(None, "UNSEEN")
        id_list = ids[0].split() if ids and ids[0] else []
        if not id_list:
            # Yeni okunmamis yok. Ilk kurulumda (DB bos) son maillerden ornek al.
            with db() as c:
                have = c.execute("SELECT COUNT(*) FROM mails").fetchone()[0]
            if have == 0:
                typ, ids = m.search(None, "ALL")
                all_ids = ids[0].split() if ids and ids[0] else []
                id_list = all_ids[-min(limit, 15):]
            else:
                m.logout()
                set_agent("postaci", "idle")
                return []
        id_list = id_list[-limit:]

        def dh(v):
            try:
                return str(make_header(decode_header(v or "")))
            except Exception:
                return v or ""

        for i in id_list:
            # PEEK: maili OKUNDU olarak isaretlemez
            typ, msg_data = m.fetch(i, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        try:
                            body = part.get_payload(decode=True).decode(
                                part.get_content_charset() or "utf-8", "ignore")
                        except Exception:
                            body = ""
                        break
            else:
                try:
                    body = msg.get_payload(decode=True).decode(
                        msg.get_content_charset() or "utf-8", "ignore")
                except Exception:
                    body = ""
            out.append({
                "id": msg.get("Message-ID", f"uid-{i.decode()}"),
                "uid": i.decode(),
                "sender": dh(msg.get("From", "")),
                "subject": dh(msg.get("Subject", "")),
                "date": dh(msg.get("Date", "")),
                "body": (body or "")[:3000],
            })
        m.logout()
        if out:
            log("info", "imap", f"{len(out)} yeni mail alindi")
    except Exception as exc:
        log("error", "imap", f"IMAP hatasi: {exc}")
    finally:
        set_agent("postaci", "idle")
    return out

# ------------------------------------------------------------------ isleme
SYS_CLASSIFY = ("Bir e-postayi siniflandir. SADECE su uc kelimeden birini yaz: "
                "urgent, normal, fyi. Baska hicbir sey yazma.")

URGENT_HINTS = [
    r"overdue", r"payment\s*(risk|due|failed)", r"\bfatura\b.*(gecik|son\s*ödem|son\s*odem)",
    r"acil", r"urgent", r"son\s*uyar[ıi]", r"hesab[ıi]n[ıi]z.*(kapat[ıi]l|askıya|dondurul)",
    r"iade\s*talebi", r"şikayet", r"sozlesme|sözleşme.*(iptal|fesih)",
    r"randevu.*(onay|iptal)", r"son\s*ödeme\s*tarihi",
]

def urgent_hint(subject: str, body: str) -> bool:
    t = (subject or "") + "\n" + (body or "")[:800]
    for p in URGENT_HINTS:
        if re.search(p, t, re.I):
            return True
    return False

CRITICAL_PATTERNS = [
    r"g[üu]venlik\s*uyar", r"security\s*(alert|warning|breach)",
    r"depolama\s*alan[ıi].*(dolu|doldu|azal)", r"storage\s*(is\s*)?(full|almost)",
    r"[şs]ifre.*(de[ğg]i[şs]|s[ıi]f[ıi]rla)", r"password.*(changed|reset)",
    r"yetkisiz\s*(giri[şs]|eri[şs]im)", r"unauthori[sz]ed\s*(access|login)",
    r"hesab[ıi]n[ıi]z.*(kapat[ıi]l|ask[ıi]ya|dondurul|silin)", r"account.*(suspend|disabled|closed)",
    r"do[ğg]rulama\s*kodu", r"verification\s*code", r"one[- ]time\s*(password|code)",
    r"e-?fatura", r"vergi\s*bor", r"icra", r"mahkeme",
]

def critical_content(subject: str, body: str) -> bool:
    t = (subject or "") + "\n" + (body or "")[:1000]
    for p in CRITICAL_PATTERNS:
        if re.search(p, t, re.I):
            return True
    return False


# ------------------------------------------------------------------ Ayla: etiketleme
AYLA_LABELS = {
    "Bulten": [r"b[uü]lten", r"newsletter", r"haftal[ıi]k", r"kampanya",
               r"promosyon", r"indirim", r"f[ıi]rsat", r"black\s*friday"],
    "Bildirim": [r"do[ğg]rulama", r"verification", r"g[uü]venlik\s*kodu",
                 r"login\s*code", r"\botp\b", r"one[- ]time"],
    "Sosyal": [r"facebook", r"instagram", r"linkedin", r"twitter",
               r"tiktok", r"pinterest"],
    "Alisveris": [r"trendyol", r"hepsiburada", r"\bn11\b", r"amazon",
                  r"aliexpress", r"temu", r"getir", r"yemeksepeti"],
    "Oyun": [r"nightcafe", r"kick\.com", r"steam", r"epicgames"],
}

PERSONAL_PROVIDERS = {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com",
                      "icloud.com", "ttmail.com", "superonline.com", "mynet.com"}

GENERIC_LOCAL = ("info", "destek", "support", "noreply", "no-reply",
                 "billing", "sales", "notifications", "bulten", "newsletter",
                 "mailer", "hello", "team", "welcome")

def ayla_is_personal(sender: str) -> bool:
    """Gercek kisi/kurum mu? (Ayla bunlara DOKUNMAZ)"""
    s = (sender or "").lower()
    m = re.search(r"([a-z0-9._%+-]+)@([a-z0-9.-]+\.[a-z]{2,})", s)
    if not m:
        return False
    local, domain = m.group(1), m.group(2)
    if any(k in local for k in GENERIC_LOCAL):
        return False
    return True

def ayla_category(sender: str, subject: str):
    t = f"{sender} {subject}".lower()
    for label, pats in AYLA_LABELS.items():
        for pat in pats:
            if re.search(pat, t, re.I):
                return label
    return None

def ayla_apply_label(item, label):
    """Gmail'e etiket ekler (IMAP COPY). Kisisel/bilinmeyen -> dokunmaz."""
    if not GM_USER or not GM_PASS or not label:
        return False
    try:
        import imaplib as _imaplib
        m = _imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        m.login(GM_USER, GM_PASS)
        try:
            m.create(f'"{label}"')
        except Exception:
            pass
        m.select("INBOX", readonly=False)
        mid = (item.get("id") or "").strip()
        ids = []
        if mid.startswith("<") and mid.endswith(">"):
            typ, data = m.search(None, f'(HEADER Message-ID "{mid}")')
            ids = data[0].split() if data and data[0] else []
        if ids:
            m.copy(ids[-1], f'"{label}"')
            ok = True
        else:
            ok = False
        m.logout()
        return ok
    except Exception as exc:
        log("warn", "ayla", f"etiket {label} basarisiz: {exc}")
        return False

MARKETING = [
    r"reklam", r"pazarlama", r"tan[ıi]t[ıi]m", r"kampanya", r"promosyon",
    r"indirim", r"f[ıi]rsat", r"b[uü]lten", r"newsletter",
    r"black\s*friday", r"cyber\s*monday", r"f[ıi]r[ıi]ldak",
    r"yeni\s*[uü]r[uü]n", r"hediye", r"kazan", r"sipari[şs]",
    r"%\s*\d+\s*indirim", r"\d+\s*tl\s*(indirim|hediye)",
    r"efsane\s*cuma", r"sepette", r"ka[çc][ıi]rma", r"sana\s*[öo]zel",
]

MARKETING_DOMAINS = [
    "hepsiburada.com", "trendyol.com", "n11.com", "amazon.", "aliexpress",
    "temu.com", "getir.com", "yemeksepeti.com", "ciceksepeti.com", "vitaminler.com",
    "biletinial.com", "kariyer.net", "nightcafe", "krea.ai", "etsy.com",
    "booking.com", "udemy.com", "coursera.org", "mailchimp", "sendgrid",
    "hubspot", "klaviyo", "campaign-archive",
]

# Pazarlama olmayan, DOKUNULMAYACAK servis/guvenlik gondericileri
TRANSACTIONAL_DOMAINS = [
    "google.com", "accounts.google", "cloudflare.com", "oracle.com",
    "oraclecloud.com", "github.com", "openrouter.ai", "alibabacloud.com",
    "microsoft.com", "apple.com", "amazonaws.com", "vercel.com",
    "supabase.io", "openai.com", "anthropic.com",
]

# Kritik/islemsel konu kaliplari -> asla etiketleme
PROTECTED_SUBJECT = [
    r"do[ğg]rulama", r"verification", r"g[uü]venlik", r"security",
    r"login", r"sign[- ]?in", r"[şs]ifre", r"password", r"alert", r"uyar[ıi]",
    r"fatura", r"invoice", r"payment", r"[öo]deme", r"approved", r"onay",
    r"suspended", r"ask[ıi]ya", r"limit", r"kota", r"quota", r"report",
    r"rapor", r"stopped", r"released", r"[sü]resi\s*dol", r"expired",
    r"abonelik", r"subscription", r"hesap", r"account", r"api\s*key",
    r"deploy", r"build", r"error", r"hata", r"backup", r"yedek",
]

GENERIC_MARKETING_LOCAL = [
    "bulten", "bulletin", "newsletter", "merhaba", "hello", "tobi",
    "kampanya", "pazarlama", "marketing", "promo", "teklif", "offer",
    "duyuru", "bultenler", "firsat", "indirimler",
]


def _is_marketing(sender: str, subject: str) -> bool:
    """Reklam/pazarlama mi? Guvenlik/islemsel ve kisisel -> False."""
    f = (sender or "").lower()
    subj = (subject or "").lower()

    m = re.search(r"([a-z0-9._%+-]+)@([a-z0-9.-]+\.[a-z]{2,})", f)
    local, domain = (m.group(1), m.group(2)) if m else ("", "")

    # 1) Islem/guvenlik gondericisi -> DOKUNMA
    if any(td in domain for td in TRANSACTIONAL_DOMAINS):
        return False

    # 2) Kritik konu -> DOKUNMA
    for pp in PROTECTED_SUBJECT:
        if re.search(pp, subj, re.I):
            return False

    # 3) Kisisel saglayici + kisisel local -> DOKUNMA
    if domain in ("gmail.com", "outlook.com", "hotmail.com", "yahoo.com",
                  "icloud.com", "ttmail.com", "superonline.com", "mynet.com"):
        if not any(k in local for k in GENERIC_MARKETING_LOCAL):
            return False

    # 4) Pazarlama domaini -> ETIKETLE
    if any(d in domain for d in MARKETING_DOMAINS):
        return True

    # 5) Pazarlama local adi -> ETIKETLE
    if any(k in local for k in GENERIC_MARKETING_LOCAL):
        return True

    # 6) Pazarlama konu/icerik kelimesi -> ETIKETLE
    txt = f"{sender} {subject}"
    if any(re.search(p2, txt, re.I) for p2 in MARKETING):
        return True

    return False


def ayla_label_marketing(label: str = "Gereksiz", limit: int = 200) -> dict:
    """
    Ayla: gelen kutusundaki reklam/pazarlama/tanitim maillerini
    Gmail'de 'Gereksiz' etiketiyle isaretler. Kisisele DOKUNMAZ.
    """
    if not GM_USER or not GM_PASS:
        return {"ok": False, "labeled": 0, "error": "Gmail bilgisi yok"}

    labeled = 0
    skipped = 0
    neutral = 0
    err = ""
    try:
        import imaplib as _im
        import email as _em
        from email.header import decode_header as _dh, make_header as _mh
        m = _im.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        m.login(GM_USER, GM_PASS)
        try:
            m.create(f'"{label}"')
        except Exception:
            pass
        m.select("INBOX", readonly=False)
        typ, data = m.search(None, "ALL")
        ids = data[0].split() if data and data[0] else []
        ids = ids[-limit:]
        for i in reversed(ids):
            try:
                t, hd = m.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
                if t != "OK" or not hd or not hd[0]:
                    continue
                msg = _em.message_from_bytes(hd[0][1])
                try:
                    frm = str(_mh(_dh(msg.get("From", ""))))
                    subj = str(_mh(_dh(msg.get("Subject", ""))))
                except Exception:
                    frm, subj = msg.get("From", ""), msg.get("Subject", "")
                if _is_marketing(frm, subj):
                    m.copy(i, f'"{label}"')
                    labeled += 1
                    continue
                if ayla_is_personal(frm):
                    skipped += 1
                else:
                    neutral += 1
            except Exception:
                continue
        m.logout()
        log("ok", "ayla", f"etiketleme: {labeled} -> '{label}', "
                          f"{skipped} kisisel atlandi, {neutral} notr")
    except Exception as exc:
        err = str(exc)
        log("error", "ayla", f"etiketleme hatasi: {exc}")
    return {"ok": not err, "labeled": labeled, "skipped_personal": skipped,
            "neutral": neutral, "error": err, "label": label}


def process_mail(item: dict):
    sender, subject, body = item["sender"], item["subject"], item["body"]
    # Korelasyon: mesaj kimliginin kisasi (PII tasimaz)
    _mid = hashlib.sha1((item.get("id") or "").encode("utf-8", "ignore")).hexdigest()[:10]
    mail_tid = f"mail-{_mid}"
    # 1) KRITIK icerik her seyden once (noreply olsa bile onemli)
    if critical_content(subject, body):
        set_agent("cirak", "busy")
        summary = ollama_chat(
            f"Bu maili tek cumleyle Turkce ozetle:\n\n{mask(subject)}\n{mask(body[:1200])}",
            max_tokens=80) or subject[:120]
        set_agent("cirak", "idle")
        _save(item, "urgent", summary, "", "kaydedildi", 0, 1)
        log("ok", "triage", f"kritik icerik: {subject[:50]}")
        return

    # 2) Ayla: kisisel/kurumsal mi? -> DOKUNMA, ama Telegram'a bildir
    if ayla_is_personal(sender):
        set_agent("filtre", "busy")
        summary = ollama_chat(
            f"Bu maili tek cumleyle Turkce ozetle:\n\n{mask(subject)}\n{mask(body[:1200])}",
            max_tokens=80) or subject[:120]
        set_agent("filtre", "idle")
        _save(item, "kisisel", summary, "", "bildirildi", 0, 1)
        short_from = re.sub(r"\s*<[^>]+>", "", sender)[:45]
        notify_user(f"📬 <b>Ayla</b>: Kişisel/kurumsal mail\n"
                    f"<b>{subject[:90]}</b>\n"
                    f"<i>{short_from}</i>\n\n{summary[:220]}")
        log("ok", "ayla", f"kisisel mail bildirildi: {subject[:50]}")
        return

    # 3) Tier 0 gurultu filtresi (+ Ayla etiketleme)
    if tier0_noise(sender, subject):
        cat = ayla_category(sender, subject) or "Gereksiz"
        ayla_apply_label(item, cat)
        _save(item, "noise", "", "", f"arsiv:{cat}", 0, 0)
        log("info", "ayla", f"gereksiz -> {cat}: {subject[:45]}")
        return

    set_agent("filtre", "busy")
    text = mask(f"{subject}\n{body[:1500]}")
    forced_urgent = urgent_hint(subject, body)
    set_agent("filtre", "idle")

    # Tier 1
    set_agent("cirak", "busy")
    label = ollama_chat(f"{SYS_CLASSIFY}\n\nKimden: {sender}\nKonu: {subject}\n\n{text}",
                        max_tokens=8).lower()
    set_agent("cirak", "idle")
    label = "urgent" if "urgent" in label else (
        "normal" if "normal" in label else ("fyi" if "fyi" in label else "normal"))
    if forced_urgent and label != "urgent":
        label = "urgent"
        log("info", "tier0", f"kritik kelime -> urgent: {subject[:40]}")

    agent_event("classify", "filtre", "cirak", "completed", mail_tid,
                f"siniflandirma: {label}")

    summary = ollama_chat(
        f"Bu maili tek cumleyle Turkce ozetle:\n\n{text}", max_tokens=80) or subject[:120]

    draft, tok = "", 0
    if label == "urgent" and active_model() == "evren":
        agent_event("draft", "cirak", "evren", "started", mail_tid,
                    "acil mail icin evren taslagi")
        draft, tok = evren_chat([
            {"role": "system", "content": "Kisa, profesyonel, Turkce bir e-posta yaniti yaz. Imza atma."},
            {"role": "user", "content": f"Kimden: {sender}\nKonu: {subject}\n\n{text}"},
        ], max_tokens=350)
        agent_event("draft", "evren", "cirak",
                    "completed" if draft else "error", mail_tid,
                    "taslak uretildi" if draft else "taslak uretilemedi")
    elif label == "urgent":
        # Cirak (Ollama) taslak yazsin
        draft = ollama_chat(
            f"Profesyonel, kisa bir Turkce e-posta yaniti yaz. Imza atma.\n"
            f"Kimden: {sender}\nKonu: {subject}\n\n{text}", max_tokens=300)

    status = "taslak hazir" if draft else "kaydedildi"
    _save(item, label, summary, draft, status, tok, 2 if draft else 1)
    log("ok" if label != "fyi" else "info", "triage",
        f"{label}: {subject[:50]}" + (f" (+{tok} tok)" if tok else ""))

def _save(item, label, summary, draft, status, tokens, tier):
    try:
        with db() as c:
            c.execute("""INSERT INTO mails(id,received_at,sender,subject,label,summary,draft,status,tokens_used,tier)
                         VALUES(?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET label=excluded.label, summary=excluded.summary,
                           draft=excluded.draft, status=excluded.status""",
                      (item["id"], datetime.now().isoformat(timespec="seconds"),
                       item["sender"], item["subject"], label, summary, draft, status, tokens, tier))
    except Exception as exc:
        log("error", "db", f"mail kaydi hatasi: {exc}")

def scan_once():
    items = imap_fetch()
    if not items:
        return 0
    new = 0
    with db() as c:
        known = {r[0] for r in c.execute("SELECT id FROM mails")}
    for it in items:
        if it["id"] in known:
            continue
        process_mail(it)
        new += 1
    if new:
        log("ok", "scan", f"{new} mail islendi")
    return new

def mail_loop():
    while True:
        try:
            scan_once()
            _hb("mail")
        except Exception as exc:
            log("error", "loop", f"tarama dongusu: {exc}")
        time.sleep(POLL_SEC)
BACKUP_DIR = ROOT.parent / "evren-backups"
BACKUP_STATE = ROOT / "data" / "backup_state.json"

def _load_backup_state():
    try:
        return json.loads(BACKUP_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_backup_state(st):
    try:
        BACKUP_STATE.write_text(json.dumps(st, indent=2), encoding="utf-8")
    except Exception:
        pass

def check_backup():
    """Cirak: yedekleri denetler, sorun varsa Telegram'a bildirir."""
    st = _load_backup_state()
    try:
        files = sorted(BACKUP_DIR.glob("evren-*.tar.gz.gpg"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            if not st.get("warned_missing"):
                notify_user("⚠️ <b>Cirak</b>: Hiç yedek dosyası yok!\n"
                            "<code>~/evren-backups</code> boş. Cron çalışmıyor olabilir.")
                st["warned_missing"] = True
                _save_backup_state(st)
            return

        newest = files[0]
        age_h = (time.time() - newest.stat().st_mtime) / 3600.0
        st["warned_missing"] = False

        # decrypt testi (bozuk arşiv tespiti)
        ok = True
        try:
            import subprocess
            r = subprocess.run(
                ["gpg", "--batch", "--quiet", "--passphrase-file",
                 str(Path.home() / ".evren-backup.pass"), "-d", str(newest)],
                capture_output=True, timeout=60)
            ok = r.returncode == 0 and len(r.stdout) > 1000
        except Exception:
            ok = False

        last_ok = st.get("last_ok_ts", 0)
        if ok and age_h < 48:
            # saglikli, gunluk teyit
            if time.time() - last_ok > 86400:
                size_mb = newest.stat().st_size / 1048576
                notify_user(
                    f"✅ <b>Cirak</b>: Yedek sağlıklı.\n"
                    f"Dosya: <code>{newest.name}</code>\n"
                    f"Boyut: {size_mb:.2f} MB\n"
                    f"Yaş: {age_h:.1f} saat\n"
                    f"Toplam arşiv: {len(files)}")
                st["last_ok_ts"] = time.time()
                _save_backup_state(st)
            return

        if not ok and not st.get("warned_corrupt_" + newest.name):
            notify_user(f"❌ <b>Cirak</b>: Yedek bozuk/şifresi açılmıyor!\n"
                        f"<code>{newest.name}</code>")
            st["warned_corrupt_" + newest.name] = True
            _save_backup_state(st)
        elif age_h >= 48 and not st.get("warned_stale"):
            notify_user(f"⚠️ <b>Cirak</b>: Yedek {age_h:.0f} saatten eski.\n"
                        f"Son: <code>{newest.name}</code>\nCron kontrol et.")
            st["warned_stale"] = True
            _save_backup_state(st)
    except Exception as exc:
        log("error", "backup-check", str(exc))

# ------------------------------------------------------------------ Ayla: 2 saatlik ozet
AYLA_DIGEST_SEC = int(CFG.get("AYLA_DIGEST_SECONDS", str(2 * 3600)) or 2 * 3600)

def ayla_digest_once(hours: int = 2):
    """Son N saatteki mail ozeti -> Telegram (the owner)."""
    try:
        with db() as c:
            rows = c.execute(
                "SELECT label, COUNT(*) n FROM mails "
                "WHERE received_at >= datetime('now', ?) GROUP BY label",
                (f"-{hours} hours",)).fetchall()
        if not rows:
            notify_user(f"📬 <b>Ayla</b>: Son {hours} saatte yeni mail yok.")
            return
        tot = sum(r["n"] for r in rows)
        cnt = {r["label"]: r["n"] for r in rows}
        lines = [f"📬 <b>Ayla — Son {hours} saat</b>",
                 f"Toplam <b>{tot}</b> mail"]
        emoji = {"urgent": "🔴", "kisisel": "👤", "normal": "📄",
                 "noise": "🗑", "fyi": "ℹ️"}
        for k in ("urgent", "kisisel", "normal", "noise", "fyi"):
            if k in cnt:
                lines.append(f"{emoji.get(k,'•')} {k}: {cnt[k]}")
        # onemli maillerin basliklari
        with db() as c:
            top = c.execute(
                "SELECT subject FROM mails WHERE label IN ('urgent','kisisel') "
                "AND received_at >= datetime('now', ?) "
                "ORDER BY received_at DESC LIMIT 5",
                (f"-{hours} hours",)).fetchall()
        if top:
            lines.append("\n<b>Dikkat:</b>")
            for r in top:
                lines.append(f"• {r['subject'][:70]}")
        notify_user("\n".join(lines))
        log("ok", "ayla", f"{hours} saatlik ozet gonderildi ({tot} mail)")
    except Exception as exc:
        log("error", "ayla", f"ozet hatasi: {exc}")

def ayla_digest_loop():
    """2 saatte bir ozet (ilk ozet 30 dk sonra)."""
    time.sleep(1800)
    while True:
        try:
            ayla_digest_once(2)
        except Exception as exc:
            log("error", "ayla", f"ozet dongusu: {exc}")
        time.sleep(AYLA_DIGEST_SEC)


SPAM_RULE_SEC = int(CFG.get("SPAM_RULE_SECONDS", str(3 * 3600)) or 3 * 3600)
SPAM_STATE = ROOT / "data" / "spam_state.json"

def _load_spam_state():
    try:
        return json.loads(SPAM_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_spam_state(st):
    try:
        SPAM_STATE.write_text(json.dumps(st, indent=2, ensure_ascii=False),
                              encoding="utf-8")
    except Exception:
        pass

def spam_rule_once(notify=True):
    """'Gereksizler' klasorunu garanti eder, spam mailleri oraya tasir."""
    a = _actions_mod
    st = _load_spam_state()
    try:
        ens = a.ensure_folder(CFG, "Gereksizler")
        if not ens.get("ok"):
            log("warn", "spam", "klasor: " + str(ens.get("error"))[:80])
            return
        if ens.get("created"):
            log("ok", "spam", "Gereksizler klasoru olusturuldu")
        r = a.move_spam(CFG, "Gereksizler", days=45)
        if not r.get("ok"):
            log("warn", "spam", "tarama: " + str(r.get("error"))[:80])
            return
        moved = int(r.get("moved") or 0)
        st["last_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        st["last_moved"] = moved
        st["total_moved"] = int(st.get("total_moved") or 0) + moved
        _save_spam_state(st)
        if moved:
            log("ok", "spam", f"{moved} spam mail Gereksizler'e tasindi")
            if notify:
                lines = [f"\U0001F5D1 <b>Ayla</b>: <b>{moved}</b> reklam/kumar maili "
                         f"<b>Gereksizler</b> klasorune tasindi."]
                for ex in (r.get("examples") or [])[:4]:
                    lines.append(f"• {ex['subject'][:60]}\n  <i>{ex['from'][:50]}</i>")
                notify_user("\n".join(lines))
    except Exception as exc:
        log("error", "spam", "kural: " + _safe_msg(str(exc))[:100])

def spam_rule_loop():
    """Acilista 90 sn sonra bir kez, sonra 3 saatte bir (reklam/kumar filtresi)."""
    time.sleep(90)
    while True:
        try:
            spam_rule_once()
            _hb("spam")
        except Exception as exc:
            log("error", "spam", "dongu: " + _safe_msg(str(exc))[:100])
        time.sleep(SPAM_RULE_SEC)

def spam_status():
    """Telegram/panel icin ozet."""
    a = _actions_mod
    st = _load_spam_state()
    try:
        n = a.folder_count(CFG, "Gereksizler")
    except Exception:
        n = None
    return {"folder_count": n, "last_ts": st.get("last_ts"),
            "last_moved": st.get("last_moved", 0),
            "total_moved": st.get("total_moved", 0)}


def backup_loop():
    """6 saatte bir yedek denetimi (Cirak gorevi)."""
    time.sleep(120)
    while True:
        check_backup()
        time.sleep(6 * 3600)

# ------------------------------------------------------------------ HTTP API
# ------------------------------------------------------------------ OPS API (read-only)
# Yeni Operasyon Merkezi paneli (/operations) icin salt-okunur ozet uclar.
# GIZLILIK: prompt, mail govdesi, secret ASLA donmez.
OPS_VERSION = "ops-1.0.0"

_OPS_AGENTS = [
    # (id, ad, rol, state-key, tip) — tip: insan | llm | kod (gercek yetenek)
    ("sefler",  "Şef",          "Şef Ajan · koordinatör",        "sefler",  "insan"),
    ("filtre",  "Ayla",         "Filtreci · mail triyajı",       "filtre",  "llm"),
    ("cirak",   "Çırak",        "Yerel Model · Ollama",          "cirak",   "llm"),
    ("apiman",  "Apiman",       "API & Kota Sorumlusu",          "apiman",  "kod"),
    ("evren",   "EVREN",        "Bulut Beyin · bulut model",     "evren",   "llm"),
    ("postaci", "Kurye",        "Postacı · SMTP",                "postaci", "kod"),
    ("arsiv",   "Arşivci",      "Arşiv · temizlik",              "arsiv",   "kod"),
]

_BUSY_TTL_SEC = int(CFG.get("AGENT_BUSY_TTL_SECONDS", "900") or 900)

_OPS_ERR_HINT = {
    "imap": "filtre", "ayla": "filtre", "filtre": "filtre", "tier0": "filtre",
    "tier1": "cirak", "cirak": "cirak", "tier2": "evren", "escalate": "evren",
    "evren-call": "evren",
    "cleanup": "arsiv", "arsiv": "arsiv", "backup-check": "cirak",
    "telegram": "sefler", "api": "sefler", "panel": "sefler",
    "quota": "apiman", "tier2-quota": "apiman", "mail-send": "postaci",
}


def _hb(name):
    """Dongu canlilik isareti (settings)."""
    try:
        with db() as c:
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      ("hb_" + name, str(int(time.time()))))
    except Exception:
        pass


def _hb_get(name):
    try:
        with db() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", ("hb_" + name,)).fetchone()
            return int(row["value"]) if row and row["value"] else 0
    except Exception:
        return 0


def _ops_agent_states():
    with db() as c:
        return {r["id"]: (r["state"], r["updated_at"]) for r in
                c.execute("SELECT id,state,updated_at FROM agent_state")}


def _ops_last_activity():
    out = {}
    try:
        with db() as c:
            for aid, *_ in _OPS_AGENTS:
                r = c.execute(
                    "SELECT ts FROM agent_events WHERE source_agent=? OR target_agent=? "
                    "ORDER BY id DESC LIMIT 1", (aid, aid)).fetchone()
                if r and r["ts"]:
                    out[aid] = r["ts"]
    except Exception:
        pass
    return out


def _ops_events_today():
    try:
        with db() as c:
            return c.execute("SELECT COUNT(*) FROM agent_events "
                             "WHERE date(ts)=date('now','localtime')").fetchone()[0]
    except Exception:
        return 0


def _ops_agents_payload():
    states = _ops_agent_states()
    last = _ops_last_activity()
    evren_proc = _opencode_process_alive()   # E2: gercek opencode prosesi
    out = []
    for aid, name, role, key, atype in _OPS_AGENTS:
        st, upd = states.get(key, ("idle", None))
        # bayat busy temizligi: cokme sonrasi "busy" takili kalmasin
        if st in ("busy", "waiting") and upd:
            try:
                import calendar as _cal
                up = time.strptime(str(upd)[:19], "%Y-%m-%d %H:%M:%S")
                age = time.time() - _cal.timegm(up)   # updated_at UTC yazilir
                if age > _BUSY_TTL_SEC:
                    st = "idle"
                    upd = f"{upd} (bayat)"
            except Exception:
                pass
        model = None
        if aid == "evren":
            model = EV_MODEL
        elif aid == "cirak":
            model = OL_MODEL
        elif aid == "apiman":
            model = "deterministik (LLM değil)"
        # E2: gercek opencode prosesi calisiyorsa EVREN "calisiyor" (busy) gorunur;
        # DB'deki evren_chat durumu tek basina yanilticiydi.
        evren_working = False
        if aid == "evren" and evren_proc and st != "error":
            st = "busy"
            evren_working = True
        out.append({
            "id": aid, "name": name, "role": role, "type": atype, "state": st,
            "state_at": upd,
            "last_activity": last.get(aid),
            "model": model,
            "process": ("opencode" if evren_working else None),
            "consumer": ("evren" if aid == "evren" else None),
        })
    return out


def _ops_agent_detail(aid):
    aid = _norm_agent((aid or "").strip().lower())
    row = next((x for x in _OPS_AGENTS if x[0] == aid), None)
    if not row:
        return None
    base = next((a for a in _ops_agents_payload() if a["id"] == aid), {})
    try:
        with db() as c:
            ev_total = c.execute("SELECT COUNT(*) FROM agent_events "
                                 "WHERE source_agent=? OR target_agent=?", (aid, aid)).fetchone()[0]
            ev_today = c.execute("SELECT COUNT(*) FROM agent_events "
                                 "WHERE (source_agent=? OR target_agent=?) "
                                 "AND date(ts)=date('now','localtime')", (aid, aid)).fetchone()[0]
            ev_done = c.execute("SELECT COUNT(*) FROM agent_events "
                                "WHERE (source_agent=? OR target_agent=?) AND status IN "
                                "('completed','transferred','info')", (aid, aid)).fetchone()[0]
            ev_err = c.execute("SELECT COUNT(*) FROM agent_events "
                               "WHERE (source_agent=? OR target_agent=?) AND status='error'",
                               (aid, aid)).fetchone()[0]
            comps = [k for k, v in _OPS_ERR_HINT.items() if v == aid]
            errs = 0
            if comps:
                q = "SELECT COALESCE(SUM(count),0) FROM errors WHERE resolved=0 AND component IN (%s)" % \
                    ",".join("?" * len(comps))
                errs = c.execute(q, comps).fetchone()[0]
            evs = c.execute("SELECT id,ts,event_type,source_agent,target_agent,status,summary "
                            "FROM agent_events WHERE source_agent=? OR target_agent=? "
                            "ORDER BY id DESC LIMIT 20", (aid, aid)).fetchall()
    except Exception:
        ev_total = ev_today = ev_done = ev_err = errs = 0
        evs = []
    base.update({
        "events_total": ev_total, "events_today": ev_today,
        "events_done": ev_done, "events_error": ev_err, "errors_open": errs,
        "events": [{"id": r["id"], "ts": (r["ts"] or "")[11:19],
                    "event_type": r["event_type"] or "", "source": r["source_agent"] or "",
                    "target": r["target_agent"] or "", "status": r["status"] or "",
                    "summary": (r["summary"] or "")[:120]} for r in evs],
    })
    return base


def _ops_services():
    """Gerçekten ölçülebilen servisler. Ölçülemeyen -> 'unknown' (uydurma yok)."""
    now = time.time()
    out = []

    # Backend (bu yanit verildigine gore calisiyor)
    out.append({"id": "backend", "name": "EVREN Backend", "state": "online"})

    # Veritabani
    try:
        with db() as c:
            c.execute("SELECT 1").fetchone()
        out.append({"id": "db", "name": "Veritabanı (SQLite)", "state": "online"})
    except Exception:
        out.append({"id": "db", "name": "Veritabanı (SQLite)", "state": "error"})

    # Ollama (yerel) - gercek HTTP probu
    ol_state = "unknown"
    try:
        http_get_json(f"{OL_BASE}/api/tags", {}, timeout=3)
        ol_state = "online"
    except Exception:
        ol_state = "offline"
    out.append({"id": "ollama", "name": "Ollama (Çırak)", "state": ol_state})

    # Telegram bot
    out.append({"id": "telegram", "name": "Telegram Bot",
                "state": "online" if globals().get("_TG_BOT") else "unknown"})

    # Mail dongusu (heartbeat)
    hb = _hb_get("mail")
    if hb:
        age = now - hb
        st = "online" if age <= POLL_SEC * 2 + 60 else "degraded"
        out.append({"id": "mail", "name": "Mail Döngüsü", "state": st,
                    "last_ok_sec": int(age)})
    else:
        out.append({"id": "mail", "name": "Mail Döngüsü", "state": "unknown"})

    # Apiman / key havuzu
    try:
        pool = quota_all()["pool"]
        st = "online" if pool.get("healthy", 0) > 0 else "error"
        out.append({"id": "apiman", "name": "Apiman / Key Havuzu", "state": st,
                    "healthy": pool.get("healthy", 0), "count": pool.get("count", 0)})
    except Exception:
        out.append({"id": "apiman", "name": "Apiman / Key Havuzu", "state": "unknown"})

    # Cloudflare Worker: sunucu kendini worker uzerinden olcemez -> unknown (durust)
    out.append({"id": "worker", "name": "Cloudflare Worker", "state": "unknown"})
    return out


# ==================================================================
# T21 — TUM SERVIS/ENTEGRASYON + LIMIT TAKIBI
# - Sadece sayi/durum doner; token/parola/mail/icerik ASLA response'a girmez.
# - Veri yoksa state=unknown + "bilinmiyor" (uydurma yok). Cache 60 sn.
# ==================================================================
_SERVICES_CACHE = {"at": 0.0, "data": None}
_SERVICES_LOCK = threading.Lock()
_LIMIT_DEGRADE = {}


def _num(x, nd=2):
    try:
        n = float(x)
    except Exception:
        return x
    if abs(n - int(n)) < 1e-9:
        return int(n)
    return round(n, nd)


def _svc_entry(sid, name, kind, state, usage=None, limit=None, pct=None,
               note="", unit="", checked_at=None, probe="ok", extra=None):
    """Tek servis karti. pct>=90 SARI, >=95 KIRMIZI (durum da guncellenir)."""
    try:
        if pct is None and usage is not None and limit not in (None, 0):
            pct = (float(usage) / float(limit)) * 100.0
    except Exception:
        pct = None
    if pct is not None:
        pct = round(float(pct), 1)
        if pct >= LIMIT_CRIT_PCT:
            state = "error"
        elif pct >= LIMIT_WARN_PCT and state == "online":
            state = "degraded"
    base = (note or "").strip()
    if usage is not None and limit not in (None, 0):
        core = f"{_num(usage)}/{_num(limit)}"
        if unit:
            core += f" {unit}"
        base = (base + " · " if base else "") + core
    if pct is not None:
        if pct >= LIMIT_CRIT_PCT:
            base += f" — %{pct:.0f} KRİTİK"
        elif pct >= LIMIT_WARN_PCT:
            base += f" — %{pct:.0f} DİKKAT"
    if not base:
        base = "bilinmiyor"
    d = {
        "id": sid, "name": name, "kind": kind, "state": state,
        "usage": _num(usage) if usage is not None else None,
        "limit": _num(limit) if limit is not None else None,
        "pct": pct, "note": base[:300],
        "checked_at": checked_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "unit": unit, "probe": probe,
    }
    if extra:
        d.update(extra)
    return d


def _cf_analytics():
    """Cloudflare GraphQL: KV/R2/Workers AI gercek gunluk kullanim (best-effort)."""
    tok = (CFG.get("CLOUDFLARE_API_TOKEN") or "").strip()
    acct = (CFG.get("CF_ACCOUNT_ID") or "").strip()
    if not tok or not acct:
        return None, "yapılandırılmadı"
    today = date.today().isoformat()
    start = (date.today() - timedelta(days=1)).isoformat()
    q = (
        "query($a:String!,$s:String!){viewer{accounts(filter:{accountTag:$a}){"
        "kvOperationsAdaptiveGroups(limit:200,filter:{date_geq:$s})"
        "{dimensions{date actionType} sum{requests}}"
        "aiInferenceAdaptiveGroups(limit:200,filter:{date_geq:$s})"
        "{dimensions{date} sum{totalNeurons}}"
        "r2StorageAdaptiveGroups(limit:20,filter:{date_geq:$s})"
        "{dimensions{date} max{payloadSize objectCount}}"
        "}}}"
    )
    try:
        r = http_post_json(
            "https://api.cloudflare.com/client/v4/graphql",
            {"query": q, "variables": {"a": acct, "s": start}},
            {"Authorization": "Bearer " + tok}, timeout=12)
    except Exception:
        return None, "Cloudflare API erişilemedi"
    if not r or not r.get("data"):
        return None, "Cloudflare API yanıtı okunamadı"
    try:
        a = ((r["data"].get("viewer") or {}).get("accounts") or [])[0]
    except Exception:
        return None, "Cloudflare hesabı okunamadı"
    def pick_date(rows):
        dates = [((x.get("dimensions") or {}).get("date")) for x in rows]
        dates = [d for d in dates if d]
        if not dates:
            return None
        return today if today in dates else max(dates)
    kv = a.get("kvOperationsAdaptiveGroups") or []
    kd = pick_date(kv)
    kv_write = sum(int((x.get("sum") or {}).get("requests") or 0) for x in kv
                   if (x.get("dimensions") or {}).get("date") == kd
                   and (x.get("dimensions") or {}).get("actionType") in ("write", "delete"))
    kv_read = sum(int((x.get("sum") or {}).get("requests") or 0) for x in kv
                  if (x.get("dimensions") or {}).get("date") == kd
                  and (x.get("dimensions") or {}).get("actionType") in ("read", "list"))
    ai = a.get("aiInferenceAdaptiveGroups") or []
    ad = pick_date(ai)
    neurons = sum(float((x.get("sum") or {}).get("totalNeurons") or 0) for x in ai
                  if (x.get("dimensions") or {}).get("date") == ad)
    r2 = a.get("r2StorageAdaptiveGroups") or []
    rd = pick_date(r2)
    r2_bytes = 0
    r2_obj = 0
    for x in r2:
        if (x.get("dimensions") or {}).get("date") == rd:
            mx = x.get("max") or {}
            r2_bytes += int(mx.get("payloadSize") or 0)
            r2_obj += int(mx.get("objectCount") or 0)
    return {"kv_write": kv_write, "kv_read": kv_read,
            "wai_neurons": round(neurons, 3),
            "r2_gb": round(r2_bytes / 1e9, 3), "r2_objects": r2_obj,
            "date": kd or ad or rd}, ""


def _imap_probe():
    if not GM_USER or not GM_PASS:
        return "unknown", "IMAP kimlik bilgisi yok"
    try:
        m = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=8)
        try:
            m.login(GM_USER, GM_PASS)
            m.select("INBOX", readonly=True)
        finally:
            try:
                m.logout()
            except Exception:
                pass
        return "online", ""
    except Exception:
        return "offline", "IMAP bağlantısı kurulamadı"


def _telegram_probe():
    tok = (CFG.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not tok:
        return "unknown", "bot token yok", ""
    try:
        d = http_get_json(f"https://api.telegram.org/bot{tok}/getMe", {}, timeout=8)
        if d and d.get("ok"):
            return "online", "", (d.get("result") or {}).get("username") or ""
        return "offline", "getMe başarısız", ""
    except Exception:
        return "offline", "Telegram API erişilemedi", ""


def _provider_probe(pid):
    """Provider durum probu. Donus: (state, note, model). Sir donmez."""
    if pid == "gemini":
        key = (PROV_CFG.get("GEMINI_API_KEY") or "").strip()
        base = (PROV_CFG.get("GEMINI_BASE_URL") or "").strip().rstrip("/")
        model = PROV_CFG.get("GEMINI_MODEL") or ""
        if not key or not base:
            return "unknown", "yapılandırılmadı", model
        try:
            http_get_json(base + "/models", {"Authorization": "Bearer " + key,
                                             "User-Agent": _PROVIDER_UA}, timeout=10)
            return "online", "", model
        except Exception:
            return "offline", "Gemini erişilemedi", model
    if pid == "groq":
        key = (PROV_CFG.get("GROQ_API_KEY") or "").strip()
        base = (PROV_CFG.get("GROQ_BASE_URL") or "").strip().rstrip("/")
        model = PROV_CFG.get("GROQ_MODEL") or ""
        if not key or not base:
            return "unknown", "yapılandırılmadı", model
        try:
            http_get_json(base + "/models", {"Authorization": "Bearer " + key,
                                             "User-Agent": _PROVIDER_UA}, timeout=10)
            return "online", "", model
        except Exception:
            return "offline", "Groq erişilemedi", model
    return "unknown", "bilinmiyor", ""


def _n8n_state():
    try:
        port = int(CFG.get("N8N_PORT", "5678") or 5678)
    except Exception:
        port = 5678
    import socket
    try:
        with socket.create_connection(("localhost", port), timeout=2):
            return "online", f"port {port} açık"
    except Exception:
        return "disabled", "devre dışı (çalışmıyor)"




def _build_services_full():
    services = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1) Cekirdek + Oracle compute (yerel)
    services.append(_svc_entry("backend", "EVREN Backend", "core", "online",
                               note="bu yanıt verildi", checked_at=now))
    try:
        with db() as c:
            c.execute("SELECT 1").fetchone()
        services.append(_svc_entry("db", "Veritabanı (SQLite)", "core", "online",
                                   note="sorgu başarılı", checked_at=now))
    except Exception:
        services.append(_svc_entry("db", "Veritabanı (SQLite)", "core", "error",
                                   note="sorgu başarısız", checked_at=now, probe="fail"))

    load1 = None
    try:
        with open("/proc/loadavg") as f:
            load1 = float(f.read().split()[0])
    except Exception:
        pass
    if load1 is None:
        services.append(_svc_entry("oracle-cpu", "Oracle CPU (OCPU)", "compute", "unknown",
                                   note="load okunamadı", checked_at=now, probe="unknown"))
    else:
        services.append(_svc_entry("oracle-cpu", "Oracle CPU (OCPU)", "compute", "online",
                                   usage=round(load1, 2), limit=LIMIT_ORACLE_OCPU,
                                   unit="OCPU", note="load1", checked_at=now))
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                mem[k] = int(v.strip().split()[0])
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", 0)
        used_gb = (total - avail) / 1024 / 1024
        services.append(_svc_entry("oracle-mem", "Oracle RAM", "compute", "online",
                                   usage=round(used_gb, 1), limit=LIMIT_ORACLE_MEM,
                                   unit="GB", note="kullanılan", checked_at=now))
    except Exception:
        services.append(_svc_entry("oracle-mem", "Oracle RAM", "compute", "unknown",
                                   note="meminfo okunamadı", checked_at=now, probe="unknown"))
    try:
        import shutil as _sh
        u = _sh.disk_usage("/")
        services.append(_svc_entry("oracle-disk", "Oracle Disk", "compute", "online",
                                   usage=round(u.used / 1e9, 1), limit=LIMIT_DISK_GB,
                                   unit="GB", note="df /", checked_at=now))
    except Exception:
        services.append(_svc_entry("oracle-disk", "Oracle Disk", "compute", "unknown",
                                   note="disk okunamadı", checked_at=now, probe="unknown"))

    # 2) Cloudflare (API'den gercek; basarisizsa unknown)
    cf, cf_note = _cf_analytics()
    if cf is None:
        for sid, nm in (("cf-kv-write", "Cloudflare KV yazma"),
                        ("cf-r2", "Cloudflare R2"),
                        ("llm-cloudflare", "Cloudflare Workers AI")):
            services.append(_svc_entry(sid, nm, "cloudflare", "unknown",
                                       note=cf_note or "bilinmiyor", checked_at=now, probe="unknown"))
    else:
        services.append(_svc_entry(
            "cf-kv-write", "Cloudflare KV yazma", "cloudflare", "online",
            usage=cf["kv_write"], limit=LIMIT_KV_WRITE, unit="yazma",
            note=f"KV okuma {cf['kv_read']}", checked_at=now,
            extra={"date": cf["date"]}))
        services.append(_svc_entry(
            "cf-r2", "Cloudflare R2", "cloudflare", "online",
            usage=cf["r2_gb"], limit=LIMIT_R2_GB, unit="GB",
            note=f"{cf['r2_objects']} nesne", checked_at=now,
            extra={"date": cf["date"]}))
        services.append(_svc_entry(
            "llm-cloudflare", "Cloudflare Workers AI", "llm", "online",
            usage=cf["wai_neurons"], limit=LIMIT_WAI_NEURON, unit="neuron",
            note="sağlayıcı havuzu", checked_at=now, extra={"date": cf["date"]}))

    # 3) LLM havuzu / saglayicilar
    try:
        qa = quota_all()
        pool = qa.get("pool") or {}
        lim = float(pool.get("total_limit") or 0)
        rem = float(pool.get("total_remaining") or 0)
        if lim > 0:
            used = max(0.0, lim - rem)
            st = "online" if (pool.get("healthy") or 0) > 0 else "error"
            services.append(_svc_entry(
                "llm-evren", "EVREN LLM havuzu", "llm", st,
                usage=round(used), limit=round(lim), unit="token",
                note=f"kalan {round(rem)} · {pool.get('healthy', 0)}/{pool.get('count', 0)} hat",
                checked_at=now, probe=("ok" if st == "online" else "fail")))
        else:
            services.append(_svc_entry("llm-evren", "EVREN LLM havuzu", "llm", "unknown",
                                       note="kota okunamadı", checked_at=now, probe="unknown"))
    except Exception:
        services.append(_svc_entry("llm-evren", "EVREN LLM havuzu", "llm", "unknown",
                                   note="kota okunamadı", checked_at=now, probe="unknown"))

    _pstat = {x["id"]: x for x in provider_status()}
    gst, gnote, gmodel = _provider_probe("gemini")
    _gs = _pstat.get("gemini") or {}
    services.append(_svc_entry("llm-gemini", "Gemini", "llm", gst,
                               usage=_gs.get("used"), limit=_gs.get("limit"),
                               unit="istek/gun",
                               note=(gnote or f"model {gmodel} · "
                                     f"{_gs.get('used', 0)}/{_gs.get('limit', '-')} istek"),
                               checked_at=now, probe=("ok" if gst == "online" else
                                                      ("unknown" if gst == "unknown" else "fail"))))
    rst, rnote, rmodel = _provider_probe("groq")
    _rs = _pstat.get("groq") or {}
    services.append(_svc_entry("llm-groq", "Groq", "llm", rst,
                               usage=_rs.get("used"), limit=_rs.get("limit"),
                               unit="istek/gun",
                               note=(rnote or f"model {rmodel} · "
                                     f"{_rs.get('used', 0)}/{_rs.get('limit', '-')} istek"),
                               checked_at=now, probe=("ok" if rst == "online" else
                                                      ("unknown" if rst == "unknown" else "fail"))))

    # Ollama (yerel) + model/RAM notu
    ol_state = "unknown"
    ol_loaded = ""
    try:
        http_get_json(f"{OL_BASE}/api/tags", {}, timeout=4)
        ol_state = "online"
        try:
            ps = http_get_json(f"{OL_BASE}/api/ps", {}, timeout=4)
            models = ps.get("models") or []
            if models:
                sz = sum(float(m.get("size") or 0) for m in models) / 1e9
                ol_loaded = f" · yüklü {round(sz, 1)} GB"
        except Exception:
            pass
    except Exception:
        ol_state = "offline"
    services.append(_svc_entry(
        "llm-ollama", "Ollama (yerel)", "llm", ol_state,
        note=f"model {OL_MODEL}{ol_loaded} · kota yok", checked_at=now,
        probe=("ok" if ol_state == "online" else "fail")))

    # 4) Telegram
    tg_state, tg_note, tg_user = _telegram_probe()
    try:
        with db() as c:
            tg_today = c.execute(
                "SELECT COUNT(*) FROM tg_messages WHERE direction='out' "
                "AND date(ts)=date('now','localtime')").fetchone()[0]
    except Exception:
        tg_today = None
    services.append(_svc_entry(
        "telegram", "Telegram Bot", "messaging", tg_state,
        note=(f"@{tg_user} · " if tg_user else "") +
             (tg_note or (f"bugün {tg_today} mesaj" if tg_today is not None
                          else "günlük sayı bilinmiyor")),
        checked_at=now, probe=("ok" if tg_state == "online" else
                               ("unknown" if tg_state == "unknown" else "fail")),
        extra={"daily_messages": tg_today}))

    # 5) Gmail IMAP + SMTP gunluk gonderim
    imap_state, imap_note = _imap_probe()
    try:
        smtp_n = int(_actions_mod.smtp_sent_today(CFG))
    except Exception:
        smtp_n = None
    last_scan = None
    try:
        with db() as c:
            row = c.execute("SELECT MAX(received_at) ts FROM mails").fetchone()
        last_scan = row["ts"] if row else None
    except Exception:
        pass
    note = imap_note or ("IMAP bağlı" + (f" · son tarama {last_scan}" if last_scan else ""))
    services.append(_svc_entry(
        "mail-gmail", "Gmail IMAP / SMTP", "mail", imap_state,
        usage=smtp_n, limit=LIMIT_GMAIL_SEND, unit="gönderim",
        note=note, checked_at=now,
        probe=("ok" if imap_state == "online" else
               ("unknown" if imap_state == "unknown" else "fail")),
        extra={"last_scan": last_scan}))

    # 6) n8n (kapali ise devre disi, hata degil)
    n8n_state, n8n_note = _n8n_state()
    services.append(_svc_entry("n8n", "n8n", "automation", n8n_state,
                               note=n8n_note, checked_at=now,
                               probe=("ok" if n8n_state == "online" else "unknown")))

    # 8) varsa ses/sherpa
    try:
        import shutil as _sh2
        if _sh2.which("sherpa"):
            services.append(_svc_entry("local-voice", "Sherpa / Ses", "local", "online",
                                       note="komut bulundu", checked_at=now))
    except Exception:
        pass

    # Genel skor = en kotu servis (disabled notr, unknown sari)
    rank = {"error": 4, "offline": 4, "degraded": 3, "unknown": 2, "online": 1, "disabled": 0}
    worst = max(services, key=lambda x: rank.get(x["state"], 2), default=None)
    ws = worst["state"] if worst else "unknown"
    if ws in ("error", "offline"):
        score, label = "RED", "KIRMIZI"
    elif ws in ("degraded", "unknown"):
        score, label = "YELLOW", "SARI"
    else:
        score, label = "GREEN", "YEŞİL"
    online_n = sum(1 for s in services if s["state"] == "online")
    unknown_n = sum(1 for s in services if s["state"] == "unknown")
    return {
        "generated_at": now,
        "overall": {"score": score, "label": label,
                    "worst": (worst["id"] if worst else None),
                    "online": online_n, "total": len(services),
                    "unknown": unknown_n},
        "thresholds": {"warn_pct": LIMIT_WARN_PCT, "crit_pct": LIMIT_CRIT_PCT,
                       "dedup_sec": LIMIT_ALERT_DEDUP_SEC},
        "services": services,
    }


def services_full(force=False):
    """T21 ortak veri: /ops/services-full + limit guard. 60 sn cache."""
    now = time.time()
    with _SERVICES_LOCK:
        d = _SERVICES_CACHE.get("data")
        if not force and d and (now - (_SERVICES_CACHE.get("at") or 0)) < 60:
            return d
        data = _build_services_full()
        _SERVICES_CACHE["at"] = now
        _SERVICES_CACHE["data"] = data
        return data


def evaluate_limit_thresholds(services, notify=True):
    """%90 SARI (tek/6sa), %95 KIRMIZI (tek/6sa) + degrade isareti. Sadece sayi/durum."""
    out = []
    for s in services or []:
        pct = s.get("pct")
        if pct is None:
            continue
        sid = s.get("id") or "?"
        name = s.get("name") or sid
        note = s.get("note") or ""
        if pct >= LIMIT_CRIT_PCT:
            level = "critical"
            _LIMIT_DEGRADE[sid] = True
            alert_resolve("limits", f"limit_{sid}_warn")
            alert("critical", "limits", f"limit_{sid}_critical",
                  f"{name} %{pct:.0f} limit riski", note,
                  dedup_sec=LIMIT_ALERT_DEDUP_SEC)
            if notify:
                _tg_dedup(f"limit_{sid}_critical",
                          f"🔴 <b>Limit riski</b> — {name}\n"
                          f"{_safe_msg(note)[:220]}\n"
                          f"<i>%95 eşiği · degrade/devre dışı değerlendiriliyor</i>",
                          window=LIMIT_ALERT_DEDUP_SEC)
        elif pct >= LIMIT_WARN_PCT:
            level = "warn"
            _LIMIT_DEGRADE.pop(sid, None)
            alert_resolve("limits", f"limit_{sid}_critical")
            alert("warn", "limits", f"limit_{sid}_warn",
                  f"{name} %{pct:.0f} uyarı eşiği", note,
                  dedup_sec=LIMIT_ALERT_DEDUP_SEC)
            if notify:
                _tg_dedup(f"limit_{sid}_warn",
                          f"🟡 <b>Limit uyarısı</b> — {name}\n"
                          f"{_safe_msg(note)[:220]}\n"
                          f"<i>%90 eşiği · 6 saat dedup</i>",
                          window=LIMIT_ALERT_DEDUP_SEC)
        else:
            level = "ok"
            _LIMIT_DEGRADE.pop(sid, None)
            alert_resolve("limits", f"limit_{sid}_critical")
            alert_resolve("limits", f"limit_{sid}_warn")
        out.append({"id": sid, "pct": pct, "level": level})
    return out


_DOWN_CRITICAL = {"backend", "db", "llm-evren", "telegram", "mail-gmail"}


def service_down_guard(services):
    """Servis down alarmlari (cozulunce DB'de kapanir; resolved Telegram'a gitmez)."""
    for s in services or []:
        sid = s.get("id")
        if sid not in _DOWN_CRITICAL:
            continue
        if s.get("probe") == "fail":
            alert("critical", "services", f"down_{sid}",
                  f"{s.get('name')} erişilemiyor",
                  s.get("note") or "", dedup_sec=1800)
        else:
            alert_resolve("services", f"down_{sid}")


def cf_kv_write_allowed():
    """KV yazma oncesi guard: limit asimi/riskinde False. Cagri yoksa future-safe."""
    try:
        d = services_full()
        s = next((x for x in d["services"] if x["id"] == "cf-kv-write"), None)
        if not s or s.get("pct") is None:
            return True
        return float(s["pct"]) < LIMIT_CRIT_PCT
    except Exception:
        return True


def llm_degrade_active():
    """Bulut LLM limiti kritikse True (cagri yolu yerel/ollama'ya dusebilir)."""
    return bool(_LIMIT_DEGRADE.get("llm-evren") or _LIMIT_DEGRADE.get("llm-cloudflare"))


def services_guard_loop():
    time.sleep(20)
    while True:
        try:
            d = services_full()
            evaluate_limit_thresholds(d.get("services"), notify=True)
            service_down_guard(d.get("services"))
        except Exception as exc:
            log("warn", "limits", f"guard turu: {_safe_msg(str(exc))[:100]}")
        time.sleep(60)


def _ops_charts():
    """Gerçek veriden grafik serileri. Veri yoksa bos dizi (uydurma yok)."""
    tok24 = {}
    ev24 = {}
    try:
        with db() as c:
            rows = c.execute(
                "SELECT ts,component,message FROM events WHERE ts >= datetime('now','-24 hours') "
                "AND component IN ('evren-call')").fetchall()
            for r in rows:
                hh = (r["ts"] or "")[11:13]
                if not hh:
                    continue
                m = re.search(r"ok \((\d+) tok", r["message"] or "")
                if not m:
                    continue
                tok24[hh] = tok24.get(hh, 0) + int(m.group(1))
            for r in c.execute("SELECT ts FROM agent_events "
                               "WHERE ts >= datetime('now','-24 hours')").fetchall():
                hh = (r["ts"] or "")[11:13]
                if hh:
                    ev24[hh] = ev24.get(hh, 0) + 1
    except Exception:
        pass
    hours = [f"{h:02d}" for h in range(24)]
    now_h = int(time.localtime().tm_hour)
    order = [(now_h - 23 + i) % 24 for i in range(24)]
    series_tok = [{"h": f"{h:02d}:00", "tok": tok24.get(f"{h:02d}", 0)} for h in order]
    series_ev = [{"h": f"{h:02d}:00", "n": ev24.get(f"{h:02d}", 0)} for h in order]

    cons = []
    try:
        with db() as c:
            rows = c.execute("SELECT day,consumer,used FROM token_usage_consumer "
                             "WHERE day >= date('now','-6 days') ORDER BY day").fetchall()
        cons = [{"day": r["day"], "consumer": r["consumer"] or "system",
                 "tokens": int(r["used"] or 0)} for r in rows]
    except Exception:
        pass

    errs = []
    try:
        with db() as c:
            rows = c.execute("SELECT date(ts) d, SUM(count) n FROM errors "
                             "WHERE ts >= datetime('now','-7 days') GROUP BY d ORDER BY d"
                             ).fetchall()
        errs = [{"day": r["d"], "n": int(r["n"] or 0)} for r in rows]
    except Exception:
        pass
    return {"token_24h": series_tok, "events_24h": series_ev,
            "consumer_7d": cons, "errors_7d": errs}


def _ops_version():
    def mt(p):
        try:
            return datetime.fromtimestamp((ROOT / "web" / p).stat().st_mtime
                                          ).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    return {"version": OPS_VERSION, "ops_html_mtime": mt("operations.html"),
            "index_html_mtime": mt("index.html"),
            "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "agent_uptime_sec": int(time.time() - _OPS_BOOT[0])}


_OPS_BOOT = [time.time()]
    # ---------------- OPS: API Key & Loglar ----------------
_OPS_CACHE = {}          # key -> (ts, data)
_OPS_CACHE_TTL = 60


def _ops_cached(key, fn):
    now = time.time()
    hit = _OPS_CACHE.get(key)
    if hit and now - hit[0] < _OPS_CACHE_TTL:
        return hit[1]
    data = fn()
    _OPS_CACHE[key] = (now, data)
    return data


_OPS_KEY_RX = re.compile(r"EVREN-(\d)")


def _ops_key_of(row):
    """Event satirindan anahtar etiketi (EVREN-1..3) cikar; yoksa None."""
    msg = row["message"] or ""
    m = _OPS_KEY_RX.search(msg)
    if m:
        return "EVREN-" + m.group(1)
    return None


def _ops_logs(level="", comp="", key="", agent="", hours=24, limit=200, since=0):
    """Gercek events/errors kayitlarindan guvenli log ozeti. Ham secret yok."""
    q = ("SELECT id,ts,level,component,message FROM events WHERE id>?")
    args = [int(since or 0)]
    if level:
        q += " AND level=?"; args.append(level)
    if comp:
        q += " AND component=?"; args.append(comp)
    if hours:
        q += " AND ts >= datetime('now',?)"; args.append(f"-{int(hours)} hours")
    q += " ORDER BY id DESC LIMIT ?"; args.append(max(1, min(int(limit or 200), 600)))
    rows = []
    with db() as c:
        for r in c.execute(q, args):
            k = _ops_key_of(r)
            if key and k != key:
                continue
            rows.append({
                "id": r["id"], "ts": r["ts"] or "", "time": (r["ts"] or "")[11:19],
                "level": r["level"] or "", "component": r["component"] or "",
                "message": _safe_msg(r["message"] or "")[:220], "key": k,
            })
    # ozet: anahtar bazli basari/hata
    per_key = {}
    for i, _k in enumerate(_KEYS):
        per_key["EVREN-" + str(i + 1)] = {"ok": 0, "err": 0}
    ok_n = err_n = 0
    for r in rows:
        good = r["level"] in ("ok", "info")
        if good:
            ok_n += 1
        elif r["level"] in ("warn", "error"):
            err_n += 1
        if r["key"] and r["key"] in per_key:
            per_key[r["key"]]["ok" if good else "err"] += 1
    with db() as c:
        comps = [r["component"] for r in c.execute(
            "SELECT DISTINCT component FROM events ORDER BY component")]
    return {"logs": rows, "summary": {"ok": ok_n, "err": err_n, "total": len(rows)},
            "per_key": per_key, "components": comps,
            "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}




def _ops_trend(series):
    """Gerçek hesaplanan eğilim. Tek nokta -> yeterli veri yok."""
    vals = [v for _, v in sorted(series.items()) if v is not None]
    if len(vals) < 2:
        return "yeterli veri yok"
    n = len(vals)
    mean_x = (n - 1) / 2.0
    mean_y = sum(vals) / n
    num = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(vals))
    den = sum((i - mean_x) ** 2 for i in range(n))
    if den == 0:
        return "dengeli"
    slope = num / den
    rng = max(vals) - min(vals)
    if rng == 0:
        return "dengeli"
    rel = slope / (rng if rng else 1)
    # düzensizlik: ardışık farkların işaret değişimi oranı
    diffs = [vals[i + 1] - vals[i] for i in range(n - 1)]
    flips = sum(1 for i in range(len(diffs) - 1) if diffs[i] * diffs[i + 1] < 0)
    if len(diffs) > 1 and flips / (len(diffs) - 1) > 0.6:
        return "düzensiz"
    if abs(rel) < 0.05:
        return "dengeli"
    return "yükseliyor" if slope > 0 else "azalıyor"


_NUTRI = {
    "sebze": ("sebze", "salata", "brokoli", "domates", "salatalık", "havuç", "ıspanak", "marul", "kabak"),
    "meyve": ("meyve", "elma", "muz", "portakal", "çilek", "kivi", "üzüm", "armut"),
    "protein": ("yumurta", "tavuk", "et", "balık", "döner", "köfte", "peynir", "yoğurt", "mercimek", "nohut", "badem", "ceviz"),
    "tahil": ("ekmek", "pilav", "makarna", "bulgur", "yulaf", "patates", "börek", "simit"),
    "islenmis": ("cips", "kola", "şeker", "çikolata", "bisküvi", "fast food", "hamburger", "sosis", "salam"),
    "icecek": ("kahve", "çay", "su", "smoothie", "meyve suyu", "ayran"),
    "alkol": ("bira", "şarap", "rakı", "viski", "votka", "alkol"),
}




def _ops_research(limit=20):
    """Panel için son araştırmalar: yalnız temiz sorgu + özet + kaynak URL'leri."""
    out = []
    try:
        with db() as c:
            rows = c.execute(
                "SELECT id,ts,agent,cap,query,sources,summary,status FROM research_log "
                "ORDER BY id DESC LIMIT ?", (max(1, min(int(limit or 20), 100)),)
            ).fetchall()
        for r in rows:
            try:
                srcs = json.loads(r["sources"] or "[]")
            except Exception:
                srcs = []
            out.append({"id": r["id"], "ts": r["ts"], "agent": r["agent"],
                        "cap": r["cap"], "query": r["query"],
                        "sources": srcs, "summary": r["summary"],
                        "status": r["status"]})
    except Exception as exc:
        return {"research": [], "error": _safe_msg(str(exc))[:120]}
    return {"research": out, "count": len(out)}


# --- CORS allowlist (G0.6): gercek panel origin'leriyle sinirla ---
_ALLOWED_ORIGINS = [o.strip() for o in
                    (CFG.get("PANEL_ALLOWED_ORIGINS") or "").split(",") if o.strip()]


def _cors_headers(origin: str):
    """Origin izinli ise echo et; degilse ACAO verme (fail-closed, ayni-origin bozulmaz)."""
    h = {
        "Access-Control-Allow-Headers": "Content-Type,X-Panel-Token",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Vary": "Origin",
    }
    if origin and origin in _ALLOWED_ORIGINS:
        h["Access-Control-Allow-Origin"] = origin
    return h


# --- Oturum tokeni (G0.4): PANEL_PASSWORD ile girisde suereli imzali token ---
PANEL_SESSION_TTL = int(CFG.get("PANEL_SESSION_TTL", "43200") or 43200)  # 12 saat


def _session_key():
    return hashlib.sha256(("sess:" + PANEL_TOKEN).encode("utf-8")).digest()


def make_session(ttl=None):
    """Suereli oturum tokeni: v1.<exp>.<hmac>. Master PANEL_TOKEN istemciye gitmez."""
    if not PANEL_TOKEN:
        return ""
    exp = int(time.time()) + int(ttl or PANEL_SESSION_TTL)
    msg = "v1.%d" % exp
    sig = hmac.new(_session_key(), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    return "%s.%s" % (msg, sig)


def valid_session(tok):
    try:
        parts = (tok or "").split(".")
        if len(parts) != 3 or parts[0] != "v1":
            return False
        exp = int(parts[1])
        if exp < int(time.time()):
            return False
        want = hmac.new(_session_key(), ("v1." + parts[1]).encode("utf-8"),
                        hashlib.sha256).hexdigest()
        return hmac.compare_digest(parts[2], want)
    except Exception:
        return False


def _token_ok(got, want):
    """Sabit zamanli token karsilastirma (G0.5). Bos deger asla eslesmez."""
    got = (got or "").strip()
    want = (want or "").strip()
    if not want or not got:
        return False
    try:
        return hmac.compare_digest(got, want)
    except Exception:
        return False






# Giris kaba kuvvet korumasi (IP basina). Bellekte; agent yeniden baslarsa sifirlanir.
_AUTH_LOCK = threading.Lock()
_AUTH_FAILS = {}          # ip -> [fail_count, first_ts, locked_until]
_AUTH_MAX_FAILS = 5
_AUTH_WINDOW = 600        # 10 dk
_AUTH_LOCK_SEC = 900      # 15 dk kilit
_AUTH_GLOBAL = {"n": 0, "since": 0.0, "until": 0.0}
_AUTH_GLOBAL_MAX = 20     # tum IP'lerden 10 dk'da azami hatali deneme

def _valid_ip(s):
    try:
        import ipaddress
        ipaddress.ip_address(s)
        return True
    except Exception:
        return False

def _auth_locked(ip):
    with _AUTH_LOCK:
        st = _AUTH_FAILS.get(ip)
        if not st:
            return 0
        now = time.time()
        if st[2] > now:
            return int(st[2] - now)
        return 0

def _auth_register_fail(ip):
    with _AUTH_LOCK:
        now = time.time()
        g = _AUTH_GLOBAL
        if now - g["since"] > _AUTH_WINDOW:
            g["n"] = 0; g["since"] = now
        g["n"] += 1
        if g["n"] >= _AUTH_GLOBAL_MAX:
            g["until"] = now + 300   # 5 dk tum girisler kapali
            g["n"] = 0; g["since"] = now
        st = _AUTH_FAILS.get(ip)
        if not st or now - st[1] > _AUTH_WINDOW:
            _AUTH_FAILS[ip] = [1, now, 0.0]
            return
        st[0] += 1
        if st[0] >= _AUTH_MAX_FAILS:
            st[2] = now + _AUTH_LOCK_SEC
            st[0] = 0
            st[1] = now

def _auth_global_left():
    with _AUTH_LOCK:
        left = _AUTH_GLOBAL["until"] - time.time()
        return int(left) if left > 0 else 0

def _auth_reset(ip):
    with _AUTH_LOCK:
        _AUTH_FAILS.pop(ip, None)

class Handler(BaseHTTPRequestHandler):
    def _sec_headers(self):
        """Tum yanitlara guvenlik basliklari (HTTPS tunel arkasinda)."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._sec_headers()
        for k, v in _cors_headers(self.headers.get("Origin") or "").items():
            self.send_header(k, v)
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _client_ip(self):
        # Servis yalniz loopback'e bind. Istekler guvenilir tunel/worker uzerinden
        # gelir. Oncelik: CF-Connecting-IP (Cloudflare tarafindan yazilir, istemci
        # sahteleyemez). Sahteleyebilecegi XFF'e guvenme (rate-limit bypass onlemi).
        peer = self.client_address[0]
        if peer in ("localhost", "::1"):
            v = (self.headers.get("CF-Connecting-IP") or "").strip()
            if v and _valid_ip(v):
                return v
        return peer

    def _authed(self):
        """Panel yetkilendirme. FAIL-CLOSED: PANEL_TOKEN yoksa erisim YOK.
        Kabul: master PANEL_TOKEN (sunucu-ici worker/CLI) VEYA gecerli suereli
        oturum tokeni (tarayici). Sabit zamanli karsilastirma (G0.1,G0.3,G0.4)."""
        if not PANEL_TOKEN:
            return False
        got = (self.headers.get("X-Panel-Token") or "").strip()
        if not got:
            return False
        try:
            if hmac.compare_digest(got, PANEL_TOKEN):
                return True
        except Exception:
            pass
        return valid_session(got)

    def _serve_static(self, name):
        """Panel'i SUNUCUDAN servis et. web/ disina cikilamaz."""
        safe = os.path.basename(name or "index.html")
        path = (ROOT / "web" / safe)
        if not path.exists() or path.suffix.lower() not in (".html", ".js", ".css",
                                                            ".png", ".svg", ".ico", ".webmanifest"):
            return self._send({"error": "not found"}, 404)
        data = path.read_bytes()
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
                 ".png": "image/png", ".ico": "image/x-icon",
                 ".webmanifest": "application/manifest+json"}.get(
                     path.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self._sec_headers()
        try:
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._sec_headers()
        for k, v in _cors_headers(self.headers.get("Origin") or "").items():
            self.send_header(k, v)
        try:
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, *a):
        pass

    # ---------------- GET
    def do_GET(self):
        p = self.path.split("?")[0].rstrip("/")
        # Panel + statik dosyalar (giris ekrani icin auth'suz erisilebilir)
        if p in ("", "/index.html"):
            return self._serve_static("index.html")
        if p in ("/operations", "/operations.html"):
            return self._serve_static("operations.html")
        if p.startswith("/static/"):
            return self._serve_static(p[len("/static/"):])
        if not self._authed():
            return self._send({"error": "yetkisiz", "auth": True}, 401)
        try:
            if p == "/panel-activity":
                # Gercek ajan hareketleri: son olaylar -> ajan + hedef
                out = []
                with db() as c:
                    rows = c.execute(
                        "SELECT ts,level,component,message FROM events "
                        "ORDER BY id DESC LIMIT 40").fetchall()
                map_comp = {
                    "tier0": "filtre", "filtre": "filtre", "triage": "filtre",
                    "ayla": "filtre", "imap": "postaci", "tier1": "cirak",
                    "cirak": "cirak", "tier2": "evren", "escalate": "evren",
                    "route": "sefler", "mail-send": "postaci",
                    "cleanup": "arsiv", "backup-check": "cirak",
                    "telegram": "sefler", "panel": "sefler",
                }
                for r in rows:
                    comp = r["component"]
                    aid = map_comp.get(comp)
                    if not aid:
                        continue
                    out.append({
                        "ts": (r["ts"] or "")[11:19],
                        "agent": aid,
                        "level": r["level"],
                        "msg": (r["message"] or "")[:80],
                    })
                return self._send(out)

            if p == "/panel-quota":
                return self._send(quota_all())

            if p == "/key-switch":
                d = {}
                try:
                    ln = int(self.headers.get("Content-Length") or 0)
                    if ln:
                        d = json.loads(self.rfile.read(ln).decode("utf-8") or "{}")
                except Exception:
                    d = {}
                target = d.get("index")
                if target is None:
                    ok = switch_key(None)
                else:
                    ok = switch_key(int(target))
                if not ok:
                    return self._send({"ok": False, "error": "degisim yapilamadi",
                                       "count": len(_KEYS)}, 400)
                return self._send({"ok": True, **quota_all()})

            if p == "/panel-stats":
                # Tanimlar (acik):
                # mails_today  = bugun (sunucu yerel tarihi) DB'ye kaydedilmis mail islemleri
                # evren_calls  = bugun basarili Evren chat cagrisi (evren-call event)
                # cirak_jobs   = bugun Cirak'a verilen is (cirak-call event)
                # errors_open  = cozulmemis hata kayitlari
                with db() as c:
                    mails_today = c.execute(
                        "SELECT COUNT(*) FROM mails WHERE date(received_at)=date('now','localtime')"
                    ).fetchone()[0]
                    evren_calls = c.execute(
                        "SELECT COUNT(*) FROM events WHERE component='evren-call' "
                        "AND date(ts)=date('now','localtime')").fetchone()[0]
                    cirak_jobs = c.execute(
                        "SELECT COUNT(*) FROM events WHERE component='cirak-call' "
                        "AND date(ts)=date('now','localtime')").fetchone()[0]
                    errors_open = c.execute(
                        "SELECT COUNT(*) FROM errors WHERE resolved=0").fetchone()[0]
                return self._send({
                    "mails_today": mails_today,
                    "evren_calls": evren_calls,
                    "cirak_jobs": cirak_jobs,
                    "errors_open": errors_open,
                })

            if p == "/panel-system":
                info = {}
                try:
                    with open("/proc/loadavg") as f:
                        la = f.read().split()
                    info["load1"] = float(la[0])
                    info["load5"] = float(la[1])
                    info["load15"] = float(la[2])
                except Exception:
                    pass
                try:
                    with open("/proc/uptime") as f:
                        info["uptime_sec"] = int(float(f.read().split()[0]))
                except Exception:
                    pass
                try:
                    with open("/proc/meminfo") as f:
                        mem = {}
                        for line in f:
                            k, v = line.split(":", 1)
                            mem[k] = int(v.strip().split()[0])
                    total = mem.get("MemTotal", 0)
                    avail = mem.get("MemAvailable", 0)
                    info["mem_total_mb"] = total // 1024
                    info["mem_used_mb"] = (total - avail) // 1024
                    info["mem_pct"] = round((total - avail) / total * 100, 1) if total else 0
                except Exception:
                    pass
                try:
                    import shutil as _sh
                    u = _sh.disk_usage("/")
                    info["disk_total_gb"] = round(u.total / 1e9, 1)
                    info["disk_used_gb"] = round(u.used / 1e9, 1)
                    info["disk_free_gb"] = round(u.free / 1e9, 1)
                    info["disk_pct"] = round(u.used / u.total * 100, 1)
                except Exception:
                    pass
                try:
                    import os as _os
                    cpus = _os.cpu_count() or 1
                    info["cpu_pct"] = round(info.get("load1", 0) / cpus * 100, 1)
                except Exception:
                    pass
                try:
                    import subprocess as _sp
                    out = _sp.run(["systemctl", "is-active", "evren-agent",
                                   "evren-tunnel", "evren-keepalive", "ollama"],
                                  capture_output=True, text=True, timeout=5).stdout.split()
                    info["services"] = dict(zip(
                        ["agent", "tunnel", "keepalive", "ollama"], out))
                except Exception:
                    pass
                try:
                    d = ROOT.parent / "evren-backups"
                    files = sorted(d.glob("evren-*.tar.gz.gpg"),
                                   key=lambda p: p.stat().st_mtime, reverse=True)
                    if files:
                        info["backup_last"] = datetime.fromtimestamp(
                            files[0].stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                        info["backup_age_h"] = round(
                            (time.time() - files[0].stat().st_mtime) / 3600, 1)
                        info["backup_count"] = len(files)
                except Exception:
                    pass
                # Oracle Always Free kotasi
                info["oracle_limits"] = {
                    "ocpu_alloc": 2, "ocpu_free": 2,
                    "ram_gb_alloc": 10, "ram_gb_free": 12,
                    "disk_gb_alloc": 100, "disk_gb_free": 200,
                    "cost": 0,
                }
                return self._send(info)

            if p == "/panel-tokens":
                q = evren_quota()
                qa = quota_all()
                used = tokens_used_today()
                # Gercek kalan = uc bagimsiz anahtarin toplami (tek anahtarin 15M'i toplam degildir)
                rem_total = qa["pool"]["total_remaining"]
                limit_total = qa["pool"]["total_limit"] or (EV_LIMIT * max(len(_KEYS), 1))
                with db() as c:
                    rows = c.execute("SELECT tier, SUM(tokens_used) t FROM mails "
                                     "WHERE tokens_used>0 GROUP BY tier").fetchall()
                    try:
                        cons = c.execute("SELECT consumer, SUM(used) u FROM token_usage_consumer "
                                         "GROUP BY consumer").fetchall()
                    except Exception:
                        cons = []
                by = [{"m": "Evren " + EV_MODEL, "tier": 2,
                       "tok": next((r["t"] for r in rows if r["tier"] == 2), 0)},
                      {"m": "Cirak " + OL_MODEL, "tier": 1, "tok": 0}]
                consumers = [{"name": r["consumer"] or "system", "used": int(r["u"] or 0)}
                             for r in cons]
                return self._send({
                    "used": int(used), "limit": int(limit_total),
                    "byModel": by, "keys": qa["keys"], "pool": qa["pool"],
                    "consumers": consumers,
                    "real": {
                        "daily_remaining": rem_total,
                        "daily_reset": q.get("daily_reset_at"),
                        "window_used": q.get("used_tokens"),
                        "window_cap": q.get("cap"),
                        "window_ratio": q.get("usage_ratio"),
                        "credits_remaining": q.get("remaining_cr"),
                        "concurrency_limit": q.get("concurrency_limit"),
                        "level": q.get("level"),
                    },
                })

            if p == "/panel-logs":
                with db() as c:
                    rows = c.execute("SELECT ts,level,component,message FROM events "
                                     "ORDER BY id DESC LIMIT 120").fetchall()
                out = []
                for r in rows:
                    out.append({"ts": r["ts"][11:19], "level": r["level"],
                                "comp": r["component"], "msg": r["message"]})
                return self._send(out)

            if p == "/panel-errors":
                with db() as c:
                    rows = c.execute("SELECT id,ts,component,message,count FROM errors "
                                     "WHERE resolved=0 ORDER BY id DESC LIMIT 50").fetchall()
                return self._send([{"id": r["id"], "ts": r["ts"][11:19],
                                    "comp": r["component"], "msg": r["message"],
                                    "count": r["count"]} for r in rows])

            if p == "/panel-escalations":
                with db() as c:
                    rows = c.execute("SELECT ts,task,cirak_raw,evren_reply,tokens "
                                     "FROM escalation ORDER BY id DESC LIMIT 40").fetchall()
                return self._send([{
                    "ts": (r["ts"] or "")[11:19],
                    "task": r["task"] or "",
                    "cirak": r["cirak_raw"] or "",
                    "evren": r["evren_reply"] or "",
                    "tokens": r["tokens"] or 0,
                } for r in rows])

            if p == "/panel-delegations":
                since = 0
                m = re.search(r"since=(\d+)", self.path)
                if m:
                    since = int(m.group(1))
                with db() as c:
                    rows = c.execute("SELECT id,target,label,source,ts FROM delegation "
                                     "WHERE id>? ORDER BY id DESC LIMIT 20", (since,)).fetchall()
                return self._send([{
                    "id": r["id"], "target": r["target"], "label": r["label"] or "",
                    "source": r["source"] or "", "ts": (r["ts"] or "")[11:19],
                } for r in rows])

            if p == "/panel-agent-events":
                since = 0
                m = re.search(r"since=(\d+)", self.path)
                if m:
                    since = int(m.group(1))
                with db() as c:
                    rows = c.execute(
                        "SELECT id,ts,event_type,source_agent,target_agent,task_id,"
                        "status,summary FROM agent_events WHERE id>? "
                        "ORDER BY id DESC LIMIT 100", (since,)).fetchall()
                return self._send([{
                    "id": r["id"], "ts": (r["ts"] or "")[11:19],
                    "event_type": r["event_type"] or "",
                    "source": r["source_agent"] or "",
                    "target": r["target_agent"] or "",
                    "task_id": r["task_id"] or "",
                    "status": r["status"] or "",
                    "summary": r["summary"] or "",
                } for r in rows])

            if p == "/panel-mails":
                with db() as c:
                    rows = c.execute("SELECT received_at,sender,subject,label,status FROM mails "
                                     "ORDER BY received_at DESC LIMIT 60").fetchall()
                return self._send([{"ts": (r["received_at"] or "")[11:16],
                                    "from": r["sender"][:60], "subj": r["subject"][:90],
                                    "tag": r["label"], "status": r["status"]} for r in rows])

            if p == "/panel-agents":
                states = {}
                with db() as c:
                    for r in c.execute("SELECT id,state FROM agent_state"):
                        states[r["id"]] = r["state"]
                cur = active_model()
                agents = [
                    {"id": "sefler", "name": "Sef", "state": states.get("sefler", "idle")},
                    {"id": "filtre", "name": "Ayla", "state": states.get("filtre", "idle")},
                    {"id": "cirak", "name": "Cirak",
                     "state": "off" if cur == "off" else states.get("cirak", "idle")},
                    {"id": "evren", "name": "Evren",
                     "state": "off" if cur in ("off", "cirak") else states.get("evren", "idle")},
                    {"id": "postaci", "name": "Kurye", "state": states.get("postaci", "idle")},
                    {"id": "arsiv", "name": "Arsivci", "state": states.get("arsiv", "idle")},
                    {"id": "apiman", "name": "Apiman", "state": "idle"},
                ]
                return self._send({"agents": agents, "active": cur})

            # ---------------- OPS (yeni /operations paneli, read-only) ----------------
            if p == "/ops/version":
                return self._send(_ops_version())
            if p == "/ops/summary":
                qa = quota_all()
                pool = qa["pool"]
                agents = _ops_agents_payload()
                busy = sum(1 for a in agents if a["state"] == "busy")
                waiting = sum(1 for a in agents if a["state"] in ("waiting",))
                offline = sum(1 for a in agents if a["state"] in ("off", "offline"))
                err_agents = sum(1 for a in agents if a["state"] == "error")
                with db() as c:
                    errs_open = c.execute("SELECT COUNT(*) FROM errors WHERE resolved=0").fetchone()[0]
                tot_rem = pool.get("total_remaining") or 0
                tot_lim = pool.get("total_limit") or 0
                return self._send({
                    "agents_total": len(agents), "agents_busy": busy,
                    "agents_waiting": waiting, "agents_offline": offline,
                    "agents_error": err_agents,
                    "errors_open": int(errs_open or 0),
                    "events_today": int(_ops_events_today()),
                    "pool_remaining": tot_rem, "pool_limit": tot_lim,
                    "pool_ratio": (round(tot_rem / tot_lim, 4) if tot_lim else None),
                    "pool_healthy": pool.get("healthy"), "pool_count": pool.get("count"),
                    "model": EV_MODEL, "active_model": active_model(),
                    "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
            if p == "/ops/agents":
                return self._send({"agents": _ops_agents_payload(),
                                   "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
            if p == "/ops/agent":
                m = re.search(r"id=([a-zA-Z_]+)", self.path)
                aid = m.group(1) if m else ""
                d = _ops_agent_detail(aid)
                if not d:
                    return self._send({"error": "ajan yok"}, 404)
                return self._send(d)
            if p == "/ops/keys":
                qa = quota_all()
                try:
                    with db() as c:
                        cons = [{"name": r["consumer"] or "system", "used": int(r["u"] or 0)}
                                for r in c.execute("SELECT consumer,SUM(used) u FROM "
                                                   "token_usage_consumer GROUP BY consumer")]
                except Exception:
                    cons = []
                return self._send({"keys": qa["keys"], "pool": qa["pool"],
                                   "active": qa.get("active"), "consumers": cons})
            if p == "/ops/events":
                since = 0
                m = re.search(r"since=(\d+)", self.path)
                if m:
                    since = int(m.group(1))
                typ = ""
                mt = re.search(r"type=([a-zA-Z_]+)", self.path)
                if mt:
                    typ = mt.group(1)
                stf = ""
                ms = re.search(r"status=([a-zA-Z_]+)", self.path)
                if ms:
                    stf = ms.group(1)
                hrs = 0
                mh = re.search(r"hours=(\d+)", self.path)
                if mh:
                    hrs = int(mh.group(1))
                lim = 200
                ml = re.search(r"limit=(\d+)", self.path)
                if ml:
                    lim = max(1, min(int(ml.group(1)), 500))
                q = ("SELECT id,ts,event_type,source_agent,target_agent,status,task_id,summary "
                     "FROM agent_events WHERE id>?")
                args = [since]
                if typ:
                    q += " AND event_type=?"; args.append(typ)
                if stf:
                    q += " AND status=?"; args.append(stf)
                if hrs:
                    q += " AND ts >= datetime('now',?)"; args.append(f"-{hrs} hours")
                q += " ORDER BY id DESC LIMIT ?"; args.append(lim)
                with db() as c:
                    rows = c.execute(q, args).fetchall()
                return self._send([{"id": r["id"], "ts": r["ts"] or "",
                                    "time": (r["ts"] or "")[11:19],
                                    "event_type": r["event_type"] or "",
                                    "source": r["source_agent"] or "",
                                    "target": r["target_agent"] or "",
                                    "status": r["status"] or "",
                                    "task_id": r["task_id"] or "",
                                    "summary": (r["summary"] or "")[:200]} for r in rows])
            if p == "/ops/errors":
                with db() as c:
                    rows = c.execute("SELECT id,ts,component,message,count,resolved FROM errors "
                                     "ORDER BY resolved ASC, id DESC LIMIT 100").fetchall()
                return self._send([{"id": r["id"], "ts": r["ts"] or "",
                                    "component": r["component"] or "",
                                    "message": _safe_msg(r["message"] or "")[:200],
                                    "count": int(r["count"] or 0),
                                    "resolved": bool(r["resolved"])} for r in rows])
            if p == "/ops/services":
                return self._send({"services": _ops_services(),
                                   "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
            if p == "/ops/services-full":
                return self._send(services_full())
            if p == "/ops/charts":
                return self._send(_ops_charts())
            if p == "/ops/logs":
                def g(name, dflt=""):
                    m = re.search(name + r"=([A-Za-z0-9_\-]+)", self.path)
                    return m.group(1) if m else dflt
                return self._send(_ops_logs(
                    level=g("level"), comp=g("comp"), key=g("key"),
                    hours=int(g("hours", "24") or 24),
                    limit=int(g("limit", "200") or 200),
                    since=int(g("since", "0") or 0)))
            if p == "/ops/research":
                return self._send(_ops_research(limit=25))
            if p == "/ops/tasks":
                m = re.search(r"status=([A-Za-z]+)", self.path)
                stf = m.group(1).upper() if m else None
                mo = re.search(r"owner=([a-z]+)", self.path)
                ow = mo.group(1) if mo else None
                return self._send({"tasks": task_list(status=stf, owner=ow)})
            if p == "/ops/locks":
                return self._send({"locks": lock_list()})
            if p == "/ops/alerts":
                try:
                    with db() as c:
                        rows = c.execute(
                            "SELECT id,first_ts,last_ts,severity,source,code,message,"
                            "detail,count,resolved FROM alerts "
                            "WHERE resolved=0 ORDER BY "
                            "CASE severity WHEN 'critical' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, "
                            "last_ts DESC LIMIT 50").fetchall()
                        recent = c.execute(
                            "SELECT COUNT(*) FROM alerts WHERE resolved=1 AND "
                            "resolved_ts >= datetime('now','-24 hours')").fetchone()[0]
                    return self._send({
                        "alerts": [{
                            "id": r["id"], "first_ts": r["first_ts"] or "",
                            "last_ts": r["last_ts"] or "",
                            "severity": r["severity"] or "warn",
                            "source": r["source"] or "", "code": r["code"] or "",
                            "message": r["message"] or "",
                            "detail": r["detail"] or "",
                            "count": int(r["count"] or 1),
                        } for r in rows],
                        "resolved_24h": int(recent or 0),
                        "critical": sum(1 for r in rows if r["severity"] == "critical"),
                        "warn": sum(1 for r in rows if r["severity"] == "warn"),
                        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    })
                except Exception as exc:
                    return self._send({"ok": False, "error": _safe_msg(str(exc))[:100]}, 500)
            if p == "/ops/changelog":
                try:
                    with db() as c:
                        rows = c.execute("SELECT ts,actor,area,summary FROM changelog "
                                         "ORDER BY id DESC LIMIT 40").fetchall()
                    return self._send([{"ts": r["ts"] or "", "actor": r["actor"] or "",
                                        "area": r["area"] or "", "summary": r["summary"] or ""}
                                       for r in rows])
                except Exception:
                    return self._send([])

            if p == "/ops/dashboard":
                qa = quota_all()
                agents = _ops_agents_payload()
                with db() as c:
                    errs_open = c.execute("SELECT COUNT(*) FROM errors WHERE resolved=0").fetchone()[0]
                evs = []
                with db() as c:
                    for r in c.execute("SELECT id,ts,event_type,source_agent,target_agent,status,"
                                       "task_id,summary FROM agent_events ORDER BY id DESC LIMIT 25"):
                        evs.append({"id": r["id"], "ts": r["ts"] or "",
                                    "time": (r["ts"] or "")[11:19],
                                    "event_type": r["event_type"] or "",
                                    "source": r["source_agent"] or "",
                                    "target": r["target_agent"] or "",
                                    "status": r["status"] or "",
                                    "task_id": r["task_id"] or "",
                                    "summary": (r["summary"] or "")[:150]})
                return self._send({
                    "summary": {"agents_total": len(agents),
                                "agents_busy": sum(1 for a in agents if a["state"] == "busy"),
                                "agents_offline": sum(1 for a in agents if a["state"] in ("off", "offline")),
                                "agents_error": sum(1 for a in agents if a["state"] == "error"),
                                "errors_open": int(errs_open or 0),
                                "events_today": int(_ops_events_today()),
                                "pool_remaining": qa["pool"].get("total_remaining"),
                                "pool_limit": qa["pool"].get("total_limit"),
                                "pool_healthy": qa["pool"].get("healthy"),
                                "pool_count": qa["pool"].get("count"),
                                "pool_ratio": (round((qa["pool"].get("total_remaining") or 0) /
                                                     (qa["pool"].get("total_limit") or 1), 4)
                                               if qa["pool"].get("total_limit") else None),
                                "model": EV_MODEL, "active_model": active_model(),
                                "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                    "agents": agents,
                    "services": _ops_services(),
                    "events": evs,
                })

            return self._send({"error": "not found"}, 404)
        except Exception as exc:
            log("error", "api", f"GET {p}: {exc}")
            return self._send({"error": str(exc)}, 500)

    # ---------------- POST
    def do_POST(self):
        p = self.path.split("?")[0].rstrip("/")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            data = {}

        try:
            if p == "/auth":
                # Sifre ekrani: PANEL_PASSWORD (varsa) yoksa PANEL_TOKEN ile dogrula.
                ip = self._client_ip()
                gleft = _auth_global_left()
                if gleft:
                    return self._send({"ok": False, "error": "cok fazla deneme",
                                       "retry": gleft}, 429)
                left = _auth_locked(ip)
                if left:
                    return self._send({"ok": False, "error": "cok fazla deneme",
                                       "retry": left}, 429)
                pw = (data.get("password") or "").strip()
                want = (CFG.get("PANEL_PASSWORD") or PANEL_TOKEN or "").strip()
                if not want:
                    return self._send({"ok": False, "error": "auth tanimli degil"}, 503)
                try:
                    good = hmac.compare_digest(pw, want)
                except Exception:
                    good = (pw == want)
                if not good:
                    _auth_register_fail(ip)
                    time.sleep(0.6)   # kaba kuvvet yavaslatma
                    log("warn", "panel", f"basarisiz giris ({ip})")
                    return self._send({"ok": False, "error": "hatali sifre"}, 401)
                _auth_reset(ip)
                # basarili: master PANEL_TOKEN DEGIL, suereli imzali oturum tokeni doner (G0.3,G0.4)
                stok = make_session()
                if not stok:
                    return self._send({"ok": False, "error": "oturum uretilemedi (token yok)"}, 503)
                log("ok", "panel", f"basarili giris ({ip})")
                return self._send({"ok": True, "token": stok,
                                   "expires_in": PANEL_SESSION_TTL})

            if not self._authed():
                return self._send({"error": "yetkisiz", "auth": True}, 401)
            if p == "/ops/research":
                q = (data.get("question") or "").strip()
                answer = "ayla"
                cap = "web_arastir"
                res = ajana_sor(answer, cap, text=q,
                                args={"question": q}, source="panel")
                return self._send({"ok": bool(res.get("ok")),
                                   "agent": res.get("agent"), "cap": res.get("cap"),
                                   "call_id": res.get("call_id"),
                                   "report": res.get("result") or "",
                                   "error": res.get("error") or ""})

            if p == "/key-switch":
                target = data.get("index")
                try:
                    ok = switch_key(None) if target is None else switch_key(int(target))
                except Exception:
                    ok = False
                if not ok:
                    return self._send({"ok": False, "error": "degisim yapilamadi",
                                       "count": len(_KEYS)}, 400)
                return self._send({"ok": True, **quota_all()})

            if p == "/chat":
                msg = (data.get("message") or "").strip()
                if not msg:
                    return self._send({"reply": ""})
                force = data.get("force")
                if force:
                    # ozel/test: mevcut davranis korunur (evren | cirak)
                    res = ask_cirak(msg, max_tokens=500, task="chat", force=force)
                    return self._send({"reply": res["reply"], "tier": res["tier"],
                                       "escalated": res["escalated"]})
                # varsayilan: Ayla (tek kapi)
                try:
                    reply, created = ayla_reply(msg)
                except Exception as exc:
                    log("error", "ayla", f"chat: {_safe_msg(type(exc).__name__)}")
                    _salvage_intent(msg, f"chat hatası: {type(exc).__name__}")
                    reply, created = ("Şu an yanıt üretemedim the owner.", [])
                return self._send({
                    "reply": reply,
                    "tier": 2,
                    "escalated": False,
                    "agent": "ayla",
                    "tasks": [{"id": t[0], "owner": t[1], "title": t[2]} for t in created],
                })

            if p == "/model-switch":
                mid = data.get("model") or "evren"
                set_active_model(mid)
                return self._send({"ok": True, "active": mid})

            if p == "/scan-now":
                n = scan_once()
                return self._send({"ok": True, "processed": n})

            if p == "/resolve-error":
                eid = int(data.get("id") or 0)
                with db() as c:
                    c.execute("UPDATE errors SET resolved=1 WHERE id=?", (eid,))
                return self._send({"ok": True})

            # ---------------- DEV kuyrugu (worker koprusu) ----------------
            if p == "/dev/jobs/claim":
                try:
                    now = time.time()
                    with db() as c:
                        row = c.execute(
                            "SELECT id,message FROM dev_jobs WHERE status='pending' "
                            "AND (due_ts IS NULL OR due_ts<=?) "
                            "ORDER BY id LIMIT 1", (now,)).fetchone()
                        if not row:
                            return self._send({"ok": True, "job": None})
                        # atomik claim: yalniz hala pending ise sahiplen (cift claim onlemi)
                        cur = c.execute(
                            "UPDATE dev_jobs SET status='running', "
                            "started_at=datetime('now'), updated_at=datetime('now'), "
                            "heartbeat_at=?, worker=?, "
                            "attempts=COALESCE(attempts,0)+1 "
                            "WHERE id=? AND status='pending'",
                            (now, str(data.get("worker") or "")[:40], row["id"]))
                        if cur.rowcount != 1:
                            return self._send({"ok": True, "job": None})
                    return self._send({"ok": True,
                                       "job": {"id": row["id"], "message": row["message"]}})
                except Exception as exc:
                    return self._send({"ok": False, "error": _safe_msg(str(exc))[:100]}, 500)
            if p == "/dev/jobs/release":
                # yazici mesgul vb: isi tuketmeden kuyruga geri birak (deneme saymaz)
                try:
                    jid = int(data.get("id") or 0)
                    with db() as c:
                        cur = c.execute("UPDATE dev_jobs SET status='pending', "
                                        "heartbeat_at=NULL, updated_at=datetime('now') "
                                        "WHERE id=? AND status='running'", (jid,))
                    return self._send({"ok": True, "released": cur.rowcount})
                except Exception as exc:
                    return self._send({"ok": False, "error": _safe_msg(str(exc))[:100]}, 500)
            if p == "/dev/jobs/heartbeat":
                try:
                    jid = int(data.get("id") or 0)
                    with db() as c:
                        cur = c.execute("UPDATE dev_jobs SET heartbeat_at=?, "
                                        "updated_at=datetime('now') "
                                        "WHERE id=? AND status='running'",
                                        (time.time(), jid))
                    return self._send({"ok": True, "updated": cur.rowcount})
                except Exception as exc:
                    return self._send({"ok": False, "error": _safe_msg(str(exc))[:100]}, 500)
            if p == "/dev/jobs/finish":
                try:
                    jid = int(data.get("id") or 0)
                    okk = bool(data.get("ok"))
                    res = _safe_msg(str(data.get("result") or ""))[:8000]
                    with db() as c:
                        c.execute("UPDATE dev_jobs SET status=?, result=?, "
                                  "finished_at=datetime('now'), updated_at=datetime('now') "
                                  "WHERE id=?", ("done" if okk else "error", res, jid))
                    log("ok" if okk else "warn", "devbot",
                        f"is #{jid} " + ("bitti" if okk else "hata"))
                    return self._send({"ok": True})
                except Exception as exc:
                    return self._send({"ok": False, "error": _safe_msg(str(exc))[:100]}, 500)
            if p == "/dev/heartbeat":
                try:
                    with db() as c:
                        c.execute("INSERT INTO settings(key,value) VALUES('dev_worker_hb',?) "
                                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                  (str(time.time()),))
                    # Anahtar uyumsuzlugu duzeltmesi: watchdog/dev_worker_online
                    # 'hb_devworker' okur; ayni sinyali oraya da yaz.
                    _hb("devworker")
                    return self._send({"ok": True})
                except Exception as exc:
                    return self._send({"ok": False, "error": _safe_msg(str(exc))[:80]}, 500)
            if p == "/dev/jobs/enqueue":
                try:
                    with db() as c:
                        cur = c.execute(
                            "INSERT INTO dev_jobs(ts,chat_id,message,status,updated_at) "
                            "VALUES(datetime('now'),?,?,'pending',datetime('now'))",
                            (str(data.get("chat_id") or "api"),
                             str(data.get("message") or "")[:4000]))
                        return self._send({"ok": True, "id": cur.lastrowid})
                except Exception as exc:
                    return self._send({"ok": False, "error": _safe_msg(str(exc))[:100]}, 500)
            # ---------------- gorev defteri + kilit ----------------
            if p == "/ops/tasks/add":
                tid = task_add(data.get("title") or "", data.get("detail") or "",
                               owner=data.get("owner") or "ayla",
                               priority=int(data.get("priority") or 1),
                               source=data.get("source") or "panel")
                return self._send({"ok": bool(tid), "id": tid})
            if p == "/ops/tasks/set":
                okk = task_set(int(data.get("id") or 0), status=data.get("status"),
                               result=data.get("result"), owner=data.get("owner"))
                return self._send({"ok": okk})
            if p == "/ops/tasks/retry":
                okk, msg = task_retry(int(data.get("id") or 0))
                return self._send({"ok": okk, "msg": msg})
            if p == "/ops/locks/acquire":
                aok, msg = lock_acquire(data.get("owner") or "ayla",
                                        data.get("area") or "", data.get("note") or "")
                return self._send({"ok": aok, "msg": msg})
            if p == "/ops/locks/release":
                return self._send({"ok": lock_release(data.get("owner") or "ayla",
                                                      data.get("area") or "")})
            return self._send({"error": "not found"}, 404)
        except Exception as exc:
            log("error", "api", f"POST {p}: {exc}")
            return self._send({"error": str(exc)}, 500)

# ------------------------------------------------------------------ main
def main():
    init_db()
    log("ok", "boot", f"Agent basladi. Port={API_PORT} Model={EV_MODEL} Yerel={OL_MODEL}")
    # G0.1: token yoksa fail-closed -> panel/uçlar kapali. Tek satir uyari.
    if not PANEL_TOKEN:
        log("warn", "security",
            "PANEL_TOKEN tanimsiz: panel ve tum kimlik-dogrulamali uclar KAPALI (fail-closed)")
    print(f"Evren Agent  -> http://localhost:{API_PORT}")
    print(f"Token limiti -> {EV_LIMIT}/gun")
    print(f"Mail tarama  -> her {POLL_SEC} sn")

    # Telegram kumanda kanali (token varsa)
    import sys as _sys
    try:
        _tg = _tg_mod.start(CFG, _sys.modules[__name__])
        globals()["_TG_BOT"] = _tg
    except Exception as exc:
        log("error", "telegram", f"baslatilamadi: {exc}")

    # the owner Dev botu (opencode koprusu)
    try:
        _dev = _dev_mod.start(CFG, _sys.modules[__name__])
        globals()["_DEV_BOT"] = _dev
    except Exception as exc:
        log("error", "devbot", f"baslatilamadi: {type(exc).__name__}")

    threading.Thread(target=mail_loop, daemon=True).start()
    threading.Thread(target=backup_loop, daemon=True).start()
    threading.Thread(target=spam_rule_loop, daemon=True).start()
    threading.Thread(target=apiman_loop, daemon=True).start()
    threading.Thread(target=ayla_digest_loop, daemon=True).start()
    threading.Thread(target=reminder_loop, daemon=True).start()
    threading.Thread(target=watchdog_loop, daemon=True).start()
    threading.Thread(target=services_guard_loop, daemon=True).start()
    threading.Thread(target=evren_bridge_loop, daemon=True).start()
    ThreadingHTTPServer((HOST_BIND, API_PORT), Handler).serve_forever()

if __name__ == "__main__":
    main()
