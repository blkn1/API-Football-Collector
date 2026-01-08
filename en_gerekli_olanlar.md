## Read API — İstatistik için “en gerekli olanlar” (v2 öncelikli)

Amaç: **Sadece istatistik** tüketen bir client’ın, **en az sayıda endpoint** ile güvenilir bir akış kurması.

Kural:
- **V2 varsa onu kullan** (gerçek geliştirme/iyileştirme burada olur)
- V2 yoksa **v1 veya /read** ile tamamla
- Aynı işi yapan iki endpoint’i **önermiyoruz** (tekrar/çakışma olmasın)

Bu doküman, `gpt_actions_openapi4.json` ile uyumludur (istatistik + yaklaşan maçlar odaklı).

---

## 0) Ortak sözleşmeler (client için kritik)

- **Auth**: Basic Auth (READ_API_BASIC_USER / READ_API_BASIC_PASSWORD).
- **Strict query params**: Endpoint’in desteklemediği query param gönderirsen **400** alırsın.
- **Zaman**: Tüm date/time mantığı **UTC**. Client display için timezone dönüştürür ama request paramları UTC’dir.
- **DB-only**: Read API, API-Football quota tüketmez; sadece Postgres CORE/MART okur.

---

## En profesyonel minimal akışlar

### A) Günün maçları / önümüzdeki maçlar (v2 — önerilen)
`GET /v2/fixtures?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD`

- **Ne verir?** Sadece **NS (Not Started)** maçlar.
- **Scope**: Sadece **tracked ligler** (config/jobs/daily.yaml → tracked_leagues).
- **Grouping**: `leagues[]` her elemanda **(league_id, kickoff_time)** bucket vardır. Aynı lig, farklı kickoff saatleri varsa **birden fazla kez** listelenebilir.
- **Sıralama**: Bucket’lar global olarak kickoff’a göre sıralıdır (UI için “yaklaşan kickoff blokları”).

Örnek (bugün + yarın):

```bash
curl -sS -u "$READ_API_BASIC_USER:$READ_API_BASIC_PASSWORD" \
  "https://readapi.zinalyze.pro/v2/fixtures?date_from=2026-01-05&date_to=2026-01-06"
```

Client önerileri:
- `match_count` = **o kickoff bucket’ındaki** maç sayısıdır (lig gün boyu toplamı değildir).
- UI’da `leagues[]`’i “kickoff blokları” gibi render etmek en doğru modeldir.
- Cache: kısa TTL önerilir (collector update hızına bağlı; ama UI “yaklaşan maçlar” için doğal olarak dinamiktir).

---

### B) Takım istatistiği (en az endpoint ile) — önerilen ana akış

#### 1) Takımı bul → `team_id`
`GET /v1/teams?search={query}&limit=...`

- **Neden bu endpoint?** İstatistik endpoint’leri `team_id` ile çalışır. Bu uç, DB’den (CORE) takım araması yapar ve client’a doğru ID’yi verir.
- **Önerilen kullanım**:
  - Kullanıcı yazdıkça `search` ile autocomplete yap.
  - Sonuçları `id + name + country` ile göster; seçilen satırın `id`’si bir sonraki çağrının anahtarıdır.
  - Çok genel aramalarda `limit` ile UI’ı boğma (10–30 idealdir).

Örnek:

```bash
curl -sS -u "$READ_API_BASIC_USER:$READ_API_BASIC_PASSWORD" \
  "https://readapi.zinalyze.pro/v1/teams?search=Fener&limit=20"
```

Notlar:
- Bu endpoint **DB-only** çalışır; API-Football quota tüketmez.
- Datanın kapsamı, collector’ın CORE’a yazdığı takımlarla sınırlıdır (pratikte tracked ligler).

---

#### 2) Takım istatistiğini tek çağrıda al (v2 — primary) → `breakdown`
`GET /v2/teams/{team_id}/breakdown?last_n=20&as_of_date=YYYY-MM-DD`

