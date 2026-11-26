"""
projects.py — Ayla proje defteri (kalici proje hafizasi)
--------------------------------------------------------
Amac: the owner ile Ayla arasinda gecen proje konusmalarini kalici tutmak.
Konusma bittikten (hatta gunler sonra) donup "su isi konusuyorduk, devam"
denebilsin; Ayla birikmis gereksinimlerden mimari/spec cikarip Evren'e
gorev olarak iletebilsin.

Depo: agents/ayla/projeler/<slug>/
  meta.json    — kimlik, durum, tarihler, bagli gorevler
  PROJE.md     — insan-okur gereksinim defteri (notlar zaman damgali)
  MIMARI.md    — uretilen mimari/spec (Ayla/Evren)

Harici bagimlilik YOK; yalnizca stdlib. Sirlar buraya yazilmaz.
"""

import difflib
import json
import re
import shutil
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "agents" / "ayla" / "projeler"

STATUSES = ("acik", "mimari_hazir", "evrene_iletildi", "kapali")
MAX_NOTE = 4000
MAX_SPEC = 24000
MAX_NAME = 80


# ------------------------------------------------------------------ yardimcilar
def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _fold(text):
    """Turkce-duyarli kucultme (arama icin)."""
    t = (text or "").replace("İ", "i").replace("I", "ı")
    return t.lower().strip()


def slugify(name):
    """'Web Sayfası Projesi' -> 'web-sayfasi-projesi' (TR karakter sadelesir)."""
    tr = str.maketrans({
        "ç": "c", "Ç": "c", "ğ": "g", "Ğ": "g", "ı": "i", "İ": "i",
        "ö": "o", "Ö": "o", "ş": "s", "Ş": "s", "ü": "u", "Ü": "u",
    })
    t = _fold(name).translate(tr)
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t[:60] or "proje"


def paths(slug):
    d = BASE / slug
    return {
        "dir": d,
        "meta": d / "meta.json",
        "proje": d / "PROJE.md",
        "mimari": d / "MIMARI.md",
    }


