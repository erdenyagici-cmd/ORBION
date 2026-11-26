"""
actions.py — Gercek mail aksiyonlari (IMAP)
--------------------------------------------
Guvenli varsayilan: silme = Trash'e tasima (kalici degil).
Kalici silme sadece acik onay parametresiyle.

Fonksiyonlar:
  list_important(days)      onemli mailleri listeler
  cleanup_older(months)     N aydan eski mailleri Trash'e tasir
  empty_trash(confirm)      Trash'i kalici bosaltir
  mailbox_stats()           klasor doluluk ozeti
  ensure_folder(name)       klasor yoksa olusturur
  move_spam(folder)         spam'lari Gereksizler'e tasir
  folder_count(name)        klasordeki mail sayisi
"""

import base64
import imaplib
import json
import logging
import os
import re
import email
import threading
from email.header import decode_header, make_header
from datetime import datetime, timedelta
from pathlib import Path

_LOG = logging.getLogger("evren.actions")

# T21: SMTP gunluk gonderim sayaci (sir yok; yalniz tarih+sayi).
_SMTP_LOCK = threading.Lock()


def _copy_then_delete(m, seq, mailbox):
    """IMAP COPY sonucu OK ise kaynagi '\\Deleted' isaretle. -> (ok, hata).

    K1: Copy basarisiz/NO donerse ASLA silme. Aksi halde mail hem hedefe
    gitmez hem kaynaktan kalici silinir (veri kaybi)."""
    try:
        typ, _data = m.copy(seq, mailbox)
    except Exception as exc:
        return False, f"copy_exc:{type(exc).__name__}"
    if typ != "OK":
        return False, f"copy_{typ}"
    try:
        styp, _sdata = m.store(seq, "+FLAGS", "(\\Deleted)")
    except Exception as exc:
        return False, f"store_exc:{type(exc).__name__}"
    if styp != "OK":
        return False, f"store_{styp}"
    return True, ""


def _smtp_counter_file():
    return Path(__file__).resolve().parent.parent / "data" / "smtp_counter.json"


def _smtp_read_unlocked():
    try:
        d = json.loads(_smtp_counter_file().read_text(encoding="utf-8"))
        if d.get("date") == datetime.now().date().isoformat():
            return int(d.get("count") or 0)
    except Exception:
        pass
    return 0


def _smtp_write_unlocked(n):
    f = _smtp_counter_file()
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"date": datetime.now().date().isoformat(),
                                   "count": int(n)}), encoding="utf-8")
        os.replace(tmp, f)
    except Exception:
        pass


def smtp_sent_today(cfg=None):
    with _SMTP_LOCK:
        return _smtp_read_unlocked()


def _smtp_reserve(cfg):
    """Gonderim oncesi rezervasyon: limit doluysa False (ASLA asmaz)."""
    try:
        limit = int((cfg or {}).get("LIMIT_GMAIL_SEND", "500") or 500)
    except Exception:
        limit = 500
    with _SMTP_LOCK:
        n = _smtp_read_unlocked()
        if n >= limit:
            return False, n, limit
        n += 1
        _smtp_write_unlocked(n)
        return True, n, limit


def _smtp_release(cfg):
    with _SMTP_LOCK:
        n = _smtp_read_unlocked()
        if n > 0:
            _smtp_write_unlocked(n - 1)


def _connect(cfg):
    host = cfg.get("IMAP_HOST", "imap.gmail.com")
    port = int(cfg.get("IMAP_PORT", "993") or 993)
    user = cfg.get("GMAIL_ADDRESS", "").strip()
    pwd = cfg.get("GMAIL_APP_PASSWORD", "").strip()
    if not user or not pwd:
        raise RuntimeError("Gmail bilgileri eksik (.env)")
    m = imaplib.IMAP4_SSL(host, port)
    m.login(user, pwd)
    return m


def _find_trash(m):
    """Gmail Trash klasor adini bul ([Gmail]/Trash, /Çöp kutusu, /Bin vb)."""
    try:
        typ, boxes = m.list()
        for b in boxes or []:
            line = b.decode("utf-8", "ignore")
            low = line.lower()
            if any(k in low for k in ("trash", "\\bin", "çöp", "cop kutusu", "bin")):
                mm = re.search(r'"([^"]+)"$', line)
                if mm:
                    return mm.group(1)
    except Exception:
        pass
    return "[Gmail]/Trash"

