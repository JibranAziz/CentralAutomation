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

- **No server-side credential storage.** Sessions live only in the uvicorn
  process memory, keyed by cookie `acs_sid` (12 h idle TTL). New browser = new
  session. Restart clears everything.
- **"Remember Client ID & Secret" checkbox** (per connect form) is purely
  client-side: `wire()` stores `{clientId, clientSecret, baseUrl|cluster}` in
  `localStorage["acs.creds.<classic|new>"]` on submit when ticked, clears it when
  unticked, and `loadRemembered()` prefills on load. The **refresh token is never
  saved** — that's the point (Classic tokens rotate, so you re-enter it each time
  it expires). Nothing about this touches the server.
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

### Repo deploy assets vs. this box

The repo ships a **self-signed** deploy path so anyone can install on any
hostname/IP without DNS or a public CA:

- `deploy/install.sh` — venv + deps, `gen-cert.sh`, renders
  `aruba-central-automation.service` (`__USER__` / `__APP_DIR__` → `aruba-ca` /
  `/opt/aruba-central-automation`) and `nginx.conf` (`__SERVER_NAME__`), starts
  both.
- `deploy/gen-cert.sh` — `openssl req -x509` into
  `/etc/ssl/aruba-central-automation/{fullchain,privkey}.pem`, SAN per name/IP
  arg, 10-year default. HSTS is intentionally left out of `deploy/nginx.conf`.

**This box (`10.0.0.151`) is unchanged** and does not use those scripts — it
keeps its hand-built vhost at
`/etc/nginx/sites-available/centralautomation.arubademo.online` pointing at the
Let's Encrypt paths above, code at `/home/jibran/apps/...`, service user
`jibran`. To redeploy here: `tar czf - app | ssh jibran@10.0.0.151 'cd
~/apps/aruba-central-automation && tar xzf -'` then
`sudo systemctl restart aruba-central-automation` (sudo/su password ends `$$`).

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
| `GET /api/overview/{flavor}` | all card counts in one shot (legacy; UI no longer uses it) |
| `GET /api/overview/{flavor}/{group}` | one metric group — `clients` \| `devices` \| `sites` \| `subscriptions` \| `ssids` \| `apGroups` \| `rfProfiles`; UI fires them all in parallel and fills each card as it resolves, with a progress-chip row. `apGroups` + `rfProfiles` are Classic-only (hidden for New via `NEW_ONLY_HIDE`) |
| `GET /api/list/{flavor}/{entity}` | `entity` ∈ clients, access-points, switches, gateways, sites, subscriptions, ap-groups, ssids, rf-profiles — normalized rows |
| `GET /api/detail/{flavor}/{client\|device\|site\|group\|ssid\|rf}/{id}` | grouped detail + `meta`; may include `devices[]` (clickable member grid). `rf` = Classic RF-profile detail |
| `GET /api/topology/{flavor}/{site-id}` | normalized `{nodes, links, isolated, roots}` for the topology diagram |

**Loading states** are animated everywhere by default: the overview cards use
shimmer values + a per-API progress-chip row; drill-down lists render a
`loadrow()` (spinner + sweeping-gradient text) plus 7 shimmer skeleton rows
(`skeletonTable`); entity detail and topology show `loadrow()` + block
skeletons. All honour `prefers-reduced-motion`.

`flavor` ∈ `new` \| `classic`. SSIDs card is on both flavors (New:
`/network-monitoring/v1/wlans`, Classic: `/monitoring/v{2,1}/networks`); its
detail shows config + a member grid of the wireless clients on that SSID
(matched by `wlanName`/`network`). Member grid items carry `kind`
(`device`/`client`) so each card opens the right detail page. New Central SSIDs
verified live (7); Classic SSID path analogous, not re-verified.

## Aruba Central API usage (upstream)

### Classic Central — verified live 2026-09-01 (tenant: internal-apigw)

