# ORBION Public Release — MANIFEST

Kaynak: canlı EVREN ağacı (**yalnızca okundu**, değiştirilmedi).
Çıktı: `/home/ubuntu/orbion-release/` — canlı sistemden bağımsız, yayına hazır kopya.

## 1. Dahil edilen dosyalar

| Dosya | Neden dahil |
|---|---|
| `scripts/agent.py` | Genel çekirdek: HTTP panel API, görev/kuyruk, LLM havuzu, log/alarm, watchdog, web araştırma köprüsü. Sağlık/Doruk kodu ve rotaları çıkarıldı. |
| `scripts/telegram_bot.py` | Genel Telegram arayüzü. Doruk/sağlık yönlendirmesi çıkarıldı. |
| `scripts/actions.py` | Genel IMAP/SMTP mail işlemleri ve limit koruması. |
| `scripts/identity.py` | Genel ajan kimlik kaydı. Doruk kaydı çıkarıldı. |
| `scripts/roles.py` | `identity.py` tarafından import edilen genel rol hiyerarşisi. Doruk rolü çıkarıldı. |
| `scripts/projects.py` | `agent.py` tarafından import edilen genel proje defteri kodu. Kişisel proje **verisi** dahil edilmedi. |
| `scripts/dev_bot.py` | `agent.py` tarafından import edilen genel kod köprüsü (Telegram ↔ kuyruk). Eski sağlık kanalı özel durumu kaldırıldı. |
| `scripts/dev_worker.py` | Genel kuyruk işçisi (opencode köprüsü). |
| `scripts/web_research.py` | Genel kaynaklı web araştırma + SSRF/kişisel veri filtresi. |
| `agents/_shared/CORE.md` | Genel ortak ajan kuralları. |
| `agents/ayla/IDENTITY.md` | Genel profesyonel asistan personası (kişisel/cinsel satırlar çıkarıldı). |
| `agents/ayla/COMMAND.md` | Genel çalışma kuralları (Doruk/sağlık satırları çıkarıldı). |
| `agents/evren/IDENTITY.md` | Genel geliştirici ajan personası. |
| `agents/evren/COMMAND.md` | Genel geliştirici çalışma kuralları. |
| `.env.example` | Yalnız değişken adları ve boş/örnek değerler; gerçek sır yok. |
| `README.md` | Kurulum, mimari ve güvenlik özeti; sağlık ajanı yalnız referans tasarım. |
| `MANIFEST.md` | Bu dosya. |

## 2. Çıkarılan dosya/klasörler

| Çıkarılan | Neden |
|---|---|
| `scripts/doruk_prompts.py` | Sağlık/Doruk persona ve prompt kodu. |
| `agents/doktor-doruk/**` | Sağlık ajanı kimliği, güvenlik kuralları, staging/versions/uploads. |
| `agents/ayla/projeler/**` | Kişisel proje notları (kurs kayıt, takvim entegrasyonu vb.). |
| Sağlıkla ilgili `.py`/`.md`/scriptler | Sağlık modülü kodu yayınlanmaz. |
| `.env`, `.env.providers` | Gerçek anahtar/token değerleri. Yalnız `.env.example` verildi. |
| `data/`, `logs/`, `backups/`, `backup_*` | Veritabanı, log ve yedeklerde kişisel veri olabilir. Hiçbir veri kopyalanmadı. |
| `docs/` (ilerleme, kararlar, kanıt) | Kişisel/operasyonel geçmiş ve kanıt dosyaları. |
| Test dosyaları (`test_*.py`) | İç IP/token/kişisel senaryo kalıntısı riski; genel çekirdek için gerekli değil. |
| `web/` panel arayüzü | İç ajan topolojisi/kişisel etiketler içeriyor; kapsam dışı bırakıldı. |
| `.git/` | Geçmiş ve uzak bağlantı bilgisi taşır. |

## 3. Kişisel veri / sır temizliği

- Kişisel adlar ve hitaplar: `the owner` / `user` / `OWNER` placeholders.
- İç IP'ler, loopback/metadata adresleri ve özel alan adları: `localhost`/`host.example`
  veya boş env varsayılanı.
- Token/anahtar değerleri: kodda **yok**; yalnız `CFG.get(...)` / `.env` adları.
- Eski API taban adresi varsayılanı kaldırıldı (env'den okunur).
- Ayla personasındaki kişisel/cinsel satırlar tamamen çıkarıldı; genel profesyonel
  asistan personası yazıldı.
- Veritabanı şemasındaki sağlık tabloları ve sağlık SQL'i çıkarıldı; hiçbir DB
  dosyası/verisi kopyalanmadı.
- `agent.py` içindeki sağlık/Doruk fonksiyonları, sabitleri, HTTP rotaları ve
  başlatma döngüleri kaldırıldı. Web araştırma modülü genel kaldı.

**Kalan kişisel referans: YOK.** Owner-bekliyor işareti kullanılmadı.

## 4. Doğrulama — sır taraması

Release klasöründe aşağıdaki desenler tarandı; her biri için bulgu sayısı:

| Desen (tanım) | Bulgu |
|---|---|
| Eski bulut API anahtar öneki (evren_llm öneki) | 0 |
| `hi_` önekli uzun token biçimi | 0 |
| GitHub PAT öneki | 0 |
| `sk-` önekli uzun anahtar biçimi | 0 |
| Google API anahtar öneki | 0 |
| Groq anahtar öneki | 0 |
| Telegram bot token biçimi (sayı:harf) | 0 |
| IPv4 adresi biçimi | 0 |
| Cloudflare Workers alan adı biçimi | 0 |
| Eski API alan adı | 0 |
| Eski kişisel ad (iki ayrı ad) | 0 / 0 |

Tarama komutu özeti (desenler bu dosyada düz metin olarak tekrar edilmedi):

```bash
# Her desen ayrı ayrı grep -rEI ile aranır; sonuç satır sayısı 0 olmalıdır.
# Kapsam: /home/ubuntu/orbion-release (binary/pycache hariç)
```

## 5. Sözdizimi doğrulaması

Her `.py` dosyası için `python3 -m py_compile` çalıştırıldı; **tamamı geçti**:

- `scripts/agent.py`
- `scripts/telegram_bot.py`
- `scripts/actions.py`
- `scripts/identity.py`
- `scripts/roles.py`
- `scripts/projects.py`
- `scripts/dev_bot.py`
- `scripts/dev_worker.py`
- `scripts/web_research.py`

## 6. Notlar

- `wear/` ve `watchface/` kaynak kodu kaynak ağaçta **bulunamadı**; bu nedenle
  pakete eklenmedi. Ayrı depoda varsa token'sız olarak eklenebilir.
- Bu paket canlı `evren-agent` servisinin davranışını değiştirmez; yalnızca ayrı
  bir klasörde yayın kopyası üretir.
- Gerçek değerler hiçbir dosyaya yazılmadı; `.env.example` yalnızca iskelet içerir.