def _dh(v):
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return v or ""


def _since_str(days):
    d = datetime.now() - timedelta(days=days)
    return d.strftime("%d-%b-%Y")


def list_important(cfg, days=7, limit=20):
    """Son N gundeki onemli (kritik anahtar kelimeli) mailleri dondurur."""
    CRIT = [
        r"g[üu]venlik", r"security", r"fatura", r"invoice", r"ödem", r"odem",
        r"bor[çc]", r"[şs]ifre", r"password", r"do[ğg]rulama", r"verification",
        r"hesab", r"account", r"acil", r"urgent", r"son\s*uyar", r"iade", r"refund",
        r"randevu", r"s[öo]zle[şs]me", r"vergi", r"banka", r"kargo", r"teslimat",
    ]
    m = _connect(cfg)
    out = []
    try:
        m.select("INBOX", readonly=True)
        typ, ids = m.search(None, f'(SINCE {_since_str(days)})')
        id_list = ids[0].split() if ids and ids[0] else []
        for i in reversed(id_list[-400:]):
            typ, data = m.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if typ != "OK" or not data or not data[0]:
                continue
            msg = email.message_from_bytes(data[0][1])
            subj = _dh(msg.get("Subject", ""))
            frm = _dh(msg.get("From", ""))
            if not any(re.search(p, subj + " " + frm, re.I) for p in CRIT):
                continue
            out.append({
                "date": _dh(msg.get("Date", "")),
                "from": frm[:70],
                "subject": subj[:120],
                "uid": i.decode(),
                "msg_id": msg.get("Message-ID", ""),
            })
            if len(out) >= limit:
                break
    finally:
        try:
            m.logout()
        except Exception:
            pass
    return out


def cleanup_older(cfg, months=1):
    """N aydan eski INBOX maillerini [Gmail]/Trash'e tasir (kalici silmez)."""
    cutoff = datetime.now() - timedelta(days=months * 30)
    before = cutoff.strftime("%d-%b-%Y")
    m = _connect(cfg)
    moved = 0
    errors = 0
    try:
        m.select("INBOX", readonly=False)
        typ, ids = m.search(None, f'(BEFORE {before})')
        id_list = ids[0].split() if ids and ids[0] else []
        if not id_list:
            return {"ok": True, "moved": 0, "errors": 0}
        # Gmail: Trash klasorune kopyala + kaynaktan sil (yalniz copy OK ise)
        chunk = 50
        trash = '"' + _find_trash(m) + '"'
        for k in range(0, len(id_list), chunk):
            part = id_list[k:k + chunk]
            seq = b",".join(part)
            ok, err = _copy_then_delete(m, seq, trash)
            if ok:
                moved += len(part)
            else:
                errors += len(part)
                _LOG.warning("cleanup_older: copy basarisiz (%s), %d mail silinmedi",
                             err, len(part))
        if moved:
            m.expunge()
    finally:
        try:
            m.logout()
        except Exception:
            pass
    if errors:
        _LOG.warning("cleanup_older: %d basarili, %d hata", moved, errors)
    return {"ok": errors == 0, "moved": moved, "errors": errors}


def _hdr(msg):
    """Header'dan guvenli (subject, from, date, msg_id) ozeti."""
    return {
        "subject": _dh(msg.get("Subject", ""))[:120],
        "from": _dh(msg.get("From", ""))[:80],
        "date": _dh(msg.get("Date", "")),
        "msg_id": msg.get("Message-ID", "") or "",
    }