def _read_json(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _read_meta(slug):
    return _read_json(paths(slug)["meta"], None)


def _write_meta(slug, meta):
    meta["updated"] = _now()
    _write_json(paths(slug)["meta"], meta)


# ------------------------------------------------------------------ CRUD
def proj_exists(slug):
    return _read_meta(slug) is not None


def proj_list(status=None):
    """Tum projeler (en son guncellenen once)."""
    out = []
    if not BASE.exists():
        return out
    for d in sorted(BASE.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        m = _read_json(d / "meta.json", None)
        if not m:
            continue
        if status and m.get("status") != status:
            continue
        out.append(m)
    return out


def proj_open():
    return [m for m in proj_list() if m.get("status") != "kapali"]


def proj_unique_slug(name):
    base = slugify(name)[: MAX_NAME]
    slug, i = base, 2
    while proj_exists(slug):
        slug = f"{base}-{i}"
        i += 1
    return slug


def proj_create(name, note="", source="telegram"):
    name = (name or "").strip()[:MAX_NAME]
    if not name:
        return None
    slug = proj_unique_slug(name)
    p = paths(slug)
    p["dir"].mkdir(parents=True, exist_ok=True)
    meta = {
        "slug": slug,
        "name": name,
        "status": "acik",
        "created": _now(),
        "updated": _now(),
        "source": source,
        "notes": [],
        "tasks": [],
        "spec": None,
    }
    _write_json(p["meta"], meta)
    header = (f"# {name}\n\n"
              f"- oluşturuldu: {meta['created']}\n"
              f"- durum: acik\n\n## Notlar\n\n")
    p["proje"].write_text(header, encoding="utf-8")
    if note:
        proj_add_note(slug, note, kind="ilk")
    return _read_meta(slug)


def proj_get(ref):
    """slug veya isimle proje getir (bulunamazsa None)."""
    if not ref:
        return None
    s = slugify(ref)
    m = _read_meta(s)
    if m:
        return m
    return None


def proj_find(ref):
    """Esnek arama: slug -> isim -> kismi/benzer. Donus: slug | None."""
    if not ref:
        return None
    ref = (ref or "").strip()
    s = slugify(ref)
    if proj_exists(s):
        return s
    q = _fold(ref)
    cands = proj_list()
    # tam isim
    for m in cands:
        if _fold(m.get("name")) == q:
            return m["slug"]
    # isim icinde gecen
    for m in cands:
        if q and q in _fold(m.get("name")):
            return m["slug"]
    # slug icinde gecen
    for m in cands:
        if q and q in _fold(m["slug"]):
            return m["slug"]
    # benzerlik
    names = {m["slug"]: _fold(m.get("name")) for m in cands}
    hit = difflib.get_close_matches(q, list(names.values()), n=1, cutoff=0.6)
    if hit:
        for slug, nm in names.items():
            if nm == hit[0]:
                return slug
    return None


def proj_add_note(slug, text, kind="not"):
    """Proje defterine zaman damgali not ekle (PROJE.md + meta.notes)."""
    meta = _read_meta(slug)
    if not meta:
        return None
    text = (text or "").strip()[:MAX_NOTE]
    if not text:
        return meta
    ts = _now()
    meta.setdefault("notes", []).append({"ts": ts, "kind": kind, "text": text})
    if len(meta["notes"]) > 200:
        meta["notes"] = meta["notes"][-200:]
    _write_meta(slug, meta)
    p = paths(slug)
    try:
        with p["proje"].open("a", encoding="utf-8") as f:
            tag = "" if kind == "not" else f"[{kind}] "
            f.write(f"- `{ts}` {tag}{text}\n")
    except Exception:
        pass
    return meta


def proj_set_status(slug, status):
    if status not in STATUSES:
        return False
    meta = _read_meta(slug)
    if not meta:
        return False
    meta["status"] = status
    _write_meta(slug, meta)
    return True


def proj_save_spec(slug, md):
    meta = _read_meta(slug)
    if not meta:
        return None
    md = (md or "").strip()[:MAX_SPEC]
    if not md:
        return None
    p = paths(slug)
    p["mimari"].write_text(md, encoding="utf-8")
    meta["spec"] = {"ts": _now(), "chars": len(md)}
    if meta.get("status") == "acik":
        meta["status"] = "mimari_hazir"
    _write_meta(slug, meta)
    return md


def proj_get_spec(slug):
    p = paths(slug)
    try:
        return p["mimari"].read_text(encoding="utf-8")
    except Exception:
        return ""


def proj_link_task(slug, task_id):
    meta = _read_meta(slug)
    if not meta:
        return False
    tid = str(task_id)
    if tid not in [str(x) for x in meta.get("tasks", [])]:
        meta.setdefault("tasks", []).append(task_id)
    _write_meta(slug, meta)
    return True


def proj_delete(slug, hard=False):
    meta = _read_meta(slug)
    if not meta:
        return False
    if hard:
        shutil.rmtree(paths(slug)["dir"], ignore_errors=True)
        return True
    meta["status"] = "kapali"
    _write_meta(slug, meta)
    return True


# ------------------------------------------------------------------ metin uretimi
def proj_notes_text(slug, limit_chars=6000):
    """Notlari tek metne indir (LLM baglami / ozet icin)."""
    meta = _read_meta(slug)
    if not meta:
        return ""
    lines = [f"PROJE: {meta['name']}  (durum: {meta.get('status')})",
             f"olusturuldu: {meta.get('created')}"]
    notes = meta.get("notes") or []
    if not notes:
        lines.append("(henuz not yok)")
    for n in notes:
        lines.append(f"- [{n.get('kind', 'not')}] {n.get('text')}")
    txt = "\n".join(lines)
    return txt[-limit_chars:] if len(txt) > limit_chars else txt


def proj_summary_line(meta):
    notes = meta.get("notes") or []
    spec = meta.get("spec")
    bits = [f"{len(notes)} not"]
    if spec:
        bits.append("mimari var")
    if meta.get("tasks"):
        bits.append(f"{len(meta['tasks'])} gorev")
    return (f"{meta['name']} — {meta.get('status')} "
            f"({', '.join(bits)}) · son: {str(meta.get('updated'))[5:16]}")


def proj_context(slug=None, max_projects=6):
    """Ayla sistem baglamina eklenecek proje ozeti.
    slug verilirse o projenin detayi, yoksa acik projelerin listesi."""
    if slug:
        meta = _read_meta(slug)
        if not meta:
            return ""
        return ("AKTIF PROJE (kalici defter):\n" + proj_notes_text(slug) +
                "\n\nBu projeyi konusurken yukaridaki notlari esas al; "
                "yeni karar/gereksinim olursa proje notu olarak kaydet.")
    opens = proj_open()
    if not opens:
        return "Acik proje yok."
    lines = ["ACIK PROJELER (kalici defter):"]
    for m in opens[:max_projects]:
        lines.append("  • " + proj_summary_line(m))
    lines.append("Proje hakkinda konusulursa yukaridaki kayitlari kullan; "
                 "yeni gereksinim gelirse proje notu olarak ekle.")
    return "\n".join(lines)
