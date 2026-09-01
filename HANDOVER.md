# Aruba Central Automation — Handover

Living document. Update it in the same commit as any change to behaviour,
deployment, or API usage.

_Last updated: 2026-09-01_

---

## What this is

Web GUI (no auth, no DB) where a user pastes their own Aruba Central API
credentials and browses their tenant. Replaced an earlier throwaway trading-bot
project on the same server.

- **Domain:** `centralautomation.arubademo.online` (HTTPS, port 443)
- **Repo:** `github.com/JibranAziz/CentralAutomation`
- **Intended to be made public** so others can self-host.

## Architecture

```
Browser ──HTTPS──> front nginx (116.255.25.100)  ──TCP/SNI passthrough──>
  10.0.0.151 : nginx (TLS terminates here) ──proxy──> 127.0.0.1:8080 uvicorn (FastAPI)
                                                        └─ httpx ──> Aruba Central APIs
```

- **No credential storage.** Sessions live only in the uvicorn process memory,
  keyed by cookie `acs_sid` (12 h idle TTL). New browser = new session. Restart
  clears everything.
- The **front nginx** at `116.255.25.100` is *not* part of this repo and is not on
  the app server. It forwards `centralautomation.arubademo.online:443` straight
  through to `10.0.0.151:443` (SNI/TCP passthrough — TLS is not terminated there).
  It is unreachable from inside the LAN (NAT hairpin), so the full external path
  can only be tested from outside.

## Server layout (`10.0.0.151`, user `jibran`)

| Thing | Path |
|---|---|
| App code | `/home/jibran/apps/aruba-central-automation/` (git checkout + `.venv`) |
| systemd unit | `/etc/systemd/system/aruba-central-automation.service` → uvicorn on `127.0.0.1:8080` |
| nginx vhost | `/etc/nginx/sites-{available,enabled}/centralautomation.arubademo.online` |
| TLS cert | `/etc/letsencrypt/live/centralautomation.arubademo.online/` (ECDSA) |
| Cert renewal | `certbot.timer` (system) + deploy hook `/etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh` |
| AWS creds for renewal | `/root/.aws/` (copied from `~jibran/.aws`) |

### TLS / cert

Issued and renewed with **`certbot --dns-route53`** (DNS-01) because the box is
behind NAT. `arubademo.online` is a Route 53 hosted zone
(`Z03311421HI3NSR3766GG`). `python3-certbot-dns-route53` is installed.
`certbot renew --dry-run` passes.

### nginx note

Server runs nginx 1.24 — use `listen 443 ssl http2;`, **not** the 1.25-only
`http2 on;` directive.

## Deploy / update procedure

From a working copy (currently `~/Documents/aruba-central-automation` on the dev
box; will become a clone of the repo):

```bash
# 1. push code to the server
tar czf - app requirements.txt deploy | \
  ssh jibran@10.0.0.151 'tar xzf - -C ~/apps/aruba-central-automation'

# 2. (if requirements changed)
ssh jibran@10.0.0.151 'cd ~/apps/aruba-central-automation && .venv/bin/pip install -r requirements.txt'

# 3. restart
ssh jibran@10.0.0.151 'sudo systemctl restart aruba-central-automation.service'

# 4. smoke test
curl -sk https://centralautomation.arubademo.online/healthz \
  --resolve centralautomation.arubademo.online:443:10.0.0.151
```

`import app.main` must succeed from the project dir before restarting.

## Backend API (this app's own endpoints)

| Route | Purpose |
|---|---|
| `GET /` | the single-page app |
| `GET /healthz` | liveness + session count |
| `GET /api/session` | current connection state for this cookie |
| `POST /api/connect/classic` | `{baseUrl, clientId, clientSecret, refreshToken}` → OAuth refresh-token grant |
| `POST /api/connect/new` | `{cluster, clientId, clientSecret}` → GreenLake client-credentials grant |
| `POST /api/disconnect/{classic\|new}` | drop that connection from the session |
| `POST /api/refresh/{classic\|new}` | re-poll device up/down using the stored token |
| `POST /api/webhooks/{classic\|new}` | `{enabled?, regenerate?}` — session-only webhook key toggle |
| `GET /api/overview/new` | Account Overview card counts |
| `GET /api/list/new/{entity}` | `entity` ∈ clients, access-points, switches, gateways, sites, subscriptions — normalized rows |
| `GET /api/detail/new/{client\|device}/{id}` | grouped detail; client id = MAC, device id = serial |

## Aruba Central API usage (upstream)

### Classic Central — **not yet tested with real credentials**

- Token: `POST https://{baseUrl}/oauth2/token` with **query params**
  `client_id, client_secret, grant_type=refresh_token, refresh_token`.
- Device tallies (best-effort): `/monitoring/v2/aps`, `/monitoring/v1/switches`,
  `/monitoring/v1/gateways`.
- Base-URL dropdown values are region API-gateway hostnames (`internal-apigw…`,
  `app1-apigw…`, `apigw-prod2…`, `eu-apigw…`, …).

### New Central — verified against a live tenant

- **Token endpoint is fixed and global:**
  `POST https://sso.common.cloud.hpe.com/as/token.oauth2`,
  `Content-Type: application/x-www-form-urlencoded`, body
  `grant_type=client_credentials&client_id=…&client_secret=…`.
  The selected **cluster** (`us1.api…`, `internal.api…`, `de1.api…`, …) is only
  the API base URL for subsequent calls, *not* for the token.
- Devices: `GET /network-monitoring/v1/devices?limit=1000` (+ `next` cursor).
  `deviceType` ∈ ACCESS_POINT / SWITCH / GATEWAY / BRIDGE, `status` ∈ ONLINE/OFFLINE.
- Clients: `GET /network-monitoring/v1/clients` (cursor). Default filter is
  `status eq 'Connected'`. `wirelessBand` = "2.4 GHz" / "5 GHz" / "6 GHz" (null
  for wired). `wirelessChannel`, `snr`, `vlanId`, `clientOperatingSystem`, `port`.
- Single client: `GET /network-monitoring/v1/clients/{mac}` works.
  **No per-serial device route** — device detail is found by scanning the list.
- Per-device client counts are joined client→device via `connectedDeviceSerial`.
- Sites: `GET /network-monitoring/v1/sites-health` (offset/limit) → `total`.
- Subscriptions: `GET https://global.api.greenlake.hpe.com/subscriptions/v1/subscriptions`
  (offset/limit, **max limit 200**) → `total`. Works with the same Central
  client-credentials token.

## Known gaps / TODO

- Classic Central: dashboard blocks not built; connect flow untested against real
  creds.
- New Central token lifetime is ~2 h; `/api/refresh` and `/api/list` return 401
  once it expires and the user must reconnect. No server-side token refresh.
- `LIST_CAP = 6000` rows per entity.
- Front nginx passthrough config lives outside this repo.

## Dev environment

- Dev box: `~/Documents/aruba-central-automation` (will be the git clone).
- Test tenant: HPE-internal New Central, cluster `internal.api.central.arubanetworks.com`.
