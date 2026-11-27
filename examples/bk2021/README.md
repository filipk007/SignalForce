# BK2021 industrial BESS feed

The feed finds Polish tenders that explicitly procure industrial or commercial battery storage. Quotes come from order item descriptions. The default minimum is 100 kWh when capacity is the qualifying evidence. Explicit industrial or commercial context can qualify a purchase whose capacity is unspecified.

Use the same PostgreSQL database and tenant as the SignalForce dashboard. Set `DATABASE_URL` as for the watch worker, with `DATABASE_URL_DIRECT` for migrations when applicable. Install the project, migrate, then use the ordinary invite command to create a workspace if needed. Keep the invite code private.

```sh
pip install -e ".[dev]"
alembic upgrade head
python -m scripts.web.invites create --label bess-example --tenant-slug bess-example
mkdir -p config
cp examples/bk2021/tenant.yaml config/bk2021.yaml
python -m scripts.bk2021 --config config/bk2021.yaml tick
python -m scripts.bk2021 --config config/bk2021.yaml status
python -m scripts.bk2021 --config config/bk2021.yaml reassign
python -m scripts.bk2021 --config config/bk2021.yaml export > active-bess.csv
uvicorn scripts.web.app:app
```

- A tick checks each configured feed's interval from its own last successful run. For a bounded trial use a narrow phrase, a short lookback and small limits. `tick --feed bess-daily --force` runs that feed immediately. A failed feed exits nonzero, records a failed run and preserves its prior success time. Other due feeds can still complete.
- A tenant has one SignalEvent per BK2021 announcement ID. Search results provide candidates. Details supply scope, publication dates, status and the Warsaw deadline. Previously discovered notices are refreshed through the detail API while BK2021 marks them published, including when search omits them. Open notices above the limits or incomplete pagination fail the feed. Use stable feed keys across ticks.
- Record each verified NIP with a domain and a public evidence URL in the tenant's local YAML. Assignment uses that map only. The status command lists unmapped tenders. After correcting the map, reassign stored entries, including closed ones, without a BK2021 request.
- Sign in and open the linked account on `/dashboard` to review tenders, including closed ones. Corrections replace the latest scope, status and deadline. If the scope drops BESS, the last qualifying quote is labeled with its version. The live dashboard score discounts expired intent immediately. The last saved score remains a historical snapshot.
- CSV contains one row per verified domain with an active, qualified tender. It uses the newest first publication date, then the stable ID in descending text order for ties. Use the CSV as an input list for Studio or Orchestrator. The integration supplies the list only.

The stable signal identity and schedule columns are source-neutral. This entrypoint, order-item payload, feed membership threshold, Polish NIP verification workflow, scoring weight, dashboard tender view and CSV contract are specific to BK2021. The normal career watcher retains its existing cadence. Schedule this tick separately if desired. Automated verification mocks HTTP. The public smoke test is a separate operation against an isolated database.
