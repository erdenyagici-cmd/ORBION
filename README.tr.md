<div align="center">

# ⬡ ORBION

**Kendi sunucunda çalışan, çok-ajanlı kişisel AI ofisi.**
Bulut bağımlılığı yok. Değiştirilebilir model havuzu. Verin senin makinende kalır.

[![License: MIT](https://img.shields.io/badge/Lisans-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Self-hosted](https://img.shields.io/badge/kendi%20sunucunda-%E2%9C%93-success)
![Zero-cost](https://img.shields.io/badge/s%C4%B1f%C4%B1r%20maliyet-Gemini%20%C2%B7%20Groq%20%C2%B7%20Cloudflare%20%C2%B7%20Ollama-informational)

*[English](README.md) · Türkçe · [Deutsch](README.de.md) · [العربية](README.ar.md) · [中文](README.zh.md)*

</div>

---

## ORBION nedir?

ORBION, tek bir küçük sunucuyu (**ücretsiz bir VM bile yeter**) kişisel bir **AI ofisine**
çevirir: işi uzman ajanlara dağıtan bir koordinatör, canlı bir web konsolu, bir Telegram
arayüzü ve hatta ajanlarının ne yaptığını gerçek zamanlı gösteren bir **Wear OS saat yüzü**.

Üç inatçı kural etrafında inşa edildi: **ücretsiz altyapıda çalış, kişisel veriyi asla
sızdırma, tek bir AI sağlayıcısına asla bağlı kalma.**

## Neden yaptım?

Bu proje, "AI asistanı" fikrinin bende bıraktığı bir rahatsızlıktan doğdu. Piyasadaki her
asistan seni bir buluta, bir aboneliğe, bir şirketin kapalı kapılarına bağlıyordu. Verin
orada bir yerde duruyordu — kimin okuduğunu bilmeden. Aylık fatura büyüyordu. Bir gün
sağlayıcı fiyatı değiştirse, modeli kapatsa, kotayı düşürse, senin "asistanın" bir anda
suskun bir kutuya dönüşüyordu.

Ben bunun tersini istedim: **benim olan, benim makinemde çalışan, benim kurallarımla
davranan bir ofis.** Ve bunu **sıfır maliyetle** yapmanın mümkün olduğunu kanıtlamak
istedim.

Bu yüzden ORBION:
- **Ücretsiz bir sunucuda** (ARM, GPU yok, ücretsiz katman) 7/24 çalışır.
- **Ücretsiz AI kotalarını** akıllıca kullanır: Gemini → Groq → Cloudflare → yerel Ollama.
  Biri düşerse otomatik diğerine geçer. Tek bir sağlayıcıya asla mahkûm değilsin.
- **Parasını verdiğin token'ı boşa harcamaz:** sınıflandırma, yönlendirme, sayım gibi
  işleri LLM'e değil, koda yaptırır. Model *son çare*, ilk tercih değil.
- **Verin sende kalır.** Kişisel veriler dışarı çıkmaz; dışarı giden tek şey *genel*
  sorular olur.

Kısacası: **AI senin olmalı.** Bir abonelik değil, senin çalıştırdığın bir sistem.

## Neden farklı

| | Tipik AI asistanı | **ORBION** |
|---|---|---|
| Nerede çalışır | Sağlayıcı bulutu | **Senin sunucun** |
| AI sağlayıcısı | Tek sağlayıcıya kilitli | **Değiştirilebilir havuz** (Gemini · Groq · Cloudflare · yerel Ollama) |
| Maliyet | Token başına fatura | **Ücretsiz katmanlar + yerel yedek** |
| İş kanıtı | "Yaptım" | **Her ajan çağrısı için `call_id` + durum** |
| Veri | Birine yüklenir | **Senin makinende kalır** |
| Varlık | Bir sohbet penceresi | **Canlı ofis, saat yüzü, Telegram** |

## Özellikler

- **Çok-ajanlı orkestrasyon** — bir koordinatör uzman ajanlara (asistan, kodcu, mail
  arşivcisi, araştırmacı) iş dağıtır. Her çağrı `call_id`, durum ve süre ile kaydedilir;
  böylece "yaptım" her zaman kanıtla desteklenir.
- **Sağlayıcıdan bağımsız LLM havuzu** — OpenAI-uyumlu adaptörlerle otomatik failover:
  Gemini → Groq → Cloudflare Workers AI → yerel Ollama. Biri düşünce ORBION sessizce
  sonrakine geçer. Tek nokta arıza yok.
- **Önce sıfır maliyet** — yapılabilecek her şeyi deterministik kod yapar (sınıflandırma,
  yönlendirme, sayım için LLM çağrısı yok). LLM *son çare*.
- **Telegram arayüzü** — doğal sohbet + her ajan için ayrı kanal.
- **Canlı web konsolu** — ajanlarının gerçek zamanlı "ofis" görünümü, servis limitlerini
  gösteren bir sistem paneli ve tek ekran operasyon panosu.
- **Wear OS eşlik uygulaması** — ajanların kadrana dizildiği bir saat yüzü ve canlı ofisi
  gösteren bir uygulama; durum ışıkları sunucudan güncellenir.
- **Tasarımdan gelen güvenlik** — asistan kaynak uydurmaz, yapmadığı işi yaptım demez,
  bir araç yoksa dürüstçe söyler.

## Mimari özeti

- **Tek süreçli çekirdek, izole thread'li ajanlar** — 2–4 vCPU'da ucuza çalışır.
- **Katı kanıt modeliyle SQLite** — `agent_calls`, `agent_tasks`, `agent_events`; her
  devredilen iş uçtan uca izlenebilir.
- **Atomik iş kuyruğu** — `claim / heartbeat / reclaim` + zaman aşımı; çöken worker işi
  sessizce kaybetmez.
- **Fail-closed güvenlik** — eksik token korumalı uçları açmaz, kapatır; logda sır yok,
  wildcard CORS yok.
- **Prompt-injection ve SSRF farkındalığı** — dış çağrılar özel/loopback/bulut-metadata
  IP aralıklarını reddeder; güvenilmeyen içerik modele gitmeden önce sınırlanır.

---

## 🩺 Gizlilik-Öncelikli Sağlık Yardımcısı *(referans tasarım — dağıtılmıyor)*

ORBION'un mimarisi, kasten **yayınlamadığımız** **sağlık-kritik** bir kullanım için
dövüldü. Sağlık verisi, kişisel verinin en hassas sınıfıdır ve açık bir repo bunun yeri
değildir. Ama tasarımı anlatmaya değer; çünkü ORBION'un etrafına çektiği güven sınırını
gösteriyor.

> **Ne yapar (özelde).** Ayrı bir sağlık ajanı — sakin, doktora düzeyinde bir hekim
> personası — sahibi için tek ve yaşayan bir tıbbi dosya yönetir; yanında bir giyilebilir
> veri hattı ve kanıta dayalı bir araştırma döngüsü vardır.

**Yapılandırılmış, kaynak etiketli kayıt**
Her kayıt kökenine göre etiketlenir — `USER_FACT`, `DEVICE_DATA`, `DOCTOR_NOTE`,
`RESEARCH_NOTE`, `PLAN`, `FOLLOW_UP`, `RESULT`, `DOCUMENT_FACT`,
`UNVERIFIED_EXTRACTION` — tam geçmişiyle. Yeni bilgi eskisini ezmez; zaman çizelgesi
korunur. Ajan *gerçek*, *gözlem* ve *takip*'i birbirinden net ayırır (tek bir kahve
kaydı "kafein problemi" açmaz).

**Kanıta dayalı araştırma, dürüstçe etiketli**
WHO, PubMed, ulusal ilaç kurumları ve klinik kılavuzlardan çeker; kanıt gücünü belirtir
(güçlü / orta / karışık / sınırlı) ve **asla kaynak uydurmaz**. Araştırma aracı yoksa,
"araştırdım" demek yerine yapamadığını söyler.

**Giyilebilir veri girişi**
Galaxy Watch → kendi sunucu: EKG dalga formu, nabız, SpO₂, uyku ve aktivite, **ayrı,
kısa ömürlü bir cihaz token'ı** ile akar — asla ana panel token'ı ile değil. Ham sinyaller
ölçüm olarak saklanır, net biçimde *tanı olarak değil*.

**Sert güvenlik rayları**
Tanı yok, reçete yok, doz değişikliği yok — ajan *bilgilendirir*, insan karar verir.
Kırmızı bayrak semptomlar (göğüs ağrısı, felç belirtisi, kanama, kendine zarar) uzun
analizi atlar ve acil servise yönlendirir (Türkiye'de 112). İlaç sorularına gerçek bilgi
+ net bir "bunu hekiminle görüş" gelir — asla "iç, iyi gelir" değil.

**İzolasyon ve sıfır sızıntı**
Sağlık ajanının hafızası genel asistandan izoledir. Ham sağlık verisi ofis balonlarına,
genel loglara, diğer ajanlara ya da yerel küçük modele asla ulaşmaz. Dış LLM çağrıları
yalnızca *genel* tıbbi sorular taşır — kişisel değerler, isimler, ölçümler makineden çıkmaz.

**Neden bu repoda değil**
KVKK/GDPR düzeyinde rıza, denetim izi ve tıbbi-sorumluluk yüzeyi, dikkatle incelenmiş bir
sınırın arkasında olmalıdır — herkese açık bir clone'da değil. Tam tasarım (veri izolasyonu,
rıza kapıları, denetim kaydı) özelde belgelenmiştir. ORBION'un desteklemek için inşa
edildiği titizlik tam da budur.

---

## Hızlı başlangıç

```bash
git clone https://github.com/erdenyagici-cmd/orbion.git
cd orbion
cp .env.example .env        # sağlayıcı anahtarlarını ve token'larını gir
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python3 scripts/agent.py    # çekirdeği başlatır (botlar + web konsolu)
```

Tam kurulum (**systemd, Cloudflare Tunnel, Telegram botu, Wear OS derleme**) için
**[docs/SETUP.md](docs/SETUP.md)**.

## Güvenlik modeli

- Her yüzey için ayrı token: panel, sağlık-ingest, saat — güven sınırları arasında
  **paylaşılan ana token yok**.
- Fail-closed: tanımsız token, ucunu açmak yerine kapatır.
- Logda, argv'de veya commit'te sır yok; `.env` git-ignored ve `chmod 600`.
- Dış çağrı SSRF korumalı (özel / loopback / metadata IP reddedilir).
- Shell/exec özellikleri **varsayılan kapalı**, açık bir opt-in flag arkasında.

## Durum

Erken ama gerçek. ORBION, ücretsiz bir ARM VM'de 7/24 canlı bir kişisel kurulumu çalıştırıyor.
API'ler ve iç yapı değişebilir; güvenlik ve kanıt ilkeleri değişmeyecek.

## Lisans

MIT — bkz. [LICENSE](LICENSE).

---

<div align="center"><sub>Ücretsiz bir sunucuda, "AI senin olmalı" inancıyla inşa edildi.</sub></div>