def trash_mail(cfg, count=1, frm=None, subject=None, msg_id=None,
               folder="INBOX", scan=500):
    """INBOX'taki BELIRLI/EN YENI mail(ler)i [Gmail]/Trash'e tasir (KALICI SILMEZ).

    - count: tasinacak en fazla mail (varsayilan 1, ust sinir 50)
    - frm: gonderen adres/adi icinde gecmeli (opsiyonel filtre)
    - subject: konu icinde gecmeli (opsiyonel filtre)
    - msg_id: tam Message-ID eslesmesi (opsiyonel, en kesin)
    Eslesme yoksa hicbir sey tasinmaz; 'bunu sil' baglami cagiran tarafta cozulur.
    Donus: {ok, moved, items:[{subject,from,date,msg_id,uid}], trash, error?}"""
    try:
        count = max(1, min(int(count or 1), 50))
    except Exception:
        count = 1
    frm = (frm or "").strip().lower() or None
    subject = (subject or "").strip().lower() or None
    msg_id = (msg_id or "").strip() or None
    m = _connect(cfg)
    moved, trash, errors = [], "", 0
    try:
        typ, _ = m.select('"' + folder + '"', readonly=False)
        if typ != "OK":
            return {"ok": False, "moved": 0, "items": [],
                    "error": f"klasor acilamadi: {folder}"}
        trash = _find_trash(m)
        typ, ids = m.search(None, "ALL")
        id_list = ids[0].split() if ids and ids[0] else []
        if not id_list:
            return {"ok": True, "moved": 0, "items": [], "trash": trash}

        # Aday listesi: msg_id -> kesin HEADER aramasi (eski mail de bulunur);
        # aksi halde en yeni `scan` kadar + (varsa) konu aramasi birlesimi.
        cand = set()
        if msg_id:
            try:
                _t, _d = m.search(None, "HEADER", "Message-ID", msg_id)
                cand |= set((_d[0].split() if _d and _d[0] else []))
            except Exception:
                pass
        if not cand:
            cand |= set(id_list[-max(scan, count):])
            if subject and all(ord(ch) < 128 for ch in subject):
                try:
                    _t, _d = m.search(None, "SUBJECT", '"' + subject + '"')
                    cand |= set((_d[0].split() if _d and _d[0] else []))
                except Exception:
                    pass
        try:
            cand = sorted(cand, key=lambda x: int(x), reverse=True)
        except Exception:
            cand = list(cand)

        seq_ids = []
        for i in cand:   # en yeniden eskiye
            if len(seq_ids) >= count:
                break
            typ, data = m.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
            if typ != "OK" or not data or not data[0]:
                continue
            msg = email.message_from_bytes(data[0][1])
            h = _hdr(msg)
            if msg_id and h["msg_id"].strip() != msg_id:
                continue
            if frm and frm not in h["from"].lower():
                continue
            if subject and subject not in h["subject"].lower():
                continue
            h["uid"] = i.decode()
            seq_ids.append((i, h))
        for i, h in seq_ids:
            ok, err = _copy_then_delete(m, i, '"' + trash + '"')
            if ok:
                moved.append(h)
            else:
                errors += 1
                _LOG.warning("trash_mail: copy basarisiz (%s), mail silinmedi: %s",
                             err, (h.get("subject") or "")[:60])
        if moved:
            try:
                m.expunge()
            except Exception:
                pass
    finally:
        try:
            m.logout()
        except Exception:
            pass
    res = {"ok": errors == 0, "moved": len(moved), "items": moved,
           "trash": trash, "errors": errors}
    if errors and not moved:
        res["error"] = "copy basarisiz; hicbir mail silinmedi"
    return res


def purge_test_messages(cfg, msg_ids, folder=None):
    """Yalniz test temizligi: verilen Message-ID'leri Trash'ten KALICI siler.
    Uretim akisinda kullanilmaz (normal silme = Trash'e tasima)."""
    if not msg_ids:
        return 0
    m = _connect(cfg)
    deleted = 0
    try:
        target = '"' + (folder or _find_trash(m)) + '"'
        typ, _ = m.select(target, readonly=False)
        if typ != "OK":
            return 0
        for mid in msg_ids:
            try:
                typ, data = m.search(None, "HEADER", "Message-ID", mid)
                seq = (data[0].split() if data and data[0] else [])
                if seq:
                    m.store(b",".join(seq), "+FLAGS", "(\\Deleted)")
                    deleted += len(seq)
            except Exception:
                continue
        m.expunge()
    finally:
        try:
            m.logout()
        except Exception:
            pass
    return deleted


