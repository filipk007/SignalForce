# BK2021 w SignalForce

- [x] Record the upstream baseline and verify the fork and base branch.
- [x] Port the BK2021 request, pagination recovery, and industrial BESS qualification.
- [x] Add tenant-scoped feed intervals and transactional replay into the existing storage.
- [x] Surface active and historical BK2021 evidence in scoring, the dashboard, and CSV.
- [x] Run focused regression tests, migrate an isolated PostgreSQL database, and verify the live public sample, HTTP dashboard, and CSV.
- [x] Publish and inspect proof, then hand the committed branch to Herdr.

Each feed keeps its own schedule and known announcement membership. A stable announcement appears once per tenant in SignalEvent even when several feeds discover it. The public detail response is authoritative for version and lifecycle. An operator-supplied, evidenced NIP mapping links to an Account. Deadline checks apply when reading active intent, including between scans.

## Verification

The fork targets `filipk007/SignalForce` on `main`. The Python 3.12 upstream baseline had 1,377 passing tests, two CI deselections and 87.49% coverage. Ruff 0.17.6 reported 489 existing lint issues and 21 files requiring formatting. The current resolver breaks the upstream watch and webhook tests with selectolax 1 or svix 2, so the integration pins the two verified versions.

The focused regression run passed 417 tests with 92.78% coverage for the BK2021 package. The live smoke used one public announcement on PostgreSQL 16 and verified both feed schedules, forced replay, one CSV row, and the signed-in dashboard over HTTP. The proof page was checked at phone and desktop widths.

# Pilot TED i RWDZ (Polska)

Minimalny zakres: pakiet `scripts/intake` obok BK2021, ten sam PostgreSQL, SignalEvent, Account, ScanRun, SignalFeed.

- [x] Próba TED: strony NicheDB, remisy, porównanie z oryginałem, XML 811160-2026.
- [x] Próba RWDZ: ZIP Wielkopolski, schemat, klucz, powtórka.
- [x] Testy przed kodem: parser i łańcuch korekt TED, czytnik ZIP, replay, częściowy błąd, terminy, firma, eksport.
- [x] `scripts/intake` + migracja `signal_feeds.meta`; panel Sygnały; wspólny CSV.
- [x] Odczyt publiczny na izolowanym PostgreSQL, panel i CSV, dowód.

## Weryfikacja pilota TED i RWDZ (2026-11-27)

- 461 testów dotkniętych obszarów przechodzi; Ruff czysty dla nowych plików.
- PostgreSQL 16 (osobny kontener): migracja w górę, w dół i `alembic check`. Odczyt publiczny: 1 aktywny wpis TED, 8 wpisów RWDZ do przeglądu, powtórka bez duplikatów.
- Hostowane NicheDB: 1 z 40 polskich ogłoszeń TED o CPV 60100000 z 21–27.11. Nie wystarcza jako jedyne źródło kandydatów.
- RWDZ: separator `;`, brak rodzaju decyzji i identyfikatora inwestora w pliku. Druga rzeczywista wersja pliku nie była dostępna.
- Fork nie ma zarejestrowanych workflow GitHub Actions, więc CI nie uruchamia się.