- Token: `POST https://{baseUrl}/oauth2/token` with **query params**
  `client_id, client_secret, grant_type=refresh_token, refresh_token`. **Refresh
  tokens are single-use / rotate** — each `oauth2/token` call consumes one, so
  don't probe with the user's live token or their browser session breaks at the
  next 2 h access-token refresh.
- Base-URL dropdown values are region API-gateway hostnames.
- Full dashboard is wired to the legacy monitoring APIs, mapped by `_classic_*`:
  - AP/switch/gateway/site/subscription counts: `?limit=1&calculate_total=true`
    → `total` on `/monitoring/v2/aps`, `/monitoring/v1/{switches,gateways}`,
    `/central/v2/sites`, `/platform/licensing/v1/subscriptions`
  - **clients**: `/monitoring/v1/clients/wireless` + `/monitoring/v1/clients/wired`
    (v2/clients returned empty). Two calls, merged; wired/wireless = which call.
    `band` is a **float** (2.4 / 5 / 6). Fields: `macaddr`, `hostname`/`name`,
    `ip_address`, `vlan`, `channel`, `snr`, `associated_device`(serial) /
    `associated_device_name`, `os_type`, `site`(name).
  - device lists: `offset`/`limit`, list key `aps`/`switches`/`gateways`; AP items
    have **no client_count** — joined from the clients list via `associated_device`.
  - sites: `/central/v2/sites` (`site_id` int) + health merged from
    `/branchhealth/v1/site` items (keyed by `name`; `device_up/down`,
    `connected_count`, `failed_count`).
  - subscriptions: `/platform/licensing/v1/subscriptions` (`total` works); item
    `license_type`, `sku`, `quantity`, `available`, `status`, `end_date` (epoch ms
    → `_epoch_date`), `subscription_type` ("EVAL").
  - **SSIDs**: `GET /configuration/v1/wlan/{group}` (v2 404s) →
    `{wlans:[{essid,name,type}]}`, one per group (5-wide semaphore + `_retry_get`).
    Merged with `/monitoring/v2/networks` (7 AOS10 SSIDs, essid/security/type) and
    the wireless clients list for live band/VLAN/client counts. For SSIDs with no
    connected clients, band + VLAN fall back to the per-group AP-CLI:
    `_cli_ssid_cfg` parses `wlan ssid-profile` blocks (`rf-band` / `allowed-band`
    → band label, `vlan` → VLAN), keyed by inner `essid` where present else the
    profile name; `_ssid_row` uses `cfgBands` / `cfgVlan` when the live values are
    absent. Detail also GETs `/configuration/v1/wlan/{group}/{ssid}` →
    `{wlan:{essid,type,vlan,hide_ssid,wpa_passphrase,captive_profile_name,zone,
    access_rules,...}}`. Verified: 40 SSIDs across 27 groups.
  - **AP-CLI cache** — the SSID and RF-profile sweeps both call `_ap_cli_get`
    for every group and run concurrently during overview load, which tripped the
    config API's rate limiter and made the two sweeps disagree (overview card
    said 10, list said 12). Fixed with `_AP_CLI_CACHE` (per `(host, group)`,
    120 s TTL, `use_cache=True` on the read-only sweeps only — never the
    read-modify-write config push, which also calls `_ap_cli_cache_clear` on a
    successful write) + a global `_AP_CLI_SEM` (4) ceiling on concurrent ap_cli
    calls + `_AP_CLI_SWEEP_LOCK` which serializes the two all-groups sweeps
    against each other (the second runs entirely off the first's warm cache) +
    `tries=5` on the sweep reads so a transiently throttled group still lands.
  - **RF Profiles** (Classic-only card, like AP Groups): `_classic_rf_profiles`
    fetches each group's AP-CLI (`_ap_cli_get(..., sweep=True, use_cache=True)`
    — retries 429/502-504 three times so rate-limited groups aren't lost, skips
    the deterministic 500 that gateway/switch groups return) and `_cli_rf_profiles`
    parses `rf <kind>-radio-profile` blocks: `dot11a` → 5 GHz, `dot11a-secondary`
    → 5 GHz (secondary), `dot11g` → 2.4 GHz, `dot11-6ghz` → 6 GHz. In the AOS-10
    config-group model these blocks are almost always **unnamed** (one radio
    profile per band, the group default) — those are keyed by the AP-group name
    (`key = name or g`), so the RF profile *is* the group. Named profiles, where
    present, are keyed by name and merged across groups. `_cli_rf_profiles`
    returns `{name: {"bands": {label: {setting: value}}}}` — flag lines
    (`spectrum-monitor`, `smart-antenna`, `channel-quality-aware`) store `True`;
    `ch-bw-range` / `allowed-channels` / `max-tx-power` / `min-tx-power` /
    `max-distance` / `csa-count` / `high-noise-backoff-time` keep their arg.
    List rows: RF Profile / Scope / Radios / TX power (5 GHz repr.) / AP Groups.
    **Drill-down** (`kind: "rf"`, `_classic_central_rf_detail`, wired into
    `_DASH` *after* its def since it lives below the dict literal): one
    `_kv_group` per radio band (2.4 / 5 / 5-secondary / 6 GHz) with Allowed
    transmit power (`_rf_power` min–max), Channel width (`_rf_width`; 2.4 GHz →
    "20 MHz", absent elsewhere → "Default"), Allowed channels (raw list, or
    "Regulatory default" when the CLI doesn't override it), plus the flags; then
    an "Applied to" group listing the AP groups. Verified live 2026-09-02: 12
    entries across 27 groups. Central's *default* channel lists / widths are not
    in the AP-CLI, so we can only show explicit overrides.
  - **Configuration writes** — see the "CLI-based configuration push" section
    below (the structured `/configuration/v2/wlan` approach was dropped because it
    can't express dot1x/RADIUS).
  - **topology**: `GET /topology_external_api/{site_id}` (int id) →
    `{devices, edges(fromIf/toIf), tunnels, rootNodes}`; `role` ∈
    IAP/Switch/Controller/VPNC/SECURITYCLOUD → node type; `rootNodes` → `roots`,
    used as the diagram's layout root.
  - Classic device/client items carry a site *name* (or `site_id` on switches);
    `_classic_resolve_site_id` maps name → numeric id via `/central/v2/sites`.
  - client/device/site detail: found by scanning the list responses.
  - **AP Groups** (Classic-only card — New Central has no groups concept):
    `GET /configuration/v2/groups` (`data` = list of `["name"]` or `{group}`);
    per-group AP/switch/gateway counts derived from the monitoring lists by
    `group_name`; detail adds `GET /configuration/v2/groups/{name}/properties`
    (`data[0].properties`: Architecture, AOSVersion, ApNetworkRole, …). The card
    is shown only when `activeFlavor === "classic"` (`data-flavor` attr).
    **Not yet verified live** (needs a fresh Classic token).
- AOS-8 Instant APs with no site assignment return an empty siteId → their
  topology section shows "unavailable" (expected).
- Classic topology drops generic placeholder nodes by name (`inet`, `wifi-sta`,
  etc. — see `_TOPO_DROP`); the diagram synthesises its own Internet + client
  nodes.

## Branding

- Header logo: `app/static/logo.jpg` (from `~/images/Untitled.jpg`). AI-generated,
  and its cloud/swoosh resembles the real Aruba mark — flagged for the user; kept
  at their request. Swap before any real public launch if that's a concern.
- Favicon: inline Wi-Fi-signal SVG data URI in the `<link rel="icon">`.

### Dashboard wiring (frontend)

One shared dashboard widget (`#dashboard`) is physically relocated into whichever
of the two dashboard tab panels is active; `activeFlavor` (`"new"` / `"classic"`)
is prepended to every `/api/{overview,list,detail,topology}/{flavor}/…` call and
to the per-flavor `overviewLoaded` flag. Nothing else in the drill-down / detail
/ topology code is flavor-specific.

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
  **No per-serial device route and no per-id site route** — device and site
  detail are found by scanning their list responses.
- Sites: each `sites-health` item carries `clients`/`devices` objects
  (`{count, health.groups[Poor/Fair/Good]}`), an `alerts.totalCount`, a
  top-level `health` (percent good), and `reasons[]` (e.g. `DEVICE_OFFLINE`).
  The Sites table shows the client/device health triplets; a row opens a site
  detail (overview, client/device health, address, lat/long).
- **Topology**: `GET /network-monitoring/v1/topology/{site-id}` →
  `devices[]` (serial, name, type, model, deviceFunction, ipv4, mac, status,
  health; unmanaged neighbours have `tpd_<mac>` serials and a friendly name) and
  `links[]` (from/to serial, speed bps, edgeType, from/toPortList, health,
  isSibling), plus `isolatedDevicesCount`. There is **no internet/gateway node** —
  the client-side diagram synthesises an Internet node, picks a root
  (gateway → L3 switch → highest-degree), BFS-layers the graph, and highlights
  the focused device/client's path to the Internet. Icons come from
  `app/static/icons/` (the `~/images/` set). See the assets memory.

## Topology view (frontend)

`loadTopology(meta)` in `index.html` runs at the bottom of the entity-detail
render. It fetches the site topology, builds an adjacency graph, adds a synthetic
`__internet__` node (and `__client__` for client details), BFS-layers from the
root, and draws an inline SVG: rounded node cards with per-type icons and
health-coloured borders, curved links, an animated dashed "active path" from the
focus node up to the Internet, hover tooltips (speed + ports), and clickable
managed nodes that open that device's detail. Isolated devices (no LLDP links)
are counted in the caption, not drawn. The client node is forced onto its own
bottom layer (max depth + 1), aligned under its parent AP; for a wireless client
the connecting link carries a band + SNR label placed ~78 % toward the client end.
Nodes are **drag-repositionable** (pointer events + `getScreenCTM` for
client→SVG coords; connected links and labels redraw live; a >4 px move
suppresses the click-to-open). Applies to both New and Classic (shared code).
- Per-device client counts are joined client→device via `connectedDeviceSerial`.
- Sites: `GET /network-monitoring/v1/sites-health` (offset/limit) → `total`.
- Subscriptions: `GET https://global.api.greenlake.hpe.com/subscriptions/v1/subscriptions`
  (offset/limit, **max limit 200**) → `total`. Works with the same Central
  client-credentials token.

## Known gaps / TODO

- Both New and Classic tokens are held as-issued; there is **no server-side token
  refresh**, so `/api/*` calls 401 after ~2 h and the user reconnects. Classic
  additionally rotates the refresh token on every use.
- `LIST_CAP = 6000` rows per entity.
- Front nginx passthrough config lives outside this repo.

## Dev environment

- Dev box: `~/Documents/aruba-central-automation` (will be the git clone).
- Test tenant: HPE-internal New Central, cluster `internal.api.central.arubanetworks.com`.

## CLI-based configuration push (Classic, AP-CLI)

The Configuration section (Classic tab) uses the **AP-CLI** API, the same way
Central Automation Studio does:
- `GET /configuration/v1/ap_cli/{group}` → `{clis:[...]}` — the group's full CLI
- `POST /configuration/v1/ap_cli/{group}` `{clis:[...]}` — **replaces the whole
  group CLI**, so you must GET, modify, POST back or you corrupt the config.

`_merge_cli(existing, submitted)` splices each top-level block of the submitted
CLI (`_cli_blocks`) into the group's live config — a block whose header line
matches is replaced in place (`_cli_replace_block`), anything else is left alone,
new blocks are appended.

`_merge_cli_submerge(existing, submitted, managed)` is the **line-level** merge
used by the RF-profile form: within a same-header block it replaces each child by
leading keyword (`_kw`), keeps unmanaged children, and drops any keyword in
`managed` that the submitted block omits (so the form's "Also remove options
that aren't set" checkbox works). Blocks with no existing match are appended.

`POST /api/config/classic/cli` `{cli, groups[], preview, submerge?, managed?, remove?}`:
for each group it GETs the live CLI, drops any block whose header is in `remove`
(`_cli_drop_block`), merges the rest (`submerge` → `_merge_cli_submerge`, else
`_merge_cli`), and either returns the merged text (`preview:true`) or POSTs it
(and `_ap_cli_cache_clear`s the group). `remove`-only (no `cli`) = delete — the
RF form's "Delete this named profile" button sends `{remove: rfpHeaders()}`. The UI has three cards:
- **Configure SSID** — seeds `wlan ssid-profile` + `wlan access-rule` into an
  editable textarea.
- **Create RADIUS server** — structured `#rform` → `wlan auth-server` block.
- **Configure RF profile** — structured 3-column (2.4/5/6 GHz) `#rfp-form`
  (`rfpToCli` / `fillRfpFromCli`). Emits `rf dot11g|dot11a|dot11-6ghz-radio-profile`
  blocks with `allowed-channels`, `min/max-tx-power`, `ch-bw-range <w>MHz <w>MHz`,
  `csa-count`, `high-noise-backoff-time`, `dot11h`, `spectrum-monitor`,
  `channel-quality-aware`, `disable-arm-wids-functions`. Pushes with
  `submerge:true`; `managed` only sent when the "remove unset options" box is
  ticked. **Profile name** field: blank → the group-default (unnamed) radio
  profile; filled → a *named* profile — header becomes
  `rf …-radio-profile <name>` (quoted iff it has spaces), each block gets a
  `zone <name>` line (Central's convention — the profile applies to APs whose
  zone is set to that name), and the 5 GHz column is also written to
  `rf dot11a-secondary-radio-profile <name>`. "Load from" lists named profiles
  in the picked group (`?names=rf dot11a-radio-profile`, plus a "group default"
  entry) and pulls the matching blocks. `_merge_cli_submerge` matches on the
  full header incl. name, so named and unnamed profiles are edited
  independently. A **Delete** button (shown once the name field is set) sends
  `{remove: rfpHeaders()}`.
  **Verified with real writes to Central 2026-09-03** (`AOS8-Test-Group`):
  create `ACS_WriteTest` → all blocks + `zone` landed, group default untouched;
  edit → TX power replaced in place, channels/CQA added, spectrum kept; delete →
  all blocks gone, group default byte-identical to baseline. Every keyword
  accepted.

All three share the group multi-select, **Preview merge** (exact per-group text)
and confirm-gated **Deploy**. `GET /api/config/classic/cli/{group}` returns one
group's current CLI (or just the `?block=a||b` blocks / `?names=` block names).

`GET /configuration/v1/ap_cli/{group}` **verified live 2026-09-02** — returns a
**bare JSON array** of CLI strings (not `{clis:[...]}`); masked secrets come back
as `********` and round-trip fine. `_merge_cli` verified against a real 122-line
group config (splice appends new blocks, replaces same-header ones). The **POST**
is not exercised (it is a live config write).

**RADIUS "Query server status" (RFC 5997) CLI syntax** (verified against live
production `wlan auth-server` blocks 2026-09-02): both Authentication + Accounting
status checks = bare `rfc5997` (no argument); accounting only = `rfc5997
acct-only`; auth only = `rfc5997 auth-only`. `rfc5997 auth-acct` is **invalid** —
AOS silently drops the line and Central shows both boxes unchecked. Fixed in
`rformToCli()` / `fillRformFromCli()` / `RADIUS_TEMPLATE`.
