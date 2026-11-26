"""
web_research.py — anahtarsız web araştırma katmanı
==================================================
Amaç: Ajanların güvenilir kaynakları internetten araştırabilmesi.

Kullanılan ücretsiz/anahtarsız yollar:
  - PubMed E-utilities (esearch + esummary + efetch)
  - WHO IRIS DSpace REST arama
  - Wikipedia API (tr/en; genel konular)
  - Doğrudan URL fetch + HTML -> metin

Gizlilik: Yalnız GENEL soru metni dışarı gider. Kişisel veri
(isim, ölçüm değeri, teşhis, tahlil, reçete, TCKN, telefon, e-posta)
tespit edilirse ağa ÇIKILMAZ. Uydurma kaynak/URL üretilmez.
"""
import html
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from html.parser import HTMLParser

UA = ("Mozilla/5.0 (compatible; EvrenResearch/1.0; "
      "+https://evren.example/research)")

_ACCESS_DATE = lambda: date.today().isoformat()

# Kişisel veri / kimlik göstergeleri (genel soru değil)
_PERSONAL_RX = [
    re.compile(r"[\w.\-+]+@[\w.\-]+\.\w+"),                       # e-posta
    re.compile(r"(?<!\d)(?:\+?90)?\s*0?5\d{2}\s*\d{3}\s*\d{2}\s*\d{2}(?!\d)"),  # telefon
    re.compile(r"(?<!\d)\d{11}(?!\d)"),                          # TCKN benzeri
    re.compile(r"(?i)\b(ad[ıi]m|ismim|benim\s+ad[ıi]m|ad[ıi]m[ıi]z)\b"),
    re.compile(r"(?i)\b(the owner|owner)\b"),
    re.compile(r"(?i)\b(tansiyonum|şekerim|sekerim|nabz[ıi]m|kilom|kilo\s*verm|"
               r"tahlillerim|tahlilim|teşhisim|teshisim|tan[ıi]m|reçetem|recetem|"
               r"ilac[ıi]m|doktorum\s+bana|bende\s+)" ),
    re.compile(r"(?i)\bben(im)?\b.{0,40}\b(tansiyon|şeker|seker|kilo|boy|nab[ıi]z|"
               r"spo2|tahlil|teşhis|teshis|tan[ıi]|ilaç|ilac|reçete|recete)\b"),
    re.compile(r"\b\d{1,3}\s*/\s*\d{1,3}\s*(?:mmhg|tansiyon)?\b", re.I),
    re.compile(r"(?i)\b\d+(?:[.,]\d+)?\s*(mmhg|mg[/\s]?dl|mg|mcg|kg|cm|bpm|°c)\b"),
]

_MEDICAL_RX = re.compile(
    r"(?i)(hipertansiyon|tansiyon|diyabet|şeker|seker|kalp|kanser|tümör|tumor|"
    r"ilaç|ilac|tedavi|beslenme|diyet|uyku|egzersiz|obezite|kilo|vitamin|"
    r"ağr[ıi]|agri|semptom|belirti|teşhis|teshis|tan[ıi]|doktor|hekim|"
    r"hastal[ıi]k|enfeksiyon|virüs|virus|a[şs][ıi]|gebelik|hamilelik|"
    r"kolesterol|böbrek|bobrek|akciğer|akciger|kemik|ruh\s*sa[gğ]l[ıi][gğ][ıi]|"
    r"stres|sigara|alkol|who|d[üu]nya\s*sa[gğ]l[ıi]k|pubmed|t[ıi]p|sa[gğ]l[ıi]k)")

