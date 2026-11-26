"""
dev_worker.py — PC tarafi: sunucudaki kuyrugu cekip opencode ile calistirir.
Kullanim:  python scripts\\dev_worker.py
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path):
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


CFG = load_env(ROOT / ".env")
BASE = (CFG.get("DEV_QUEUE_URL") or CFG.get("PANEL_PUBLIC_URL") or "").rstrip("/")
TOKEN = (CFG.get("DEV_QUEUE_TOKEN") or CFG.get("PANEL_TOKEN") or "").strip()
PROJECT = CFG.get("DEV_PROJECT_DIR") or str(ROOT)
POLL = int(CFG.get("DEV_POLL_SECONDS", "8") or 8)
TIMEOUT = int(CFG.get("DEV_JOB_TIMEOUT", "1800") or 1800)


def api(path, payload=None, timeout=40):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json", "X-Panel-Token": TOKEN},
        method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run_opencode(message):
    cmd = ["opencode", "run", message, "--format", "json"]
    try:
        p = subprocess.run(cmd, cwd=PROJECT, capture_output=True,
                           text=True, timeout=TIMEOUT, shell=False)
        out = (p.stdout or "").strip()
        if not out:
            return p.returncode == 0, (p.stderr or "(cikti yok)")[:3000]
        return p.returncode == 0, clean_output(out)
    except subprocess.TimeoutExpired:
        return False, f"(zaman asimi: {TIMEOUT}s)"
    except FileNotFoundError:
        return False, "opencode bulunamadi (PATH)"
    except Exception as exc:
        return False, f"(hata: {type(exc).__name__})"


def clean_output(raw):
    """opencode --format json akisindan son asistan metnini cikar."""
    texts = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            texts.append(line)
            continue
        t = ev.get("type") or ""
        if t in ("text", "message", "assistant"):
            part = ev.get("text") or (ev.get("message") or {}).get("content") or ""
            if isinstance(part, str) and part.strip():
                texts.append(part.strip())
        elif t == "error":
            err = ev.get("error") or {}
            texts.append("HATA: " + str(err.get("message") or err)[:300])
    out = "\n".join(texts).strip()
    return out[:6000] if out else raw[:4000]


def _single_writer_ok():
    """Ayni repoya iki yazici (manuel opencode vs worker) girmesin.
    evren-open ile ayni flock dosyasi; ayrica baska opencode prosesi varsa dur."""
    import fcntl
    try:
        lock_path = os.path.expanduser("~/.evren-opencode.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return None
        # kilit disinda (eski) bir opencode varsa da baslatma
        try:
            r = subprocess.run(["pgrep", "-x", "opencode"], capture_output=True, timeout=5)
            if r.returncode == 0 and (r.stdout or b"").strip():
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
                return None
        except Exception:
            pass
        return fd
    except Exception:
        return None


def _heartbeat_loop(jid, stop):
    while not stop.is_set():
        try:
            api("/dev/jobs/heartbeat", {"id": jid})
        except Exception:
            pass
        stop.wait(60)


def main():
    if not BASE or not TOKEN:
        log("DEV_QUEUE_URL / DEV_QUEUE_TOKEN eksik (.env). Cikiliyor.")
        return
    log(f"Worker basladi. sunucu={BASE} proje={PROJECT}")
    fails = 0
    while True:
        try:
            api("/dev/heartbeat", {"worker": os.environ.get("COMPUTERNAME", "pc")})
            job = api("/dev/jobs/claim", {"worker": os.environ.get("COMPUTERNAME", "pc")})
            fails = 0
        except Exception as exc:
            fails += 1
            if fails in (1, 10) or fails % 50 == 0:
                log(f"sunucu erisilemedi ({type(exc).__name__})")
            time.sleep(min(POLL * min(fails, 6), 60))
            continue

        if not job or not job.get("job"):
            time.sleep(POLL)
            continue

        j = job["job"]
        jid, msg = j["id"], j["message"]
        # Tek yazici korumasi: manuel opencode oturumu varsa bu isi simdi alma.
        lockfd = _single_writer_ok()
        if lockfd is None:
            try:
                api("/dev/jobs/release", {"id": jid})
            except Exception:
                pass
            log(f"#{jid} yazici mesgul (manuel opencode) -> kuyruga birakildi")
            time.sleep(POLL)
            continue
        log(f"#{jid} alindi: {msg[:70]}")
        import threading
        stop = threading.Event()
        hb = threading.Thread(target=_heartbeat_loop, args=(jid, stop), daemon=True)
        hb.start()
        try:
            ok, result = run_opencode(msg)
        finally:
            stop.set()
        try:
            api("/dev/jobs/finish", {"id": jid, "ok": ok, "result": result})
            log(f"#{jid} {'BITTI' if ok else 'HATA'} ({len(result)} kr)")
        except Exception as exc:
            log(f"#{jid} sonuc yazilamadi: {type(exc).__name__}")
        try:
            import fcntl
            fcntl.flock(lockfd, fcntl.LOCK_UN)
            os.close(lockfd)
        except Exception:
            pass
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("kapatildi")
