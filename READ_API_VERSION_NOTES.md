## Read API Version Notes (Derin Açıklamalar)

Bu doküman, Read API’de yeni bir “major versiyon” (v2, v3, …) eklediğimizde **neden/neyi/ nasıl** değiştiğini ve pratik kullanımını anlatır.

- `READ_API_USAGE_GUIDE.md`: hızlı tarif / katalog (kısa, pratik)
- `READ_API_VERSION_NOTES.md` (bu dosya): **versiyon bazlı, geniş açıklamalar**, yanılgılar, edge-case’ler, client signal fikirleri

> Terminoloji: her yerde **`for` = takım**, **`against` = rakip**.

---

## v2 — Büyük değişiklikler (genel)

### v2 tasarım ilkesi
- **DB-only**: Read API quota tüketmez; sadece Postgres CORE/MART okur.
- **Deterministik**: aynı DB state için aynı output (tahmin yok).
- **Strict query params**: endpoint’in desteklemediği param gönderilirse 400.

---

## v2.1 — `/v2/fixtures` (NS grouped, tracked-only) — Derin Doküman

Endpoint:
- `GET /v2/fixtures?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD`

Ne sağlar?
- Seçilen UTC tarih aralığında **başlamamış (NS)** maçları döndürür.
- Sadece **tracked ligler** (kaynak: `config/jobs/daily.yaml -> tracked_leagues`).
- Aynı ligde farklı kickoff saatleri varsa, lig **aynı gün içinde birden fazla kez** listelenebilir (kickoff bucket).
- “UI için ilk sayfa” senaryosu: **en yakın kickoff bloklarını** hızlıca çıkarmak.

Neyi sağlamaz?
- LIVE/FT maçlar (sadece `NS`).
- Lig bazında “tüm günün” NS maçlarını tek satırda toplamak (bilinçli olarak kickoff-time’a göre bölünür).
- Lig/ülke bazında filtreleme (şimdilik sadece tracked + date range).

### Model (yüksek seviye)
- `leagues[]` aslında “(league_id, kickoff_time)” bucket listesidir.
- `match_count` sadece o kickoff anındaki maç sayısıdır.

### Alanlar (field-by-field)

#### ok (`$.ok`)
- Ne anlatır: Response başarılı üretildi mi?
- Nasıl kullanılmalı: Client ilk gate. `ok=false` ise metrikleri ignore + retry/log.
- Birlikte okunması gereken alanlar: `$.date_range`, `$.total_match_count`.
- Yanılgı/tuzağı: `ok=true` “her şey var” demek değil; `leagues=[]` olabilir (tracked yok / o aralıkta NS yok).
- Edge-case: tracked lig yoksa deterministik olarak boş döner.
- Örnek yorum: “ok=true, liste üretildi.”
- Signal fikri: `ok && total_match_count>0` ise “NS feed hazır”.

#### date_range (`$.date_range.from`, `$.date_range.to`)
- Ne anlatır: Uygulanan UTC tarih aralığı.
- Nasıl kullanılmalı: Cache key + UI header. `date_to<date_from` zaten 400.
- Birlikte okunması gereken alanlar: `$.leagues`.
- Yanılgı/tuzağı: Kullanıcı local timezone sanabilir; burada **UTC date**’tir.
- Edge-case: tek gün için from==to.
- Örnek yorum: “2026-01-05 gününün NS maçları.”
- Signal fikri: Cache TTL kısa (NS feed hızlı değişir; ama bu endpoint DB-only olduğu için DB update hızına bağlı).

#### total_match_count (`$.total_match_count`)
- Ne anlatır: Dönen tüm kickoff bucket’larındaki maçların toplamı.
- Nasıl kullanılmalı: UI badge, pagination ihtiyacı, “bugün kaç NS var” quick stat.
- Birlikte okunması gereken alanlar: `$.leagues[*].match_count`.
- Yanılgı/tuzağı: “ligde toplam NS” sanma; bu, date window içindeki **dönen bucket’ların toplamı**.
- Edge-case: 0 (hiç maç yok).
- Örnek yorum: “Bu aralıkta toplam 3 NS maç var.”
- Signal fikri: `total_match_count` aşırı yükselirse (örn. >200) UI grouping/virtualization gerekebilir.