def archive_all_inbox(cfg, folder="Arsiv_EVREN"):
    """TÜM gelen kutusunu ARŞİVLER — KALICI SİLMEZ (geri alınabilir).
    Gmail: '\\Inbox' etiketini kaldırır (Tüm Postalar'da kalır).
    Değilse: hedef klasöre kopyalar, sonra INBOX'tan çıkarır (kopya korunur).
    Dönüş: {ok, moved, mode} veya {ok:False,error}."""
    m = _connect(cfg)
    moved = 0
    errors = 0
    mode = ""
    try:
        typ, _ = m.select("INBOX", readonly=False)
        if typ != "OK":
            return {"ok": False, "error": "INBOX acilamadi"}
        typ, ids = m.search(None, "ALL")
        id_list = ids[0].split() if ids and ids[0] else []
        if not id_list:
            return {"ok": True, "moved": 0, "mode": "bos"}
        gmail = False
        try:
            _t, caps = m.capability()
            gmail = b"X-GM-EXT-1" in (caps or b"")
        except Exception:
            gmail = False
        if gmail:
            # Gmail arşiv: INBOX etiketini kaldır (silme yok)
            for k in range(0, len(id_list), 50):
                part = id_list[k:k + 50]
                seq = b",".join(part)
                try:
                    styp, _sdata = m.store(seq, "-X-GM-LABELS", "(\\Inbox)")
                except Exception as exc:
                    styp = f"exc:{type(exc).__name__}"
                if styp == "OK":
                    moved += len(part)
                else:
                    errors += len(part)
                    _LOG.warning("archive_all_inbox(gmail): %d mail arsivlenemedi (%s)",
                                 len(part), styp)
            mode = "gmail-archive"
        else:
            # güvenli: önce hedef klasöre kopyala, sonra INBOX'tan çıkar
            enc = _imap_utf7_encode(folder)
            try:
                m.create('"' + enc + '"')
            except Exception:
                pass
            for k in range(0, len(id_list), 50):
                part = id_list[k:k + 50]
                seq = b",".join(part)
                ok, err = _copy_then_delete(m, seq, '"' + enc + '"')
                if ok:
                    moved += len(part)
                else:
                    errors += len(part)
                    _LOG.warning("archive_all_inbox: copy basarisiz (%s), "
                                 "%d mail silinmedi", err, len(part))
            if moved:
                m.expunge()
            mode = "copy-" + folder
    finally:
        try:
            m.logout()
        except Exception:
            pass
    res = {"ok": errors == 0, "moved": moved, "mode": mode, "errors": errors}
    if errors and not moved:
        res["error"] = "copy basarisiz; hicbir mail silinmedi"
    return res


def empty_trash(cfg, confirm=False):
    """[Gmail]/Trash'i KALICI bosaltir. confirm=True olmadan calismaz."""
    if not confirm:
        return {"ok": False, "error": "Onay gerekli (confirm=true)"}
    m = _connect(cfg)
    deleted = 0
    try:
        trash = _find_trash(m)
        typ, _ = m.select('"' + trash + '"', readonly=False)
        if typ != "OK":
            return {"ok": False, "error": f"Trash klasoru acilamadi: {trash}"}
        typ, ids = m.search(None, "ALL")
        id_list = ids[0].split() if ids and ids[0] else []
        for k in range(0, len(id_list), 50):
            seq = b",".join(id_list[k:k + 50])
            m.store(seq, "+FLAGS", "(\\Deleted)")
            deleted += len(id_list[k:k + 50])
        m.expunge()
    finally:
        try:
            m.logout()
        except Exception:
            pass
    return {"ok": True, "deleted": deleted}


def _imap_utf7_decode(name: str) -> str:
    """IMAP modified UTF-7 -> normal metin (Gmail klasor adlari icin)."""
    try:
        out = []
        i = 0
        while i < len(name):
            c = name[i]
            if c == "&":
                j = name.find("-", i)
                if j == -1:
                    out.append(name[i:]); break
                chunk = name[i + 1:j]
                if chunk == "":
                    out.append("&")
                else:
                    b64 = chunk.replace(",", "/")
                    pad = "=" * (-len(b64) % 4)
                    try:
                        out.append(base64.b64decode(b64 + pad).decode("utf-16-be"))
                    except Exception:
                        out.append(name[i:j + 1])
                i = j + 1
            else:
                out.append(c)
                i += 1
        return "".join(out)
    except Exception:
        return name


