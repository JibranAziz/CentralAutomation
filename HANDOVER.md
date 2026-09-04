# Aruba Central Automation — Handover

Living document. Update it in the same commit as any change to behaviour,
deployment, or API usage.

_Last updated: 2026-09-03_

---

## Change workflow (read first)

Every change ships to **both** places, and updates **both** docs — separately:

1. **Code** → commit to the repo **and** deploy to the running server.
2. **Docs** → this file (`HANDOVER.md`, committed) stays **generic** — no domain,
   host, IP, tenant, cert-provider or credential specifics ever go here.
   Anything specific to a particular deployment goes in `HANDOVER.local.md`
   (gitignored via `*.local.md`), which is maintained on the operator's side and
   **never committed**.
3. `README.md` is the end-user install guide — keep it generic too.

So a single change can touch: the code, `HANDOVER.md`, `HANDOVER.local.md`, and
`README.md` — update each that's affected.

---

## What this is

A web GUI (no auth, no database) where a user pastes their own Aruba Central API
credentials and browses their tenant — devices, clients, sites, subscriptions,
SSIDs, AP groups, RF profiles — with search, filters, CSV export, an interactive
topology view, and a Classic-Central **configuration** section (SSID / RADIUS /
RF-profile push and AP-group create/delete) via the AP-CLI API.

- **Repo:** `github.com/JibranAziz/CentralAutomation` (MIT, public).
- Not affiliated with or endorsed by HPE / Aruba. You supply your own API
  credentials and talk directly to your own Central tenant.

## Architecture

```
Browser ──HTTPS──> nginx (terminates TLS) ──proxy──> 127.0.0.1:8080 uvicorn (FastAPI)
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

## Stack

- **Backend** — FastAPI + Uvicorn (`app/main.py`), `httpx` for the upstream
  calls. In-memory per-cookie sessions, no persistence, no build step.
- **Frontend** — one static page (`app/static/index.html`), vanilla JS. `GET /`
  serves it fresh via `FileResponse`, so front-end-only changes need no restart;
  only `app/main.py` changes require `systemctl restart`.

## Deploy

**End-user step-by-step (install git → clone → run) is in [README.md](README.md).**
This section is the engineering summary.

The repo ships a **self-signed TLS** path so it installs on any hostname or bare
IP with no DNS and no public CA. On Debian/Ubuntu, from the repo root:

```bash
sudo apt install -y git python3-venv nginx openssl rsync curl
sudo ./deploy/install.sh <hostname-or-IP>        # e.g. central.lan  or  192.168.1.50
```

`deploy/install.sh`:
- creates a venv + installs `requirements.txt`;
- `deploy/gen-cert.sh` — `openssl req -x509` into
  `/etc/ssl/aruba-central-automation/{fullchain,privkey}.pem`, a SAN per
  name/IP argument, 10-year default;
- renders the systemd unit (`deploy/aruba-central-automation.service`,
  `__USER__` → `aruba-ca`, `__APP_DIR__` → `/opt/aruba-central-automation`) and
  the nginx vhost (`deploy/nginx.conf`, `__SERVER_NAME__`), then starts both.

Open `https://<host>/` and accept the self-signed warning once.

- nginx: the vhost uses `listen 443 ssl http2;` — the `http2 on;` form needs
  nginx ≥ 1.25.
- HSTS is intentionally **not** set in `deploy/nginx.conf` (it would lock a host
  out of plain HTTP permanently, which is wrong with a self-signed cert).

### The systemd service

`install.sh` writes `/etc/systemd/system/aruba-central-automation.service`, runs
`systemctl daemon-reload`, then `systemctl enable --now` — so:

- **It starts on boot** (`[Install] WantedBy=multi-user.target` + `enable`) and
  **survives reboots**.
- `Restart=on-failure` / `RestartSec=5` — systemd restarts the app ~5 s after a
  crash.
- `Type=simple` uvicorn, no `ExecReload` → `systemctl reload
  aruba-central-automation` is a no-op; use `restart` to pick up new backend
  code. `app/static/*` changes need nothing (`GET /` serves the file fresh).
- Manage it with `systemctl {status,restart,stop}` and
  `journalctl -u aruba-central-automation -f`.
- Runs as a dedicated unprivileged system user (`aruba-ca`), `NoNewPrivileges`,
  `PrivateTmp`.

### Production with a real certificate