#### leagues (`$.leagues[]`)
- Ne anlatır: Her eleman bir kickoff bucket: aynı lig aynı kickoff zamanı.
- Nasıl kullanılmalı: UI’da “sıradaki kickoff grupları” şeklinde kartlar üret.
- Birlikte okunması gereken alanlar: `$.leagues[*].matches`, `$.leagues[*].match_count`.
- Yanılgı/tuzağı: “league_id tekil olur” sanma; aynı lig farklı saatlerde tekrar gelir.
- Edge-case: boş liste.
- Örnek yorum: “Segunda Liga 18:00 kickoff bucket’ı listede.”
- Signal fikri: kickoff bucket’ları `leagues[]` sırasına göre tüket (zaten global kickoff’a göre sıralı).

#### league_id (`$.leagues[*].league_id`)
- Ne anlatır: Lig kimliği.
- Nasıl kullanılmalı: UI routing (league page), filtreleme (client-side).
- Birlikte okunması gereken alanlar: `league_name`, `country_name`, `season`.
- Yanılgı/tuzağı: Aynı `league_id` birden çok satırda olabilir (farklı kickoff).
- Edge-case: yok (DB join gerektirir; yoksa row zaten çıkmaz).
- Örnek yorum: “league_id=95 kickoff bucket’ı.”
- Signal fikri: `league_id` bazlı “upcoming bucket count” sayacı.

#### league_name / country_name (`$.leagues[*].league_name`, `$.leagues[*].country_name`)
- Ne anlatır: UI label’ları.
- Nasıl kullanılmalı: Human-readable gösterim; karar metriği değildir.
- Birlikte okunması gereken alanlar: `league_id`.
- Yanılgı/tuzağı: İsimler zamanla değişebilir; ID her zaman kaynak doğruluk.
- Edge-case: null (teoride; ama join ile dolu beklenir).
- Örnek yorum: “Portugal / Segunda Liga.”
- Signal fikri: yok (label).

#### season (`$.leagues[*].season`)
- Ne anlatır: Fixture’ın season değeri (kickoff bucket içindeki fixtures ile uyumlu).
- Nasıl kullanılmalı: UI filtre, downstream query param.
- Birlikte okunması gereken alanlar: `league_id`.
- Yanılgı/tuzağı: Aynı date range içinde farklı season karışımı nadirdir ama teorik olarak olabilir.
- Edge-case: null (DB’de season yoksa).
- Örnek yorum: “2025 sezonu kickoff bucket’ı.”
- Signal fikri: “current season health check” (season null görürsen data quality flag).

#### match_count (`$.leagues[*].match_count`)
- Ne anlatır: Bu kickoff bucket’ındaki maç sayısı; **`matches.length` ile eşit olmalı**.
- Nasıl kullanılmalı: UI’da “18:00’da 3 maç” gibi grup sayacı.
- Birlikte okunması gereken alanlar: `matches`.
- Yanılgı/tuzağı: “ligde toplam 3 maç var” sanma; sadece o kickoff anı.
- Edge-case: 0 olmamalı (bucket boş dönmez).
- Örnek yorum: “Bu kickoff saatinde 3 maç var.”
- Signal fikri: `match_count>=5` ise kickoff bucket “high concurrency window” (notification batching).

#### has_matches (`$.leagues[*].has_matches`)
- Ne anlatır: Tarihsel olarak boş grup döndürmemek için sabit `true`.
- Nasıl kullanılmalı: Genelde gereksiz; backward compat için var.
- Birlikte okunması gereken alanlar: `match_count`.
- Yanılgı/tuzağı: false bekleme; bu endpoint boş grup üretmez.
- Edge-case: yok.
- Örnek yorum: “Bucket dolu.”
- Signal fikri: yok.

#### matches (`$.leagues[*].matches[]`)
- Ne anlatır: O kickoff bucket’ındaki maç listesi.
- Nasıl kullanılmalı: UI listesi; match detail’e geçiş; “yaklaşan maçlar”.
- Birlikte okunması gereken alanlar: `date_utc`, `home_team_id`, `away_team_id`, `id`.
- Yanılgı/tuzağı: Tek maçlık bucket normaldir; match_count=1 demek kickoff anında tek maç var.
- Edge-case: matches boş olmamalı.
- Örnek yorum: “18:00 kickoff’ta Benfica B - Porto B var.”
- Signal fikri: `id` üzerinden detail prefetch kuyruğu (rate-limit yok ama DB yükünü dengeli tut).