def mailbox_stats(cfg):
    m = _connect(cfg)
    res = {}
    try:
        typ, boxes = m.list()
        for b in boxes or []:
            line = b.decode("utf-8", "ignore")
            mm = re.search(r'"([^"]+)"$', line)
            raw_name = mm.group(1) if mm else line.split()[-1]
            name = _imap_utf7_decode(raw_name)
            try:
                t, _ = m.select(f'"{raw_name}"', readonly=True)
                if t == "OK":
                    t2, cnt = m.search(None, "ALL")
                    n = len(cnt[0].split()) if cnt and cnt[0] else 0
                    res[name] = n
            except Exception:
                continue
    finally:
        try:
            m.logout()
        except Exception:
            pass
    return res
# ------------------------------------------------------------------ Gereksizler (spam) kurali
# Guclu spam sinyalleri (kumar/bahis + tipik kampanya dili). Yanlis pozitif olmasin
# diye "kisi/kurum" korunur: kritik (banka/fatura/guvenlik) mailler ASLA tasinmaz.
SPAM_STRONG = [
    r"kumar", r"bahis", r"casino", r"kazino", r"slot\s*(oyun|bonus)", r"bet\b",
    r"betting", r"poker", r"rulet", r"jackpot", r"canl[ıi]\s*bahis",
]
SPAM_PROMO = [
    r"bedava\s*bonus", r"ho[şs]geldin\s*bonusu", r"deneme\s*bonusu",
    r"free\s*spin", r"promosyon\s*kodu", r"yat[ıi]r[ıi]m\s*bonusu",
    r"hemen\s*kazan", r"para\s*kazan", r"kazanmak\s*i[çc]in",
]
SPAM_SAFE = [
    r"g[üu]venlik", r"security", r"fatura", r"invoice", r"[öo]dem", r"bor[çc]",
    r"[şs]ifre", r"password", r"do[ğg]rulama", r"verification", r"banka",
    r"randevu", r"vergi", r"kargo", r"teslimat", r"ekstre", r"hesab",
]


