# INVEST Alert

Lekka PWA do monitorowania cen akcji, krypto i innych tickerów z prostym panelem mobilnym, historią odczytów i powiadomieniami push.

Aplikacja działa bez własnego backendu aplikacyjnego:
- frontend to statyczne `index.html`,
- harmonogram i logika sprawdzania cen lecą przez GitHub Actions,
- stan zapisuje się w repo w `data/last_run.json`,
- dane mikrostruktury OML zapisują się w `data/market_micro.json`,
- subskrypcja Web Push trzymana jest w `data/subscription.json`.

## Co robi projekt

- sprawdza ceny dla wielu tickerów według własnych slotów czasowych,
- zapisuje ostatnie odczyty do PWA,
- trzyma historię punktów z ostatnich 5 dni,
- rysuje mały wykres liniowy pod listą kursów,
- dla OML scrapuje arkusz zleceń i ostatnie transakcje,
- liczy proste metryki mikrostruktury, np. `order book imbalance` i presję 1h,
- wysyła push, gdy cena wyjdzie poza ustawione widełki,
- pozwala ręcznie wywołać odświeżenie z poziomu PWA.

## Obsługiwane instrumenty

Konfiguracja jest wielotickerowa. Możesz monitorować np.:
- akcje,
- krypto,
- inne symbole dostępne w aktualnym źródle danych.

Każdy ticker ma własne:
- `id`,
- `name`,
- `ticker`,
- `currency`,
- `lower` i `upper`,
- `schedule`,
- `weekdaysOnly`.

Przykład konfiguracji siedzi w `data/config.json`.

## Jak to działa

1. GitHub Actions uruchamia `scripts/check_price.py` według crona.
2. Skrypt pobiera kurs dla każdego tickera.
3. Jeśli slot pasuje, zapisuje odczyt do `data/last_run.json`.
4. Dla `OML` dodatkowo scrapuje arkusz zleceń i tabelę transakcji do `data/market_micro.json`.
5. Frontend pobiera `config.json`, `last_run.json` i `market_micro.json`, a potem pokazuje sloty, historię, wykres i bloki mikrostruktury.
6. Jeśli cena spadnie poniżej `lower` albo wzrośnie powyżej `upper`, wysyłany jest push.

Projekt ma też mechanizm nadrabiania pominiętych slotów scheduled run, jeśli GitHub odpali cron z opóźnieniem.

## Struktura repo

```text
.
├── .github/workflows/check-price.yml
├── data/
│   ├── config.json
│   ├── last_run.json
│   ├── market_micro.json
│   └── subscription.json
├── icons/
├── index.html
├── manifest.webmanifest
├── scripts/
│   ├── check_price.py
│   └── requirements.txt
└── sw.js
```

## Wymagane sekrety GitHub

Workflow używa sekretów repo:
- `VAPID_PRIVATE_KEY`
- `VAPID_PUBLIC_KEY`
- `VAPID_SUBJECT`

Nie trzymaj ich w repo ani w README.

## Ręczne użycie z PWA

Z poziomu aplikacji można:
- zapisać repo i PAT lokalnie w przeglądarce,
- zmieniać progi dla aktywnego tickera,
- zapisać subskrypcję push do repo,
- odpalić ręczny refresh przez `repository_dispatch`.

## Lokalne uruchomienie

Do samego podglądu frontendu wystarczy prosty serwer statyczny, np.:

```bash
python3 -m http.server
```

Jeśli chcesz uruchomić checker lokalnie:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r scripts/requirements.txt
python scripts/check_price.py
```

Do wysyłki push lokalnie nadal potrzebne są poprawne zmienne środowiskowe VAPID.

## Ograniczenia

- obecne źródło danych jest skonfigurowane pod bieżący fetch w skrypcie,
- mikrostruktura OML pochodzi ze scrapingu publicznej strony i jest opóźniona,
- historia wykresu buduje się z zapisanych slotów, więc po świeżym wdrożeniu potrzebuje kilku przebiegów,
- iOS potrafi mocno cache'ować ikonę PWA i manifest.

## Bezpieczeństwo

- nie commituj PAT-a,
- nie commituj prywatnych kluczy VAPID,
- nie wrzucaj do repo prywatnych endpointów ani danych urządzenia poza tym, co jest niezbędne do działania push.