##### match.id (`$.leagues[*].matches[*].id`)
- Ne anlatır: Fixture ID (primary key).
- Nasıl kullanılmalı: `/read/fixtures/{fixture_id}` gibi detail uçlarına geçişte kullan.
- Birlikte okunması gereken alanlar: `updated_at_utc`, `status_short`.
- Yanılgı/tuzağı: Aynı fixture farklı endpoint’lerde farklı şekillerde görünebilir; ID değişmez.
- Edge-case: yok.
- Örnek yorum: “fixture_id=1398121.”
- Signal fikri: “new fixture detected” = previously unseen id.

##### match.round (`$.leagues[*].matches[*].round`)
- Ne anlatır: Round/hafta bilgisi.
- Nasıl kullanılmalı: UI label; takvim filtrelemede yardımcı.
- Birlikte okunması gereken alanlar: `league_id`, `season`.
- Yanılgı/tuzağı: Round string formatı ligden lige değişir.
- Edge-case: null.
- Örnek yorum: “Regular Season - 17.”
- Signal fikri: yok.

##### match.date_utc / timestamp_utc (`$.leagues[*].matches[*].date_utc`, `$.leagues[*].matches[*].timestamp_utc`)
- Ne anlatır: Kickoff zamanı (UTC ISO + epoch).
- Nasıl kullanılmalı: Sıralama, countdown, timezone display dönüşümü (client-side).
- Birlikte okunması gereken alanlar: `status_short`.
- Yanılgı/tuzağı: Local timezone sanma; display için client dönüştürür, storage/logic UTC kalır.
- Edge-case: null (olmamalı).
- Örnek yorum: “Kickoff: 2026-01-05 18:00 UTC.”
- Signal fikri: “starts_soon” = kickoff - now <= 30m.

##### match.status_short / status_long (`$.leagues[*].matches[*].status_short`, `$.leagues[*].matches[*].status_long`)
- Ne anlatır: Maç statüsü; bu endpoint’te `status_short` pratikte `NS`.
- Nasıl kullanılmalı: UI badge; sanity check.
- Birlikte okunması gereken alanlar: `updated_at_utc`.
- Yanılgı/tuzağı: NS dışı görürsen data quality bug/DB drift.
- Edge-case: yok.
- Örnek yorum: “Not Started.”
- Signal fikri: Eğer NS endpoint’inde NS dışı sayarsan alarm.

##### match.home_team_id/name, away_team_id/name (`$.leagues[*].matches[*].home_team_id`, `home_team_name`, `away_team_id`, `away_team_name`)
- Ne anlatır: Takımlar.
- Nasıl kullanılmalı: UI render + team page route.
- Birlikte okunması gereken alanlar: fixture id, date_utc.
- Yanılgı/tuzağı: Name değişebilir; ID kalıcı.
- Edge-case: null (olmamalı).
- Örnek yorum: “Benfica B vs FC Porto B.”
- Signal fikri: Team-based “upcoming count” aggregation.

##### match.updated_at_utc (`$.leagues[*].matches[*].updated_at_utc`)
- Ne anlatır: CORE fixture kaydının en son güncellenme zamanı.
- Nasıl kullanılmalı: Cache invalidation (client-side), “stale UI” tespiti.
- Birlikte okunması gereken alanlar: `date_utc`, `status_short`.
- Yanılgı/tuzağı: updated_at yeni değilse “kickoff değişmedi” sanma; sadece DB update anı.
- Edge-case: null.
- Örnek yorum: “Bu fixture en son 2025-12-16’da update edilmiş.”
- Signal fikri: `stale_ns = now - updated_at_utc > 7d` ise data refresh check.

### Quick recipes (v2/fixtures)
- “Bugün hangi kickoff saatlerinde yoğunluk var?” → `leagues[].date_utc` + `match_count` ile histogram.
- “Sıradaki kickoff gruplarını sırayla push notification’a çevir” → `leagues[]` zaten global kickoff’a göre sıralı.

### Anti-patterns (v2/fixtures)
- Aynı `league_id`’yi tekil sanıp client-side map’e yazıp satırları ezmek.
- `match_count`’ı “ligde gün boyu toplam maç” sanmak.
- UTC date’leri local date sanıp yanlış gün filtrelemek.

---

## v2.2 — `/v2/teams/{team_id}/breakdown` — Derin Doküman

