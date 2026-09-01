# Aruba Central Automation

A lightweight web GUI for connecting to **Aruba Central** (Classic or New Central)
with your own API credentials and browsing your account — devices, clients, sites
and subscriptions — with search, filters and CSV export.

No database. No stored credentials. Each browser session holds its own connection
in the server's memory only; a different browser starts fresh, and everything is
gone on restart.

> Not affiliated with or endorsed by HPE / Aruba. You supply your own API
> credentials and talk directly to your own Central tenant.

## Features

- **Connect Central** tab — connect Classic Central (OAuth refresh-token flow) and
  New Central (HPE GreenLake client-credentials flow) side by side. Live device
  up/down counts per connection.
- **New Central** dashboard
  - **Account Overview** — colour-coded cards: Clients, Access Points, Switches,
    Gateways, Sites, Subscriptions.
  - Click any card → a searchable, filterable table of the underlying records
    with **Download CSV**.
  - **Clients** table adds a frequency-band filter (2.4 / 5 / 6 GHz / Wired) and
    per-client Channel / SNR / VLAN / Link / OS.
  - Click a client or device row → a full detail view (identity, connection,
    wireless, status, location …).
- **Classic Central** dashboard — placeholder, blocks in progress.

## Stack

- **Backend** — FastAPI + Uvicorn (`app/main.py`), `httpx` for the Central APIs.
  In-memory per-cookie sessions, no persistence.
- **Frontend** — a single static page (`app/static/index.html`), vanilla JS, no
  build step. IBM Plex type, light/dark aware.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8080
# open http://127.0.0.1:8080
```

## Deploy

See [HANDOVER.md](HANDOVER.md) for the full production setup (systemd + nginx +
Let's Encrypt via Route 53 DNS-01). In short:

- `app/main.py` runs under Uvicorn on `127.0.0.1:8080` as a systemd service
  (`deploy/aruba-central-automation.service`).
- nginx terminates TLS and proxies to it (`deploy/nginx-centralautomation.conf`).

## API credentials

- **Classic Central** — Aruba Central → Account Home → API Gateway →
  System Apps & Tokens: Client ID, Client Secret, Refresh Token, plus your
  region's API gateway base URL.
- **New Central** — New Central → API Gateway → API client credentials:
  Client ID and Client Secret. Tokens are issued by
  `https://sso.common.cloud.hpe.com/as/token.oauth2`; pick your cluster for the
  API base URL used afterwards.

## License

MIT — see [LICENSE](LICENSE).