def _imap_utf7_encode(text: str) -> str:
    """Normal metin -> IMAP modified UTF-7 (Turkce klasor adlari icin)."""
    out, buf = [], []

    def flush():
        if buf:
            raw = "".join(buf).encode("utf-16-be")
            b64 = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
            out.append("&" + b64 + "-")
            buf.clear()

    for ch in text:
        o = ord(ch)
        if 0x20 <= o <= 0x7E:
            flush()
            out.append("&" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def _folder_index(m):
    """(raw_name, decoded_name) listesi dondurur."""
    res = []
    try:
        typ, boxes = m.list()
        for b in boxes or []:
            line = b.decode("utf-8", "ignore")
            mm = re.search(r'"([^"]+)"$', line)
            raw = mm.group(1) if mm else line.split()[-1]
            res.append((raw, _imap_utf7_decode(raw)))
    except Exception:
        pass
    return res


def ensure_folder(cfg, name="Gereksizler"):
    """Klasor yoksa olusturur. Var olan klasore dokunmaz."""
    m = _connect(cfg)
    try:
        for _raw, dec in _folder_index(m):
            if dec.strip().lower() == name.strip().lower():
                return {"ok": True, "created": False, "name": name}
        enc = _imap_utf7_encode(name)
        typ, data = m.create('"' + enc + '"')
        if typ != "OK":
            return {"ok": False, "error": f"klasor olusturulamadi: {data}"}
        return {"ok": True, "created": True, "name": name}
    finally:
        try:
            m.logout()
        except Exception:
            pass


def _spam_whitelist(cfg):
    raw = (cfg.get("MAIL_WHITELIST", "") or "").strip()
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def move_spam(cfg, folder="Gereksizler", days=45, dry_run=False, limit=200):
    """INBOX'taki spam'lari 'folder'a tasir. Kisileri/kritik mailleri korur."""
    wl = _spam_whitelist(cfg)
    m = _connect(cfg)
    moved, scanned, examples, errors = 0, 0, [], 0
    try:
        raw_target = None
        for raw, dec in _folder_index(m):
            if dec.strip().lower() == folder.strip().lower():
                raw_target = raw
                break
        if not raw_target:
            return {"ok": False, "error": f"klasor yok: {folder}"}

        typ, _ = m.select("INBOX", readonly=bool(dry_run))
        if typ != "OK":
            return {"ok": False, "error": "INBOX acilamadi"}

        typ, ids = m.search(None, f"(SINCE {_since_str(days)})")
        id_list = ids[0].split() if ids and ids[0] else []
        targets = []
        for i in reversed(id_list[-800:]):
            if len(targets) >= limit:
                break
            typ, data = m.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if typ != "OK" or not data or not data[0]:
                continue
            scanned += 1
            msg = email.message_from_bytes(data[0][1])
            subj = _dh(msg.get("Subject", ""))
            frm = _dh(msg.get("From", ""))
            blob = (subj + " " + frm).lower()

            if any(w and w in blob for w in wl):
                continue
            if any(re.search(p, blob, re.I) for p in SPAM_SAFE):
                continue
            strong = any(re.search(p, blob, re.I) for p in SPAM_STRONG)
            promo = any(re.search(p, blob, re.I) for p in SPAM_PROMO)
            if not strong and not promo:
                continue
            targets.append(i)
            if len(examples) < 6:
                examples.append({"from": frm[:60], "subject": subj[:90]})

        if not dry_run and targets:
            chunk = 50
            for k in range(0, len(targets), chunk):
                part = targets[k:k + chunk]
                seq = b",".join(part)
                ok, err = _copy_then_delete(m, seq, '"' + raw_target + '"')
                if ok:
                    moved += len(part)
                else:
                    errors += len(part)
                    _LOG.warning("move_spam: copy basarisiz (%s), %d mail silinmedi",
                                 err, len(part))
            if moved:
                try:
                    m.expunge()
                except Exception:
                    pass
        else:
            moved = len(targets) if dry_run else 0
    finally:
        try:
            m.logout()
        except Exception:
            pass
    return {"ok": errors == 0, "moved": moved, "scanned": scanned,
            "examples": examples, "dry_run": bool(dry_run), "folder": folder,
            "errors": errors}


def folder_count(cfg, folder="Gereksizler"):
    m = _connect(cfg)
    try:
        for raw, dec in _folder_index(m):
            if dec.strip().lower() == folder.strip().lower():
                typ, _ = m.select('"' + raw + '"', readonly=True)
                if typ != "OK":
                    return None
                typ, cnt = m.search(None, "ALL")
                return len(cnt[0].split()) if cnt and cnt[0] else 0
        return None
    finally:
        try:
            m.logout()
        except Exception:
            pass


# ------------------------------------------------------------------ SMTP gonderim
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr, formatdate, make_msgid


def send_mail(cfg, to, subject, body, html=None):
    """Gmail SMTP ile mail gonderir. Gonderici adi: ORBION_AI_NAME (varsayilan Orbion_Assistant)."""
    host = cfg.get("SMTP_HOST", "smtp.gmail.com")
    port = int(cfg.get("SMTP_PORT", "465") or 465)
    user = cfg.get("GMAIL_ADDRESS", "").strip()
    pwd = cfg.get("GMAIL_APP_PASSWORD", "").strip()
    name = cfg.get("ORBION_AI_NAME", "Orbion_Assistant").strip() or "Orbion_Assistant"

    if not user or not pwd:
        return {"ok": False, "error": "Gmail bilgileri eksik (.env)"}
    if not to or "@" not in to:
        return {"ok": False, "error": "Gecersiz alici adresi"}

    # T21: gunluk gonderim guard'i (rezerve et; basarisizsa geri birak).
    ok_send, sent_n, send_lim = _smtp_reserve(cfg)
    if not ok_send:
        return {"ok": False,
                "error": f"Gunluk gonderim limiti dolu ({sent_n}/{send_lim})",
                "limit": send_lim, "used": sent_n}

    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((name, user))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="gmail.com")
    msg["X-Mailer"] = "Orbion_Assistant"

    msg.attach(MIMEText(body or "", "plain", "utf-8"))
    if html:
        msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=45) as s:
                s.login(user, pwd)
                s.sendmail(user, [to], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=45) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
                s.login(user, pwd)
                s.sendmail(user, [to], msg.as_string())
        return {"ok": True, "from": f"{name} <{user}>", "to": to, "subject": subject}
    except smtplib.SMTPAuthenticationError:
        _smtp_release(cfg)
        return {"ok": False, "error": "SMTP kimlik dogrulama hatasi (App Password?)"}
    except Exception as exc:
        _smtp_release(cfg)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