# PubMed için küçük, dürüst sorgu çevirisi (uydurma değil; yalnız arama terimi)
_TR_EN = {
    "hipertansiyon": "hypertension", "tansiyon": "blood pressure",
    "yaşam tarzı": "lifestyle", "yasam tarzi": "lifestyle",
    "yaşam": "lifestyle", "tarzı": "lifestyle", "tarzi": "lifestyle",
    "önerileri": "recommendations", "onerileri": "recommendations",
    "öneri": "recommendations", "oneri": "recommendations",
    "diyabet": "diabetes", "şeker": "diabetes", "seker": "diabetes",
    "kalp": "heart", "kanser": "cancer", "beslenme": "nutrition",
    "uyku": "sleep", "egzersiz": "exercise", "kilo": "weight",
    "obezite": "obesity", "vitamin": "vitamin", "ağrı": "pain", "agri": "pain",
    "tedavi": "treatment", "ilaç": "medication", "ilac": "medication",
    "kullanımı": "use", "kullanimi": "use", "kullanım": "use", "kullanim": "use",
    "ilaçları": "medications", "ilaclari": "medications",
    "ilaçlar": "medications", "ilaclar": "medications",
    "ilacı": "medication", "ilaci": "medication",
    "dozu": "dose", "doz": "dose", "yan etki": "side effect",
    "yan etkileri": "side effects", "hamile": "pregnant",
    "gebelik": "pregnancy", "hamilelik": "pregnancy", "aşı": "vaccine",
    "asi": "vaccine", "enfeksiyon": "infection", "virüs": "virus",
    "virus": "virus", "çocuk": "child", "cocuk": "child", "yaşlı": "elderly",
    "yasli": "elderly", "stres": "stress", "sigara": "smoking",
    "alkol": "alcohol", "kolesterol": "cholesterol", "böbrek": "kidney",
    "bobrek": "kidney", "akciğer": "lung", "akciger": "lung", "kemik": "bone",
    "d vitamini": "vitamin d",
    "tıp": "medicine", "tip": "medicine",
}


def sanitize_query(question):
    """(ok, temiz_soru, neden). Kişisel veri varsa ok=False (ağa çıkılmaz)."""
    q = " ".join((question or "").split()).strip()
    if not q:
        return False, "", "soru metni yok"
    for rx in _PERSONAL_RX:
        if rx.search(q):
            return False, "", "kisisel_veri"
    if len(q) > 300:
        q = q[:300].rsplit(" ", 1)[0]
    return True, q, ""


def _addr_blocked(ip):
    """SSRF: loopback/link-local/private/reserved/multicast/unspecified -> True."""
    try:
        a = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return True
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return (a.is_private or a.is_loopback or a.is_link_local or a.is_reserved
            or a.is_multicast or a.is_unspecified)


def validate_url(url):
    """K3: yalniz http(s) + herkese acik IP. (ok, neden).

    DNS'i cozer; her adres icin loopback/link-local (fe80::/10),
    RFC1918 private, unspecified and metadata addresses are rejected."""
    try:
        p = urllib.parse.urlsplit((url or "").strip())
    except Exception:
        return False, "gecersiz_url"
    if p.scheme not in ("http", "https"):
        return False, "sema_yasak"
    host = p.hostname
    if not host:
        return False, "host_yok"
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        return False, "port_gecersiz"
    # Literal IP ise dogrudan kontrol et.
    try:
        ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        pass
    else:
        if _addr_blocked(host):
            return False, "ssrf_engel"
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except Exception:
        return False, "dns_hatasi"
    if not infos:
        return False, "dns_bos"
    for info in infos:
        ip = info[4][0]
        if _addr_blocked(ip):
            return False, "ssrf_engel:" + str(ip)
    return True, ""


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Her redirect hop'unu yeniden dogrula (SSRF redirect bypass'i engeller)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ok, why = validate_url(newurl)
        if not ok:
            raise urllib.error.URLError("redirect engellendi: " + why)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SafeRedirect())