Endpoint:
- `GET /v2/teams/{team_id}/breakdown?last_n=20&as_of_date=YYYY-MM-DD`

### Ne sağlar? Neyi sağlamaz?
- **Sağlar**: Son N tamamlanmış maç (FT/AET/PEN) üzerinden deterministik özet: goller (1Y/2Y), kartlar (1Y/2Y), korner/ofsayt totals, ev/deplasman split, rakip zorluğu (form).
- **Sağlamaz**: per-match liste, korner/ofsayt half split, xG, lineup kalite, sakatlık vb.

### Window nasıl okunur? (`$.window`)
- `last_n`: istenen maksimum maç sayısı (default 20, cap 50).
- `played`: gerçekten hesaplamaya giren maç sayısı. `played < last_n` normaldir.
- `as_of_utc`: cutoff (as_of_date verilirse UTC end-of-day).

> `played` ile `matches_available` farkı kritiktir: bazı bloklar “kısmi veri” ile hesaplanır (özellikle events/stats/form eksikse).\n
> **API sözleşmesi olarak öneri**: `*_avg` ve `*_rate` alanları, ilgili bloğun `matches_available` paydasına göre hesaplanmalı; aksi halde bias büyür.

### Split neden var? (`$.overall`, `$.home`, `$.away`)
- Ev/deplasman etkisi yüzünden (tempo, hakem standardı, oyun planı) aynı takımın metrikleri sistematik kayabilir.
- Upcoming fixture home ise `home` bloğunu; away ise `away` bloğunu ana referans al; `overall` ile stabilize et.

### Formüller (sözleşme)
- `gf_avg = gf / played`
- `ga_avg = ga / played`
- `total_goals_avg = total_goals / played`
- `over_1_5_rate = count( (gf_i + ga_i) >= 2 ) / played`
- `over_2_5_rate = count( (gf_i + ga_i) >= 3 ) / played`
- Half avg’ler için öneri: `half.gf_avg = half.gf / half.matches_available`

### Alanlar (field-by-field)
Not: Aşağıdaki metrikler `$.overall`, `$.home`, `$.away` altında aynı şemada bulunur. JSONPath’te `{S}` = `overall|home|away`.

#### segment played (`$.{S}.played`)
- Ne anlatır: Bu segmentte kaç maç var.
- Nasıl kullanılmalı: Home/away kıyaslarında örneklem dengesini kontrol et.
- Birlikte okunması gereken alanlar: `$.window.played`, `$.{S}.opponent_strength.matches_available`.
- Yanılgı/tuzağı: `home.played` küçükse ev etkisi hakkında kesin hüküm çıkarma.
- Edge-case: 0 maç.
- Örnek yorum: Örnek JSON’da `home.played=4` → ev metrikleri daha oynak.
- Signal fikri: `played>=6` altına “low confidence” etiketi.

#### goals (`$.{S}.goals.*`)
- Ne anlatır: Takımın gol üretimi/yeme ve gol eşiği frekansı.
- Nasıl kullanılmalı: `gf_avg` (hücum) + `ga_avg` (savunma) birlikte okunmalı; `total_goals_avg` maçın açıklığını gösterir.
- Birlikte okunması gereken alanlar: `$.{S}.goals_by_half`, `$.{S}.opponent_strength`, `$.{S}.played`, `$.away` vs `$.home`.
- Yanılgı/tuzağı: All-competitions karışımı lig standardını bozabilir; skor etkisi (öndeyken tempo düşüşü) total’i tempo sanmana neden olur.
- Edge-case: `played=0` ise avg/rate null olmalı (öneri).
- Örnek yorum: overall `gf_avg=1.7`, `ga_avg=1.4`, `over_2_5_rate=0.7` → maçlar çoğunlukla 3+ gol bandına çıkmış.
- Signal fikri: `edge = (gf_avg - ga_avg)`; opponent_strength ile ölçekle.

#### goals_by_half (1Y) (`$.{S}.goals_by_half.first_half.*`)
- Ne anlatır: İlk yarı gol profili.
- Nasıl kullanılmalı: “Hızlı başlayan” vs “geç açılan” profil için 2Y ile karşılaştır.
- Birlikte okunması gereken alanlar: `$.{S}.goals_by_half.second_half`, `$.{S}.goals`, `$.{S}.played`, `$.{S}.opponent_strength`.
- Yanılgı/tuzağı: Half verisi score veya events’ten gelebilir; kaynak eksikleri `matches_available` ile görünür.
- Edge-case: `matches_available < played` → half avg paydasını `matches_available` say.
- Örnek yorum: overall 1Y `gf_avg=0.9`, `ga_avg=0.5` → ilk yarıda avantaj kuruyor.
- Signal fikri: `fast_start = (gf_avg - ga_avg)`; threshold > 0.25 ve matches_available>=6.

