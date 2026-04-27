# OML Alert — PWA + GitHub Actions

PWA na iPhone'a (i każde inne urządzenie z Chrome/Safari 16.4+) wysyłająca **Web Push** o kursie spółki **One More Level (GPW: OML)** trzy razy dziennie — **9:30 / 14:30 / 17:30 (Europe/Warsaw)** — gdy kurs przekroczy ustawione progi.

Architektura: GitHub Pages hostuje frontend, GitHub Actions co 10 min sprawdza kurs ze [stooq.pl](https://stooq.pl) i wysyła push przez VAPID do subskrypcji zapisanej w repo. Brak własnego serwera, wszystko 100% darmowe.

---

## Jak to działa

```
┌─────────────────────────┐         ┌────────────────────────────┐
│ iPhone — PWA            │         │ GitHub Repo                │
│ (Safari, Add to Home)   │  PUT    │ data/config.json (progi)   │
│  ─ ustawiasz progi      ├────────▶│ data/subscription.json     │
│  ─ włączasz powiadom.   │ GitHub  │ data/last_run.json (state) │
└─────────────────────────┘  API    └──────────────┬─────────────┘
                                                   │ co 10 min
                                                   ▼
                                    ┌────────────────────────────┐
                                    │ GitHub Action (cron)       │
                                    │  1. czyta config + sub     │
                                    │  2. GET stooq.pl?s=oml     │
                                    │  3. jeśli kurs poza widełk.│
                                    │     → webpush(VAPID) ──────┼──▶ APNs ──▶ iPhone
                                    └────────────────────────────┘
```

---

## Krok 1 — Repo na GitHubie

1. Załóż **prywatne** repo, np. `azbroja/oml-pwa`.
2. Wgraj zawartość tej paczki do repa:
   ```bash
   cd oml-pwa
   git init && git add . && git commit -m "init"
   git branch -M main
   git remote add origin git@github.com:<ty>/<repo>.git
   git push -u origin main
   ```
   **NIE pushuj `VAPID_KEYS.txt`** — `.gitignore` go pomija, ale upewnij się, że nie poszedł.

---

## Krok 2 — VAPID keys → GitHub Secrets

Otwórz `VAPID_KEYS.txt` (jest w paczce). Skopiuj wartości i ustaw je w **Repo → Settings → Secrets and variables → Actions → New repository secret**:

| Nazwa secretu | Wartość |
|---|---|
| `VAPID_PUBLIC_KEY` | linia z URL-safe base64 (87 znaków, zaczyna się od `B`) |
| `VAPID_PRIVATE_KEY` | cały blok PEM, **z linijkami `-----BEGIN ... PRIVATE KEY-----` i `-----END ... PRIVATE KEY-----`** |
| `VAPID_SUBJECT` | `mailto:azbroja@outlook.com` |

> Klucze w `VAPID_KEYS.txt` zostały wygenerowane w sandboxie. Po wdrożeniu wygeneruj nowe lokalnie:
> ```bash
> openssl ecparam -genkey -name prime256v1 -noout -out vapid_priv.pem
> cat vapid_priv.pem      # → VAPID_PRIVATE_KEY
> openssl ec -in vapid_priv.pem -pubout -outform DER 2>/dev/null \
>   | tail -c 65 | base64 | tr -d '=\n' | tr '/+' '_-'   # → VAPID_PUBLIC_KEY
> ```

---

## Krok 3 — Włącz GitHub Pages

**Repo → Settings → Pages**:

- **Source**: *Deploy from a branch*
- **Branch**: `main` / folder `/ (root)`
- Zapisz. Po minucie zobaczysz URL typu `https://<ty>.github.io/<repo>/`.

---

## Krok 4 — Personal Access Token (do PWA)

PWA zapisuje progi i subskrypcję bezpośrednio w repo, więc potrzebuje tokena z prawem do pisania.

**GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**:

- **Resource owner**: ty
- **Repository access**: *Only select repositories* → wybierz `oml-pwa`
- **Repository permissions**:
  - **Contents** → *Read and write*
  - **Metadata** → *Read-only* (ustawia się sam)
- **Expiration**: do woli (np. 1 rok)

Zapisz token (zaczyna się od `github_pat_…`). Pojedyncza kopia — GitHub pokaże go tylko raz.

---

## Krok 5 — Pierwsze uruchomienie na iPhonie

1. Otwórz **Safari** (musi być Safari, nie Chrome) na iPhonie i wejdź na URL z kroku 3.
2. **Udostępnij → „Dodaj do ekranu początkowego"**.
3. **Otwórz aplikację z ekranu początkowego** (nie z Safari — push działa tylko z home-screen PWA).
4. W aplikacji wypełnij sekcję „Połączenie z GitHub":
   - Repo: `<ty>/oml-pwa`
   - Personal Access Token: `github_pat_…`
   - VAPID public key: skopiuj z `VAPID_KEYS.txt`
   - kliknij **Zapisz lokalnie**.
5. W sekcji „Progi cenowe" wpisz dolny i górny próg (PLN), kliknij **Zapisz progi do repo**.
6. W sekcji „Powiadomienia" kliknij **Włącz powiadomienia push** → zezwól w systemowym dialogu iOS.
   - Pojawi się commit w repo: `chore: update push subscription`.
7. Możesz kliknąć **Wyślij testowe** — powinno przyjść powiadomienie lokalne.

---

## Krok 6 — Test pełnego pipeline'u

W repo: **Actions → OML price check → Run workflow** (manual dispatch ustawia `FORCE_NOTIFY=true`, więc wyśle powiadomienie nawet jeśli kurs jest w widełkach). Powinieneś dostać push na iPhonie w ciągu kilkunastu sekund.

---

## Jak zmienić progi

Otwórz aplikację na ekranie początkowym, wpisz nowe wartości w „Progi cenowe", kliknij **Zapisz progi do repo**. Następna iteracja crona (≤10 min) użyje nowych progów.

## Wymagania iOS

- iOS **16.4** lub nowszy (Web Push w Safari).
- PWA **musi być zainstalowana** przez „Dodaj do ekranu początkowego". W Safari (jako strona) push się **nie wyświetli**.
- Pierwsze uruchomienie po instalacji: aplikacja prosi o pozwolenie na powiadomienia — kliknij **Zezwól**.

## Limity i koszty

- GitHub Actions: free plan = 2000 minut / mc dla prywatnych repo. Każdy run zajmuje ~30 s; przy cronie co 10 min w godz. 7-16 UTC pon-pt to ~60 runs/dzień × 22 dni × 0,5 min = **~660 min/mc**. Mieścimy się.
- Stooq: brak limitu API; dane z opóźnieniem ~15 min (do alertu wystarczy).
- VAPID/Web Push: bez limitu, bezpłatne (komunikacja Apple/Google ↔ przeglądarka).

## Bezpieczeństwo

- Klucz **prywatny VAPID** jest tylko w GitHub Secrets — nigdy nie wraca do PWA.
- **PAT** jest tylko w `localStorage` urządzenia (nie commitowany). Można dać mu fine-grained scope = tylko to repo.
- Repo **prywatne**, więc `subscription.json` i `config.json` nie są publiczne.
- Jeśli zgubisz iPhone'a — usuń subskrypcję ręcznie: `data/subscription.json` ustaw na `{"endpoint": "", "keys": {"p256dh":"","auth":""}}` i unieważnij PAT.

## Struktura repo

```
oml-pwa/
├── index.html                    # PWA (SPA, single file)
├── sw.js                         # service worker (push handler)
├── manifest.webmanifest
├── icons/
│   ├── icon-192.png
│   ├── icon-512.png
│   └── icon.svg
├── data/
│   ├── config.json               # progi (edytowane przez PWA)
│   ├── subscription.json         # endpoint pushy (edytowane przez PWA)
│   └── last_run.json             # dedupe (commitowane przez bota)
├── scripts/
│   ├── check_price.py
│   └── requirements.txt
├── .github/
│   └── workflows/
│       └── check-price.yml
├── .nojekyll                     # GitHub Pages bez Jekylla
├── .gitignore
└── README.md
```

## Diagnostyka

| Problem | Co sprawdzić |
|---|---|
| Brak powiadomień o 9:30 | **Actions** → ostatni run → log: czy `slot=09:30`? Jeśli `slot=None`, to cron się przesunął — sprawdź godzinę logu (UTC). |
| `WebPushException 410 Gone` | Subskrypcja wygasła (np. po przeładowaniu PWA). Kliknij **Włącz powiadomienia push** ponownie. |
| `403` przy zapisie z PWA | PAT bez `contents:write` lub wygasł. Wygeneruj nowy. |
| Stooq zwraca `N/D` | Sesja jeszcze się nie zaczęła; cron 9:30 może trafić na stary close — to OK, alert porówna ostatnią dostępną wartość. |
| iOS nie pyta o pozwolenie | PWA otwarta z Safari, nie z home screen. Dodaj do ekranu i otwórz **stamtąd**. |

---

Sources: [stooq.pl OML CSV](https://stooq.com/q/l/?s=oml&f=sd2t2ohlcv&h&e=csv), [Web Push API on iOS](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/), [GitHub Actions schedule](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#schedule).
