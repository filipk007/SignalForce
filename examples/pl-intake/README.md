# Pilot: przetargi TED na transport drogowy i decyzje RWDZ (Wielkopolska)

Oba źródła zapisują do tego samego PostgreSQL co panel SignalForce i BK2021 (`SignalEvent`, `Account`, `ScanRun`, `SignalFeed`). To próba. Nie ma tu harmonogramu produkcyjnego ani wysyłki.

## Uruchomienie (lokalny, osobny Postgres)

```sh
docker run -d --name signalforce-pl-pg -e POSTGRES_HOST_AUTH_METHOD=trust \
  -e POSTGRES_DB=signalforce -p 127.0.0.1:55432:5432 postgres:16
export DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:55432/signalforce
pip install -e ".[dev]"
alembic upgrade head
python -m scripts.web.invites create --label pl-pilot --tenant-slug pl-pilot
mkdir -p config && cp examples/pl-intake/tenant.yaml config/intake.yaml
python -m scripts.intake --config config/intake.yaml tick       # tylko źródła po terminie
python -m scripts.intake --config config/intake.yaml status     # zdrowie i kolejka przeglądu
python -m scripts.intake --config config/intake.yaml reassign   # po zmianie companies/decisions
python -m scripts.intake --config config/intake.yaml export > active.csv
SESSION_SECRET=$(openssl rand -hex 32) uvicorn scripts.web.app:app   # /dashboard/signals
```

`tick --source ted|rwdz --force` uruchamia jedno źródło od razu. Błąd jednego źródła kończy polecenie kodem 1, zapisuje nieudany `ScanRun`, nie przesuwa jego znacznika sukcesu i nie zatrzymuje drugiego. Testy mockują HTTP. Odczyt publiczny to osobna czynność.

## TED (`ted-pl`)

- **Kandydaci**: publiczne NicheDB `items?source=eu-tenders&tags=pl&sort=id&order=asc&since=…&after=…`. `since` to początek ostatniego pełnego przebiegu minus `overlap_hours`. `sort=updated` nie ma użytecznego kursora: `after`/`before` filtrują po ID, a `offset` działa tylko dla odległości.
- **Dowód**: oryginalny XML eForms. Reguła `ted-road-transport-v1`: część z CPV `601…` i miejscem realizacji w Polsce. Termin i status pochodzą wyłącznie z TED. Zapisane otwarte i niejasne postępowania są odczytywane ponownie przez wyszukiwarkę TED (`procedure-identifier`), nawet gdy NicheDB ich nie pokaże.
- **Klucz**: `tenant + ted_road_transport + <instancja>:<ID procedury>`. Korekta zmienia wpis tylko wtedy, gdy wskazuje znane ogłoszenie z tej procedury. W przeciwnym razie powstaje osobny kandydat `pub:<numer>` do przeglądu. Ogłoszenie o wyniku zamyka rozstrzygnięte części. Brak terminu daje przegląd.
- **Firma**: tylko wpis `companies` (REGON/NIP z ogłoszenia → domena + dowód).
- **Pokrycie**: hostowana instancja czyta do 250 ogłoszeń UE na przebieg i w próbie pomijała większość polskich. `publications` dodaje jawne numery do weryfikacji. NicheDB nie wystarcza jako jedyne źródło kandydatów.

## RWDZ (`rwdz-wlkp`)

- ZIP z GUNB, pobranie warunkowe (ETag/Last-Modified) i SHA-256. Cały plik jest walidowany: jeden CSV, 31 kolumn, UTF-8, separator `;`. Strona GUNB deklaruje `#`. Inny nagłówek lub uszkodzony ZIP to błąd i wersja się nie przesuwa.
- Reguła `rwdz-plant-expansion-v1`: decyzja z ostatnich `lookback_days`, kategoria XVIII, zamierzenie „rozbudowa istniejącego…”, opis z rozbudową zakładu, produkcji lub fabryki.
- **Klucz sprawy**: `numer_urzad/numer_gunb` (wiersze różnią się działkami). W kluczu SignalEvent długie wartości są skracane skrótem.
- Plik **nie podaje rodzaju decyzji ani identyfikatora inwestora**, więc każdy wpis trafia do przeglądu. Aktywuje go dopiero wpis `decisions` po ręcznym sprawdzeniu w wyszukiwarce GUNB (ma captchę) i potwierdzeniu domeny. Ważność `intent_days` od daty decyzji to hipoteza.
- Zachowywana jest nazwa inwestora tylko dla osób prawnych. Projektanci, adresy i działki nie są zapisywane. Brak wiersza w nowym pliku daje przegląd, bez uznawania decyzji za wycofaną.

## Eksport

Kolumny: `domain, company_name, signal_type, signal_id, signal_date, signal_url, deadline, why_now`. Tylko aktywne wpisy z potwierdzoną firmą (TED, RWDZ i BK2021), jeden wiersz na domenę: najpierw otwarte przetargi, potem nowsza data i stały klucz. Historia zostaje w panelu.