#### goals_by_half (2Y) (`$.{S}.goals_by_half.second_half.*`)
- Ne anlatır: İkinci yarı gol profili.
- Nasıl kullanılmalı: 2Y `ga_avg` yükseliyorsa “geç kırılganlık” riski.
- Birlikte okunması gereken alanlar: `$.{S}.cards_by_half.second_half`, `$.{S}.opponent_strength`, `$.{S}.goals_by_half.first_half`.
- Yanılgı/tuzağı: Skor etkisi çok güçlüdür (öndeyken geri çekilme).
- Edge-case: `matches_available` düşükse yorum zayıf.
- Örnek yorum: overall 2Y `ga_avg=0.9` > 1Y `0.5` → maç sonu daha çok gol yiyor.
- Signal fikri: `late_fragility = second_half.ga_avg - first_half.ga_avg`.

#### cards_by_half (1Y) (`$.{S}.cards_by_half.first_half.*`)
- Ne anlatır: İlk yarı disiplin/sertlik (for/against).
- Nasıl kullanılmalı: `yellow_for_avg` agresiflik/disiplin; `yellow_against_avg` rakip sertliği.
- Birlikte okunması gereken alanlar: `$.{S}.cards_by_half.second_half`, `$.{S}.corners_totals`, `$.{S}.offsides_totals`, `$.{S}.played`.
- Yanılgı/tuzağı: Hakem/lig kart standardı farklı; gerideyken daha çok kart bias’ı.
- Edge-case: `matches_available<played` ise avg paydasını `matches_available` kabul et (öneri).
- Örnek yorum: overall 1Y `yellow_for_avg=0.2` ama `yellow_against_avg=0.7` → rakipler daha sert başlıyor.
- Signal fikri: `discipline_gap = yellow_for_avg - yellow_against_avg`.

#### cards_by_half (2Y) (`$.{S}.cards_by_half.second_half.*`)
- Ne anlatır: İkinci yarı disiplin/sertlik.
- Nasıl kullanılmalı: 2Y sarı artışı “maç sonu kontrol” riskini gösterir; red nadir olduğu için dikkatli.
- Birlikte okunması gereken alanlar: `$.{S}.goals_by_half.second_half`, `$.{S}.played`.
- Yanılgı/tuzağı: Küçük örneklemde red oranı yanıltır.
- Edge-case: `matches_available` düşük.
- Örnek yorum: overall 2Y `yellow_for_avg=1.6` → maç sonu çok kart görüyor.
- Signal fikri: `late_discipline_risk = yellow_for_avg + 3*red_for_avg`.

#### corners_totals (`$.{S}.corners_totals.*`)
- Ne anlatır: Korner hacmi (baskı proxy) for/against.
- Nasıl kullanılmalı: `for_avg` yüksekse atak baskısı; `against_avg` yüksekse rakip baskısı.
- Birlikte okunması gereken alanlar: `$.{S}.offsides_totals`, `$.{S}.opponent_strength`, `$.{S}.goals.gf_avg`.
- Yanılgı/tuzağı: Tempo sanma; kanat oyunu korneri şişirebilir.
- Edge-case: stats eksikse `matches_available<played`.
- Örnek yorum: overall `for_avg=6`, `against_avg=4.5` → baskı üstünlüğü.
- Signal fikri: `corner_pressure = for_avg - against_avg` (zorluk ile ölçekle).

#### offsides_totals (`$.{S}.offsides_totals.*`)
- Ne anlatır: Arkaya koşu / ofsayt tuzağı proxy (for/against).
- Nasıl kullanılmalı: `for_avg` yüksekse koşu niyeti; `against_avg` savunma çizgisi/tuzağı.
- Birlikte okunması gereken alanlar: `$.{S}.corners_totals`, `$.{S}.goals_by_half`, `$.{S}.opponent_strength`.
- Yanılgı/tuzağı: VAR/hakem standardı ligden lige değişir; all-competitions bias.
- Edge-case: stats eksikse `matches_available`.
- Örnek yorum: overall `for_avg=1.7` → sürekli arkaya koşuyor.
- Signal fikri: `run_in_behind_intent = offsides.for_avg`.