Point `ssl_certificate` / `ssl_certificate_key` in the nginx vhost at your ACME
(Let's Encrypt / certbot) paths instead of the self-signed ones. Everything else
stays the same.

### Update procedure

```bash
# 1. push code
rsync -a --delete --exclude .git --exclude .venv \
  app/ requirements.txt deploy/ <server>:<app_dir>/

# 2. only if requirements.txt changed
ssh <server> '<app_dir>/.venv/bin/pip install -r <app_dir>/requirements.txt'

# 3. restart — only needed for app/main.py (backend) changes
ssh <server> 'sudo systemctl restart aruba-central-automation'

# 4. smoke test
curl -sk https://<host>/healthz
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
| `GET /api/overview/{flavor}/{group}` | one metric group — `clients` \| `devices` \| `sites` \| `subscriptions` \| `ssids` \| `apGroups` \| `rfProfiles`; the UI fires them all in parallel and fills each card as it resolves, with a progress-chip row. `apGroups` + `rfProfiles` are Classic-only (hidden for New via `NEW_ONLY_HIDE`) |
| `GET /api/list/{flavor}/{entity}` | `entity` ∈ clients, access-points, switches, gateways, sites, subscriptions, ap-groups, ssids, rf-profiles — normalized rows |
| `GET /api/detail/{flavor}/{client\|device\|site\|group\|ssid\|rf}/{id}` | grouped detail + `meta`; may include `devices[]` (clickable member grid). `rf` = Classic RF-profile detail |
| `GET /api/topology/{flavor}/{site-id}` | normalized `{nodes, links, isolated, roots}` for the topology diagram |
| `GET /api/config/{flavor}/groups` | list group names (Classic) |
| `GET /api/config/{flavor}/cli/{group}` | one group's CLI, or `?block=a\|\|b` blocks / `?names=<prefix>` block names |
| `POST /api/config/{flavor}/cli` | `{cli, groups[], preview, submerge?, managed?, remove?}` — merge/push CLI |
| `POST /api/config/{flavor}/group` | `{name, password, devTypes[], swTypes[], apRole}` — create a group |
| `DELETE /api/config/{flavor}/group/{name}` | delete a group |

**Loading states** are animated everywhere by default: the overview cards use
shimmer values + a per-API progress-chip row; drill-down lists render a
`loadrow()` (spinner + sweeping-gradient text) plus 7 shimmer skeleton rows
(`skeletonTable`); entity detail and topology show `loadrow()` + block
skeletons. All honour `prefers-reduced-motion`.

`flavor` ∈ `new` \| `classic`. The SSIDs card is on both flavors (New:
`/network-monitoring/v1/wlans`, Classic: `/monitoring/v{2,1}/networks` + config);
its detail shows config + a member grid of the wireless clients on that SSID.
Member grid items carry `kind` (`device`/`client`) so each card opens the right
detail page.

### Dashboard wiring (frontend)

One shared dashboard widget (`#dashboard`) is physically relocated into whichever
of the two dashboard tab panels is active; `activeFlavor` (`"new"` / `"classic"`)
is prepended to every `/api/{overview,list,detail,topology,config}/{flavor}/…`
call and to the per-flavor `overviewLoaded` flag. Nothing else in the drill-down
/ detail / topology code is flavor-specific.

## Aruba Central API usage (upstream)

### Classic Central — verified against a live tenant

- Token: `POST https://{baseUrl}/oauth2/token` with **query params**
  `client_id, client_secret, grant_type=refresh_token, refresh_token`. **Refresh
  tokens are single-use / rotate** — each `oauth2/token` call consumes one, so
  don't probe with a user's live token or their browser session breaks at the
  next 2 h access-token refresh.
- Base-URL dropdown values are region API-gateway hostnames.
- The dashboard maps to the legacy monitoring APIs (`_classic_*` helpers):
  - AP/switch/gateway/site/subscription counts: `?limit=1&calculate_total=true`
    → `total` on `/monitoring/v2/aps`, `/monitoring/v1/{switches,gateways}`,
    `/central/v2/sites`, `/platform/licensing/v1/subscriptions`.
  - **clients**: `/monitoring/v1/clients/wireless` + `/monitoring/v1/clients/wired`
    (v2/clients returned empty). Two calls, merged; wired/wireless = which call.
    `band` is a **float** (2.4 / 5 / 6). Fields: `macaddr`, `hostname`/`name`,
    `ip_address`, `vlan`, `channel`, `snr`, `associated_device`(serial) /
    `associated_device_name`, `os_type`, `site`(name).
  - device lists: `offset`/`limit`, list key `aps`/`switches`/`gateways`; AP
    items have **no client_count** — joined from the clients list via
    `associated_device`.
  - sites: `/central/v2/sites` (`site_id` int) + health merged from
    `/branchhealth/v1/site` items (keyed by `name`).
  - subscriptions: `/platform/licensing/v1/subscriptions`; item `license_type`,
    `sku`, `quantity`, `available`, `status`, `end_date` (epoch ms →
    `_epoch_date`), `subscription_type`.
  - **SSIDs**: `GET /configuration/v1/wlan/{group}` (v2 404s) →
    `{wlans:[{essid,name,type}]}`, one per group (5-wide semaphore +
    `_retry_get`). Merged with `/monitoring/v2/networks` and the wireless clients
    list for live band/VLAN/client counts. For SSIDs with no connected clients,
    band + VLAN fall back to the per-group AP-CLI: `_cli_ssid_cfg` parses
    `wlan ssid-profile` blocks (`rf-band` / `allowed-band` → band label, `vlan` →
    VLAN), keyed by inner `essid` where present else the profile name; `_ssid_row`
    uses `cfgBands` / `cfgVlan` when the live values are absent. Detail also GETs
    `/configuration/v1/wlan/{group}/{ssid}` →
    `{wlan:{essid,type,vlan,hide_ssid,wpa_passphrase,captive_profile_name,zone,
    access_rules,...}}`.
  - **AP-CLI cache** — the SSID and RF-profile sweeps both call `_ap_cli_get`
    for every group and run concurrently during overview load, which trips the
    config API's rate limiter and makes the two sweeps disagree. Fixed with
    `_AP_CLI_CACHE` (per `(host, group)`, 120 s TTL, `use_cache=True` on the
    read-only sweeps only — never the read-modify-write config push, which also
    calls `_ap_cli_cache_clear` on a successful write) + a global `_AP_CLI_SEM`
    (4) ceiling on concurrent ap_cli calls + `_AP_CLI_SWEEP_LOCK` which
    serializes the two all-groups sweeps against each other (the second runs
    entirely off the first's warm cache) + `tries=5` on the sweep reads so a
    transiently throttled group still lands.
  - **RF Profiles** (Classic-only card): `_classic_rf_profiles` fetches each
    group's AP-CLI (`_ap_cli_get(..., sweep=True, use_cache=True)` — retries
    429/502-504, skips the deterministic 500 that gateway/switch groups return)
    and `_cli_rf_profiles` parses `rf <kind>-radio-profile` blocks: `dot11a` →
    5 GHz, `dot11a-secondary` → 5 GHz (secondary), `dot11g` → 2.4 GHz,
    `dot11-6ghz` → 6 GHz. In the AOS-10 config-group model these blocks are
    usually **unnamed** (one radio profile per band, the group default) — keyed
    by the AP-group name (`key = name or g`), so the RF profile *is* the group.
    Named profiles are keyed by name and merged across groups. `_cli_rf_profiles`
    returns `{name: {"bands": {label: {setting: value}}}}` — flag lines
    (`spectrum-monitor`, `smart-antenna`, `channel-quality-aware`, `dot11h`)
    store `True`; `ch-bw-range` / `allowed-channels` / `max-tx-power` /
    `min-tx-power` / `max-distance` / `csa-count` / `high-noise-backoff-time` /
    `zone` keep their arg. **Drill-down** (`kind: "rf"`,
    `_classic_central_rf_detail`, wired into `_DASH` *after* its def since it
    lives below the dict literal): one `_kv_group` per radio band with allowed
    transmit power (`_rf_power` min–max), channel width (`_rf_width`; 2.4 GHz →
    "20 MHz", absent elsewhere → "Default"), allowed channels (raw list, or
    "Regulatory default" when the CLI doesn't override it), the flags, the AP
    zone, and an "Applied to" group. Central's *default* channel lists / widths
    are not in the AP-CLI, so only explicit overrides show.
  - **topology**: `GET /topology_external_api/{site_id}` (int id) →
    `{devices, edges(fromIf/toIf), tunnels, rootNodes}`; `role` ∈
    IAP/Switch/Controller/VPNC/SECURITYCLOUD → node type; `rootNodes` → `roots`,
    used as the diagram's layout root.
  - Classic device/client items carry a site *name* (or `site_id` on switches);
    `_classic_resolve_site_id` maps name → numeric id via `/central/v2/sites`.
  - client/device/site detail: found by scanning the list responses.
  - **AP Groups** (`GET /configuration/v2/groups` — `data` = list of `["name"]`
    or `{group}`, `limit` caps at 20 → paginate); per-group AP/switch/gateway
    counts derived from the monitoring lists by `group_name`; detail adds
    `GET /configuration/v2/groups/{name}/properties`
    (`data[0].properties`: Architecture, AOSVersion, ApNetworkRole, …).
    New Central has its own equivalent (see below).
- AOS-8 Instant APs with no site assignment return an empty siteId → their
  topology section shows "unavailable" (expected).
- Classic topology drops generic placeholder nodes by name (`inet`, `wifi-sta`,
  … — see `_TOPO_DROP`); the diagram synthesises its own Internet + client nodes.

### New Central — verified against a live tenant

- **Token endpoint is fixed and global:**
  `POST https://sso.common.cloud.hpe.com/as/token.oauth2`,
  `Content-Type: application/x-www-form-urlencoded`, body
  `grant_type=client_credentials&client_id=…&client_secret=…`.
  The selected **cluster** (`us1.api…`, `eu1.api…`, …) is only the API base URL
  for subsequent calls, *not* for the token.
- Devices: `GET /network-monitoring/v1/devices?limit=1000` (+ `next` cursor).
  `deviceType` ∈ ACCESS_POINT / SWITCH / GATEWAY / BRIDGE, `status` ∈
  ONLINE/OFFLINE.
- Clients: `GET /network-monitoring/v1/clients` (cursor). Default filter is
  `status eq 'Connected'`. `wirelessBand` = "2.4 GHz" / "5 GHz" / "6 GHz" (null
  for wired). `wirelessChannel`, `snr`, `vlanId`, `clientOperatingSystem`,
  `port`.
- Single client: `GET /network-monitoring/v1/clients/{mac}` works.
  **No per-serial device route and no per-id site route** — device and site
  detail are found by scanning their list responses.
- Sites: each `sites-health` item carries `clients`/`devices` objects
  (`{count, health.groups[Poor/Fair/Good]}`), an `alerts.totalCount`, a
  top-level `health` (percent good), and `reasons[]` (e.g. `DEVICE_OFFLINE`).
- **Topology**: `GET /network-monitoring/v1/topology/{site-id}` →
  `devices[]` (serial, name, type, model, deviceFunction, ipv4, mac, status,
  health; unmanaged neighbours have `tpd_<mac>` serials and a friendly name) and
  `links[]` (from/to serial, speed bps, edgeType, from/toPortList, health,
  isSibling), plus `isolatedDevicesCount`. There is **no internet/gateway
  node** — the client-side diagram synthesises an Internet node, picks a root
  (gateway → L3 switch → highest-degree), BFS-layers the graph, and highlights
  the focused device/client's path to the Internet.
- Subscriptions: `GET https://global.api.greenlake.hpe.com/subscriptions/v1/subscriptions`
  (offset/limit, **max limit 200**) → `total`. Works with the same Central
  client-credentials token.

#### New Central — configuration model (`/network-config/v1alpha1`)

Verified against a live tenant. The config model is **library profiles**
(named, reusable, per resource type) + **config-assignments** (attach a profile
to a *scope* / device-function). Helpers: `_nc_get(client, host, hdr, root, params)`.

- `GET /network-config/v1/global` → `{scopeId}`; `GET /network-config/v1/sites`
  → sites with `scopeId`.
- `GET /network-config/v1alpha1/device-collections` → **AP Groups** for New
  Central: `items[]` of `{scopeId, scopeName, deviceCount, isIap8x, description}`
  (`_new_central_ap_groups` / `_nc_group_row`). Drill-down
  (`_new_central_group_detail`, ident = `scopeId`) shows architecture + device
  count, then the assigned profiles grouped by `device-function` → `profile-type`
  (from `config-assignments?scope-id=…`). Monitoring devices carry no
  device-collection ref, so member devices aren't listed.
- `GET /network-config/v1alpha1/radios` → **RF Profiles**: `profile[]` of
  `{name, radio:[{profile: RADIO_2DOT4G|RADIO_5G|RADIO_2ND_5G|RADIO_6G|RADIO_2ND_6GHZ,
  mode, enable, ieee802dot11h, arm-control:{channels-for-*, min/max-channel-bandwidth
  (BW_20MHZ…), min/max-tx-power, zero-wait-dfs}}]}` (`_new_central_rf_list` /
  `_nc_rf_row`). Drill-down `_new_central_rf_detail`: one `_kv_group` per radio
  with power / width (`_nc_width`) / channels (`_nc_chan_list`, strips `CHAN_`
  and `_6GHZ`) / flags, plus "Assigned to" from `config-assignments?profile-type=radios`.
- `GET /network-config/v1alpha1/wlan-ssids` → SSID library profiles
  (`{wlan-ssid:[{ssid, enable, essid.name, forward-mode, dot11k/r, …}]}`).
- `GET /network-config/v1alpha1/auth-servers` → RADIUS servers
  (`{auth-server:[{name, type:RADIUS, radius-server-mode, auth-server-address,
  auth-port, enable-radsec, …}]}`); `.../server-groups` → RADIUS server groups.
- `GET /network-config/v1alpha1/roles` → access-rule roles.
- `GET /network-config/v1alpha1/config-assignments` →
  `{config-assignment:[{scope-id, device-function (CAMPUS_AP / MOBILITY_GW /
  ACCESS_SWITCH / …), profile-type, profile-instance, scope-name, scope-type}]}`;
  filter with `?scope-id=` / `?profile-type=` / `?device-function=`. Assign via
  `POST` the same path `{config-assignment:[{scope-id, device-function,
  profile-type, profile-instance}]}`; unassign via
  `DELETE /network-config/v1alpha1/config-assignments/{scopeId}/{deviceFunction}/{profileType}/{profileInstance}`.
- **AP Groups + RF Profiles overview cards now show for both flavors** —
  `NEW_ONLY_HIDE` is `{}`; the two cards no longer carry `data-flavor="classic"`.
  Frontend `curDef()` resolves a `DETAIL` entry's `byFlavor.<flavor>` override
  (New AP-groups list has different columns / idKey than Classic).

#### New Central — configuration *writes*

Library-profile CRUD (all verified live 2026-09-04):
- **create / update:** `PUT /network-config/v1alpha1/<type>/<name>` with the
  resource JSON → `200 SUCC_001`. `<type>` ∈ `wlan-ssids` / `auth-servers` /
  `radios`. `NC_KIND_TYPE` maps the UI kind → resource type; `_nc_ssid_body` /
  `_nc_radius_body` / `_nc_rf_body` build the payload from the form fields.
  Secrets are written `{secret-type: PLAIN_TEXT, plaintext-value: …}` and read
  back as opaque `vault:v6:…`.
- **delete:** `DELETE /network-config/v1alpha1/<type>/<name>` (`nc_config_delete`
  first `DELETE`s any `config-assignments` for that profile).
- **assign:** `POST /network-config/v1alpha1/config-assignments`
  `{config-assignment:[{scope-id, device-function: CAMPUS_AP, profile-type,
  profile-instance}]}`. **Best-effort** — Central often rejects it:
  `radios` = "single instance per scope" (a group already has one),
  `wlan-ssids` = needs a matching `aruba-role`. The library-profile create
  always succeeds regardless; the per-scope assign result is reported and the
  user finishes assignment in the Central UI.
- **Add AP group is NOT possible** on this cluster — `POST
  /network-config/v1alpha1/device-collections` → `API_ACCESS_RESTRICTED_IN_HYBRID_CLUSTER`;
  no per-name PUT. So the New Central config section has **3 cards** (SSID /
  RADIUS / RF), not 4, with a hint to make groups in the UI.
- Endpoints: `POST /api/config/new/{ssid|radius|rf}` `{fields, scopes[]}`,
  `DELETE /api/config/new/{kind}/{name}`, `GET /api/config/new/scopes`.
- UI: separate `#nc-config-section` / `#nc-config-form` (shown when
  `activeFlavor === "new"`); delete is a danger button on the New Central SSID
  and RF-profile detail pages (`dangerRow` / `deleteNcProfile`).

## Topology view (frontend)

`loadTopology(meta)` in `index.html` runs at the bottom of the entity-detail
render. It fetches the site topology, builds an adjacency graph, adds a synthetic
`__internet__` node (and `__client__` for client details), BFS-layers from the
root, and draws an inline SVG: rounded node cards with per-type icons (from
`app/static/icons/`) and health-coloured borders, curved links, an animated
dashed "active path" from the focus node up to the Internet, hover tooltips
(speed + ports), and clickable managed nodes that open that device's detail.
Isolated devices (no LLDP links) are counted in the caption, not drawn. The
client node is forced onto its own bottom layer, aligned under its parent AP; for
a wireless client the connecting link carries a band + SNR label placed ~78 %
toward the client end. Nodes are **drag-repositionable** (pointer events +
`getScreenCTM` for client→SVG coords; connected links and labels redraw live; a
>4 px move suppresses the click-to-open). Shared code across New and Classic.

## CLI-based configuration push (Classic, AP-CLI)

The Configuration section (Classic tab) uses the **AP-CLI** API:
- `GET /configuration/v1/ap_cli/{group}` → a **bare JSON array** of CLI strings
  (not `{clis:[...]}`); masked secrets come back as `********` and round-trip.
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
(`_cli_drop_block`), merges the rest, and either returns the merged text
(`preview:true`) or POSTs it (and `_ap_cli_cache_clear`s the group).
`remove`-only (no `cli`) = delete.

### Configuration section cards

- **Configure SSID** — seeds `wlan ssid-profile` + `wlan access-rule` into an
  editable textarea. Editing the name field after a Load renames the CLI
  references (`renameCliRefs`: `wlan ssid-profile` / `wlan access-rule` /
  `wlan mac-acl` / a matching `essid`) on blur — so "load then rename" clones it.
- **Create RADIUS server** — structured `#rform` → `wlan auth-server` block.
  RFC 5997 "Query server status" CLI: both Authentication + Accounting = bare
  `rfc5997`; accounting only = `rfc5997 acct-only`; auth only = `rfc5997
  auth-only`. `rfc5997 auth-acct` is **invalid** — AOS silently drops the line
  and Central shows both boxes unchecked.
- **Configure RF profile** — structured 3-column (2.4/5/6 GHz) `#rfp-form`
  (`rfpToCli` / `fillRfpFromCli`). Emits `rf dot11g|dot11a|dot11-6ghz-radio-profile`
  blocks with `allowed-channels`, `min/max-tx-power`, `ch-bw-range <w>MHz <w>MHz`,
  `csa-count`, `high-noise-backoff-time`, `dot11h`, `spectrum-monitor`,
  `channel-quality-aware`, `disable-arm-wids-functions`. Pushes with
  `submerge:true`; `managed` sent only when the "remove unset options" box is
  ticked. **Profile name** field: blank → the group-default (unnamed) radio
  profile; filled → a *named* profile — header becomes
  `rf …-radio-profile <name>` (quoted iff it has spaces), each block gets a
  `zone <name>` line (Central's convention — the profile applies to APs whose
  zone is set to that name), and the 5 GHz column is also written to
  `rf dot11a-secondary-radio-profile <name>`. "Load from" lists named profiles
  in the picked group (`?names=rf dot11a-radio-profile`, plus a "group default"
  entry). `_merge_cli_submerge` matches on the full header incl. name, so named
  and unnamed profiles are edited independently. A **Delete** button (shown once
  the name field is set) sends `{remove: rfpHeaders()}`.
- **Add AP group** (`#grp-form`: name / admin password / device-type checkboxes /
  switch types / AP network role):
  - create: `POST /configuration/v1/groups`
    `{group, group_attributes:{template_group:false, group_password}}` → 201,
    then `PATCH /configuration/v2/groups/{name}/properties`
    `{properties:{AllowedDevTypes, ApNetworkRole, AllowedSwitchTypes?}}`.
  - delete: `DELETE /configuration/v1/groups/{name}` → 200. In the UI, delete is
    a danger button on the AP-group detail page (`openEntity` when
    `meta.kind === "group"`).
  - **Architecture is always Instant / AOS-8** — the `aos10` flag, `Architecture`
    in the create body, and a properties PATCH all return success but silently
    no-op. An AOS-10 group must be created in the Central UI; the form says so.

All the CLI-push cards share the group multi-select, **Preview merge** (exact
per-group text) and a confirm-gated **Deploy**.

## Branding

- Header logo: `app/static/logo.jpg` — AI-generated; its cloud/swoosh resembles
  the real Aruba mark. Swap it before any real public launch if that's a concern.
- Favicon: an inline Wi-Fi-signal SVG data URI in `<link rel="icon">`.

## Known gaps / TODO

- Both New and Classic tokens are held as-issued; there is **no server-side token
  refresh**, so `/api/*` calls 401 after ~2 h and the user reconnects. Classic
  additionally rotates the refresh token on every use.
- `LIST_CAP = 6000` rows per entity.