def _http_get(url, timeout=16):
    ok, why = validate_url(url)
    if not ok:
        raise urllib.error.URLError("ssrf: " + why)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json,text/html,application/xml,text/xml,*/*",
        "Accept-Language": "tr,en;q=0.8",
    })
    with _OPENER.open(req, timeout=timeout) as r:
        return r.read(), r.status, r.geturl()


def _snippet(text, n=600):
    t = re.sub(r"<[^>]+>", " ", text or "")
    t = html.unescape(t)
    t = " ".join(t.split())
    return t[:n]


def _strip_html(text):
    return _snippet(text, 12000)


class _TextExtract(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title, self._skip = [], "", 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style", "noscript", "svg"):
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip:
            return
        d = " ".join((data or "").split())
        if not d:
            return
        if self._in_title:
            self.title = (self.title + " " + d).strip()
        else:
            self.parts.append(d)


def fetch_url(url, max_chars=6000):
    """Doğrudan URL: (ok, title, metin, hata)."""
    ok, why = validate_url(url)
    if not ok:
        return False, "", "", why
    try:
        raw, status, final = _http_get(url, timeout=18)
        ct = ""
        try:
            # urllib yanıt başlığı burada yok; metin/HTML ayrımını içerikten yap
            pass
        except Exception:
            pass
        body = raw.decode("utf-8", "ignore")
        if "html" in body[:400].lower() or "<html" in body[:800].lower():
            p = _TextExtract()
            try:
                p.feed(body)
            except Exception:
                pass
            txt = " ".join(p.parts)
            title = p.title or urllib.parse.urlparse(url).netloc
        else:
            txt, title = body, urllib.parse.urlparse(url).netloc
        txt = " ".join(txt.split())[:max_chars]
        return True, _snippet(title, 140), txt, ""
    except Exception as exc:
        return False, "", "", type(exc).__name__


def search_wikipedia(question, n=3):
    out = []
    for lang in ("tr", "en"):
        try:
            u = (f"https://{lang}.wikipedia.org/w/api.php?action=query&list=search"
                 f"&srsearch={urllib.parse.quote(question)}&format=json&utf8=1"
                 f"&srlimit={max(1, min(int(n), 8))}")
            raw, _st, _fu = _http_get(u)
            d = json.loads(raw.decode("utf-8", "ignore"))
            for r in (d.get("query", {}).get("search") or []):
                title = r.get("title") or ""
                if not title:
                    continue
                url = f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
                # REST özeti varsa onu kullan (daha temiz içerik)
                snip = _snippet(r.get("snippet") or "", 700)
                try:
                    su = (f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/"
                          f"{urllib.parse.quote(title.replace(' ', '_'))}")
                    sraw, _s2, _f2 = _http_get(su)
                    sj = json.loads(sraw.decode("utf-8", "ignore"))
                    ex = (sj.get("extract") or "").strip()
                    if ex:
                        snip = _snippet(ex, 900)
                except Exception:
                    pass
                out.append({"name": f"Wikipedia ({lang}): {title}", "url": url,
                            "snippet": snip, "provider": "Wikipedia",
                            "accessed": _ACCESS_DATE()})
                if len(out) >= n:
                    return out
        except Exception:
            continue
        if out:
            break
    return out


def _translate_for_pubmed(q):
    out = q
    for tr, en in sorted(_TR_EN.items(), key=lambda x: -len(x[0])):
        out = re.sub(re.escape(tr), en, out, flags=re.I)
    return out


def search_pubmed(question, n=4):
    try:
        term = _translate_for_pubmed(question)
        u = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
             f"?db=pubmed&term={urllib.parse.quote(term)}&retmode=json"
             f"&retmax={max(1, min(int(n), 10))}&sort=relevance")
        raw, _st, _fu = _http_get(u)
        d = json.loads(raw.decode("utf-8", "ignore"))
        ids = (d.get("esearchresult", {}) or {}).get("idlist") or []
        if not ids:
            return []
        su = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
              f"?db=pubmed&id={','.join(ids)}&retmode=json")
        sraw, _s2, _f2 = _http_get(su)
        sj = json.loads(sraw.decode("utf-8", "ignore"))
        abstracts = {}
        try:
            eu = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
                  f"?db=pubmed&id={','.join(ids)}&rettype=abstract&retmode=text")
            eraw, _s3, _f3 = _http_get(eu, timeout=25)
            etxt = eraw.decode("utf-8", "ignore")
            for block in re.split(r"\n\s*\n(?=PMID-)", etxt):
                m = re.search(r"PMID-\s*(\d+)", block)
                if not m:
                    continue
                ab = re.search(r"(?:AB|ABSTRACT)\s*-\s*(.*?)(?:\n[A-Z]{2,4}\s*-|$)", block, re.S)
                abstracts[m.group(1)] = _snippet(ab.group(1) if ab else block, 900)
        except Exception:
            pass
        out = []
        for pmid in ids:
            r = (sj.get("result", {}) or {}).get(pmid) or {}
            title = (r.get("title") or "").strip() or "PubMed kaydı"
            out.append({"name": f"PubMed: {_snippet(title, 160)}",
                        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                        "snippet": abstracts.get(pmid) or _snippet(title, 300),
                        "provider": "PubMed", "accessed": _ACCESS_DATE()})
        return out
    except Exception:
        return []


def search_titck(question, n=2):
    """TİTCK site içi arama (searchGeneral). En iyi çaba; site kararsızsa [] döner."""
    q0 = (question or "").strip()
    out = _titck_fetch(q0, n)
    # Uzun soru TİTCK'te 0 sonuç verebilir: en uzun anlamlı terimle tek tekrar
    # (yalnız TİTCK site aramasını genişletir; uydurma kaynak değil).
    if not out and len(q0.split()) > 1:
        toks = re.findall(r"[0-9A-Za-zÇĞİÖŞÜçğıöşü]{4,}", q0)
        pick = next((t for t in toks if t.lower() in _TR_EN), None)
        if not pick and toks:
            pick = max(toks, key=len)
        if pick and pick.lower() != q0.lower():
            out = _titck_fetch(pick, n)
    return out


def _titck_fetch(q, n=2):
    try:
        if not q:
            return []
        u = ("https://www.titck.gov.tr/searchGeneral?term="
             + urllib.parse.quote(q))
        raw, _st, _fu = _http_get(u, timeout=12)
        body = raw.decode("utf-8", "ignore")
        if "v2-list-card" not in body:
            return []
        out, seen = [], set()
        rx = re.compile(
            r'<a[^>]+href="([^"]+)"[^>]+class="[^"]*v2-list-card[^"]*"[^>]*>(.*?)</a>',
            re.S | re.I)
        for m in rx.finditer(body):
            href = html.unescape(m.group(1)).strip()
            inner = m.group(2)
            if href.startswith("/"):
                href = "https://www.titck.gov.tr" + href
            if not href.startswith("http"):
                continue
            tm = re.search(r'card-title-text[^>]*>(.*?)</h[1-6]>', inner, re.S | re.I)
            title = _snippet(tm.group(1), 220) if tm else ""
            if not title:
                continue
            key = re.sub(r"[#?].*$", "", href).rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            sn = ""
            sm = re.search(r'card-snippet-text[^>]*>(.*?)</p>', inner, re.S | re.I)
            if sm:
                sn = _snippet(sm.group(1), 700)
            out.append({"name": f"TİTCK: {title}", "url": href,
                        "snippet": sn or title, "provider": "TİTCK",
                        "accessed": _ACCESS_DATE()})
            if len(out) >= max(1, int(n)):
                break
        return out
    except Exception:
        return []


def search_who(question, n=3):
    try:
        u = ("https://iris.who.int/server/api/discover/search/objects"
             f"?query={urllib.parse.quote(question)}&size={max(1, min(int(n), 8))}")
        raw, _st, _fu = _http_get(u)
        d = json.loads(raw.decode("utf-8", "ignore"))
        objs = (((d.get("_embedded") or {}).get("searchResult") or {})
                .get("_embedded") or {}).get("objects") or []
        out = []
        for o in objs:
            io = (o.get("_embedded") or {}).get("indexableObject") or {}
            md = io.get("metadata") or {}

            def _v(key):
                vals = md.get(key) or []
                return str((vals[0] or {}).get("value") or "").strip() if vals else ""

            title = _v("dc.title") or io.get("name") or ""
            url = _v("dc.identifier.uri") or (
                f"https://iris.who.int/handle/{io.get('handle')}" if io.get("handle") else "")
            if not title or not url:
                continue
            ab = _v("dc.description.abstract") or _v("dc.description")
            out.append({"name": f"WHO IRIS: {_snippet(title, 160)}",
                        "url": url, "snippet": _snippet(ab or title, 900),
                        "provider": "WHO IRIS", "accessed": _ACCESS_DATE()})
        return out
    except Exception:
        return []


def _dedupe(sources):
    seen, out = set(), []
    for s in sources:
        u = (s.get("url") or "").strip()
        if not u:
            continue
        key = re.sub(r"[#?].*$", "", u).rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def collect(question, deep=False):
    """Kaynak topla. Dönüş: {ok, query, sources, error, medical, deep}."""
    ok, q, err = sanitize_query(question)
    if not ok:
        return {"ok": False, "query": "", "sources": [], "error": err,
                "medical": False, "deep": deep}
    sources = []
    # Doğrudan URL varsa onu gerçekten çek
    for m in re.finditer(r"https?://[^\s<>\"']+", q):
        u = m.group(0).rstrip(").,;")
        fok, title, txt, ferr = fetch_url(u)
        if fok and txt:
            sources.append({"name": f"Sayfa: {title or u}", "url": u,
                            "snippet": _snippet(txt, 1200), "provider": "Doğrudan fetch",
                            "accessed": _ACCESS_DATE()})
    medical = bool(_MEDICAL_RX.search(q))
    n = 6 if deep else 3
    if medical:
        sources += search_pubmed(q, n=n)
        # WHO IRIS İngilizce indeksli; arama terimini dürüstçe çevir (uydurma yok)
        sources += search_who(_translate_for_pubmed(q), n=(4 if deep else 2))
        # TİTCK öncelikli (Türkiye ilaç/cihaz kurumu); erişilemezse sessizce atlanır
        sources += search_titck(q, n=(3 if deep else 2))
    sources += search_wikipedia(q, n=(5 if deep else 3))
    sources = _dedupe(sources)
    if deep:
        # En güvenilir 2 kaynağın tam metnini çekmeye çalış (mümkünse)
        for s in list(sources[:2]):
            if s.get("provider") in ("Doğrudan fetch", "Wikipedia"):
                continue
            fok, title, txt, _ferr = fetch_url(s["url"])
            if fok and len(txt) > len(s.get("snippet") or ""):
                s["snippet"] = _snippet(txt, 1800)
        sources = sources[:10]
    else:
        sources = sources[:6]
    if not sources:
        return {"ok": False, "query": q, "sources": [], "error": "bulunamadi",
                "medical": medical, "deep": deep}
    return {"ok": True, "query": q, "sources": sources, "error": "",
            "medical": medical, "deep": deep}


def source_refs(sources):
    """DB/panel için yalnız güvenli metadata (özet metni yok)."""
    return [{"name": s.get("name", ""), "url": s.get("url", ""),
             "provider": s.get("provider", ""),
             "accessed": s.get("accessed", "")} for s in (sources or [])]


def deterministic_summary(sources, deep=False):
    """Model yoksa bile kaynaklara dayalı, uydurmasız kısa özet."""
    if not sources:
        return ""
    lines = []
    for s in sources[: (6 if deep else 4)]:
        sn = _snippet(s.get("snippet") or "", 260)
        if sn:
            lines.append(f"- {s.get('name')}: {sn}")
    return "\n".join(lines)


def format_report(res, summary="", deep=False):
    if not res or not res.get("ok"):
        err = (res or {}).get("error") or "bilinmeyen"
        if err == "kisisel_veri":
            return ("\U0001F512 <b>Güvenlik</b>: Soru kişisel/kimlik verisi "
                    "içeriyor; dış aramaya GÖNDERMEDİM. Lütfen genel bir soru yazın.")
        if err == "bulunamadi":
            return ("\U0001F4DA <b>Araştırma</b>: Bu soru için erişilebilir kaynak "
                    "bulunamadı (uydurma kaynak üretmem).")
        if err == "soru metni yok":
            return "\U0001F4DA <b>Araştırma</b>: Soru metni boş."
        return f"\U0001F4DA <b>Araştırma yapılamadı</b>: {html.escape(str(err)[:160])}"
    q = html.escape(res.get("query") or "")
    sources = res.get("sources") or []
    head = "\U0001F4DA <b>Araştırma raporu</b>" + (" (derin)" if deep else "")
    lines = [head, f"Sorgu: <i>{q}</i>", ""]
    body = (summary or "").strip() or deterministic_summary(sources, deep)
    if body:
        lines += ["<b>Özet</b>", body, ""]
    lines.append("<b>Kaynaklar</b>")
    for i, s in enumerate(sources, 1):
        lines.append(f"{i}. {html.escape(s.get('name') or '')} "
                     f"({html.escape(s.get('provider') or '')})")
        lines.append(f"   {html.escape(s.get('url') or '')}")
        lines.append(f"   Erişim: {html.escape(s.get('accessed') or '')}")
    lines += ["", "<i>Not: Tanı değildir, bilgi amaçlıdır. Kaynaklar erişildiği "
                  "tarihteki içeriklerine göre özetlenmiştir; uydurma kaynak yoktur.</i>"]
    return "\n".join(lines)
