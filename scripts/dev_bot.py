"""
dev_bot.py — Orbion Dev botu (Telegram <-> opencode koprusu)
===========================================================
Mimari:
  Telegram  ->  dev_jobs (DB kuyrugu)  <- PC worker (opencode run)
                     |
                     +-> sonuc -> Telegram

Sunucuda 7/24 calisir. PC kapaliysa is kuyrukta bekler.
Guvenlik: yalniz izinli chat_id; bot shell calistirmaz; yalniz kuyruga yazar.
"""
import json
import re
import threading
import time
import urllib.parse
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"
MAXMSG = 3800


class DevBot:
    def __init__(self, token, allowed_chat, agent):
        self.token = token
        self.allowed = str(allowed_chat or "").strip()
        self.agent = agent          # agent modulu (db, log)
        self.offset = 0
        self._stop = False

    # ---------------- HTTP
    def _call(self, method, **params):
        url = API.format(token=self.token, method=method)
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=70) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode()[:200]
                self._log("warn", f"{method}: HTTP {e.code} {body}")
            except Exception:
                pass
            return None
        except Exception as exc:
            self._log("warn", f"{method}: {type(exc).__name__}")
            return None

    def _log(self, level, msg):
        try:
            self.agent.log(level, "devbot", msg)
        except Exception:
            pass

    def send(self, chat_id, text):
        if not text:
            return
        for i in range(0, len(text), MAXMSG):
            chunk = text[i:i + MAXMSG]
            r = self._call("sendMessage", chat_id=chat_id, text=chunk,
                           parse_mode="HTML", disable_web_page_preview="true")
            if r and r.get("ok"):
                continue
            plain = re.sub(r"<[^>]+>", "", chunk)
            self._call("sendMessage", chat_id=chat_id, text=plain,
                       disable_web_page_preview="true")

    # ---------------- dongu
    def _wait_ready(self):
        """getMe ile dogrula; 429/ag hatasinda artan beklemeyle tekrar dene."""
        wait = 30
        while not self._stop:
            me = self._call("getMe")
            if me and me.get("ok"):
                self._log("ok", f"Dev bot hazir: @{me['result'].get('username','?')}")
                return True
            code = (me or {}).get("error_code")
            if code == 429:
                ra = ((me or {}).get("parameters") or {}).get("retry_after", wait)
                try:
                    ra = int(ra)
                except Exception:
                    ra = wait
                # Telegram flood cezasini her istek SIFIRLAR. Dokunmamak gerek.
                # Bu yuzden 429'da uzun bekle (30 dk), tekrar denemeyi seyrelt.
                ra = max(1800, min(ra, 3600))
                self._log("warn", f"getMe 429, {ra} sn bekleniyor")
                time.sleep(ra)
                wait = min(wait * 2, 900)
                continue
            if code == 401:
                self._log("error", "getMe 401: token gecersiz, bot kapatildi")
                return False
            self._log("warn", f"getMe basarisiz ({code or 'ag'}), {wait} sn sonra tekrar")
            time.sleep(wait)
            wait = min(wait * 2, 900)
        return False

    def run(self):
        if not self._wait_ready():
            return
        while not self._stop:
            try:
                res = self._call("getUpdates", offset=self.offset, timeout=40)
            except Exception:
                time.sleep(5)
                continue
            if not res or not res.get("ok"):
                err = (res or {}).get("error_code")
                if err == 429:
                    ra = (res.get("parameters") or {}).get("retry_after", 10)
                    time.sleep(min(int(ra) + 1, 60))
                else:
                    time.sleep(4)
                continue
            for upd in res.get("result", []):
                self.offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if msg:
                    try:
                        self.handle(msg)
                    except Exception as exc:
                        self._log("error", f"handle: {type(exc).__name__}")
            time.sleep(0.3)

    # ---------------- yonlendirme
    def handle(self, msg):
        chat_id = str(msg["chat"]["id"])
        text = (msg.get("text") or "").strip()
        if not text:
            return
        # K4: FAIL-CLOSED. DEV_BOT_CHAT_ID tanimsizsa islem yok; TOFU kaldirildi.
        if not self.allowed:
            self._log("warn", "DEV_BOT_CHAT_ID tanimsiz; mesaj reddedildi (fail-closed)")
            return
        if chat_id != self.allowed:
            return self.send(chat_id, "Bu bot kisisel. Erisim yok.")

        low = text.lower()

        if low.startswith("/start") or low.startswith("/help") or low.startswith("/yardim"):
            return self.send(chat_id, self.help_text())

        if low.startswith("/durum"):
            return self.send(chat_id, self.status_text())

        if low.startswith("/iptal"):
            n = self.cancel_all()
            return self.send(chat_id, f"Bekleyen {n} is iptal edildi.")

        if low.startswith("/son"):
            return self.send(chat_id, self.last_results())

        # D2: acikca "/kod ..." -> dev kuyrugu (evren-bridge/PC worker)
        if low.startswith("/kod"):
            body = text[len("/kod"):].strip()
            if not body:
                return self.send(chat_id, "Kullanim: /kod <gorev>")
            jid = self.enqueue(chat_id, body)
            if jid:
                return self.send(chat_id, f"📥 <b>#{jid}</b> kod kuyruguna alindi.")
            return self.send(chat_id, "Kuyruga alinamadi (DB hatasi).")

        jid = self.enqueue(chat_id, text)
        if jid:
            return self.send(chat_id, f"📥 <b>#{jid}</b> kod kuyruguna alindi.")
        return self.send(chat_id, "Kuyruga alinamadi (DB hatasi).")

    def help_text(self):
        return (
            "<b>Orbion Dev</b> — kod köprüsü\n\n"
            "Normal mesaj yaz → kod kuyruğuna alınır.\n\n"
            "/kod &lt;gorev&gt; — kodu evren-bridge/PC worker kuyruğuna gönder\n"
            "/durum — kod kuyruğu ve PC durumu\n"
            "/son — son 5 kod işinin sonucu\n"
            "/iptal — bekleyen kod işlerini iptal et"
        )

    # ---------------- DB islemleri
    def enqueue(self, chat_id, text):
        try:
            with self.agent.db() as c:
                cur = c.execute(
                    "INSERT INTO dev_jobs(ts,chat_id,message,status,updated_at) "
                    "VALUES(datetime('now'),?,?,'pending',datetime('now'))",
                    (str(chat_id), text[:4000]))
                return cur.lastrowid
        except Exception as exc:
            self._log("error", f"enqueue: {type(exc).__name__}")
            return None

    def status_text(self):
        try:
            with self.agent.db() as c:
                rows = c.execute(
                    "SELECT status, COUNT(*) n FROM dev_jobs "
                    "WHERE status IN ('pending','running') GROUP BY status").fetchall()
                done = c.execute(
                    "SELECT COUNT(*) FROM dev_jobs WHERE date(ts)=date('now','localtime') "
                    "AND status IN ('done','error')").fetchone()[0]
                hb = c.execute("SELECT value FROM settings WHERE key='dev_worker_hb'"
                               ).fetchone()
            st = {r["status"]: r["n"] for r in rows}
        except Exception:
            return "DB hatasi."
        hb_age = None
        if hb and hb["value"]:
            try:
                hb_age = int(time.time() - float(hb["value"]))
            except Exception:
                hb_age = None
        if hb_age is None:
            pc = "❌ PC'den hic sinyal yok (worker kapali?)"
        elif hb_age < 60:
            pc = f"✅ PC bagli ({hb_age} sn once)"
        else:
            pc = f"⚠️ PC sinyali eski ({hb_age} sn)"
        return (f"<b>Durum</b>\n"
                f"Bekleyen: <b>{st.get('pending', 0)}</b>\n"
                f"Calisan: <b>{st.get('running', 0)}</b>\n"
                f"Bugun biten: <b>{done}</b>\n"
                f"{pc}")

    def cancel_all(self):
        try:
            with self.agent.db() as c:
                cur = c.execute("UPDATE dev_jobs SET status='cancelled', "
                                "updated_at=datetime('now') WHERE status='pending'")
                return cur.rowcount
        except Exception:
            return 0

    def last_results(self):
        try:
            with self.agent.db() as c:
                rows = c.execute("SELECT id,status,substr(result,1,300) r FROM dev_jobs "
                                 "WHERE status IN ('done','error','cancelled') "
                                 "ORDER BY id DESC LIMIT 5").fetchall()
        except Exception:
            return "DB hatasi."
        if not rows:
            return "Henuz biten is yok."
        out = ["<b>Son isler</b>"]
        for r in rows:
            icon = "✅" if r["status"] == "done" else ("❌" if r["status"] == "error" else "⏹")
            out.append(f"{icon} <b>#{r['id']}</b> — {(r['r'] or '').strip()[:200]}")
        return "\n".join(out)

    # ---------------- sonuc bildirimi (worker yazar, bot okur)
    def result_loop(self):
        """Worker'in yazdigi sonuclari Telegram'a gonder."""
        try:
            with self.agent.db() as c:
                c.execute("INSERT OR IGNORE INTO settings(key,value) "
                          "VALUES('dev_last_notified','0')")
        except Exception:
            pass
        while not self._stop:
            try:
                with self.agent.db() as c:
                    row = c.execute("SELECT value FROM settings "
                                    "WHERE key='dev_last_notified'").fetchone()
                    last = int((row["value"] if row and row["value"] else 0) or 0)
                    # D2: kod kuyrugu sonuclari evren-bridge uzerinden raporlanir;
                    # bu bot sonuçları GONDERMEZ (çakışma yok).
                    rows = c.execute(
                        "SELECT id,chat_id,status,result FROM dev_jobs "
                        "WHERE id>? AND status IN ('done','error') "
                        "AND chat_id != 'evren-bridge' ORDER BY id LIMIT 10",
                        (last,)).fetchall()
                for r in rows:
                    jid, chat, st, res = r["id"], r["chat_id"], r["status"], r["result"] or ""
                    icon = "✅" if st == "done" else "❌"
                    head = f"{icon} <b>#{jid}</b> " + ("bitti" if st == "done" else "hata")
                    body = res.strip()[:2500] or "(bos sonuc)"
                    self.send(chat, f"{head}\n\n{body}")
                    last = jid
                if rows:
                    with self.agent.db() as c:
                        c.execute("INSERT INTO settings(key,value) VALUES('dev_last_notified',?) "
                                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                  (str(last),))
            except Exception as exc:
                self._log("error", f"result_loop: {type(exc).__name__}")
            time.sleep(4)


def start(cfg, agent):
    token = (cfg.get("DEV_BOT_TOKEN") or "").strip()
    if not token:
        try:
            agent.log("warn", "devbot", "DEV_BOT_TOKEN bos, Dev bot kapali")
        except Exception:
            pass
        return None
    chat = (cfg.get("DEV_BOT_CHAT_ID") or "").strip()
    if not chat:
        try:
            agent.log("warn", "devbot",
                      "DEV_BOT_CHAT_ID bos -> bot CALISTIRILMADI (fail-closed)")
        except Exception:
            pass
        return None
    bot = DevBot(token, chat, agent)
    threading.Thread(target=bot.run, daemon=True).start()
    threading.Thread(target=bot.result_loop, daemon=True).start()
    return bot
