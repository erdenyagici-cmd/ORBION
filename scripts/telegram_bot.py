"""
telegram_bot.py — Telegram kumanda kanali (harici kutuphane YOK)
------------------------------------------------------------------
Long polling ile calisir; webhook/tunnel/domain gerekmez.

Komutlar:
  /start /help     yardim
  /durum           sistem durumu
  /onemli [gun]    son N gunun onemli mailleri
  /kutu            klasor doluluk ozeti
  /temizle <ay>    N aydan eski mailleri Cop'e tasi
  /bosalttrash     Cop kutusunu bosalt (onay ister)
  /gereksizler     reklam/kumar mailini Gereksizler klasorune tasi
  /yetenek         kim ne yapabiliyor
  /sor <ajan> <ne> ajani dogrudan calistir
  /projeler        proje defteri (kalici proje hafizasi)
  /proje ...       yeni | <ad> | devam | mimari | ilet | not | kapat
  /model <x>       evren | off
  /tara            hemen tara
  serbest metin    -> sef ajan yonlendirir (Tier2 -> Ollama)
"""

import json
import queue
import re
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import identity as ident

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramBot:
    def __init__(self, token, allowed_chat_id, agent):
        self.token = token
        self.allowed = str(allowed_chat_id or "").strip()
        self.agent = agent  # agent modulu (fonksiyonlara erisim)
        self.offset = 0
        self._stop = False
        self._last_call = None        # T3/G1: son gercek ajan cagrisi (agent/topic/call_id)
        self._last_direct_agent = None  # son cevap veren gercek ajan (baglam)
        # T24/V3: gelen mesajlar TEK kuyruk + TEK worker ile SIRAYLA islenir.
        # Boylece arka arkaya 2 mesajin cevabi karismaz (#290/#291 bug'i).
        self._queue = queue.Queue()
        self._worker_started = False

    def _worker_loop(self):
        """Mesajlari sirayla isle; uzun islem getUpdates dongusunu bloklamaz."""
        while not self._stop:
            try:
                msg = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.handle(msg)
            except Exception as exc:
                try:
                    self.agent.log("error", "telegram",
                                   f"worker handle: {type(exc).__name__}")
                except Exception:
                    pass
            finally:
                try:
                    self._queue.task_done()
                except Exception:
                    pass

    # ---------------- HTTP
    def _call(self, method, **params):
        url = API.format(token=self.token, method=method)
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=70) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode()[:160]
            except Exception:
                body = ""
            try:
                self.agent.log("error", "telegram",
                               f"{method}: HTTP {exc.code} {body}")
            except Exception:
                pass
            return {"ok": False, "error_code": exc.code, "description": body}
        except Exception as exc:
            try:
                self.agent.log("error", "telegram",
                               f"{method}: {type(exc).__name__}")
            except Exception:
                pass
            return None

    # izin verilen HTML etiketleri (Telegram destekli)
    _OK_TAGS = ("b", "i", "u", "s", "code", "pre", "a")

    @classmethod
    def _sanitize_html(cls, text):
        """Ham '<' '>' karakterlerini kacir; yalniz izinli etiketleri birak.
        Ornek: lab degerleri '<1 Negatif' -> '&lt;1 Negatif' (Telegram 400 onlenir)."""
        out = []
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == "<":
                m = re.match(r"</?(" + "|".join(cls._OK_TAGS) + r")(\s[^>]*)?>", text[i:])
                if m:
                    out.append(m.group(0))
                    i += len(m.group(0))
                    continue
                out.append("&lt;")
                i += 1
                continue
            if ch == ">":
                out.append("&gt;")
                i += 1
                continue
            if ch == "&":
                # zaten entity mi?
                m = re.match(r"&(amp|lt|gt|quot|#\d+);", text[i:])
                if m:
                    out.append(m.group(0))
                    i += len(m.group(0))
                    continue
                out.append("&amp;")
                i += 1
                continue
            out.append(ch)
            i += 1
        return "".join(out)

    def send(self, chat_id, text, agent=None, topic=None, call_id=None):
        """HTML dener (guvenli sanitize), basarisiz olursa duz metne duser.
        Gonderilen her parca tg_messages'a yazilir (denetim + reply yonlendirmesi).
        Donus: tum parcalar gonderildiyse True, aksi halde False."""
        if not text:
            return True
        all_ok = True
        for i in range(0, len(text), 3900):
            chunk = text[i:i + 3900]
            safe = self._sanitize_html(chunk)
            r = self._call("sendMessage", chat_id=chat_id, text=safe,
                           parse_mode="HTML", disable_web_page_preview="true")
            if not (r and r.get("ok")):
                # yedek: duz metin (tagleri sok, entity'leri coz)
                plain = re.sub(r"<[^>]+>", "", safe)
                plain = plain.replace("&lt;", "<").replace("&gt;", ">") \
                             .replace("&amp;", "&").replace("&quot;", '"')
                r = self._call("sendMessage", chat_id=chat_id, text=plain,
                               disable_web_page_preview="true")
            if not (r and r.get("ok")):
                all_ok = False
            try:
                mid = (r or {}).get("result", {}).get("message_id")
                self.agent.tg_log(chat_id, "out", msg_id=mid, agent=agent or "ayla",
                                  topic=topic, call_id=call_id, text=chunk)
            except Exception:
                pass
        return all_ok

    # ---------------- ana dongu
    def run(self):
        me = self._call("getMe")
        if not me or not me.get("ok"):
            try:
                self.agent.log("error", "telegram", "Token gecersiz (getMe basarisiz)")
            except Exception:
                pass
            return
        uname = me["result"].get("username", "?")
        # offset kalici: restart'ta eski mesajlar tekrar oynatilmasin
        try:
            saved = int(self.agent.tg_offset_load() or 0)
            if saved > self.offset:
                self.offset = saved
        except Exception:
            pass
        try:
            self.agent.log("ok", "telegram",
                           f"Bot hazir: @{uname} (offset={self.offset})")
        except Exception:
            pass
        # T24/V3: tek worker (sirali isleme)
        if not self._worker_started:
            self._worker_started = True
            threading.Thread(target=self._worker_loop, daemon=True).start()
        while not self._stop:
            res = self._call("getUpdates", offset=self.offset, timeout=50)
            if not res or not res.get("ok"):
                time.sleep(3)
                continue
            for upd in res.get("result", []):
                self.offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    # offset'i yine de kalicilastir
                    try:
                        self.agent.tg_offset_save(self.offset)
                    except Exception:
                        pass
                    continue
                # T24/V3: siraya al (offset zaten yukarida ilerletildi)
                try:
                    self._queue.put(msg)
                except Exception:
                    pass
                try:
                    self.agent.tg_offset_save(self.offset)
                except Exception:
                    pass
            time.sleep(0.4)

    # ---------------- yonlendirme
    def handle(self, msg):
        chat_id = str(msg["chat"]["id"])
        self._chat_id = chat_id  # D3: per-chat gecmis (tg:<id>:ayla)
        text = (msg.get("text") or "").strip()
        if not text:
            return
        mid = msg.get("message_id")
        reply_to = (msg.get("reply_to_message") or {}).get("message_id")
        # cift teslim / restart tekrari korumasi + gelen mesaj defteri
        try:
            if mid and self.agent.tg_seen(mid, chat_id):
                return
            self.agent.tg_log(chat_id, "in", msg_id=mid, reply_to=reply_to, text=text)
        except Exception:
            pass
        # reply ile geliyorsa hangi ajanin konusuna cevap verildigini bul
        self._reply_ctx = None
        if reply_to:
            try:
                self._reply_ctx = self.agent.tg_lookup_agent_by_msg(reply_to, chat_id)
            except Exception:
                self._reply_ctx = None
        # K4: FAIL-CLOSED. Sahip chat_id tanimli degilse hicbir mesaj islenmez.
        # TOFU (ilk yazan sahip olur) KALDIRILDI.
        if not self.allowed:
            try:
                self.agent.log("warn", "telegram",
                               "TELEGRAM_CHAT_ID tanimsiz; mesaj reddedildi (fail-closed)")
            except Exception:
                pass
            return
        if chat_id != self.allowed:
            self.send(chat_id, "Bu bot kisisel. Erisim izniniz yok.\nchat_id: <code>%s</code>" % chat_id)
            return

        low = text.lower()
        try:
            if low.startswith("/start") or low.startswith("/help"):
                return self.send(chat_id, self.help_text())

            if low.startswith("/ekip") or low.startswith("/kim"):
                return self.send(chat_id, "<b>Ekip</b>\n\n" + ident.roster_text())

            if low.startswith("/durum"):
                parts = [self.cmd_status()]
                try:
                    parts.append("")
                    parts.append(self.agent.ayla_task_table())
                except Exception:
                    pass
                try:
                    _al = self.agent.ayla_alert_summary()
                    if _al:
                        parts.append("")
                        parts.append(_al)
                except Exception:
                    pass
                return self.send(chat_id, "\n".join(parts))

            if low.startswith("/onemli"):
                m = re.search(r"(\d+)", low)
                days = int(m.group(1)) if m else 7
                return self.send(chat_id, self.cmd_important(days))

            if low.startswith("/kutu"):
                return self.send(chat_id, self.cmd_mailbox())

            if low.startswith("/temizle"):
                m = re.search(r"(\d+)", low)
                months = int(m.group(1)) if m else 1
                return self.send(chat_id, self.cmd_cleanup(months))

            if low.startswith("/bosalttrash"):
                return self.send(chat_id, self.cmd_empty_trash())

            if low.startswith("/gereksizler") or low.startswith("/spam"):
                return self.send(chat_id, self.cmd_gereksizler())

            if re.search(r"(reklam|kumar|bahis|gereksiz|spam).*(tas[iı]|filtrele|temizle|ayr)",
                         low):
                return self.send(chat_id, self.cmd_gereksizler(repath=True))

            if low.startswith("/model"):
                parts = low.split()
                target = parts[1] if len(parts) > 1 else "evren"
                return self.send(chat_id, self.cmd_model(target))

            if low.startswith("/gonder") or low.startswith("/gönder"):
                # /gonder alici@x.com | Konu | Mesaj
                raw = text[len("/gonder"):].strip()
                return self.send(chat_id, self.cmd_send(raw))

            if low.startswith("/tara"):
                n = self.agent.scan_once()
                return self.send(chat_id, f"Tarama tamam: <b>{n}</b> yeni mail islendi.")

            if low.startswith("/anahtar"):
                self.send(chat_id, self.cmd_keys(text[len("/anahtar"):].strip()))
                return

            if low.startswith("/yetenek") or low.startswith("/kim-ne"):
                return self.send(chat_id, self.agent.ajan_yetenek_metni())

            if low.startswith("/sor "):
                rest = text[len("/sor"):].strip()
                res = self.agent.ajan_metinden_calistir(rest, source="telegram",
                                                        require_intent=False)
                if not res:
                    return self.send(chat_id, "Kimi sorayım? Ör: "
                                              "<code>/sor apiman kota</code>")
                return self.send(chat_id, self.agent.cap_metni(res),
                                 agent=res.get("agent"), topic=res.get("cap"),
                                 call_id=res.get("call_id"))

            if low.startswith("/projeler") or low == "/proje" or low.startswith("/proje "):
                # mimari/ilet agir (muhakeme modeli) -> arka planda calis, bloklamasin
                if re.match(r"(?i)^\s*/proje\s+(mimari|mimarı|spec|plan|ilet|"
                            r"g[oö]nder|evrene)\b", text.strip()):
                    threading.Thread(target=self._proje_slow,
                                     args=(chat_id, text.strip()), daemon=True).start()
                    return self.send(chat_id, "\u23F3 Hazırlanıyor… bitince haber vereceğim.")
                return self.send(chat_id, self.cmd_proje(text.strip()))

            if low.startswith("/gorev") or low.startswith("/görev") or low.startswith("/is "):
                parts = text.split(" ", 1)
                filt = parts[1].strip().lower() if len(parts) > 1 else ""
                return self.send(chat_id, self.cmd_tasks(filt))

            if low.startswith("/biten"):
                return self.send(chat_id, self.cmd_tasks("done"))

            if low.startswith("/durum.md") or low.startswith("/ozet"):
                return self.send(chat_id, self.cmd_durum())

            # ===== TAM YETKILI AJAN MODU (sadece sahip + ENABLE_SHELL_CMD=1) =====
            # K4: /sh /calistir /yaz /servis /oku varsayilan KAPALI; .env'de
            # ENABLE_SHELL_CMD=1 yoksa komut calistirilmaz.
            if (low.startswith("/sh ") or low.startswith("/calistir ")
                    or low.startswith("/oku ") or low.startswith("/yaz ")
                    or low.startswith("/servis ")):
                if not self._shell_enabled():
                    return self.send(chat_id, "Shell komutlari kapali "
                                              "(ENABLE_SHELL_CMD=1 gerekli).")
                if low.startswith("/sh ") or low.startswith("/calistir "):
                    cmd = text.split(" ", 1)[1].strip()
                    return self.send(chat_id, self.cmd_shell(cmd))

                if low.startswith("/oku "):
                    path = text.split(" ", 1)[1].strip()
                    return self.send(chat_id, self.cmd_read(path))

                if low.startswith("/yaz "):
                    # /yaz <yol> | <icerik>
                    rest = text.split(" ", 1)[1].strip()
                    return self.send(chat_id, self.cmd_write(rest))

                if low.startswith("/servis "):
                    rest = text.split(" ", 1)[1].strip().split()
                    return self.send(chat_id, self.cmd_service(rest))

            if low.startswith("/agent"):
                return self.send(chat_id, self.cmd_agent_mode(text[len("/agent"):].strip()))

            # serbest metin -> Asistan niyeti anlar. T24/V3: worker zaten ayri
            # thread'de sirali isliyor; burada SENKRON cagir (sira korunur).
            self._async_task(chat_id, text)
            return

        except Exception as exc:
            try:
                self.agent.log("error", "telegram", f"handle: {exc}")
            except Exception:
                pass
            return self.send(chat_id, f"Hata: <code>{exc}</code>")

    def _async_task(self, chat_id, text):
        """Tek kapi: isi Ayla alir, cevabi Ayla imzalar. Diger ajanlar patrona yazmaz."""
        try:
            try:
                out, agent = self.commander(text)
            except Exception as exc:
                try:
                    self.agent.log("error", "ayla", f"async: {type(exc).__name__}")
                except Exception:
                    pass
                self.send(chat_id, f"\u26A0\uFE0F <b>Ayla</b>: bir sorun çıktı, "
                                   f"inceleyip dönüyorum.")
                return
            # commander zaten "🤵 Ayla" imzali dondurur; cift imza olmasin.
            # Gercek ajan cevabinda Ayla basligi ZORLA eklenmez (agent kimligi korunur).
            if out and agent in ("ayla", "filtre") and "Ayla" not in out[:40]:
                out = "\U0001F935 <b>Ayla</b>\n\n" + out
            call = getattr(self, "_last_call", None) or {}
            self.send(chat_id, out, agent=agent or "ayla",
                      topic=call.get("cap"), call_id=call.get("call_id"))
        except Exception as exc:
            try:
                self.agent.log("error", "telegram", f"async: {exc}")
            except Exception:
                pass
            self.send(chat_id, f"İşlem hatası: <code>{exc}</code>")

    def _is_mail_scan_request(self, low):
        """K6: yalniz acik mail tarama ('mail tara', 'mailleri tara' ...).
        'sayfayi yenile', 'ilacimi kontrol et' gibi ifadeler TETIKLEMEZ."""
        return bool(
            re.search(r"\b(mail|mailler|mailleri|gelen\s*kutu|inbox|kutu)\b", low)
            and re.search(r"\b(tara|tarama|taramasi|taramas[ıi])\b", low)
            and not re.search(r"(mail\w*\s*(d[ıi][şs][ıi]nda|haric|hari[çc]))", low)
        )

    def _shell_enabled(self):
        """K4: /sh /calistir /yaz /servis /oku yalniz ENABLE_SHELL_CMD=1 ise."""
        try:
            return str(self.agent.CFG.get("ENABLE_SHELL_CMD", "")).strip() == "1"
        except Exception:
            return False

    def help_text(self):
        return (
            "<b>Evren Asistan</b>\n"
            "<i>Evren = beyin, Cirak (Ollama) = isci. Isler once ciraga gider.</i>\n\n"
            "<b>Komutlar</b>\n"
            "/durum — sistem durumu\n"
            "/onemli [gun] — son N gunun onemli mailleri (varsayilan 7)\n"
            "/kutu — klasor doluluk ozeti\n"
            "/temizle [ay] — N aydan eski mailleri Cop'e tasi\n"
            "/bosalttrash — Cop kutusunu bosalt\n"
            "/gereksizler — reklam/kumar mailini Gereksizler klasorune tasi\n"
            "/yetenek — kim ne yapabiliyor\n"
            "/sor &lt;ajan&gt; &lt;ne&gt; — ajanı doğrudan çalıştır (ör. /sor apiman kota)\n"
            "/projeler — açık proje defteri\n"
            "/proje yeni &lt;ad&gt; · /proje &lt;ad&gt; · /proje devam &lt;ad&gt;\n"
            "/proje mimari &lt;ad&gt; · /proje ilet &lt;ad&gt; · /proje not &lt;ad&gt; | metin\n"
            "/gonder alici@x.com | Konu | Mesaj — mail at\n"
            "/model evren|cirak|off — model secimi\n"
            "/tara — hemen tara\n\n"
            "<b>TAM YETKILI MOD</b>\n"
            "/sh &lt;komut&gt; — sunucuda komut calistir\n"
            "/oku &lt;yol&gt; — dosya oku\n"
            "/yaz &lt;yol&gt; | &lt;icerik&gt; — dosya yaz\n"
            "/servis &lt;ad&gt; [start|stop|restart|status]\n"
            "/agent &lt;gorev&gt; — Evren'e sistem baglamiyla gorev\n\n"
            "<b>Serbest yazabilirsin:</b>\n"
            "• maillerime bak, onemli olanlari soyle\n"
            "• mail kutum doldu, son 1 ayi tut gerisini sil\n"
            "• cop kutusunu bosalt\n"
            "• ali@ornek.com'a mail at: toplanti yarin 10'da\n"
            "• bugun ne var?"
        )

    # ---------------- komutlar
    def cmd_status(self):
        a = self.agent
        used = a.tokens_used_today()
        return (
            f"<b>Durum</b>\n"
            f"Model: <code>{a.active_model()}</code>\n"
            f"Token: <b>{used:,}</b> / {a.EV_LIMIT:,}\n"
            f"Mail tarama: {a.POLL_SEC} sn\n"
            f"Ollama: <code>{a.OL_MODEL}</code>\n"
            f"Evren: <code>{a.EV_MODEL}</code>"
        ).replace(",", ".")

    def cmd_important(self, days):
        acts = self.agent.actions
        rows = acts.list_important(self.agent.CFG, days=days, limit=15)
        try:
            # T20: "bunu sil" bağlamı için son gösterilen mailleri sakla
            self.agent.set_last_mail_ctx(rows, days=days)
        except Exception:
            pass
        if not rows:
            return f"Son {days} gunde onemli mail bulunamadi."
        out = [f"<b>Son {days} gunun onemli mailleri</b> ({len(rows)})\n"]
        for r in rows:
            out.append(f"• <b>{r['subject']}</b>\n  <i>{r['from']}</i>")
        return "\n".join(out)

    def cmd_mailbox(self):
        acts = self.agent.actions
        st = acts.mailbox_stats(self.agent.CFG)
        if not st:
            return "Klasor bilgisi alinamadi."
        key = sorted(st.items(), key=lambda x: -x[1])[:12]
        out = ["<b>Klasor dolulugu</b>\n"]
        for name, n in key:
            out.append(f"• {name}: <b>{n}</b>")
        return "\n".join(out)

    def cmd_cleanup(self, months):
        self.agent.set_agent("arsiv", "busy")
        try:
            return self._cleanup_impl(months)
        finally:
            self.agent.set_agent("arsiv", "idle")

    def _cleanup_impl(self, months):
        acts = self.agent.actions
        r = acts.cleanup_older(self.agent.CFG, months=months)
        n = int((r or {}).get("moved") or 0)
        err = int((r or {}).get("errors") or 0)
        if n == 0:
            if err:
                return (f"Tasima basarisiz: {err} mail kopyalanamadi; "
                        f"hicbiri silinmedi.")
            return f"{months} aydan eski mail yok."
        self.agent.log("warn", "cleanup", f"{n} mail Cop'e tasindi ({months} ay)")
        return (f"<b>{n}</b> mail Cop kutusuna tasindi ({months}+ ay eski).\n"
                + (f"{err} mail kopyalanamadi; silinmedi.\n" if err else "")
                + f"Kalici silmek icin: /bosalttrash")

    def _proje_slow(self, chat_id, raw):
        """Mimari uretimi / Evren'e iletim: arka planda calis, sonucu gonder."""
        try:
            out = self.cmd_proje(raw)
        except Exception as exc:
            out = f"\u26A0 Islem hatasi: <code>{type(exc).__name__}</code>"
        try:
            self.send(chat_id, out or "(bos sonuc)")
        except Exception:
            pass

    def cmd_proje(self, raw):
        """Proje defteri komutlari (LLM'siz, deterministik).
        /proje | /proje yeni <ad> | /proje <ad> | /proje not <ad> | <metin>
        /proje devam <ad> | /proje mimari <ad> | /proje ilet <ad> | /proje kapat <ad>"""
        a = self.agent
        body = re.sub(r"(?i)^\s*/projeler\s*", "", raw or "").strip()
        body = re.sub(r"(?i)^\s*/proje\s*", "", body).strip()

        if not body or re.match(r"(?i)^(liste|listele|hepsi)$", body):
            return a.ayla_project_report()

        m = re.match(r"(?i)^yeni\s*[:\-]?\s*(.+)$", body)
        if m:
            name = re.sub(r"(?i)\s+not\s*[:\-].*$", "", m.group(1)).strip()
            note = ""
            mn = re.search(r"(?i)not\s*[:\-]\s*(.+)$", m.group(1))
            if mn:
                note = mn.group(1).strip()
            meta = a._proj.proj_create(name, note=note, source="telegram")
            if not meta:
                return "Proje adı gerekli: <code>/proje yeni &lt;ad&gt;</code>"
            a.proj_active_set(meta["slug"])
            return (f"\U0001F5C2 <b>Proje açıldı:</b> {meta['name']}\n"
                    f"Aktif proje olarak seçildi. Konuşarak not ekleyebilirsin.")

        m = re.match(r"(?i)^not\s+(.+)$", body)
        if m:
            rest = m.group(1)
            if "|" in rest:
                ref, txt = rest.split("|", 1)
            else:
                ref, txt = "", rest
            slug = a.proj_resolve(ref.strip())
            if not slug:
                return ("Proje seçilemedi. <code>/proje not &lt;ad&gt; | metin</code> "
                        "veya önce <code>/proje yeni &lt;ad&gt;</code>")
            a._proj.proj_add_note(slug, txt.strip(), kind="not")
            a.proj_active_set(slug)
            meta = a._proj.proj_get(slug)
            return f"\U0001F4DD <b>{meta['name']}</b> defterine not eklendi."

        m = re.match(r"(?i)^(devam|surdur|sürdür)\s*(.*)$", body)
        if m:
            slug = a.proj_resolve(m.group(2).strip())
            if not slug:
                return "Hangi proje? <code>/proje devam &lt;ad&gt;</code>"
            a.proj_active_set(slug)
            return a.ayla_project_report(slug) + \
                "\n\n<i>Bu projeden devam ediyoruz; notları bağlama aldım.</i>"

        m = re.match(r"(?i)^(mimari|mimarı|spec|plan)\s*(.*)$", body)
        if m:
            slug = a.proj_resolve(m.group(2).strip())
            if not slug:
                return "Hangi proje? <code>/proje mimari &lt;ad&gt;</code>"
            self.agent.set_agent("sefler", "busy")
            try:
                spec, err = a.ayla_project_spec(slug)
            finally:
                self.agent.set_agent("sefler", "idle")
            meta = a._proj.proj_get(slug)
            if err:
                return f"\u26A0 Mimari üretilemedi: {err}"
            head = (f"\U0001F3D7 <b>{meta['name']}</b> mimarisi hazır "
                    f"({len(spec)} karakter).")
            return head + "\n\n<pre>" + spec[:3200].replace("<", "&lt;").replace(">", "&gt;") + \
                ("…</pre>" if len(spec) > 3200 else "</pre>")

        m = re.match(r"(?i)^(ilet|gonder|gönder|evrene)\s*(.*)$", body)
        if m:
            slug = a.proj_resolve(m.group(2).strip())
            if not slug:
                return "Hangi proje? <code>/proje ilet &lt;ad&gt;</code>"
            tid, err = a.ayla_project_transfer(slug)
            meta = a._proj.proj_get(slug)
            if err:
                return f"\u26A0 İletilemedi: {err}"
            return (f"\U0001F4E4 <b>{meta['name']}</b> Evren'e iletildi "
                    f"(görev <b>#{tid}</b>).\n"
                    f"Durum takibi: <code>/gorev</code>")

        m = re.match(r"(?i)^(kapat|arsivle|arşivle)\s+(.+)$", body)
        if m:
            slug = a.proj_resolve(m.group(2).strip())
            if not slug:
                return "Hangi proje?"
            a._proj.proj_set_status(slug, "kapali")
            return f"\U0001F5C2 <b>{a._proj.proj_get(slug)['name']}</b> kapatıldı."

        # aksi halde: proje detayi
        slug = a.proj_resolve(body)
        if not slug:
            slug = a._proj.proj_find(body)
        if not slug:
            return ("Proje bulunamadı: <code>" + body[:60] + "</code>\n"
                    "Liste: <code>/projeler</code>")
        a.proj_active_set(slug)
        return a.ayla_project_report(slug)

    def cmd_gereksizler(self, repath=False):
        """Gereksizler klasoru durumu + (istege bagli) hemen tarama."""
        try:
            st = self.agent.spam_status()
        except Exception as exc:
            return f"Durum alinamadi: {exc}"
        n = st.get("folder_count")
        out = ["\U0001F5D1 <b>Gereksizler</b>",
               "Klasordeki mail: <b>%s</b>" % ("?" if n is None else n),
               "Son tarama: <b>%s</b>" % (st.get("last_ts") or "henuz yok"),
               "Son tasinan: <b>%s</b>" % st.get("last_moved", 0),
               "Toplam tasinan: <b>%s</b>" % st.get("total_moved", 0)]
        if repath:
            try:
                self.agent.spam_rule_once()
                st2 = self.agent.spam_status()
                out.append("")
                out.append("Yeni tarama: <b>%s</b> mail tasindi." %
                           st2.get("last_moved", 0))
            except Exception as exc:
                out.append(f"Tarama hatasi: {exc}")
        return "\n".join(out)

    def cmd_empty_trash(self):
        self.agent.set_agent("arsiv", "busy")
        try:
            return self._empty_trash_impl()
        finally:
            self.agent.set_agent("arsiv", "idle")

    def _empty_trash_impl(self):
        acts = self.agent.actions
        r = acts.empty_trash(self.agent.CFG, confirm=True)
        if r.get("ok"):
            self.agent.log("warn", "cleanup", f"Cop bosaltildi: {r['deleted']}")
            return f"Cop kutusu bosaltildi: <b>{r['deleted']}</b> mail kalici silindi."
        return f"Basarisiz: {r.get('error')}"

    def cmd_model(self, target):
        if target.startswith("ev"):
            mid = "evren"
        elif target.startswith("cirak") or target.startswith("çırak") or target.startswith("ollama"):
            mid = "cirak"
        else:
            mid = "off"
        self.agent.set_active_model(mid)
        label = {"evren": "Evren (bulut)", "cirak": "Cirak (Ollama, yerel)", "off": "Kapali"}[mid]
        return f"Model: <b>{label}</b>"

    def cmd_send(self, raw):
        """Format: alici@x.com | Konu | Mesaj  (ayirici: | veya ;)"""
        if not raw:
            return ("<b>Kullanim</b>\n"
                    "<code>/gonder alici@ornek.com | Konu | Mesaj metni</code>\n\n"
                    "Ayirici olarak <code>|</code> kullan.")
        parts = [p.strip() for p in re.split(r"[|;]", raw)]
        if len(parts) < 3:
            return ("Eksik bilgi. Format:\n"
                    "<code>/gonder alici@ornek.com | Konu | Mesaj</code>")
        to, subject, body = parts[0], parts[1], parts[2]
        r = self.agent.actions.send_mail(self.agent.CFG, to, subject, body)
        if r.get("ok"):
            self.agent.log("ok", "mail-send", f"gonderildi -> {to} | {subject[:40]}")
            return (f"Mail gonderildi\n"
                    f"Kimden: <code>{r['from']}</code>\n"
                    f"Kime: <code>{r['to']}</code>\n"
                    f"Konu: {r['subject']}")
        self.agent.log("error", "mail-send", f"{to}: {r.get('error')}")
        return f"Gonderilemedi: <code>{r.get('error')}</code>"

    # ---------------- sef ajan (serbest metin) ----------------
    def _detect_intent(self, text):
        low = (text or "").lower()
        if re.search(r"(mail|gelen|kutu)", low) and re.search(
                r"(bak|kontrol|onemli|önemli|bildir|ozet|özet|var\s*mi|neler)", low):
            return "ayla", "Ayla'ya iletiyorum — mailleri kontrol ediyor..."
        if re.search(r"(reklam|pazarlama|tan[ıi]t[ıi]m|b[uü]lten|spam|gereksiz)", low) \
                and re.search(r"(etiketle|i[şs]aretle|ayir|ayır)", low):
            return "ayla", "Ayla'ya iletiyorum — reklam/pazarlama mailleri 'Gereksiz' etiketleniyor..."
        if re.search(r"(sil|temizle|bosalt|boşalt|cop|çöp)", low):
            return "arsivci", "Arşivci'ye iletiyorum — temizlik yapıyor..."
        if re.search(r"(gonder|gönder|mail\s*at)", low) and "@" in low:
            return "kurye", "Kurye'ye iletiyorum — mail gönderiyor..."
        return None, None

    # Donus: (metin, ajan_adi)
    def _remember_call(self, res):
        """Gercek ajan cagrisini kaydet: giden mesaj agent/topic/call_id tasir (T3/G1)."""
        if not res:
            return
        self._last_call = res
        a = res.get("agent") or ""
        if a and a not in ("ayla", "filtre"):
            self._last_direct_agent = a

    def _routing_only(self, text):
        """'bunu ilet' gibi sadece yonlendirme metni mi (icerik yok)?"""
        t = (text or "").strip().lower()
        t = re.sub(r"^.*?(?:apiman|ar[sş]ivci|evren|ayla)\w*\s*[,:;\-]?\s*",
                   "", t)
        t = re.sub(r"\b(yaps[ıi]n|yapacak|yap|bunu|[şs]unu|sunu|iletsin|ilet|"
                   r"baks[ıi]n|halletsin|ilgilen(sin)?|halletsin)\b", "", t).strip(" .,;:-")
        return len(t) < 4

    def _reply_call(self, rc, text):
        """Reply edilen ajana gercek cagri yap. Donus: (metin, agent) veya None."""
        aid = rc.get("agent") or ""
        if aid in ("", "ayla", "filtre") and not rc.get("call_agent"):
            return None
        if rc.get("call_agent"):
            aid = rc["call_agent"]
        cap = rc.get("topic") or ""
        reg = getattr(self.agent, "AGENT_REGISTRY", {}).get(
            getattr(self.agent, "_reg_norm", lambda x: x)(aid), {})
        caps = reg.get("caps") or {}
        if cap not in caps:
            cap = self.agent.cap_tahmin(aid, text)
        # sadece yonlendirme metni + metin isteyen yetenek -> guvenli readonly'e dus
        if caps.get(cap, {}).get("needs") == "text" and self._routing_only(text):
            for k, v in caps.items():
                if v.get("readonly"):
                    cap = k
                    break
        res = self.agent.ajana_sor(aid, cap, text=text, source="reply")
        if res and res.get("ok"):
            self._remember_call(res)
            return (self.agent.cap_metni(res), res.get("agent") or aid)
        if res:
            self._remember_call(res)  # hata da kanit olarak yazilsin
        return None

    def _wipe_all(self, low):
        """'tüm kutuyu sil/boşalt/temizle', 'hepsini sil', 'gelen kutusunu temizle'
        -> TÜM kutu (arşiv) işi mi? 'çöp kutusu' ve 'N aydan eski' ayrı iştir."""
        if re.search(r"(cop|çöp)", low):
            return False
        if (re.search(r"(gelen\s*kutu|kutuyu|inbox|mailbox|t[uü]m\s*kutu)", low)
                and re.search(r"(sil|bo[sş]alt|temizle)", low)
                and not re.search(r"\d+\s*(ay|g[uü]n|hafta)|eski", low)):
            return True
        return bool(
            re.search(r"(t[uü]m|hepsi|hepsini|b[uü]t[uü]n|komple)\w*[^.]{0,20}"
                      r"(sil|bo[sş]alt|temizle)|"
                      r"(sil|bo[sş]alt|temizle)\w*[^.]{0,20}"
                      r"(t[uü]m|hepsi|hepsini|b[uü]t[uü]n)", low))

    def _archive_all(self):
        """Tüm kutuyu ARŞİVLE (gerçek yetenek, call_id'li; kalıcı silme YOK)."""
        res = self.agent.ajana_sor("arsivci", "arsivle", source="telegram")
        self._remember_call(res)
        if res and res.get("ok"):
            return ("\U0001F935 <b>Ayla</b> \u2192 " + self.agent.cap_metni(res),
                    res.get("agent") or "arsivci")
        return ("\U0001F935 <b>Ayla</b>\n\n" + (self.agent.cap_metni(res)
                if res else "Arşivleme yeteneğine ulaşamadım."), "arsivci")

    def _call_agent(self, aid, cap, text, args=None):
        """Gerçek ajan çağrısı: call_id'li kanıt + doğru agent imzası."""
        res = self.agent.ajana_sor(aid, cap, text=text, args=args, source="telegram")
        self._remember_call(res)
        if res and res.get("ok"):
            return ("\U0001F935 <b>Ayla</b> \u2192 " + self.agent.cap_metni(res),
                    res.get("agent") or aid)
        return ("\U0001F935 <b>Ayla</b>\n\n" + (self.agent.cap_metni(res)
                if res else "Yeteneğe ulaşamadım."), aid)

    def _single_mail_delete(self, text, low):
        """T20: tek/belirli mail silme niyeti -> (cap, args) | ('ask', None) | None.

        'en son maili sil' -> son_sil; 'bunu/şu maili sil' -> baglamdan konu_sil;
        N-ay/eski/tüm kutu istekleri BURADA ele alinmaz (mevcut yollar korunur)."""
        if not re.search(r"(sil|kald[\u0131i]r|ta[\u015fs][\u0131i]|ç[oö]pe|temizle)", low):
            return None
        # toplu temizlik / tüm kutu -> diger yollara birak
        if re.search(r"\d+\s*(ay|ayl[\u0131i]k)|aydan\s*eski|eski\s*mail|eski\s*mailler", low):
            return None
        try:
            if self._wipe_all(low):
                return None
        except Exception:
            pass
        # baglam: bunu / sunu / bu mail / su mail / "son gosterilen"
        if re.search(r"(bunu|[\u015fs]unu|sunu|bu\s*mail[i]?|[\u015fs]u\s*mail[i]?|"
                     r"az\s*önceki\s*mail)", low):
            try:
                ctx = self.agent.last_mail_ctx()
            except Exception:
                ctx = {"items": []}
            items = ctx.get("items") or []
            if not items or ctx.get("age", 1e9) > 7200:
                return ("ask", None)
            it = items[0]
            if it.get("msg_id"):
                return ("konu_sil", {"msg_id": it["msg_id"]})
            if it.get("subject"):
                return ("konu_sil", {"konu": it["subject"]})
            return ("ask", None)
        # acik konu: "fatura konulu maili sil" / "konusu: fatura ..."
        mk = (re.search(r"(.{2,80}?)\s+konulu\s+mail", low)
              or re.search(r"konu(?:su)?\s*[:=]?\s*[\"']?(.{2,80}?)[\"']?\s+"
                           r"(?:olan\s+)?mail", low)
              or re.search(r"mail[i]?\s+sil\s*[:=]\s*(.{2,80})$", low))
        if mk:
            return ("konu_sil", {"konu": mk.group(1).strip(" '\"")})
        # message-id acikca
        mi = re.search(r"message[-\s]?id\s*[:=]\s*<?([^\s>]+)>?", text or "", re.I)
        if mi:
            return ("konu_sil", {"msg_id": mi.group(1).strip()})
        # en son / son N mail (+ opsiyonel gonderen)
        if re.search(r"(en\s*son|son|en\s*yeni|en\s*g[uü]ncel)\s*(gelen\s*)?"
                     r"(\d+\s*)?mail|son\s*gelen\s*mail", low):
            args = {}
            m = re.search(r"(\d+)\s*(?:adet\s*)?mail", low)
            if m:
                try:
                    args["count"] = max(1, min(int(m.group(1)), 50))
                except Exception:
                    pass
            mf = re.search(r"([\w.\-+]+@[\w.\-]+\.\w+)", text or "")
            if mf:
                args["from"] = mf.group(1)
            return ("son_sil", args)
        # yalniz "maili sil" -> belirsiz, sormak daha guvenli
        if re.search(r"\bmail[i]?\s*(sil|kald[\u0131i]r)", low):
            return ("ask", None)
        return None

    def _ayla_say(self, text):
        """Serbest metni GERCEK modele birak (T2/G3: ezber mail ozetine dusme YOK)."""
        ch = f"tg:{self._chat_id}:ayla" if getattr(self, "_chat_id", None) else None
        try:
            reply, created = self.agent.ayla_reply(text, channel=ch)
        except Exception as exc:
            try:
                self.agent.log("error", "ayla", f"route: {type(exc).__name__}")
                self.agent._salvage_intent(text, f"route hatası: {type(exc).__name__}")
            except Exception:
                pass
            reply, created = ("\u015eu an yan\u0131t \u00fcretemedim the owner.", [])
        head = "\U0001F935 <b>Ayla</b>"
        if created:
            head += "\n" + "\n".join(
                f"\u2705 #{t[0]} kaydettim \u2014 {t[2]} \u2192 <i>{t[1]}</i>"
                for t in created)
        return (head + "\n\n" + (reply or "(bos)"), "ayla")

    def _mail_ask_ok(self, low):
        """Gerçekten mail ÖZETİ/LİSTESİ isteği mi? Olumsuzlama ('mail dışında',
        'mail haric') ve 'mail dışında ne var' gibi ifadeleri dışlar (F2)."""
        if re.search(r"mail\w*\s*(d[ıi][şs][ıi]nda|haric|hari[çc]|olmadan|yok)|"
                     r"(d[ıi][şs][ıi]nda|haric|hari[çc])\s*mail", low):
            return False
        return bool(
            re.search(r"(mail|gelen\s*kutu|inbox|kutum)", low)
            and re.search(r"(bak|kontrol|onemli|önemli|bildir|ozet|özet|"
                          r"var\s*mi|neler|ne\s*var|liste|g[oö]ster)", low)
            and not re.search(r"(gonder|gönder|at\b|yaz)", low))

    _ADDRESS_VERB = (r"(sor|s[öo]yle|yaps[ıi]n|yapt[ıi]r|baks[ıi]n|bakar\s*m[ıi]s[ıi]n|"
                     r"ilet|g[öo]nder|havale\s*et|delege\s*et|dan[ıi][şs]|rica\s*et)")

    def _alias_regex(self, a):
        return re.escape(a)

    def _resolve_target(self, low):
        """K6: acik hitap varsa ajan id'si, yoksa None (modele birakilir).

        Kabul edilen kaliplar (alias kelime sinirinda):
          1) "<alias>'e <fiil>"  (oraya sor/soyle/ilet/gonder/baksin...)
          2) "<alias>, ..." / "<alias>: ..."  (dogrudan hitap)
          3) "<alias> selam/merhaba"
        'bulutlu hava', 'yerel haber', 'ilacimi kontrol et' gibi ifadeler
        bu kaliplarin hicbirine uymaz -> yonlendirme YOK."""
        try:
            done = set()
            for aid, alts in ident.ALIASES.items():
                for a in alts:
                    if a in done:
                        continue
                    done.add(a)
                    ae = self._alias_regex(a)
                    # 1) alias (+ dative/possessive eki) ... fiil
                    rx1 = (r"(?<!\w)" + ae +
                           r"(?:'?[a-zçğıöşü]+)?\s+[^.!?]{0,30}?" +
                           self._ADDRESS_VERB + r"\b")
                    if re.search(rx1, low):
                        return aid
                    # 2) dogrudan hitap: alias + virgul/iki nokta
                    rx2 = r"^\s*" + ae + r"(?:'?[a-zçğıöşü]+)?\s*[,:]"
                    if re.search(rx2, low):
                        return aid
                    # 3) alias + selamlasma
                    rx3 = r"^\s*" + ae + r"\s+(selam|merhaba|hey|hi)\b"
                    if re.search(rx3, low):
                        return aid
            return None
        except Exception:
            return None

    def commander(self, text):
        low = text.lower()
        self._last_call = None

        # === REPLY: bir ajan mesajina yanit -> dogrudan o ajana (kisir dongu biter) ===
        try:
            rc = getattr(self, "_reply_ctx", None)
            if rc:
                out = self._reply_call(rc, text)
                if out:
                    return out
        except Exception:
            pass

        # === IS YUKU / AKTIVITE (F2): sabit tablo YOK. Gerçek veri model bağlamında
        #     (ayla_context: ajan durumları, açık görevler, dev kuyruğu, olaylar).
        #     "evren ne yapıyor" gibi sorular buradan modele gider; sabit metne düşmez. ===
        try:
            if self.agent.workload_intent(low):
                return self._ayla_say(text)
        except Exception:
            pass

        # === AJAN CAGRI: "apiman'a soyle ...", "doktor ... baksin" ===
        try:
            res = self.agent.ajan_metinden_calistir(text, source="telegram")
            if res:
                self._remember_call(res)
                head = self.agent.cap_metni(res)
                return ("\U0001F935 <b>Ayla</b> \u2192 " + head,
                        res.get("agent") or "ayla")
        except Exception:
            pass

        # === BAĞLAM: "bunu yapsın" + son gerçek ajan -> o ajan ===
        try:
            if (getattr(self, "_last_direct_agent", None)
                    and re.search(r"\b(bunu|[şs]unu|sunu|onun?)\b", low)
                    and re.search(r"(yaps[ıi]n|yapacak|halletsin|ilgilensin)", low)):
                out = self._reply_call({"agent": self._last_direct_agent, "topic": ""}, text)
                if out:
                    return out
        except Exception:
            pass

        # === PROJE DEFTERI (deterministik) ===
        try:
            if re.search(r"\bproje", low):
                # yeni proje
                mnew = re.search(r"(?:yeni\s*proje|proje\s*(?:a[cç]|olu[sş]tur|ba[sş]lat))"
                                 r"\s*[:\-]?\s*(.+)", low)
                if mnew:
                    return (self.cmd_proje("yeni " + mnew.group(1).strip()), "ayla")
                if re.search(r"(projeler[ıi]m|proje\s*listesi|hangi\s*projeler|"
                             r"projeler(i)?\s*neler|a[cç][ıi]k\s*projeler|proje\s*var\s*m[ıi])",
                             low):
                    return (self.cmd_proje(""), "ayla")
                mdev = re.search(r"([\w\sçğıöşüİ-]{3,40})\s*projesi.{0,20}"
                                 r"(devam|kald[ıi][gğ][ıi]m[ıi]z|konus)", low)
                if re.search(r"(devam\s*edelim|kald[ıi][gğ][ıi]m[ıi]z\s*yerden|"
                             r"konusuyorduk|konu[sş]uyorduk)", low):
                    ref = mdev.group(1).strip() if mdev else ""
                    return (self.cmd_proje("devam " + ref), "ayla")
                if re.search(r"(mimari|mimar[ıi]|spec|plan|toparla)", low):
                    return (self.cmd_proje("mimari"), "ayla")
                if re.search(r"(evrene|evren'e|evrene)\s*(ilet|g[oö]nder|ver)|"
                             r"(ilet|g[oö]nder)\s*evrene", low):
                    return (self.cmd_proje("ilet"), "ayla")
                if re.search(r"proje\s*(not|ekle)|not\s*(ekle|d[uü][sş])", low):
                    txt = re.sub(r"(?i).*?(proje\s*not|not\s*ekle|not\s*d[uü][sş])"
                                 r"\s*[:\-]?\s*", "", text).strip()
                    if txt:
                        return (self.cmd_proje("not | " + txt), "ayla")
                # "X projesi" -> detay (K6: yalniz acik detay/durum istegi;
                # belirsizse modele birak)
                mpro = re.search(r"([\w\sçğıöşüİ-]{3,40})\s*projesi", low)
                if mpro and re.search(
                        r"(durum|detay|g[öo]ster|ne\s*durumda|anlat|nerede|"
                        r"nas[ıi]l\s*gidiyor|ilerleme)", low):
                    return (self.cmd_proje(mpro.group(1).strip()), "ayla")
        except Exception:
            pass

        # === ETIKETLEME (en once): reklam/pazarlama -> 'Gereksiz' ===
        if re.search(r"(reklam|pazarlama|tan[ıi]t[ıi]m|b[uü]lten|spam|gereksiz)", low) \
                and re.search(r"(etiketle|i[şs]aretle)", low):
            lbl = "Spam" if re.search(r"spam", low) else "Gereksiz"
            r = self.agent.ayla_label_marketing(lbl)
            if r.get("ok"):
                return (f"\U0001F4EC <b>Ayla</b>: <b>{r['labeled']}</b> mail "
                        f"'{lbl}' olarak etiketlendi.\n"
                        f"<i>{r['skipped_personal']} kişisel/kurumsal mail atlandı.</i>",
                        "ayla")
            return (f"\U0001F4EC <b>Ayla</b>: etiketleme başarısız — "
                    f"<code>{r.get('error')}</code>", "ayla")

        # === GEREKSIZ/SPAM TEMIZLE: gerçek arşivci yeteneği (silmez, taşır) ===
        if re.search(r"(gereksiz|spam|reklam|pazarlama)", low) and re.search(
                r"(sil|temizle|kald[ıi]r|ta[şs][ıi])", low):
            return self._call_agent("arsivci", "gereksiz_temizle", text)

        # === MAIL NIYETI (hedeften ONCE): yalnız GERÇEK mail özeti isteği ===
        if self._mail_ask_ok(low):
            m = re.search(r"(\d+)\s*(gun|gün|hafta)", low)
            days = 7
            if m:
                days = int(m.group(1)) * (7 if "hafta" in m.group(2) else 1)
            n = self.agent.scan_once()
            return ("\U0001F935 <b>Ayla</b>\n\n" + self.cmd_important(days) + f"\n\n<i>({n} yeni mail tarandı)</i>", "ayla")

        # === COP/TEMIZLIK niyeti: her zaman Arsivci ===
        if re.search(r"(cop|çöp).*(bosalt|boşalt|temizle)", low):
            return ("\U0001F935 <b>Ayla</b>\n\n" + self.cmd_empty_trash(), "ayla")
        if self._wipe_all(low):
            return self._archive_all()
        # T20: tek/belirli mail silme (bunu/son mail/konulu) — N-ay temizliği DEĞİL
        _sd = self._single_mail_delete(text, low)
        if _sd:
            _cap, _args = _sd
            if _cap == "ask":
                return ("\U0001F935 <b>Ayla</b>\n\nHangi maili silmemi istiyorsunuz? "
                        "Önce mail özetini gösterin ya da konu/ID belirtin. "
                        "Yanlış maili silmemek için işlem yapmadım.", "ayla")
            return self._call_agent("arsivci", _cap, text, args=_args)
        if re.search(r"(sil|temizle)", low) and re.search(
                r"(\d+\s*(ay|ayl[ıi]k)|aydan\s*eski|eski\s*mail)", low):
            m = re.search(r"(\d+)\s*(ay|ayl[ıi]k)", low)
            months = int(m.group(1)) if m else 1
            return ("\U0001F935 <b>Ayla</b>\n\n" + self.cmd_cleanup(months), "ayla")


        # === HEDEF YONLENDIRME: "ciraga soyle ...", "evrene soyle ..."
        # K6: alias yalniz KELIME SINIRINDA ve ACIK iletisim fiili/address ile.
        # "bulutlu hava", "yerel haber" gibi cumleler artik yanlis ajana gitmez.
        target = self._resolve_target(low)

        # Isimle hitap YETERLI: "sef selam", "evren merhaba", "cirak 2+2"
        # Ismi metinden cikar, kalan kismi gorev kabul et
        if target:
            task = self._strip_routing(text)
            task = re.sub(r"(?i)^\s*(selam|merhaba|hey|hi)\b[\s,!.]*", "", task or "").strip()

            # === HAREKET AJANLARI: gercek isi yap (LLM'e gonderme) ===
            low2 = (task or low)
            # === MADDE 6: Cirak the owner konusmaz -> Evren'e yonlendir ===
            if target == "cirak":
                self.agent.log("info", "route", "cirak izole -> evren")
                res = self.agent.ask_cirak(task or text, max_tokens=600,
                                           task="route", force="evren")
                head = ("\U0001F916 <b>Asistan</b>: Çırak the owner çalışmaz, "
                        "işi <b>Evren</b>'e iletiyorum")
                reply = res.get("reply") or "(Evren cevap veremedi)"
                return (f"\U0001F935 <b>Ayla</b>\n\nBu işi <b>Evren</b>'e iletiyorum.\n\n{reply}", "ayla")

            # "asistana soyle X" -> Asistan koordine eder, isi Ayla/Arsivci/Kurye yapar
            if target == "asistan":
                if self._wipe_all(low2):
                    return self._archive_all()
                if self._mail_ask_ok(low2):
                    m = re.search(r"(\d+)\s*(gun|gün|hafta)", low2)
                    days = 7
                    if m:
                        days = int(m.group(1)) * (7 if "hafta" in m.group(2) else 1)
                    n = self.agent.scan_once()
                    return ("\U0001F935 <b>Ayla</b>\n\n" + self.cmd_important(days) + f"\n\n<i>({n} yeni mail tarandı)</i>", "ayla")
                if re.search(r"(sil|temizle|bosalt|boşalt|cop|çöp)", low2):
                    if re.search(r"cop|çöp", low2):
                        _out = self.cmd_empty_trash()
                    else:
                        _sd2 = self._single_mail_delete(text, low2)
                        if _sd2:
                            _cap2, _args2 = _sd2
                            if _cap2 == "ask":
                                return ("\U0001F935 <b>Ayla</b>\n\nHangi maili silmemi "
                                        "istiyorsunuz? Önce mail özetini gösterin ya da "
                                        "konu/ID belirtin.", "ayla")
                            return self._call_agent("arsivci", _cap2, text, args=_args2)
                        if re.search(r"\d+\s*(ay|ayl[ıi]k)|aydan\s*eski|eski\s*mail",
                                     low2):
                            _m = re.search(r"(\d+)\s*(ay|ayl[ıi]k)", low2)
                            _out = self.cmd_cleanup(int(_m.group(1)) if _m else 1)
                        else:
                            _out = self.cmd_cleanup(1)
                    return ("\U0001F935 <b>Ayla</b>\n\n" + _out, "ayla")
                return self._ayla_say(text)
            if target == "ayla":
                # ETIKETLEME istegi -> gercek isi yap
                if re.search(r"(etiketle|i[şs]aretle)", low2):
                    lbl = "Gereksiz"
                    if re.search(r"spam", low2):
                        lbl = "Spam"
                    r = self.agent.ayla_label_marketing(lbl)
                    if r.get("ok"):
                        return (f"\U0001F4EC <b>Ayla</b>: <b>{r['labeled']}</b> mail "
                                f"'{lbl}' olarak etiketlendi.\n"
                                f"<i>{r['skipped_personal']} kişisel/kurumsal mail atlandı"
                                f" (onlara dokunulmadı).</i>", "ayla")
                    return (f"\U0001F4EC <b>Ayla</b>: etiketleme başarısız — "
                            f"<code>{r.get('error')}</code>", "ayla")
                # Ayla'ya mail GONDERME istegi mi? -> Kurye'ye devret
                m_to3 = re.search(r"([\w.\-+]+@[\w.\-]+\.\w+)", text)
                if m_to3 and re.search(r"(?i)(gonder|gönder|mail\s*at|\bat\b|yolla|postala)", low2):
                    out = self.cmd_send_smart(text, m_to3.group(1))
                    return ("\U0001F4EC <b>Ayla</b> → \U0001F4E4 <b>Kurye</b>\n" + out, "ayla")
                if re.search(r"(?i)(onemli|önemli|kritik|acil|bildir|ne\s*var|ozet|özet)", low2) \
                        and re.search(r"(?i)\b(mail|gelen|kutu|inbox)\b", low2) \
                        and self._mail_ask_ok(low2):
                    m = re.search(r"(\d+)\s*(gun|gün|hafta)", low2)
                    days = 7
                    if m:
                        days = int(m.group(1)) * (7 if "hafta" in m.group(2) else 1)
                    return (self.cmd_important(days), "ayla")
                # Acikca mail ozeti degilse: modele birak. Canned mail ozeti YOK (F2).
                if self._mail_ask_ok(low2):
                    n = self.agent.scan_once()
                    return (self.cmd_important(7) + f"\n\n<i>({n} yeni mail tarandı)</i>", "ayla")
                return self._ayla_say(text)
            if target == "arsivci":
                if self._wipe_all(low2):
                    return self._archive_all()
                if re.search(r"(?i)cop|çöp|bosalt|boşalt", low2):
                    return ("\U0001F935 <b>Ayla</b>\n\n" + self.cmd_empty_trash(), "ayla")
                _sd3 = self._single_mail_delete(text, low2)
                if _sd3:
                    _cap3, _args3 = _sd3
                    if _cap3 == "ask":
                        return ("\U0001F935 <b>Ayla</b>\n\nHangi maili silmemi "
                                "istiyorsunuz? Önce mail özetini gösterin ya da "
                                "konu/ID belirtin.", "ayla")
                    return self._call_agent("arsivci", _cap3, text, args=_args3)
                m = re.search(r"(\d+)\s*(ay|ayl[ıi]k)", low2)
                months = int(m.group(1)) if m else 1
                return ("\U0001F935 <b>Ayla</b>\n\n" + self.cmd_cleanup(months), "ayla")
            if target == "kurye":
                m_to2 = re.search(r"([\w.\-+]+@[\w.\-]+\.\w+)", text)
                if m_to2:
                    # Kurye = Ayla'nin yardimcisi. Sonuc Ayla adina raporlanir.
                    out = self.cmd_send_smart(text, m_to2.group(1))
                    return (f"\U0001F4EC <b>Ayla</b> → \U0001F4E4 <b>Kurye</b>\n{out}", "ayla")
            if not task and not re.search(r"(s[\u00f6o]yle|yap|delege|havale|ilet|iste|rica)", low):
                # Sadece selamlama -> kisa karsilik, kendini tanitma YOK
                task = "Kullanici selam verdi. Tek kelimeyle karsilik ver."
            label = ident.name_of(target)
            self.agent.log("info", "route", f"zorunlu hedef={target} | gorev={task[:50]}")
            # Isim gecse de TEK CEVAPLAYICI Ayla'dir: cevap Ayla imzali doner.
            res = self.agent.ask_cirak(task, max_tokens=600, task="route", force=target)
            if not res.get("reply"):
                return ("\U0001F935 <b>Ayla</b>\n\n<i>(boş cevap)</i>", "ayla")
            notu = f"\U0001F935 <b>Ayla</b>\n\n<b>{label}</b> notu:\n{res['reply']}"
            if res.get("escalated"):
                notu = ("\U0001F935 <b>Ayla</b>\n\n<b>Evren</b> notu "
                        f"<i>(Çırak takıldı)</i>:\n{res['reply']}")
            return (notu, "ayla")

        # === MAIL GONDERME (cekimli fiiller dahil) ===
        m_to = re.search(r"([\w.\-+]+@[\w.\-]+\.\w+)", text)
        send_intent = re.search(
            r"(mail\w*\s*(at|g[\u00f6o]nder)"
            r"|g[\u00f6o]nder\w*\s*(mail|e-?posta)"
            r"|e-?posta\w*\s*(at|g[\u00f6o]nder)"
            r"|postala|mail\w*\s*yolla|yolla\w*\s*mail)", low)
        if m_to and send_intent:
            out = self.cmd_send_smart(text, m_to.group(1))
            return ("\U0001F4EC <b>Ayla</b> → \U0001F4E4 <b>Kurye</b>\n" + out, "ayla")

        # === KUTU DOLULUGU ===
        if re.search(r"kutu", low) and re.search(r"(dolu|doluluk|boyut|ka[\u00e7c]\s*mail|stats)", low):
            return ("\U0001F935 <b>Ayla</b>\n\n" + self.cmd_mailbox(), "ayla")

        # === DURUM: serbest metin sabit metne düşmez (F2). /durum slash komutu kalır. ===

        # === TEMIZLIK (kelime sinirli: "nasil" icindeki "sil" yakalanmaz) ===
        if re.search(r"(?<![a-z\u00e7\u011f\u0131\u00f6\u015f\u00fc])(sil|temizle|bo[\u015fs]alt|bosalt)(?![a-z\u00e7\u011f\u0131\u00f6\u015f\u00fc])", low):
            if self._wipe_all(low):
                return self._archive_all()
            if re.search(r"[\u00e7c][\u00f6o]p\s*kutu", low) and not re.search(r"\d", low):
                return (self.cmd_empty_trash(), "arsiv")
            _sd4 = self._single_mail_delete(text, low)
            if _sd4:
                _cap4, _args4 = _sd4
                if _cap4 == "ask":
                    return ("\U0001F935 <b>Ayla</b>\n\nHangi maili silmemi istiyorsunuz? "
                            "Önce mail özetini gösterin ya da konu/ID belirtin. "
                            "Yanlış maili silmemek için işlem yapmadım.", "ayla")
                return self._call_agent("arsivci", _cap4, text, args=_args4)
            m = re.search(r"(son\s*)?(\d+)\s*(ay|ayl[\u0131i]k)", low)
            months = int(m.group(2)) if m else 1
            return (self.cmd_cleanup(months), "arsiv")

        # === ONEMLI MAIL: yalnız GERÇEK mail özeti isteği (negasyon hariç) ===
        if self._mail_ask_ok(low):
            m = re.search(r"(\d+)\s*(g[\u00fcu]n|hafta)", low)
            days = 7
            if m:
                days = int(m.group(1)) * (7 if "hafta" in m.group(2) else 1)
            return ("\U0001F935 <b>Ayla</b>\n\n" + self.cmd_important(days), "ayla")

        # === TARAMA (K6): yalniz ACIK mail tarama komutu. "yenile/kontrol et"
        # artik IMAP taramasi tetiklemez; belirsizse modele birakilir. ===
        if self._is_mail_scan_request(low):
            n = self.agent.scan_once()
            return (f"\U0001F935 <b>Ayla</b>\n\nTarama tamam: <b>{n}</b> yeni mail.", "ayla")

        # === SERBEST METIN -> Ayla (tek kapi, tek cevaplayici) ===
        return self._ayla_say(text)

    def _strip_routing(self, text):
        """'ciraga soyle: X' -> 'X'"""
        t = text
        t = re.sub(r"^.*?(?:[çc][ıi]ra[ğgk]|[eE]vren)\s*(?:a|e|ya|ye)?\s*"
                   r"(?:s[öo]yle|yaps[ıi]n|yap|delege\s*et|havale\s*et|g[öo]nder|ilet)"
                   r"\s*[:,\-]?\s*", "", t, flags=re.I)
        return t.strip(" :,-")

    # ================= TAM YETKILI AJAN MODU =================
    def _run(self, cmd, timeout=60, cwd=None):
        import subprocess
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True,
                               text=True, timeout=timeout, cwd=cwd)
            out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
            return out.strip() or "(cikti yok)"
        except subprocess.TimeoutExpired:
            return f"(zaman asimi: {timeout}s)"
        except Exception as ex:
            return f"(hata: {type(ex).__name__})"

    def cmd_shell(self, cmd):
        # GUVENLIK: komut metni ve cikti panel event'lerine YAZILMAZ.
        # Yalniz guvenli meta (kategori, exit durumu) loglanir.
        import time as _t
        t0 = _t.time()
        out = self._run(cmd, timeout=90, cwd="/home/ubuntu/evren")
        dur = round(_t.time() - t0, 1)
        ok = not (out.startswith("(hata:") or out.startswith("(zaman asimi"))
        self.agent.log("info" if ok else "warn", "shell-cmd",
                       f"{'ok' if ok else 'fail'} ({dur}s)")
        return f"<b>$ {cmd}</b>\n```\n{out[:3500]}\n```"

    def cmd_read(self, path):
        try:
            if not path.startswith("/"):
                path = "/home/ubuntu/evren/" + path
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = f.read()
            return f"<b>{path}</b>\n```\n{data[:3500]}\n```"
        except Exception as ex:
            return f"okunamadi: {ex}"

    def cmd_write(self, rest):
        if "|" not in rest:
            return "Format: /yaz <yol> | <icerik>"
        path, content = rest.split("|", 1)
        path, content = path.strip(), content.strip()
        try:
            if not path.startswith("/"):
                path = "/home/ubuntu/evren/" + path
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"yazildi: {path} ({len(content)} karakter)"
        except Exception as ex:
            return f"yazilamadi: {ex}"

    def cmd_service(self, parts):
        if len(parts) < 1:
            return "Format: /servis <ad> [start|stop|restart|status]"
        name = parts[0]
        action = parts[1] if len(parts) > 1 else "status"
        if action not in ("start", "stop", "restart", "status"):
            return "gecersiz islem"
        out = self._run(f"sudo systemctl {action} {name} 2>&1; sudo systemctl is-active {name}")
        return f"<b>{name} {action}</b>\n<code>{out[:600]}</code>"

    def cmd_agent_mode(self, task):
        """Evren'e tam baglamla gorev ver: sistem bilgisi + arac listesi."""
        if not task:
            return ("<b>Ajan modu</b>\n"
                    "/agent &lt;gorev&gt; — Evren'e sistem baglamiyla gorev ver\n"
                    "Ornek: /agent disk dolulugunu kontrol et ve ozetle")
        ctx = self._run(
            "echo '--- UPTIME ---'; uptime; "
            "echo '--- DISK ---'; df -h / | tail -1; "
            "echo '--- RAM ---'; free -m | head -2; "
            "echo '--- SERVISLER ---'; "
            "for s in evren-agent evren-tunnel evren-keepalive ollama; do "
            "printf '%s=%s ' $s $(systemctl is-active $s); done; echo; "
            "echo '--- EVREN DOSYALARI ---'; ls ~/evren", timeout=30)
        prompt = (
            "Sen Oracle sunucusunda calisan bir sistem ajanisin. "
            "Asagida sistem durumu ve kullanicinin gorevi var. "
            "Kisa, net, teknik cevap ver. Gerekirse komut oner.\n\n"
            f"SISTEM:\n{ctx}\n\nGOREV: {task}"
        )
        res = self.agent.ask_cirak(prompt, max_tokens=700, task="agent")
        return res.get("reply") or "(cevap yok)"

    def cmd_keys(self, arg):
        a = self.agent
        if arg.isdigit():
            idx = int(arg) - 1
            ok = a.switch_key(idx)
            if not ok:
                return f"Anahtar {arg} aktif edilemedi (indeks 1-{len(a._KEYS)})."
        q = a.quota_all()
        lines = ["<b>API Anahtarları</b>\n"]
        for k in q["keys"]:
            mark = "✅ AKTİF" if k["active"] else ""
            rem = k["daily_remaining"]
            rem_s = f"{rem:,}".replace(",", ".") if rem is not None else "-"
            lim = k["daily_limit"] or 15000000
            lines.append(f"{k['label']}: <b>{rem_s}</b> / {lim:,}".replace(",", ".") +
                         f" · kredi {k['credits']} {mark}")
        lines.append("\n<i>/anahtar 1|2|3 → geç</i>")
        return "\n".join(lines)

    def cmd_send_smart(self, text, to):
        """Serbest metinden alici+konu+mesaj cikarip mail atar (cirak ile)."""
        # "sonraki" ifadesini at, alicidan sonrasini mesaj kabul et
        m = re.search(re.escape(to) +
                      r"(?:['\u2019](?:a|e|ya|ye|n[ae]|n[\u0131i]n))?\s*[,:\-]?\s*(.*)$",
                      text, re.S)
        rest = (m.group(1) if m else "").strip()
        rest = re.sub(r"^(mail\s*(at|g[öo]nder)[:\s]*|e-?posta\s*(at|g[öo]nder)[:\s]*)", "",
                      rest, flags=re.I).strip()
        rest = re.sub(r"^(adres\w*|hesab\w*)\s*(mail\s*at\w*|e-?posta\s*g[\u00f6o]nder\w*)?\s*[:,]?\s*", "", rest, flags=re.I).strip()
        if rest.lower().startswith("mail at"):
            rest = re.sub(r"^mail\s*at\w*\s*[:,]?\s*", "", rest, flags=re.I).strip()
        if not rest:
            return ("Alici bulundu: <code>%s</code>\n"
                    "Konu ve mesaj icin formatı kullan:\n"
                    "<code>/gonder %s | Konu | Mesaj</code>" % (to, to))
        # Konu/mesaj ayirici varsa kullan
        parts = [p.strip() for p in re.split(r"[|;]", rest)]
        if len(parts) >= 2:
            subject, body = parts[0], " ".join(parts[1:])
        else:
            # tek blok -> konu: ilk cumle, mesaj: tamami
            subject = (rest[:60].split(".")[0] or "Mesaj").strip()
            body = rest
        r = self.agent.actions.send_mail(self.agent.CFG, to, subject, body)
        if r.get("ok"):
            self.agent.log("ok", "mail-send", f"gonderildi -> {to} | {subject[:40]}")
            return (f"Mail gonderildi\n"
                    f"Kimden: <code>{r['from']}</code>\n"
                    f"Kime: <code>{r['to']}</code>\n"
                    f"Konu: {r['subject']}\n\n"
                    f"<i>{body[:200]}</i>")
        self.agent.log("error", "mail-send", f"{to}: {r.get('error')}")
        return f"Gonderilemedi: <code>{r.get('error')}</code>"


def start(cfg, agent):
    """agent.py tarafindan cagrilir."""
    token = cfg.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        try:
            agent.log("warn", "telegram", "TELEGRAM_BOT_TOKEN bos, kanal kapali")
        except Exception:
            pass
        return None
    chat_id = cfg.get("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        try:
            agent.log("warn", "telegram",
                      "TELEGRAM_CHAT_ID bos -> bot CALISTIRILMADI (fail-closed)")
        except Exception:
            pass
        return None
    bot = TelegramBot(token, chat_id, agent)
    threading.Thread(target=bot.run, daemon=True).start()
    return bot