#### opponent_strength (`$.{S}.opponent_strength.*`)
- Ne anlatır: Rakip zorluğu düzeltmesi için form puanı ortalaması (last5 W=3/D=1/L=0).
- Nasıl kullanılmalı: Gol/kart/korner gibi metrikleri “kolay/zor fikstür” bağlamında düzeltir.
- Birlikte okunması gereken alanlar: `$.{S}.goals.gf_avg`, `$.{S}.goals.ga_avg`, `$.{S}.played`, `$.{S}.opponent_strength.by_outcome`.
- Yanılgı/tuzağı: Form tek boyut; sakatlık/rotasyon yok. Ligler arası ölçek farkını çözmez.
- Edge-case: `matches_available<played` (form yok); outcome altlarında `matches_available=0` → avg null.
- Örnek yorum: overall `avg_points_last5=7` → rakipler orta-zor.
- Signal fikri: `adj_attack = gf_avg * (avg_points_last5/7)` (clamp).

### Quick recipes (v2/team breakdown)
- Gol trend: `goals.gf_avg/ga_avg` + `goals_by_half` ile “erken/geç” profil.
- Disiplin/tempo proxy: `cards_by_half` + `corners_totals` + `offsides_totals`.
- Ev/deplasman: upcoming match context’e göre `home` veya `away` bloğunu ana al.
- Zorluk düzeltme: `opponent_strength` ile “kolay fikstür şişmesi”ni düzelt.

### Anti-patterns (v2/team breakdown)
- `matches_available` farkını yok sayıp half/stats avg’lerini yanlış paydayla yorumlamak.
- `over_2_5_rate`’i “takım 2.5 üst atıyor” sanmak (bu maç toplamı).
- `home.played=4` iken ev profilini “kesin” saymak.

---

## v2.3 — `/v2/matchup/predict` (anomaly-aware scoreline prediction) — Derin Doküman

Endpoint:
- `GET /v2/matchup/predict?home_team_id=...&away_team_id=...&last_n=5&as_of_date=YYYY-MM-DD`

Ne sağlar?
- İki takımın **son N tamamlanmış maçından** (FT/AET/PEN), tüm turnuvalar dahil, deterministik bir “skorline olasılık” özeti üretir.
- Çıktı formatı: **1 most_likely + 2 alternative + 3 unexpected** skorline (toplam 6 adet) ve bunların olasılıkları.

Nasıl çalışır (pratik, anlaşılır özet):
- Her takım için last-N maçlar çekilir (home+away karışık).
- Her maç için:
  - **Recency weight**: en yeni maçlar biraz daha ağır basar.
  - **Opponent strength**: rakibin `form` (last5) puanına göre kolay/zor rakip bias’ı düzeltilir.
  - **Anomali**: aşırı uç skorlar (örn. 6-0) tamamen silinmez; **ağırlığı düşürülür**.
- Sonuç: `expected_goals_home/away` (\(\lambda\)) üretilir ve Poisson grid üzerinde skorline olasılıkları hesaplanır.

Yanılgı/tuzağı:
- Bu endpoint “kesin skor” söylemez; **olasılık dağılımı** üretir.
- Anomali düzeltilmiş olsa bile “tek maçlık aşırı sonuç” küçük örneklemde tüm modeli oynatabilir → `warnings` ve window ölçümlerini dikkate al.

Edge-case’ler:
- Takımlardan biri için last-N içinde hiç tamamlanmış maç yoksa: `400 detail=insufficient_history`
- Rakip form verisi eksikse: `warnings` içinde `missing_opponent_form_points_for_some_matches` görülür (yine de sonuç üretilir).

Client-side signal önerisi:
- “Confidence” üretmek için basit kural: `min(home_played, away_played) < 5` ise confidence düşür; `warnings` varsa ek düşür.

### Client-side signal fikirleri (3)
- `AttackVsDefenseEdge`: `gf_avg - ga_avg` (zorluk ile ölçekle).
- `LateGameRisk`: `second_half.ga_avg + 0.3*second_half.yellow_for_avg + 2*second_half.red_for_avg`.
- `PressureProfile`: `corners.for_avg + 0.5*offsides.for_avg - 0.5*corners.against_avg`.