- **Neden bu endpoint?** “Bir kaç endpoint ile tüm istediğimiz istatistikler” hedefi için v2’de ana uç budur. Son N tamamlanmış maçtan (FT/AET/PEN) deterministik özet üretir:
  - **goals+cards by half**
  - **corners/offsides totals**
  - **overall + home + away split**
  - **rakip zorluğu (form) bağlamı**

Önerilen parametreler:
- **`last_n=20`**: dengeli örneklem (çok küçük N → oynak, çok büyük N → güncellik kaybı).
- **`as_of_date`**: “bugüne kadar” gibi sabit bir pencere üretmek için kullan; aksi halde “şimdi” baz alınır.

Örnek:

```bash
curl -sS -u "$READ_API_BASIC_USER:$READ_API_BASIC_PASSWORD" \
  "https://readapi.zinalyze.pro/v2/teams/611/breakdown?last_n=20&as_of_date=2026-01-05"
```

Client-side okuma kuralı (kritik):
- `overall` genel profil; upcoming maç **home** ise `home`, **away** ise `away` bloğunu birincil referans al.
- `matches_available` < `played` görülen alt bloklarda (örn. half/stats/form eksikliği) “confidence düşük” etiketi kullan.

---

## İstatistik için “gerçekten gerekli” endpoint listesi (tekrarsız)

- **(Lookup)** `/v1/teams`: sadece `team_id` bulmak için (v2 karşılığı yok).
- **(Primary stats, v2)** `/v2/teams/{team_id}/breakdown`: last-N deterministik özet (önerilen).
- **(Secondary stats, v1 — sadece ihtiyaç varsa)** `/v1/teams/{team_id}/metrics`: prediction-feature tarzı “daha geniş” sayısal özet (shots, possession vb. gibi alanlar burada olabilir). Breakdown yetmiyorsa kullan.
- **(League-season stats, /read — sadece ihtiyaç varsa)** `/read/team_statistics`: belirli `league_id + season + team_id` bağlamında raw takım istatistikleri + form (detay ham veri ihtiyacı için).
- **(Upcoming fixtures, v2)** `/v2/fixtures`: günün/önümüzdeki maçlar (tracked-only, NS-only).

Minimal akış: **2 endpoint** ile biter:
- `/v1/teams` → `team_id`
- `/v2/teams/{team_id}/breakdown` → istatistik

---

## Ne zaman “secondary” endpoint’lere çıkmalıyım?

### `/v1/teams/{team_id}/metrics` (secondary)
Kullan:
- Breakdown’ın kapsamadığı metrikler lazımsa (örn. **shots / possession** gibi “fixture_statistics” tabanlı feature’lar).
- Model/ML feature set’i ile geriye dönük uyum istiyorsan.

Kullanma:
- Sadece “1Y/2Y gol-kart + korner/ofsayt + ev/deplasman + rakip zorluğu” istiyorsan; bunlar breakdown’ın asıl işi.

### `/read/team_statistics` (secondary, league+season bağlamı)
Kullan:
- Tek bir lig/sezon bağlamında ham “team statistics” gerekiyorsa (örn. aynı lig içinde kıyas).
- “Form” gibi ham alanları league_id+season ile sabitlemek istiyorsan.

Kullanma:
- Ligler arası karışık, son-N “genel form profili” istiyorsan; bunun primary’i breakdown.

---

## Yaygın hatalar / tuzaklar (kısa)

- **Auth karışıklığı**: Bu akış Basic Auth varsayar (Bearer değil).
- **UTC/Local karışıklığı**: `date_from/date_to/as_of_date` UTC’dir.
- **Strict query**: Fazladan query param → 400 (client SDK’larında otomatik param ekleyen kodlara dikkat).
- **/v2/fixtures yorum hatası**: `match_count` lig toplamı değil; aynı lig farklı saatse ayrı bucket gelir.



