"""
identity.py — Kimlik kaydi (tek kaynak)
----------------------------------------
Butun ajanlar ve modeller bu isimleri bilir.
Kullanici isimle hitap ettiginde Asistan dogru ajana yonlendirir.
"""

import roles

# id -> kimlik
AGENTS = {
    "evren": {
        "name": "Evren",
        "role": "Sef beyin / bulut model",
        "desc": "Karmasik isleri cozer, planlama ve taslak yazar. Bulutta calisir, gunluk token kotasi vardir.",
        "engine": "deepseek-v4.1-flash (bulut)",
    },
    "cirak": {
        "name": "Cirak",
        "role": "Yerel model / isci",
        "desc": "Basit ve orta isleri yapar: siniflandirma, ozet, alan cikarimi, kisa cevaplar. Yerelde (Ollama) calisir, token maliyeti yok.",
        "engine": "qwen3.5:4b (Ollama, yerel)",
    },
    "asistan": {
        "name": "Asistan",
        "role": "Telegram arayuzu",
        "desc": "Kullanicidan emir alir, isi dogru ajana iletir, sonucu kullaniciya dondurur.",
        "engine": "Telegram bot",
    },
    "kurye": {
        "name": "Kurye",
        "role": "Postaci",
        "desc": "Mail gonderir (SMTP). Gonderici adi: Orbion_Assistant.",
        "engine": "Gmail SMTP",
    },
    "ayla": {
        "name": "Ayla",
        "role": "Filtreci",
        "desc": "Mailleri tarar, onemsizleri eler, onemlileri listeler (Tier 0 + IMAP).",
        "engine": "IMAP + kural motoru",
    },
    "arsivci": {
        "name": "Arsivci",
        "role": "Arsiv / temizlik",
        "desc": "Eski mailleri Cop'e tasir, cop kutusunu bosaltir.",
        "engine": "IMAP",
    },
}

# takma adlar (kullanicinin yazabilecegi sekiller)
ALIASES = {
    "evren": ["evren", "bulut", "beyin", "sef", "şef"],
    "cirak": ["cirak", "çırak", "cirag", "çırağ", "ciraga", "çırağa", "ollama",
             "yerel", "qwen", "qwen3.5", "kucuk model", "küçük model"],
    "asistan": ["asistan", "bot", "telegram"],
    "kurye": ["kurye", "postaci", "postacı", "mailci", "mail'ci"],
    "ayla": ["ayla", "filtreci", "filtre", "filtreleyici"],
    "arsivci": ["arsivci", "arşivci", "arsiv", "arşiv", "temizlikci", "temizlikçi"],
}


def resolve(mention: str):
    """Kullanicinin yazdigi isim -> agent id. Bulunamazsa None."""
    t = (mention or "").lower().strip()
    for aid, alts in ALIASES.items():
        for a in alts:
            if a in t:
                return aid
    return None


def name_of(aid: str) -> str:
    return AGENTS.get(aid, {}).get("name", aid)


def system_prompt(aid: str) -> str:
    """Kisa kimlik: sadece isim+rol. Kendini tanitmaya ZORLAMAZ."""
    me = AGENTS.get(aid)
    if not me:
        return ""
    rules = [
        f"Senin adin {me['name']}. Rolun: {me['role']}.",
        "KURALLAR:",
        "- Kendini TANITMA. 'Ben bir yapay zeka asistaniyim' deme. Ozur dileme.",
        "- Selamlasma gelirse tek kelimeyle karsilik ver ve bekle.",
        "- Sadece istenen isi yap, sonucu kisa ve net ver.",
        "- 'Kimsin?' diye acikca sorulursa 1 cumleyle soyle.",
        "- Yapamayacagin isi uydurma; 'bunu yapamam' de.",
    ]
    try:
        rules.append("")
        rules.append(roles.role_prompt(aid))
    except Exception:
        pass
    return "\n".join(rules)


def roster_text() -> str:
    """Telegram /ekip komutu icin."""
    lines = []
    for aid, a in AGENTS.items():
        lines.append(f"<b>{a['name']}</b> — {a['role']}\n  <i>{a['desc']}</i>")
    return "\n".join(lines)
