"""Aruba Central Automation — thin backend.

Sessions live only in this process's memory, keyed by a per-browser cookie.
Nothing is written to disk and nothing persists a restart. A different browser
(no cookie) always starts a fresh session.
"""
from __future__ import annotations

import asyncio
import os
import re
from urllib.parse import quote
import secrets
import threading
import time
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# New Central issues access tokens from one global HPE GreenLake SSO endpoint;
# the selected cluster is only the base URL for subsequent API calls.
NEW_CENTRAL_TOKEN_URL = "https://sso.common.cloud.hpe.com/as/token.oauth2"

COOKIE_NAME = "acs_sid"
SESSION_TTL = 12 * 3600  # seconds of inactivity before a session is dropped
HTTP_TIMEOUT = 20.0

app = FastAPI(title="Aruba Central Automation", docs_url=None, redoc_url=None)

_lock = threading.Lock()
_sessions: dict[str, dict[str, Any]] = {}


# --------------------------------------------------------------------------- #
# session helpers
# --------------------------------------------------------------------------- #
def _now() -> float:
    return time.time()


def _gc() -> None:
    cutoff = _now() - SESSION_TTL
    with _lock:
        for sid in [s for s, v in _sessions.items() if v["seen"] < cutoff]:
            _sessions.pop(sid, None)


def _get(request: Request) -> Optional[dict[str, Any]]:
    sid = request.cookies.get(COOKIE_NAME)
    if not sid:
        return None
    with _lock:
        sess = _sessions.get(sid)
        if sess is not None:
            sess["seen"] = _now()
        return sess


def _ensure(request: Request) -> tuple[str, dict[str, Any]]:
    _gc()
    sid = request.cookies.get(COOKIE_NAME)
    with _lock:
        sess = _sessions.get(sid) if sid else None
        if sess is None:
            sid = secrets.token_urlsafe(32)
            sess = {"created": _now(), "seen": _now(), "classic": None, "new": None}
            _sessions[sid] = sess
        else:
            sess["seen"] = _now()
    return sid, sess


def _attach_cookie(resp: JSONResponse, sid: str) -> JSONResponse:
    resp.set_cookie(
        COOKIE_NAME, sid,
        max_age=SESSION_TTL, httponly=True, secure=True, samesite="lax", path="/",
    )
    return resp


def _public(conn: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not conn:
        return {"connected": False}
    return {
        "connected": True,
        "url": conn["url"],
        "devices": conn.get("devices"),
        "webhooks": conn.get("webhooks", True),
        "webhookKey": conn.get("webhookKey"),
    }


def _state(sess: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        "classic": _public(sess["classic"] if sess else None),
        "new": _public(sess["new"] if sess else None),
    }


def _clean_host(value: str) -> str:
    host = (value or "").strip()
    host = re.sub(r"^https?://", "", host, flags=re.I)
    return host.strip("/ ").strip()


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


# --------------------------------------------------------------------------- #
# Aruba Central OAuth
# --------------------------------------------------------------------------- #
async def _token_request(
    url: str,
    *,
    params: Optional[dict[str, str]] = None,
    data: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(url, params=params, data=data,
                                 headers={"Accept": "application/json"})
    if resp.status_code != 200:
        detail = resp.text.strip()
        if len(detail) > 300:
            detail = detail[:300] + "…"
        raise _TokenError(
            f"Central rejected the request ({resp.status_code}). "
            f"Check the credentials and selected URL. {detail}"
        )
    try:
        data = resp.json()
    except ValueError:
        raise _TokenError("Central returned an unexpected (non-JSON) response.")
    if "access_token" not in data:
        raise _TokenError("Central did not return an access token.")
    return data


class _TokenError(Exception):
    pass


# Device categories shown in the connected banner, in display order.
DEVICE_CATEGORIES = [
    ("ap", "Access Points"),
    ("switch", "Switches"),
    ("gateway", "Gateways"),
    ("bridge", "Bridges"),
]


def _empty_devices() -> dict[str, dict[str, int]]:
    return {key: {"up": 0, "down": 0} for key, _ in DEVICE_CATEGORIES}


def _categorize(raw_type: str) -> Optional[str]:
    t = (raw_type or "").upper()
    if "ACCESS_POINT" in t or t in ("AP", "IAP"):
        return "ap"
    if "SWITCH" in t or t in ("CX", "SW"):
        return "switch"
    if "GATEWAY" in t or "CONTROLLER" in t or t in ("GW", "MC"):
        return "gateway"
    if "BRIDGE" in t:
        return "bridge"
    return None


async def _new_central_devices(host: str, token: str) -> Optional[dict[str, dict[str, int]]]:
    """Tally on-boarded devices by category / reachability for New Central.

    Best-effort: returns None (banner shows "—") if the inventory call fails.
    """
    tally = _empty_devices()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    url = f"https://{host}/network-monitoring/v1/devices"
    params: dict[str, str] = {"limit": "1000"}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            for page in range(25):  # safety cap
                r = await client.get(url, headers=headers, params=params)
                if r.status_code != 200:
                    return None if page == 0 else tally
                body = r.json()
                for item in body.get("items", []):
                    cat = _categorize(item.get("deviceType", ""))
                    if not cat:
                        continue
                    online = str(item.get("status", "")).upper() == "ONLINE"
                    tally[cat]["up" if online else "down"] += 1
                nxt = body.get("next")
                if not nxt:
                    break
                params["next"] = str(nxt)
        return tally
    except Exception:
        return None


async def _get_total(client: httpx.AsyncClient, url: str, headers: dict[str, str],
                     params: Optional[dict[str, str]] = None) -> Optional[int]:
    for attempt in range(3):
        try:
            r = await client.get(url, headers=headers, params=params or {})
            if r.status_code == 200:
                return r.json().get("total")
            if r.status_code not in (429, 500, 502, 503, 504):
                return None
        except Exception:
            pass
        if attempt < 2:
            await asyncio.sleep(0.4 * (attempt + 1))
    return None


async def _new_central_device_totals(
    client: httpx.AsyncClient, host: str, headers: dict[str, str]
) -> Optional[dict[str, int]]:
    totals = {"accessPoints": 0, "switches": 0, "gateways": 0}
    seen = False
    params: dict[str, str] = {"limit": "1000"}
    url = f"https://{host}/network-monitoring/v1/devices"
    try:
        for _ in range(25):
            r = await client.get(url, headers=headers, params=params)
            if r.status_code != 200:
                return totals if seen else None
            seen = True
            body = r.json()
            for item in body.get("items", []):
                cat = _categorize(item.get("deviceType", ""))
                if cat == "ap":
                    totals["accessPoints"] += 1
                elif cat == "switch":
                    totals["switches"] += 1
                elif cat == "gateway":
                    totals["gateways"] += 1
            nxt = body.get("next")
            if not nxt:
                break
            params["next"] = str(nxt)
        return totals
    except Exception:
        return None if not seen else totals


async def _new_central_overview(host: str, token: str) -> dict[str, Optional[int]]:
    """Account-overview tallies for the New Central dashboard (best-effort per metric)."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    out: dict[str, Optional[int]] = {
        "clients": None, "accessPoints": None, "switches": None,
        "gateways": None, "sites": None, "subscriptions": None,
        "apGroups": None, "ssids": None, "rfProfiles": None,
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            clients_total, sites_total, subs_total, ssid_total, dev_totals = await asyncio.gather(
                _get_total(client, f"https://{host}/network-monitoring/v1/clients",
                           headers, {"limit": "1"}),
                _get_total(client, f"https://{host}/network-monitoring/v1/sites-health",
                           headers, {"limit": "1"}),
                _get_total(client, "https://global.api.greenlake.hpe.com/subscriptions/v1/subscriptions",
                           headers, {"limit": "1"}),
                _get_total(client, f"https://{host}/network-monitoring/v1/wlans",
                           headers, {"limit": "1"}),
                _new_central_device_totals(client, host, headers),
            )
        out["clients"] = clients_total
        out["sites"] = sites_total
        out["subscriptions"] = subs_total
        out["ssids"] = ssid_total
        if dev_totals:
            out.update(dev_totals)
    except Exception:
        pass
    try:
        groups = await _new_central_ap_groups(host, token)
        out["apGroups"] = len(groups) if groups is not None else None
    except Exception:
        pass
    try:
        rf = await _new_central_rf_list(host, token)
        out["rfProfiles"] = len(rf) if rf is not None else None
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- #
# New Central entity lists (dashboard drill-down)
# --------------------------------------------------------------------------- #
LIST_CAP = 6000


async def _fetch_all(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], *,
    style: str, params: Optional[dict[str, str]] = None, item_key: str = "items",
) -> tuple[list[dict[str, Any]], Optional[int], int]:
    params = dict(params or {})
    rows: list[dict[str, Any]] = []
    if style == "cursor":
        params.setdefault("limit", "1000")
        for _ in range(20):
            r = await client.get(url, headers=headers, params=params)
            if r.status_code != 200:
                return rows, None, r.status_code
            body = r.json()
            rows.extend(body.get(item_key, []))
            nxt = body.get("next")
            if not nxt or len(rows) >= LIST_CAP:
                return rows, body.get("total"), 200
            params["next"] = str(nxt)
        return rows, None, 200
    # offset style
    limit = int(params.get("limit", 200))
    params["limit"] = str(limit)
    total: Optional[int] = None
    offset = 0
    for _ in range(60):
        params["offset"] = str(offset)
        r = await client.get(url, headers=headers, params=params)
        if r.status_code != 200:
            return rows, total, r.status_code
        body = r.json()
        batch = body.get(item_key, [])
        rows.extend(batch)
        total = body.get("total", total)
        offset += limit
        if len(batch) < limit or len(rows) >= LIST_CAP or (total and len(rows) >= total):
            return rows, total, 200
    return rows, total, 200


def _norm_client(c: dict[str, Any]) -> dict[str, Any]:
    band = c.get("wirelessBand")
    if not band:
        band = "Wired" if c.get("clientConnectionType") == "Wired" else "—"
    return {
        "name": c.get("hostName") or c.get("clientName") or c.get("macAddress") or "—",
        "mac": c.get("macAddress") or "—",
        "ip": c.get("ipv4") or c.get("ipv6") or "—",
        "vlan": c.get("vlanId") or "—",
        "band": band,
        "channel": c.get("wirelessChannel") or "—",
        "snr": c.get("snr") if c.get("snr") not in (None, "") else "—",
        "link": c.get("wirelessChannel") or c.get("port") or "—",
        "connType": c.get("clientConnectionType") or "—",
        "connectedTo": c.get("connectedTo") or "—",
        "os": c.get("clientOperatingSystem") or "—",
        "site": c.get("siteName") or "—",
        "status": c.get("status") or "—",
    }


def _norm_device(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": d.get("deviceName") or d.get("serialNumber") or "—",
        "serial": d.get("serialNumber") or "—",
        "model": d.get("model") or "—",
        "ip": d.get("ipv4") or "—",
        "mac": d.get("macAddress") or "—",
        "clients": None,
        "site": d.get("siteName") or "—",
        "firmware": d.get("firmwareVersion") or "—",
        "status": "Up" if str(d.get("status", "")).upper() == "ONLINE" else "Down",
    }


def _health_triplet(obj: Optional[dict[str, Any]]) -> dict[str, int]:
    groups = ((obj or {}).get("health") or {}).get("groups") or []
    g = {x.get("name"): x.get("value", 0) for x in groups}
    return {"poor": g.get("Poor", 0), "fair": g.get("Fair", 0), "good": g.get("Good", 0)}


def _norm_site(s: dict[str, Any]) -> dict[str, Any]:
    addr = s.get("address") or {}
    return {
        "name": s.get("siteName") or "—",
        "id": s.get("id") or "",
        "clients": (s.get("clients") or {}).get("count", 0),
        "clientHealth": _health_triplet(s.get("clients")),
        "devices": (s.get("devices") or {}).get("count", 0),
        "deviceHealth": _health_triplet(s.get("devices")),
        "alerts": (s.get("alerts") or {}).get("totalCount", 0),
        "city": addr.get("city") or "—",
        "country": addr.get("country") or "—",
    }


def _site_detail(s: dict[str, Any]) -> dict[str, Any]:
    addr = s.get("address") or {}
    loc = s.get("location") or {}
    ch, dh, sh = _health_triplet(s.get("clients")), _health_triplet(s.get("devices")), _health_triplet(s)
    reasons = "; ".join(
        f"{r.get('reason')}" + (f" ({(r.get('data') or {}).get('count')})" if (r.get('data') or {}).get('count') is not None else "")
        for r in (s.get("reasons") or [])
    )
    src = {
        "health": f"{sh['good']}% good" if any(sh.values()) else "—",
        "clientCount": (s.get("clients") or {}).get("count", 0),
        "deviceCount": (s.get("devices") or {}).get("count", 0),
        "alertCount": (s.get("alerts") or {}).get("totalCount", 0),
        "reasons": reasons,
        "cGood": ch["good"], "cFair": ch["fair"], "cPoor": ch["poor"],
        "dGood": dh["good"], "dFair": dh["fair"], "dPoor": dh["poor"],
        "address": addr.get("address"), "city": addr.get("city"), "state": addr.get("state"),
        "zip": addr.get("zipCode"), "country": addr.get("country"),
        "lat": loc.get("latitude"), "long": loc.get("longitude"),
        "id": s.get("id"),
    }
    groups = [
        _kv_group("Overview", src, [
            ("health", "Site health"), ("clientCount", "Clients"), ("deviceCount", "Devices"),
            ("alertCount", "Open alerts"), ("reasons", "Health reasons"), ("id", "Site ID"),
        ]),
        _kv_group("Client health", src, [("cGood", "Good"), ("cFair", "Fair"), ("cPoor", "Poor")]),
        _kv_group("Device health", src, [("dGood", "Good"), ("dFair", "Fair"), ("dPoor", "Poor")]),
        _kv_group("Address", src, [
            ("address", "Street"), ("city", "City"), ("state", "State"),
            ("zip", "Postcode"), ("country", "Country"),
        ]),
        _kv_group("Location", src, [("lat", "Latitude"), ("long", "Longitude")]),
    ]
    return {
        "title": s.get("siteName") or "Site",
        "subtitle": ", ".join(p for p in (addr.get("city"), addr.get("country")) if p),
        "status": "",
        "groups": [g for g in groups if g],
        "meta": {"kind": "site", "siteId": s.get("id")},
    }


def _topo_node_type(d: dict[str, Any]) -> str:
    if d.get("internet"):
        return "internet"
    t = str(d.get("type") or "").lower()
    fn = str(d.get("deviceFunction") or "").lower()
    nm = str(d.get("name") or "").lower()
    if "gateway" in t or "gateway" in fn or "controller" in fn:
        return "gateway"
    if any(k in nm for k in ("firewall", "gateway", "router", "edge-", " edge", "-fw", "fw-")):
        return "gateway"
    if "point" in t or t == "ap" or "iap" in t or nm.startswith("ap-") or "-ap-" in nm:
        return "ap"
    if "switch" in t or "switch" in fn or "switch" in nm:
        if any(k in fn for k in ("core", "aggreg", "distrib", "routing", "l3")):
            return "l3switch"
        return "l2switch"
    return "other"


def _fmt_speed(bps: Any) -> str:
    try:
        v = float(bps or 0)
    except (TypeError, ValueError):
        return ""
    if v >= 1e9:
        return f"{v / 1e9:g} Gbps"
    if v >= 1e6:
        return f"{v / 1e6:g} Mbps"
    return ""


async def _new_central_topology(host: str, token: str, site_id: str) -> Optional[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(
                f"https://{host}/network-monitoring/v1/topology/{site_id}", headers=headers)
        if r.status_code != 200:
            return None
        body = r.json()
    except Exception:
        return None

    devs = body.get("devices") or []
    if isinstance(devs, dict):
        devs = devs.get("items", [])
    raw_links = body.get("links") or []
    if isinstance(raw_links, dict):
        raw_links = raw_links.get("items", [])

    nodes: dict[str, dict[str, Any]] = {}
    for d in devs:
        s = d.get("serial")
        if not s:
            continue
        nodes[s] = {
            "serial": s,
            "name": d.get("name") or s,
            "type": _topo_node_type(d),
            "model": d.get("model") or "",
            "function": d.get("deviceFunction") or "",
            "ip": d.get("ipv4") or "",
            "mac": d.get("mac") or "",
            "status": d.get("status") or "",
            "health": d.get("health") or "",
            "unmanaged": str(s).startswith("tpd_"),
        }

    links: list[dict[str, Any]] = []
    for lk in raw_links:
        f, t = lk.get("from"), lk.get("to")
        if not f or not t:
            continue
        for endp in (f, t):
            if endp not in nodes:
                unmanaged = str(endp).startswith("tpd_")
                nodes[endp] = {
                    "serial": endp,
                    "name": "Unmanaged device" if unmanaged else str(endp),
                    "type": "other", "model": "", "function": "",
                    "ip": "", "mac": "", "status": "", "health": "",
                    "unmanaged": True,
                }
        links.append({
            "from": f, "to": t,
            "speed": _fmt_speed(lk.get("speed")),
            "edgeType": lk.get("edgeType") or "",
            "fromPort": ((lk.get("fromPortList") or [{}])[0] or {}).get("name") or "",
            "toPort": ((lk.get("toPortList") or [{}])[0] or {}).get("name") or "",
            "health": lk.get("health") or "",
        })

    return {
        "nodes": list(nodes.values()),
        "links": links,
        "isolated": body.get("isolatedDevicesCount", 0),
    }


async def _new_central_site_detail(host: str, token: str, site_id: str) -> Optional[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            raw, _t, _sc = await _fetch_all(
                client, f"https://{host}/network-monitoring/v1/sites-health", headers,
                style="offset", params={"limit": "100"})
            match = next((x for x in raw if str(x.get("id", "")) == str(site_id)), None)
            return _site_detail(match) if match else None
    except Exception:
        return None


def _norm_sub(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "description": s.get("skuDescription") or "—",
        "sku": s.get("sku") or "—",
        "tier": s.get("tierDescription") or s.get("tier") or "—",
        "quantity": s.get("quantity") or "—",
        "available": s.get("availableQuantity") or "—",
        "status": s.get("subscriptionStatus") or "—",
        "end": (s.get("endTime") or "")[:10] or "—",
        "eval": "Eval" if s.get("isEval") else "—",
    }


async def _client_counts_by_serial(
    client: httpx.AsyncClient, host: str, headers: dict[str, str]
) -> dict[str, int]:
    """Map device serial -> number of connected clients (best-effort)."""
    counts: dict[str, int] = {}
    try:
        raw, _total, sc = await _fetch_all(
            client, f"https://{host}/network-monitoring/v1/clients", headers, style="cursor")
        if sc == 200 or raw:
            for c in raw:
                serial = c.get("connectedDeviceSerial")
                if serial:
                    counts[serial] = counts.get(serial, 0) + 1
    except Exception:
        pass
    return counts


def _kv_group(label: str, source: dict[str, Any], spec: list[tuple[str, str]]) -> Optional[dict[str, Any]]:
    fields = []
    for key, disp in spec:
        val = source.get(key)
        if val in (None, "", "-"):
            continue
        fields.append([disp, str(val)])
    return {"label": label, "fields": fields} if fields else None


def _client_detail(c: dict[str, Any]) -> dict[str, Any]:
    wireless = str(c.get("clientConnectionType", "")).lower() == "wireless"
    if not c.get("snr"):  # Central reports 0 when SNR is unavailable (e.g. some 6 GHz clients)
        c = dict(c)
        c["snr"] = None
    groups = [
        _kv_group("Identity", c, [
            ("hostName", "Hostname"), ("clientName", "Client name"), ("macAddress", "MAC address"),
            ("clientCategory", "Category"), ("clientFunction", "Function"),
            ("clientOperatingSystem", "Operating system"),
            ("clientManufacturer", "Manufacturer"), ("clientVendor", "Vendor"),
        ]),
        _kv_group("Connection", c, [
            ("status", "Status"), ("clientConnectionType", "Connection type"),
            ("connectedTo", "Connected to"), ("connectedDeviceType", "Upstream device type"),
            ("connectedDeviceSerial", "Upstream serial"), ("siteName", "Site"),
            ("vlanId", "VLAN ID"), ("vlanName", "VLAN name"), ("connectedAt", "Connected at"),
        ]),
        _kv_group("Network", c, [
            ("ipv4", "IPv4"), ("ipv6", "IPv6"), ("role", "Role"),
            ("port", "Port"), ("tunnelType", "Tunnel type"),
        ]),
    ]
    if wireless:
        groups.append(_kv_group("Wireless", c, [
            ("wirelessBand", "Band"), ("wirelessChannel", "Channel"), ("phyType", "PHY type"),
            ("snr", "SNR"), ("wlanName", "WLAN / SSID"), ("bssid", "BSSID"),
            ("radioMacAddress", "Radio MAC"), ("wirelessSecurity", "Security"),
            ("keyManagement", "Key management"), ("authenticationType", "Authentication"),
        ]))
    name = c.get("hostName") or c.get("clientName") or c.get("macAddress") or "Client"
    return {
        "title": name,
        "subtitle": c.get("macAddress") or "",
        "status": c.get("status") or "",
        "groups": [g for g in groups if g],
        "meta": {
            "kind": "client",
            "siteId": c.get("siteId"),
            "focusSerial": c.get("connectedDeviceSerial"),
            "clientName": name,
            "clientMac": c.get("macAddress"),
            "clientIp": c.get("ipv4") or c.get("ipv6"),
            "connType": c.get("clientConnectionType"),
            "band": c.get("wirelessBand"),
            "snr": c.get("snr"),
            "channel": c.get("wirelessChannel"),
        },
    }


def _device_detail(d: dict[str, Any], client_count: Optional[int]) -> dict[str, Any]:
    online = str(d.get("status", "")).upper() == "ONLINE"
    up_ms = d.get("uptimeInMillis") or 0
    uptime = ""
    if up_ms:
        days = up_ms // 86400000
        hours = (up_ms % 86400000) // 3600000
        uptime = f"{days}d {hours}h"
    extra = dict(d)
    extra["status"] = "Up" if online else "Down"
    extra["deviceType"] = {
        "ACCESS_POINT": "Access Point", "SWITCH": "Switch",
        "GATEWAY": "Gateway", "BRIDGE": "Bridge",
    }.get(str(d.get("deviceType", "")).upper(), d.get("deviceType"))
    if uptime:
        extra["_uptime"] = uptime
    if client_count is not None:
        extra["_clients"] = client_count
    groups = [
        _kv_group("Identity", extra, [
            ("deviceName", "Name"), ("serialNumber", "Serial"), ("macAddress", "MAC address"),
            ("model", "Model"), ("partNumber", "Part number"),
            ("deviceType", "Type"), ("deviceFunction", "Function"),
        ]),
        _kv_group("Status", extra, [
            ("status", "Status"), ("_uptime", "Uptime"), ("_clients", "Connected clients"),
            ("firmwareVersion", "Firmware"), ("configStatus", "Config status"),
            ("configLastModifiedAt", "Config modified"), ("lastSeenAt", "Last seen"),
        ]),
        _kv_group("Location", extra, [
            ("siteName", "Site"), ("deployment", "Deployment"), ("role", "Role"),
            ("ipv4", "IPv4"), ("ipv6", "IPv6"), ("clusterName", "Cluster"),
        ]),
    ]
    return {
        "title": d.get("deviceName") or d.get("serialNumber") or "Device",
        "subtitle": (d.get("model") or "") + (" · " + d.get("serialNumber") if d.get("serialNumber") else ""),
        "status": "Up" if online else "Down",
        "groups": [g for g in groups if g],
        "meta": {
            "kind": "device",
            "siteId": d.get("siteId"),
            "focusSerial": d.get("serialNumber"),
        },
    }


async def _new_central_client_detail(host: str, token: str, mac: str) -> Optional[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(
                f"https://{host}/network-monitoring/v1/clients/{mac}", headers=headers)
            if r.status_code == 200:
                body = r.json()
                item = body.get("items", [body])[0] if isinstance(body, dict) else body
                if isinstance(item, dict) and item:
                    return _client_detail(item)
            # fall back to scanning the list
            raw, _t, sc = await _fetch_all(
                client, f"https://{host}/network-monitoring/v1/clients", headers, style="cursor")
            for c in raw:
                if str(c.get("macAddress", "")).lower() == mac.lower():
                    return _client_detail(c)
    except Exception:
        pass
    return None


async def _new_central_device_detail(host: str, token: str, serial: str) -> Optional[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            raw, _t, sc = await _fetch_all(
                client, f"https://{host}/network-monitoring/v1/devices", headers, style="cursor")
            match = next((x for x in raw if str(x.get("serialNumber", "")) == serial), None)
            if match is None:
                return None
            by_serial = await _client_counts_by_serial(client, host, headers)
            return _device_detail(match, by_serial.get(serial))
    except Exception:
        return None


async def _new_central_list(host: str, token: str, entity: str) -> tuple[Optional[list[dict[str, Any]]], int]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    total: Optional[int] = None
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        if entity == "clients":
            raw, total, sc = await _fetch_all(
                client, f"https://{host}/network-monitoring/v1/clients", headers, style="cursor")
            if sc != 200 and not raw:
                return None, 0
            rows = [_norm_client(x) for x in raw]
        elif entity in ("access-points", "switches", "gateways"):
            cat = {"access-points": "ap", "switches": "switch", "gateways": "gateway"}[entity]
            raw, total, sc = await _fetch_all(
                client, f"https://{host}/network-monitoring/v1/devices", headers, style="cursor")
            if sc != 200 and not raw:
                return None, 0
            by_serial = await _client_counts_by_serial(client, host, headers)
            rows = []
            for x in raw:
                if _categorize(x.get("deviceType", "")) != cat:
                    continue
                row = _norm_device(x)
                row["clients"] = by_serial.get(row["serial"])
                rows.append(row)
        elif entity == "sites":
            raw, total, sc = await _fetch_all(
                client, f"https://{host}/network-monitoring/v1/sites-health", headers,
                style="offset", params={"limit": "100"})
            if sc != 200 and not raw:
                return None, 0
            rows = [_norm_site(x) for x in raw]
        elif entity == "subscriptions":
            raw, total, sc = await _fetch_all(
                client, "https://global.api.greenlake.hpe.com/subscriptions/v1/subscriptions",
                headers, style="offset", params={"limit": "200"})
            if sc != 200 and not raw:
                return None, 0
            rows = [_norm_sub(x) for x in raw]
        elif entity == "ssids":
            raw, total, sc = await _fetch_all(
                client, f"https://{host}/network-monitoring/v1/wlans", headers, style="cursor")
            if sc != 200 and not raw:
                return None, 0
            rows = [_norm_wlan(x) for x in raw]
        elif entity == "ap-groups":
            body = await _nc_get(client, host, headers, "device-collections")
            if body is None:
                return None, 0
            rows = [_nc_group_row(g) for g in body.get("items", [])]
            rows.sort(key=lambda r: r["name"].lower())
            total = len(rows)
        elif entity == "rf-profiles":
            body = await _nc_get(client, host, headers, "radios")
            if body is None:
                return None, 0
            total = None
            abody = await _nc_get(client, host, headers, "config-assignments",
                                  {"profile-type": "radios"})
            used: dict[str, set[str]] = {}
            for a in (abody or {}).get("config-assignment", []):
                if a.get("scope-name"):
                    used.setdefault(a.get("profile-instance", ""), set()).add(a["scope-name"])
            rows = []
            for p in body.get("profile", []):
                row = _nc_rf_row(p)
                row["groups"] = ", ".join(sorted(used.get(p.get("name", ""), []))) or "—"
                rows.append(row)
            rows.sort(key=lambda r: r["name"].lower())
        else:
            return None, 0
    return rows, total if total is not None else len(rows)


def _norm_wlan(w: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": w.get("wlanName") or w.get("name") or "—",
        "status": "Enabled" if str(w.get("status", "")).upper() in ("ENABLED", "UP", "1") else "Disabled",
        "security": w.get("security") or "—",
        "securityLevel": w.get("securityLevel") or w.get("type") or "—",
        "band": w.get("band") or "—",
        "vlan": str(w.get("vlan") or "—"),
        "clients": w.get("clientCount"),
    }


async def _new_central_ssid_detail(host: str, token: str, name: str) -> Optional[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        wlans, _t, _sc = await _fetch_all(
            client, f"https://{host}/network-monitoring/v1/wlans", headers, style="cursor")
        w = next((x for x in wlans if (x.get("wlanName") or x.get("name")) == name), None)
        if w is None:
            return None
        clients, _t2, _sc2 = await _fetch_all(
            client, f"https://{host}/network-monitoring/v1/clients", headers, style="cursor")
    members = [{
        "name": _pick(c, "hostName", "clientName", "macAddress", default="?"),
        "serial": c.get("macAddress"), "category": "client", "kind": "client",
        "status": c.get("status") or "",
    } for c in clients if c.get("wlanName") == name]
    cfg = {
        "security": w.get("security"), "securityLevel": w.get("securityLevel"),
        "band": w.get("band"), "vlan": w.get("vlan"),
        "status": "Enabled" if str(w.get("status", "")).upper() == "ENABLED" else "Disabled",
    }
    usage = {"clientCount": w.get("clientCount"), "connected": len(members)}
    groups = [
        _kv_group("Configuration", cfg, [
            ("status", "Status"), ("security", "Security"), ("securityLevel", "Security level"),
            ("band", "Bands"), ("vlan", "VLAN"),
        ]),
        _kv_group("Clients", usage, [
            ("clientCount", "Reported client count"), ("connected", "Connected now"),
        ]),
    ]
    return {
        "title": name,
        "subtitle": w.get("security") or "SSID",
        "status": cfg["status"],
        "groups": [g for g in groups if g],
        "devices": members,
        "meta": {"kind": "ssid"},
    }


# --------------------------------------------------------------------------- #
# New Central configuration model (network-config/v1alpha1)
# --------------------------------------------------------------------------- #
NC_CFG = "/network-config/v1alpha1"


async def _nc_get(client: httpx.AsyncClient, host: str, headers: dict[str, str],
                  root: str, params: Optional[dict[str, str]] = None):
    r = await _retry_get(client, f"https://{host}{NC_CFG}/{root}", headers, params)
    if r is None or r.status_code != 200:
        return None
    return r.json() if r.content else {}


_NC_RADIO_BAND = {"RADIO_2DOT4G": "2.4 GHz", "RADIO_5G": "5 GHz",
                  "RADIO_2ND_5G": "5 GHz (secondary)", "RADIO_6G": "6 GHz",
                  "RADIO_2ND_6GHZ": "6 GHz (secondary)", "RADIO_2ND_6G": "6 GHz (secondary)"}
_NC_BW = {"BW_20MHZ": "20 MHz", "BW_40MHZ": "40 MHz", "BW_80MHZ": "80 MHz",
          "BW_160MHZ": "160 MHz", "BW_320MHZ": "320 MHz"}


def _nc_chan_list(arm: dict[str, Any]) -> str:
    for k in ("channels-for-2dot4GHz", "channels-for-5GHz", "channels-for-6GHz"):
        v = arm.get(k)
        if v:
            return ", ".join(str(c).replace("CHAN_", "").replace("_6GHZ", "").replace("_5GHZ", "")
                             for c in v)
    return "Regulatory default"


def _nc_width(arm: dict[str, Any]) -> Optional[str]:
    lo = _NC_BW.get(arm.get("min-channel-bandwidth"), arm.get("min-channel-bandwidth"))
    hi = _NC_BW.get(arm.get("max-channel-bandwidth"), arm.get("max-channel-bandwidth"))
    if not lo and not hi:
        return None
    return lo if lo == hi else f"{lo} – {hi}"


async def _new_central_ap_groups(host: str, token: str) -> Optional[list[dict[str, Any]]]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        body = await _nc_get(client, host, headers, "device-collections")
    if body is None:
        return None
    return body.get("items", [])


async def _new_central_rf_list(host: str, token: str) -> Optional[list[dict[str, Any]]]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        body = await _nc_get(client, host, headers, "radios")
    if body is None:
        return None
    return body.get("profile", [])


def _nc_group_row(g: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": g.get("scopeName") or "—",
        "id": str(g.get("scopeId") or g.get("id") or ""),
        "devices": g.get("deviceCount", 0),
        "arch": "AOS-8 Instant" if g.get("isIap8x") else "AOS-10",
        "description": g.get("description") or "—",
    }


def _nc_rf_row(p: dict[str, Any]) -> dict[str, Any]:
    radios = p.get("radio", []) or []
    bands = [_NC_RADIO_BAND.get(r.get("profile"), r.get("profile") or "?") for r in radios]
    a5 = next((r for r in radios if r.get("profile") == "RADIO_5G"), radios[0] if radios else {})
    arm = (a5 or {}).get("arm-control", {}) or {}
    return {
        "name": p.get("name") or "—",
        "scope": "Named profile",
        "radios": ", ".join(bands) or "—",
        "txpower": (f"{arm.get('min-tx-power')}–{arm.get('max-tx-power')} dBm"
                    if arm.get("min-tx-power") and arm.get("max-tx-power") else "—"),
        "groups": "—",
    }


async def _new_central_group_detail(host: str, token: str, ident: str) -> Optional[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        gbody = await _nc_get(client, host, headers, "device-collections")
        abody = await _nc_get(client, host, headers, "config-assignments",
                              {"scope-id": str(ident)})
    items = (gbody or {}).get("items", [])
    g = next((x for x in items if str(x.get("scopeId")) == str(ident)
              or x.get("scopeName") == ident), None)
    if g is None:
        return None
    assigns = (abody or {}).get("config-assignment", [])
    by_fn: dict[str, dict[str, list[str]]] = {}
    for a in assigns:
        by_fn.setdefault(a.get("device-function", "?"), {}).setdefault(
            a.get("profile-type", "?"), []).append(a.get("profile-instance", "?"))
    groups = [_kv_group("Group", {
        "arch": "AOS-8 Instant" if g.get("isIap8x") else "AOS-10",
        "devices": g.get("deviceCount", 0),
        "desc": g.get("description") or None,
        "scope": str(g.get("scopeId") or ""),
    }, [("arch", "Architecture"), ("devices", "Devices"), ("desc", "Description"),
        ("scope", "Scope ID")])]
    for fn in sorted(by_fn):
        src = {pt: ", ".join(sorted(set(v))) for pt, v in sorted(by_fn[fn].items())}
        grp = _kv_group(_humanize(fn) + " profiles", src,
                        [(pt, _humanize(pt)) for pt in src])
        if grp:
            groups.append(grp)
    return {
        "title": g.get("scopeName") or str(ident),
        "subtitle": "Device collection (AP group)",
        "status": "",
        "groups": groups,
        "devices": [],
        "meta": {"kind": "group"},
    }


async def _new_central_rf_detail(host: str, token: str, name: str) -> Optional[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        body = await _nc_get(client, host, headers, "radios")
        abody = await _nc_get(client, host, headers, "config-assignments",
                              {"profile-type": "radios"})
    p = next((x for x in (body or {}).get("profile", []) if x.get("name") == name), None)
    if p is None:
        return None
    used_by = sorted({a.get("scope-name") for a in (abody or {}).get("config-assignment", [])
                      if a.get("profile-instance") == name and a.get("scope-name")})
    groups = []
    for r in p.get("radio", []) or []:
        band = _NC_RADIO_BAND.get(r.get("profile"), r.get("profile") or "?")
        arm = r.get("arm-control", {}) or {}
        src = {
            "power": (f"{arm.get('min-tx-power')}–{arm.get('max-tx-power')} dBm"
                      if arm.get("min-tx-power") and arm.get("max-tx-power") else None),
            "width": _nc_width(arm),
            "channels": _nc_chan_list(arm),
            "mode": r.get("mode"),
            "enabled": "Yes" if r.get("enable") else "No",
            "dot11h": "On" if r.get("ieee802dot11h") else None,
            "bgscan": "On" if r.get("background-spectrum-monitoring") else None,
            "zwdfs": "On" if arm.get("zero-wait-dfs") else None,
        }
        grp = _kv_group(f"{band} radio", src, [
            ("enabled", "Enabled"), ("mode", "Mode"), ("power", "Allowed transmit power"),
            ("width", "Channel width"), ("channels", "Allowed channels"),
            ("dot11h", "802.11h"), ("bgscan", "Background spectrum monitoring"),
            ("zwdfs", "Zero-wait DFS"),
        ])
        if grp:
            groups.append(grp)
    groups.append({"label": "Assigned to",
                   "fields": [["AP groups / scopes", ", ".join(used_by) or "—"]]})
    return {
        "title": name, "subtitle": "RF profile (radios)", "status": "",
        "groups": groups, "devices": [], "meta": {"kind": "rf"},
    }


async def _classic_central_devices(host: str, token: str) -> Optional[dict[str, dict[str, int]]]:
    """Tally devices by category / status for Classic Central (best-effort)."""
    tally = _empty_devices()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    sources = (
        ("ap", "/monitoring/v2/aps", "aps"),
        ("switch", "/monitoring/v1/switches", "switches"),
        ("gateway", "/monitoring/v1/gateways", "gateways"),
    )
    got_one = False
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            for cat, path, list_key in sources:
                offset = 0
                while True:
                    r = await client.get(
                        f"https://{host}{path}", headers=headers,
                        params={"limit": 1000, "offset": offset, "calculate_total": "true"},
                    )
                    if r.status_code != 200:
                        break
                    got_one = True
                    body = r.json()
                    rows = body.get(list_key, []) or []
                    for row in rows:
                        up = str(row.get("status", "")).lower() in ("up", "online", "true")
                        tally[cat]["up" if up else "down"] += 1
                    if len(rows) < 1000:
                        break
                    offset += 1000
        return tally if got_one else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Classic Central — overview / lists / details / topology
#
# NOTE: the legacy Aruba Central monitoring APIs have not been exercised with a
# real token yet. Field extraction is defensive (tries several key spellings);
# expect a debugging pass once Classic credentials are available.
# --------------------------------------------------------------------------- #
def _pick(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return default


def _classic_band(v: Any) -> Optional[str]:
    s = str(v or "").strip().lower().replace("ghz", "").strip()
    if not s:
        return None
    if s.startswith("2.4") or s == "2":
        return "2.4 GHz"
    if s.startswith("6"):
        return "6 GHz"
    if s.startswith("5"):
        return "5 GHz"
    return str(v)


def _classic_up(v: Any) -> bool:
    return str(v).strip().lower() in ("up", "online", "true", "1", "connected")


CLASSIC_SOURCES = {
    "access-points": ("/monitoring/v2/aps", "aps"),
    "switches": ("/monitoring/v1/switches", "switches"),
    "gateways": ("/monitoring/v1/gateways", "gateways"),
}
# Classic clients live on split v1 endpoints (v2/clients returned empty on the
# tenant tested); wired vs wireless is which endpoint, not a field.
CLASSIC_CLIENT_SOURCES = (
    ("/monitoring/v1/clients/wireless", False),
    ("/monitoring/v1/clients/wired", True),
)


def _classic_norm_client(c: dict[str, Any], wired: bool) -> dict[str, Any]:
    band = None if wired else _classic_band(_pick(c, "band", "radio_band", "frequency"))
    return {
        "name": _pick(c, "name", "hostname", "username", "macaddr", default="—"),
        "mac": _pick(c, "macaddr", "mac_address", "mac", default="—"),
        "ip": _pick(c, "ip_address", "ipv4", "ip", default="—"),
        "vlan": str(_pick(c, "vlan", "vlan_id", default="—")),
        "band": "Wired" if wired else (band or "—"),
        "channel": str(_pick(c, "channel", "wireless_channel", default="—")),
        "snr": _pick(c, "snr", "signal_to_noise", default="—"),
        "connType": "Wired" if wired else "Wireless",
        "connectedTo": _pick(c, "associated_device_name", "connected_device_name",
                             "network", "associated_device", default="—"),
        "os": _pick(c, "os_type", "client_os", "operating_system", default="—"),
        "site": _pick(c, "site", "site_name", default="—"),
        "status": "Connected",
    }


def _classic_norm_device(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": _pick(d, "name", "hostname", "serial", default="—"),
        "serial": _pick(d, "serial", "serial_number", default="—"),
        "model": _pick(d, "model", "device_model", "part_number", default="—"),
        "ip": _pick(d, "ip_address", "ipv4", "ip", default="—"),
        "mac": _pick(d, "macaddr", "mac_address", "mac", default="—"),
        "clients": _pick(d, "client_count", "clients", "connected_clients"),
        "site": _pick(d, "site", "site_name", default="—"),
        "firmware": _pick(d, "firmware_version", "firmware", default="—"),
        "status": "Up" if _classic_up(_pick(d, "status", "state", default="")) else "Down",
    }


def _epoch_date(v: Any) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v or "")[:10]
    if n > 1e12:  # milliseconds
        n /= 1000.0
    if n <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(n))
    except (ValueError, OSError):
        return ""


def _classic_norm_sub(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "description": _pick(s, "license_type", "sku_description", "description",
                             "acpapp_name", "subscription_type", default="—"),
        "sku": _pick(s, "sku", "subscription_key", default="—"),
        "tier": _pick(s, "license_type", "tier", "subscription_type", default="—"),
        "quantity": _pick(s, "quantity", "total", default="—"),
        "available": _pick(s, "available", "available_quantity", default="—"),
        "status": _pick(s, "status", "subscription_status", default="—"),
        "end": _epoch_date(_pick(s, "end_date", "expiry", default="")) or "—",
        "eval": "Eval" if str(_pick(s, "subscription_type", default="")).upper() == "EVAL"
                or _pick(s, "is_eval", default=False) else "—",
    }


def _classic_norm_site(s: dict[str, Any], health: dict[str, Any]) -> dict[str, Any]:
    name = _pick(s, "site_name", "name", default="—")
    h = health.get(name, {})
    dev_up = int(_pick(h, "device_up", "wired_device_up", default=0) or 0)
    dev_dn = int(_pick(h, "device_down", "wired_device_down", default=0) or 0)
    cl_ok = int(_pick(h, "connected_count", "client_connected_count", default=0) or 0)
    cl_bad = int(_pick(h, "failed_count", "client_failed_count", default=0) or 0)
    return {
        "name": name,
        "id": str(_pick(s, "site_id", "id", default="")),
        "clients": cl_ok + cl_bad,
        "clientHealth": {"poor": cl_bad, "fair": 0, "good": cl_ok},
        "devices": dev_up + dev_dn or _pick(s, "associated_device_count", default=0),
        "deviceHealth": {"poor": dev_dn, "fair": 0, "good": dev_up},
        "alerts": int(_pick(h, "alert_count", default=0) or 0),
        "city": _pick(s, "city", default="—"),
        "country": _pick(s, "country", default="—"),
    }


async def _classic_branch_health(client: httpx.AsyncClient, host: str,
                                 headers: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        rows, _t, _sc = await _fetch_all(
            client, f"https://{host}/branchhealth/v1/site", headers,
            style="offset", params={"limit": "100"}, item_key="items")
        if not rows:
            rows, _t, _sc = await _fetch_all(
                client, f"https://{host}/branchhealth/v1/site", headers,
                style="offset", params={"limit": "100"}, item_key="sites")
        for r in rows:
            out[_pick(r, "name", "site_name", default="")] = r
    except Exception:
        pass
    return out


async def _classic_central_overview(host: str, token: str) -> dict[str, Optional[int]]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    out: dict[str, Optional[int]] = {
        "clients": None, "accessPoints": None, "switches": None,
        "gateways": None, "sites": None, "subscriptions": None,
        "apGroups": None, "ssids": None, "rfProfiles": None,
    }
    probes = {
        "accessPoints": "/monitoring/v2/aps",
        "switches": "/monitoring/v1/switches",
        "gateways": "/monitoring/v1/gateways",
        "sites": "/central/v2/sites",
        "subscriptions": "/platform/licensing/v1/subscriptions",
    }

    async def _count_groups(client):
        try:
            names, _sc = await _classic_group_names(client, host, headers)
            return len(names) if names is not None else None
        except Exception:
            return None
    async def _count_rows(client, path):
        try:
            rows, _t, sc = await _fetch_all(
                client, f"https://{host}{path}", headers, style="offset",
                params={"limit": "1000"}, item_key="clients")
            return len(rows) if (sc == 200 or rows) else None
        except Exception:
            return None

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            calls = [
                _get_total(client, f"https://{host}{p}", headers,
                           {"limit": "1", "calculate_total": "true"})
                for p in probes.values()
            ]
            calls += [_count_rows(client, p) for p, _ in CLASSIC_CLIENT_SOURCES]
            calls.append(_count_groups(client))
            results = await asyncio.gather(*calls)
        for key, val in zip(probes.keys(), results):
            out[key] = val
        cw, cd = results[-3], results[-2]
        if cw is not None or cd is not None:
            out["clients"] = (cw or 0) + (cd or 0)
        out["apGroups"] = results[-1]
    except Exception:
        pass
    try:
        smap = await _classic_ssid_map(host, token)
        out["ssids"] = len(smap) if smap else None
    except Exception:
        pass
    try:
        rp = await _classic_rf_profiles(host, token)
        out["rfProfiles"] = len(rp) if rp else None
    except Exception:
        pass
    return out


def _group_names_from(items: Any) -> list[str]:
    out: list[str] = []
    for it in items or []:
        if isinstance(it, list) and it:
            out.append(str(it[0]))
        elif isinstance(it, dict):
            out.append(str(_pick(it, "group", "group_name", "name", default="")))
        elif isinstance(it, str):
            out.append(it)
    return [n for n in out if n]


async def _retry_get(client: httpx.AsyncClient, url: str, headers: dict[str, str],
                     params: Optional[dict[str, Any]] = None, tries: int = 3,
                     retry_on: tuple[int, ...] = (429, 500, 502, 503, 504)):
    r = None
    for i in range(tries):
        try:
            r = await client.get(url, headers=headers, params=params or {})
            if r.status_code == 200 or r.status_code not in retry_on:
                return r
        except Exception:
            r = None
        if i < tries - 1:
            await asyncio.sleep(0.5 * (i + 1))
    return r


async def _classic_group_names(client: httpx.AsyncClient, host: str,
                               headers: dict[str, str]) -> tuple[Optional[list[str]], int]:
    # Classic config endpoints cap `limit` at 20.
    last_sc = 0
    for path in ("/configuration/v2/groups", "/configuration/v1/groups"):
        r = await _retry_get(client, f"https://{host}{path}", headers, {"limit": 20, "offset": 0})
        if r is None:
            continue
        last_sc = r.status_code
        if r.status_code != 200:
            continue
        body = r.json() if r.content else {}
        items = body.get("data") or body.get("groups") or body.get("items") or []
        names = _group_names_from(items)
        total = int(body.get("total") or 0)
        offset = 20
        while len(names) < total and offset < 4000:
            r2 = await _retry_get(client, f"https://{host}{path}", headers,
                                  {"limit": 20, "offset": offset})
            if r2 is None or r2.status_code != 200:
                break
            b2 = r2.json() if r2.content else {}
            more = _group_names_from(b2.get("data") or b2.get("groups") or [])
            if not more:
                break
            names += more
            offset += 20
        return names, 200
    return None, last_sc


async def _classic_group_device_counts(client: httpx.AsyncClient, host: str,
                                       headers: dict[str, str]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for cat, path, key in (("aps", "/monitoring/v2/aps", "aps"),
                           ("switches", "/monitoring/v1/switches", "switches"),
                           ("gateways", "/monitoring/v1/gateways", "gateways")):
        raw, _t, _sc = await _fetch_all(
            client, f"https://{host}{path}", headers, style="offset",
            params={"limit": "1000"}, item_key=key)
        for d in raw:
            g = _pick(d, "group_name", "group")
            if g:
                counts.setdefault(g, {"aps": 0, "switches": 0, "gateways": 0})[cat] += 1
    return counts


async def _classic_central_list(host: str, token: str, entity: str
                                ) -> tuple[Optional[list[dict[str, Any]]], int]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        if entity == "clients":
            rows, seen = [], False
            for path, wired in CLASSIC_CLIENT_SOURCES:
                raw, _t, sc = await _fetch_all(
                    client, f"https://{host}{path}", headers, style="offset",
                    params={"limit": "1000"}, item_key="clients")
                if sc == 200 or raw:
                    seen = True
                    rows += [_classic_norm_client(x, wired) for x in raw]
            if not seen:
                return None, 0
        elif entity in CLASSIC_SOURCES:
            path, key = CLASSIC_SOURCES[entity]
            raw, total, sc = await _fetch_all(
                client, f"https://{host}{path}", headers, style="offset",
                params={"limit": "1000", "calculate_total": "true"}, item_key=key)
            if sc != 200 and not raw:
                return None, 0
            counts: dict[str, int] = {}
            for cpath, _w in CLASSIC_CLIENT_SOURCES:
                craw, _t, _sc = await _fetch_all(
                    client, f"https://{host}{cpath}", headers, style="offset",
                    params={"limit": "1000"}, item_key="clients")
                for c in craw:
                    dev = _pick(c, "associated_device", "associated_device_mac")
                    if dev:
                        counts[dev] = counts.get(dev, 0) + 1
            rows = []
            for x in raw:
                row = _classic_norm_device(x)
                if row["clients"] is None:
                    row["clients"] = counts.get(row["serial"])
                rows.append(row)
        elif entity == "ssids":
            smap = await _classic_ssid_map(host, token)
            if not smap:
                return None, 0
            rows = [_ssid_row(nm, e) for nm, e in sorted(smap.items(), key=lambda x: x[0].lower())]
        elif entity == "ap-groups":
            names, _sc = await _classic_group_names(client, host, headers)
            dc = await _classic_group_device_counts(client, host, headers)
            if names is None:                       # config API unavailable — derive from devices
                names = sorted(dc.keys(), key=str.lower)
                if not names:
                    return None, 0
            rows = []
            for n in names:
                c = dc.get(n, {"aps": 0, "switches": 0, "gateways": 0})
                rows.append({"name": n, "aps": c["aps"], "switches": c["switches"],
                             "gateways": c["gateways"]})
        elif entity == "rf-profiles":
            rp = await _classic_rf_profiles(host, token)
            if rp is None:
                return None, 0
            rows = []
            for nm, e in sorted(rp.items(), key=lambda x: x[0].lower()):
                labels = sorted(e["bands"], key=lambda b: _RF_RADIO_ORDER.get(b, 9))
                a = next((e["bands"][b] for b in ("5 GHz", "2.4 GHz", "6 GHz") if b in e["bands"]), {})
                rows.append({
                    "name": nm,
                    "scope": "Named profile" if e["named"] else "Group default",
                    "radios": ", ".join(labels) or "—",
                    "txpower": _rf_power(a) or "—",
                    "groups": ", ".join(sorted(e["groups"])) or "—",
                })
        elif entity == "sites":
            raw, total, sc = await _fetch_all(
                client, f"https://{host}/central/v2/sites", headers, style="offset",
                params={"limit": "1000", "calculate_total": "true"}, item_key="sites")
            if sc != 200 and not raw:
                return None, 0
            health = await _classic_branch_health(client, host, headers)
            rows = [_classic_norm_site(x, health) for x in raw]
        elif entity == "subscriptions":
            raw, total, sc = await _fetch_all(
                client, f"https://{host}/platform/licensing/v1/subscriptions", headers,
                style="offset", params={"limit": "1000"}, item_key="subscriptions")
            if sc != 200 and not raw:
                return None, 0
            rows = [_classic_norm_sub(x) for x in raw]
        else:
            return None, 0
    return rows, len(rows)


async def _classic_find(host: str, token: str, entity: str, match) -> Optional[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if entity not in CLASSIC_SOURCES:
        return None
    path, key = CLASSIC_SOURCES[entity]
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        raw, _t, _sc = await _fetch_all(
            client, f"https://{host}{path}", headers, style="offset",
            params={"limit": "1000", "calculate_total": "true"}, item_key=key)
    return next((x for x in raw if match(x)), None)


async def _classic_resolve_site_id(host: str, token: str, name: Any) -> str:
    if not name:
        return ""
    if str(name).isdigit():
        return str(name)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            rows, _t, _sc = await _fetch_all(
                client, f"https://{host}/central/v2/sites", headers, style="offset",
                params={"limit": "1000"}, item_key="sites")
        for s in rows:
            if _pick(s, "site_name", "name") == name:
                return str(_pick(s, "site_id", "id", default=""))
    except Exception:
        pass
    return ""


async def _classic_central_client_detail(host: str, token: str, mac: str) -> Optional[dict[str, Any]]:
    raw, wired = None, False
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        for path, is_wired in CLASSIC_CLIENT_SOURCES:
            rows, _t, _sc = await _fetch_all(
                client, f"https://{host}{path}", headers, style="offset",
                params={"limit": "1000"}, item_key="clients")
            hit = next((c for c in rows if str(_pick(c, "macaddr", "mac_address", default="")).lower() == mac.lower()), None)
            if hit:
                raw, wired = hit, is_wired
                break
    if not raw:
        return None
    groups = [
        _kv_group("Identity", raw, [
            ("name", "Name"), ("hostname", "Hostname"), ("macaddr", "MAC address"),
            ("os_type", "Operating system"), ("client_category", "Category"), ("manufacturer", "Vendor"),
        ]),
        _kv_group("Connection", raw, [
            ("connection", "Connection type"), ("associated_device_name", "Connected to"),
            ("site", "Site"), ("group_name", "Group"), ("vlan", "VLAN"),
            ("network", "SSID"), ("authentication_type", "Authentication"),
        ]),
        _kv_group("Network", raw, [("ip_address", "IPv4"), ("username", "User"), ("speed", "Speed")]),
    ]
    if not wired:
        groups.append(_kv_group("Wireless", raw, [
            ("band", "Band"), ("channel", "Channel"), ("snr", "SNR"), ("rssi", "RSSI"),
            ("radio_number", "Radio"), ("health", "Health"),
        ]))
    return {
        "title": _pick(raw, "name", "hostname", "macaddr", default="Client"),
        "subtitle": _pick(raw, "macaddr", default=""),
        "status": "Connected",
        "groups": [g for g in groups if g],
        "meta": {
            "kind": "client",
            "siteId": await _classic_resolve_site_id(host, token, _pick(raw, "site_id", "site")),
            "focusSerial": _pick(raw, "associated_device", "associated_device_mac"),
            "clientName": _pick(raw, "name", "hostname", "macaddr", default="Client"),
            "clientMac": _pick(raw, "macaddr", default=""),
            "clientIp": _pick(raw, "ip_address"),
            "connType": "Wired" if wired else "Wireless",
            "band": None if wired else _classic_band(_pick(raw, "band")),
            "snr": _pick(raw, "snr"),
            "channel": _pick(raw, "channel"),
        },
    }


async def _classic_central_device_detail(host: str, token: str, serial: str) -> Optional[dict[str, Any]]:
    raw = None
    for entity in ("access-points", "switches", "gateways"):
        raw = await _classic_find(
            host, token, entity,
            lambda d: str(_pick(d, "serial", "serial_number", default="")) == serial)
        if raw:
            break
    if not raw:
        return None
    up = _classic_up(_pick(raw, "status", "state", default=""))
    groups = [
        _kv_group("Identity", raw, [
            ("name", "Name"), ("serial", "Serial"), ("macaddr", "MAC address"),
            ("model", "Model"), ("device_type", "Type"),
        ]),
        _kv_group("Status", raw, [
            ("status", "Status"), ("uptime", "Uptime"), ("client_count", "Connected clients"),
            ("firmware_version", "Firmware"), ("cpu_utilization", "CPU %"), ("mem_total", "Memory"),
        ]),
        _kv_group("Location", raw, [
            ("site", "Site"), ("group_name", "Group"), ("ip_address", "IPv4"),
            ("public_ip_address", "Public IP"), ("labels", "Labels"),
        ]),
    ]
    return {
        "title": _pick(raw, "name", "serial", default="Device"),
        "subtitle": (_pick(raw, "model", default="") + " · " + _pick(raw, "serial", default="")).strip(" ·"),
        "status": "Up" if up else "Down",
        "groups": [g for g in groups if g],
        "meta": {"kind": "device",
                 "siteId": await _classic_resolve_site_id(host, token, _pick(raw, "site_id", "site")),
                 "focusSerial": _pick(raw, "serial", "serial_number")},
    }


async def _classic_central_site_detail(host: str, token: str, site_id: str) -> Optional[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        raw, _t, _sc = await _fetch_all(
            client, f"https://{host}/central/v2/sites", headers, style="offset",
            params={"limit": "1000"}, item_key="sites")
        health = await _classic_branch_health(client, host, headers)
    match = next((x for x in raw if str(_pick(x, "site_id", "id", default="")) == str(site_id)
                  or _pick(x, "site_name", "name") == site_id), None)
    if not match:
        return None
    row = _classic_norm_site(match, health)
    src = {
        "clients": row["clients"], "devices": row["devices"], "alerts": row["alerts"],
        "cGood": row["clientHealth"]["good"], "cPoor": row["clientHealth"]["poor"],
        "dGood": row["deviceHealth"]["good"], "dPoor": row["deviceHealth"]["poor"],
        "address": _pick(match, "address"), "city": _pick(match, "city"),
        "state": _pick(match, "state"), "zip": _pick(match, "zipcode", "zip"),
        "country": _pick(match, "country"), "lat": _pick(match, "latitude"),
        "long": _pick(match, "longitude"), "id": row["id"],
    }
    groups = [
        _kv_group("Overview", src, [
            ("clients", "Clients"), ("devices", "Devices"), ("alerts", "Open alerts"), ("id", "Site ID")]),
        _kv_group("Client health", src, [("cGood", "Connected"), ("cPoor", "Failed")]),
        _kv_group("Device health", src, [("dGood", "Up"), ("dPoor", "Down")]),
        _kv_group("Address", src, [
            ("address", "Street"), ("city", "City"), ("state", "State"),
            ("zip", "Postcode"), ("country", "Country")]),
        _kv_group("Location", src, [("lat", "Latitude"), ("long", "Longitude")]),
    ]
    return {
        "title": row["name"],
        "subtitle": ", ".join(p for p in (src["city"], src["country"]) if p),
        "status": "",
        "groups": [g for g in groups if g],
        "meta": {"kind": "site", "siteId": row["id"] or row["name"]},
    }


async def _classic_ssid_map(host: str, token: str) -> dict[str, dict[str, Any]]:
    """All SSIDs across every AP group (config) merged with monitoring stats."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    out: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=45.0) as client:
        names, _sc = await _classic_group_names(client, host, headers)
        sem = asyncio.Semaphore(5)   # config API rate-limits hard under a burst

        async def _grp(g: str) -> tuple[str, list[tuple[str, str, str]], dict[str, dict[str, Any]]]:
          async with sem:
            clis, _csc, _cm = await _ap_cli_get(client, host, headers, g, tries=5, sweep=True, use_cache=True)
            cfg = _cli_ssid_cfg(clis)
            for path in (f"/configuration/v1/wlan/{quote(g, safe='')}",
                         f"/configuration/v2/wlan/{quote(g, safe='')}"):
                r = await _retry_get(client, f"https://{host}{path}", headers)
                if r is None or r.status_code != 200:
                    continue
                body = r.json() if r.content else {}
                wl = body.get("wlans") or body.get("data") or body.get("ssids") or []
                res: list[tuple[str, str, str]] = []
                for w in wl:
                    if isinstance(w, str):
                        res.append((w, "", ""))
                    elif isinstance(w, dict):
                        nm = _pick(w, "name", "essid", "ssid", "profile-name")
                        if nm:
                            res.append((nm,
                                        _pick(w, "opmode", "security", "key_management", default=""),
                                        _pick(w, "type", "access_type", default="")))
                return g, res, cfg
            return g, [], cfg

        async with _AP_CLI_SWEEP_LOCK:
            swept = await asyncio.gather(*[_grp(n) for n in (names or [])])
        for g, res, cfg in swept:
            for nm, sec, typ in res:
                e = out.setdefault(nm, {"groups": set(), "security": "", "type": "", "mon": {}})
                e["groups"].add(g)
                if sec and not e["security"]:
                    e["security"] = sec
                if typ and not e["type"]:
                    e["type"] = typ
            for nm, c in cfg.items():
                e = out.setdefault(nm, {"groups": set(), "security": "", "type": "", "mon": {}})
                if c.get("bands"):
                    e.setdefault("cfgBands", set()).update(c["bands"])
                if c.get("vlan") and not e.get("cfgVlan"):
                    e["cfgVlan"] = c["vlan"]

        for path in ("/monitoring/v2/networks", "/monitoring/v1/networks"):
            raw, _t, sc = await _fetch_all(
                client, f"https://{host}{path}", headers, style="offset",
                params={"limit": "1000"}, item_key="networks")
            if sc == 200 or raw:
                for n in raw:
                    nm = _pick(n, "essid", "name", "network", "ssid")
                    if nm:
                        out.setdefault(nm, {"groups": set(), "security": "", "type": "", "mon": {}})
                        out[nm]["mon"] = n
                break

        # live client tallies + bands per SSID from the wireless clients list
        craw, _t2, _sc2 = await _fetch_all(
            client, f"https://{host}/monitoring/v1/clients/wireless", headers,
            style="offset", params={"limit": "1000"}, item_key="clients")
        for c in craw:
            nm = _pick(c, "network", "ssid", "essid")
            if not nm:
                continue
            e = out.setdefault(nm, {"groups": set(), "security": "", "type": "", "mon": {}})
            e["clientCount"] = e.get("clientCount", 0) + 1
            b = _classic_band(_pick(c, "band"))
            if b:
                e.setdefault("bands", set()).add(b)
            v = _pick(c, "vlan", "vlan_id")
            if v:
                e.setdefault("vlans", set()).add(str(v))
    return out


_BAND_ORDER = {"2.4 GHz": 0, "5 GHz": 1, "6 GHz": 2}


def _ssid_row(name: str, e: dict[str, Any]) -> dict[str, Any]:
    m = e.get("mon") or {}
    bands = e.get("bands") or e.get("cfgBands")
    vlans = e.get("vlans")
    live = e.get("clientCount")
    mon_ct = _pick(m, "client_count", "num_clients", "clients", "associated_client_count")
    return {
        "name": name,
        "status": "Enabled" if _pick(m, "enabled", "is_enabled", default=True) else "Disabled",
        "security": e.get("security") or _pick(m, "security", "security_type", default="—"),
        "securityLevel": e.get("type") or _pick(m, "type", "wlan_type", default="—"),
        "band": ", ".join(sorted(bands, key=lambda b: _BAND_ORDER.get(b, 9))) if bands
                else _pick(m, "band", default="—"),
        "vlan": ", ".join(sorted(vlans)) if vlans
                else str(_pick(m, "vlan", "vlan_id", default=None) or e.get("cfgVlan") or "—"),
        "clients": live if live is not None else mon_ct,
        "groups": ", ".join(sorted(e.get("groups", []))) or "—",
    }


async def _classic_central_ssid_detail(host: str, token: str, name: str) -> Optional[dict[str, Any]]:
    smap = await _classic_ssid_map(host, token)
    e = smap.get(name)
    if e is None:
        return None
    row = _ssid_row(name, e)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    members: list[dict[str, str]] = []
    wcfg: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        craw, _t, _sc = await _fetch_all(
            client, f"https://{host}/monitoring/v1/clients/wireless", headers, style="offset",
            params={"limit": "1000"}, item_key="clients")
        grp = sorted(e.get("groups", []))
        if grp:
            r = await _retry_get(
                client, f"https://{host}/configuration/v1/wlan/{quote(grp[0], safe='')}/{quote(name, safe='')}",
                headers)
            if r is not None and r.status_code == 200:
                wcfg = (r.json() or {}).get("wlan") or {}
    for c in craw:
        if _pick(c, "network", "ssid", "essid") == name:
            members.append({
                "name": _pick(c, "name", "hostname", "macaddr", default="?"),
                "serial": _pick(c, "macaddr", default=""),
                "category": "client", "kind": "client", "status": "Connected",
            })
    cfg = {
        "essid": _pick(wcfg, "essid") or name,
        "type": _pick(wcfg, "type") or row["securityLevel"],
        "vlan": str(_pick(wcfg, "vlan") or row["vlan"]),
        "hidden": "Yes" if _pick(wcfg, "hide_ssid") else "No" if wcfg else "—",
        "passphrase": "Set" if _pick(wcfg, "wpa_passphrase") else "—",
        "captive": _pick(wcfg, "captive_profile_name"),
        "zone": _pick(wcfg, "zone"),
    }
    groups = [
        _kv_group("Configuration", {**row, **cfg}, [
            ("status", "Status"), ("type", "Type"), ("security", "Security"),
            ("band", "Bands"), ("vlan", "VLAN"), ("hidden", "Hidden SSID"),
            ("passphrase", "Passphrase"), ("captive", "Captive portal"), ("zone", "Zone"),
            ("groups", "AP groups"),
        ]),
        _kv_group("Clients", {"c": row["clients"], "n": len(members)}, [
            ("c", "Reported client count"), ("n", "Connected now"),
        ]),
    ]
    return {
        "title": name,
        "subtitle": row["security"] if row["security"] != "—" else "SSID",
        "status": row["status"],
        "groups": [g for g in groups if g],
        "devices": members,
        "meta": {"kind": "ssid"},
    }


def _humanize(k: str) -> str:
    words: list[str] = []
    for part in re.split(r"[_\-\s]+", str(k).strip()):
        for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", part) or [part]:
            words.append(w)
    if not words:
        return str(k)
    return " ".join(w if (w.isupper() and len(w) <= 3) else w.capitalize() for w in words)


async def _classic_central_group_detail(host: str, token: str, name: str) -> Optional[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    props: dict[str, Any] = {}
    members: list[dict[str, str]] = []
    counts = {"aps": 0, "switches": 0, "gateways": 0}
    cat_kind = {"aps": "ap", "switches": "switch", "gateways": "gateway"}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        for ppath in (f"/configuration/v2/groups/{quote(name, safe='')}/properties",
                      f"/configuration/v1/groups/{quote(name, safe='')}/properties"):
            try:
                r = await client.get(f"https://{host}{ppath}", headers=headers)
            except Exception:
                continue
            if r.status_code != 200:
                continue
            body = r.json() if r.content else {}
            data = body.get("data") or []
            if data and isinstance(data[0], dict):
                props = data[0].get("properties") or {k: v for k, v in data[0].items() if k != "group"}
            elif isinstance(body.get("properties"), dict):
                props = body["properties"]
            break
        for cat, path, key in (("aps", "/monitoring/v2/aps", "aps"),
                               ("switches", "/monitoring/v1/switches", "switches"),
                               ("gateways", "/monitoring/v1/gateways", "gateways")):
            raw, _t, _sc = await _fetch_all(
                client, f"https://{host}{path}", headers, style="offset",
                params={"limit": "1000"}, item_key=key)
            for d in raw:
                if _pick(d, "group_name", "group") == name:
                    counts[cat] += 1
                    members.append({
                        "name": _pick(d, "name", "serial", default="?"),
                        "serial": _pick(d, "serial", "serial_number", default=""),
                        "category": cat_kind[cat],
                        "status": "Up" if _classic_up(_pick(d, "status", "state", default="")) else "Down",
                    })

    def _prop(v: Any) -> Any:
        return ", ".join(map(str, v)) if isinstance(v, (list, tuple)) else v

    prop_src = {k: _prop(v) for k, v in props.items() if v not in (None, "", [], {})}
    groups = [
        _kv_group("Devices", {"aps": counts["aps"], "switches": counts["switches"],
                              "gateways": counts["gateways"]},
                  [("aps", "Access points"), ("switches", "Switches"), ("gateways", "Gateways")]),
        _kv_group("Group properties", prop_src, [(k, _humanize(k)) for k in prop_src]),
    ]
    return {
        "title": name,
        "subtitle": "Configuration group",
        "status": "",
        "groups": [g for g in groups if g],
        "devices": members,
        "meta": {"kind": "group"},
    }


CLASSIC_ROLE_TYPE = {
    "IAP": "ap", "AP": "ap", "ACCESS POINT": "ap",
    "SWITCH": "l2switch", "STACK": "l2switch",
    "CONTROLLER": "gateway", "VPNC": "gateway", "MOBILITY_CONTROLLER": "gateway",
    "GATEWAY": "gateway", "MCR": "gateway", "SDWAN_GW": "gateway",
    "SECURITYCLOUD": "internet",
}


async def _classic_central_topology(host: str, token: str, site_id: str) -> Optional[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    site_id = await _classic_resolve_site_id(host, token, site_id) or site_id
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(f"https://{host}/topology_external_api/{site_id}", headers=headers)
        if r.status_code != 200:
            return None
        body = r.json()
    except Exception:
        return None
    root = body.get("result") or body.get("topology") or body
    raw_nodes = root.get("devices") or root.get("nodes") or []
    raw_links = root.get("edges") or root.get("links") or []
    tunnels = root.get("tunnels") or []
    roots = [str(x) for x in (root.get("rootNodes") or [])]

    nodes: dict[str, dict[str, Any]] = {}
    for d in raw_nodes:
        s = _pick(d, "serial", "serialNumber", "id", "name")
        if not s:
            continue
        role = str(_pick(d, "role", "deviceRole", "device_type", "type", default="")).upper()
        st = _pick(d, "status", "state", default="")
        nodes[s] = {
            "serial": s,
            "name": _pick(d, "name", "hostname", default=s),
            "type": CLASSIC_ROLE_TYPE.get(role, "other"),
            "model": _pick(d, "model", default=""),
            "function": _pick(d, "role", "deviceRole", default=""),
            "ip": _pick(d, "ipAddress", "ip_address", "ipv4", default=""),
            "mac": _pick(d, "macaddr", "mac", default=""),
            "status": "ONLINE" if str(st) in ("1", "up", "Up", "UP", "online") else "OFFLINE",
            "health": _pick(d, "health", default=""),
            "unmanaged": bool(_pick(d, "unmanaged", default=False)),
        }

    # drop generic placeholder nodes the topology API injects (internet marker,
    # station stand-ins) — the diagram synthesises its own Internet + client nodes
    _TOPO_DROP = {"inet", "internet", "wan", "cloud", "sta", "wifi-sta", "wifi sta",
                  "wifi_sta", "wired-sta", "wireless-sta", "wifi-client", "wired-client"}
    dropped = {s for s, n in nodes.items()
               if str(n.get("name", "")).strip().lower() in _TOPO_DROP}
    for s in dropped:
        nodes.pop(s, None)

    def _add_edge(lk: dict[str, Any], edge_type_default: str = "") -> None:
        fi = lk.get("fromIf") or lk.get("from_if") or {}
        ti = lk.get("toIf") or lk.get("to_if") or {}
        f = _pick(fi, "serial") or _pick(lk, "source", "from", "sourceSerial")
        t = _pick(ti, "serial") or _pick(lk, "target", "to", "destSerial")
        if not f or not t or f in dropped or t in dropped:
            return
        for endp, iff in ((f, fi), (t, ti)):
            if endp not in nodes:
                nm = _pick(iff, "deviceName", default=str(endp))
                if str(nm).strip().lower() in _TOPO_DROP:
                    dropped.add(endp)
                    return
                nodes[endp] = {
                    "serial": endp, "name": nm,
                    "type": "other", "model": "", "function": "",
                    "ip": _pick(iff, "ipAddress", default=""), "mac": "",
                    "status": "", "health": "", "unmanaged": True,
                }
        links.append({
            "from": f, "to": t,
            "speed": _fmt_speed(_pick(lk, "speed", "linkSpeed", default=0)),
            "edgeType": _pick(lk, "edge_type", "type", "edgeType", default=edge_type_default),
            "fromPort": str(_pick(fi, "name", "portNumber", default="")
                            or _pick(lk, "sourceIfName", "fromPort", default="")),
            "toPort": str(_pick(ti, "name", "portNumber", default="")
                          or _pick(lk, "destIfName", "toPort", default="")),
            "health": _pick(lk, "health", "status", default=""),
        })

    links: list[dict[str, Any]] = []
    for lk in raw_links:
        _add_edge(lk)
    for tn in tunnels:
        _add_edge(tn, "TUNNEL")

    if not nodes:
        return None
    return {"nodes": list(nodes.values()), "links": links, "isolated": 0, "roots": roots}


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    with _lock:
        n = len(_sessions)
    return {"ok": True, "sessions": n}


@app.get("/api/session")
async def api_session(request: Request) -> JSONResponse:
    return JSONResponse(_state(_get(request)))


@app.post("/api/connect/classic")
async def connect_classic(request: Request) -> JSONResponse:
    body = await request.json()
    host = _clean_host(body.get("baseUrl", ""))
    client_id = (body.get("clientId") or "").strip()
    client_secret = (body.get("clientSecret") or "").strip()
    refresh_token = (body.get("refreshToken") or "").strip()

    if not (host and client_id and client_secret and refresh_token):
        return _err(400, "Base URL, Client ID, Client Secret and Refresh Token are all required.")

    try:
        tok = await _token_request(
            f"https://{host}/oauth2/token",
            params={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
    except _TokenError as exc:
        return _err(502, str(exc))
    except httpx.HTTPError:
        return _err(504, f"Could not reach {host}. Check the selected Base URL.")

    devices = await _classic_central_devices(host, tok["access_token"])

    sid, sess = _ensure(request)
    sess["classic"] = {
        "url": f"https://{host}",
        "host": host,
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", refresh_token),
        "expires_at": _now() + int(tok.get("expires_in", 7200)),
        "webhooks": True,
        "webhookKey": "whk_" + secrets.token_urlsafe(12),
        "devices": devices,
    }
    return _attach_cookie(JSONResponse(_state(sess)), sid)


@app.post("/api/connect/new")
async def connect_new(request: Request) -> JSONResponse:
    body = await request.json()
    host = _clean_host(body.get("cluster", ""))
    client_id = (body.get("clientId") or "").strip()
    client_secret = (body.get("clientSecret") or "").strip()

    if not (host and client_id and client_secret):
        return _err(400, "Cluster, Client ID and Client Secret are all required.")

    try:
        tok = await _token_request(
            NEW_CENTRAL_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
    except _TokenError as exc:
        return _err(502, str(exc))
    except httpx.HTTPError:
        return _err(504, "Could not reach the HPE GreenLake token endpoint.")

    devices = await _new_central_devices(host, tok["access_token"])

    sid, sess = _ensure(request)
    sess["new"] = {
        "url": f"https://{host}",
        "host": host,
        "access_token": tok["access_token"],
        "expires_at": _now() + int(tok.get("expires_in", 7200)),
        "webhooks": True,
        "webhookKey": "whk_" + secrets.token_urlsafe(12),
        "devices": devices,
    }
    return _attach_cookie(JSONResponse(_state(sess)), sid)


_FLAVORS = ("new", "classic")
_FLAVOR_LABEL = {"new": "New Central", "classic": "Classic Central"}
_DASH = {
    "new": {
        "overview": _new_central_overview, "list": _new_central_list,
        "client": _new_central_client_detail, "device": _new_central_device_detail,
        "site": _new_central_site_detail, "topology": _new_central_topology,
        "ssid": _new_central_ssid_detail, "group": _new_central_group_detail,
        "rf": _new_central_rf_detail,
    },
    "classic": {
        "overview": _classic_central_overview, "list": _classic_central_list,
        "client": _classic_central_client_detail, "device": _classic_central_device_detail,
        "site": _classic_central_site_detail, "topology": _classic_central_topology,
        "group": _classic_central_group_detail, "ssid": _classic_central_ssid_detail,
    },
}


def _dash_conn(request: Request, flavor: str) -> tuple[Optional[dict[str, Any]], Optional[JSONResponse]]:
    if flavor not in _FLAVORS:
        return None, _err(404, "Unknown environment.")
    sess = _get(request)
    conn = sess.get(flavor) if sess else None
    if not conn:
        return None, _err(409, f"Connect {_FLAVOR_LABEL[flavor]} first.")
    if conn.get("expires_at", 0) < _now():
        return None, _err(401, f"The access token has expired — reconnect {_FLAVOR_LABEL[flavor]}.")
    return conn, None


@app.get("/api/overview/{flavor}")
async def overview(flavor: str, request: Request) -> JSONResponse:
    conn, err = _dash_conn(request, flavor)
    if err:
        return err
    data = await _DASH[flavor]["overview"](conn["host"], conn["access_token"])
    return JSONResponse({"overview": data})


# metric groups that the dashboard loads independently (progressive fill)
OVERVIEW_GROUPS = {
    "clients": ["clients"],
    "devices": ["accessPoints", "switches", "gateways"],
    "sites": ["sites"],
    "subscriptions": ["subscriptions"],
    "ssids": ["ssids"],
    "apGroups": ["apGroups"],
    "rfProfiles": ["rfProfiles"],
}


async def _overview_part(flavor: str, group: str, host: str, token: str) -> dict[str, Optional[int]]:
    hdr = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=45.0) as cx:
        if flavor == "new":
            if group == "clients":
                return {"clients": await _get_total(cx, f"https://{host}/network-monitoring/v1/clients", hdr, {"limit": "1"})}
            if group == "devices":
                return await _new_central_device_totals(cx, host, hdr) or {
                    "accessPoints": None, "switches": None, "gateways": None}
            if group == "sites":
                return {"sites": await _get_total(cx, f"https://{host}/network-monitoring/v1/sites-health", hdr, {"limit": "1"})}
            if group == "subscriptions":
                return {"subscriptions": await _get_total(
                    cx, "https://global.api.greenlake.hpe.com/subscriptions/v1/subscriptions", hdr, {"limit": "1"})}
            if group == "ssids":
                return {"ssids": await _get_total(cx, f"https://{host}/network-monitoring/v1/wlans", hdr, {"limit": "1"})}
            if group == "apGroups":
                g = await _new_central_ap_groups(host, token)
                return {"apGroups": len(g) if g is not None else None}
            if group == "rfProfiles":
                rf = await _new_central_rf_list(host, token)
                return {"rfProfiles": len(rf) if rf is not None else None}
        else:  # classic
            if group == "clients":
                tot = 0
                got = False
                for path, _w in CLASSIC_CLIENT_SOURCES:
                    rows, _t, sc = await _fetch_all(cx, f"https://{host}{path}", hdr,
                                                   style="offset", params={"limit": "1000"}, item_key="clients")
                    if sc == 200 or rows:
                        got = True
                        tot += len(rows)
                return {"clients": tot if got else None}
            if group == "devices":
                out: dict[str, Optional[int]] = {}
                for key, path in (("accessPoints", "/monitoring/v2/aps"),
                                  ("switches", "/monitoring/v1/switches"),
                                  ("gateways", "/monitoring/v1/gateways")):
                    out[key] = await _get_total(cx, f"https://{host}{path}", hdr,
                                                {"limit": "1", "calculate_total": "true"})
                return out
            if group == "sites":
                return {"sites": await _get_total(cx, f"https://{host}/central/v2/sites", hdr,
                                                  {"limit": "1", "calculate_total": "true"})}
            if group == "subscriptions":
                return {"subscriptions": await _get_total(
                    cx, f"https://{host}/platform/licensing/v1/subscriptions", hdr, {"limit": "1"})}
            if group == "apGroups":
                names, _sc = await _classic_group_names(cx, host, hdr)
                return {"apGroups": len(names) if names is not None else None}
    if flavor == "classic" and group == "ssids":
        smap = await _classic_ssid_map(host, token)
        return {"ssids": len(smap) if smap else None}
    if flavor == "classic" and group == "rfProfiles":
        rp = await _classic_rf_profiles(host, token)
        return {"rfProfiles": len(rp) if rp is not None else None}
    return {}


@app.get("/api/overview/{flavor}/{group}")
async def overview_group(flavor: str, group: str, request: Request) -> JSONResponse:
    if group not in OVERVIEW_GROUPS:
        return _err(404, "Unknown metric group.")
    conn, err = _dash_conn(request, flavor)
    if err:
        return err
    try:
        values = await _overview_part(flavor, group, conn["host"], conn["access_token"])
    except Exception:
        values = {k: None for k in OVERVIEW_GROUPS[group]}
    return JSONResponse({"values": values})


@app.get("/api/list/{flavor}/{entity}")
async def list_entity(flavor: str, entity: str, request: Request) -> JSONResponse:
    if entity not in {"clients", "access-points", "switches", "gateways", "sites",
                      "subscriptions", "ap-groups", "ssids", "rf-profiles"}:
        return _err(404, "Unknown entity.")
    conn, err = _dash_conn(request, flavor)
    if err:
        return err
    rows, _ = await _DASH[flavor]["list"](conn["host"], conn["access_token"], entity)
    if rows is None:
        return _err(502, f"Central did not return {entity.replace('-', ' ')} for this API client.")
    return JSONResponse({"rows": rows, "total": len(rows)})


@app.get("/api/detail/{flavor}/{kind}/{ident}")
async def detail(flavor: str, kind: str, ident: str, request: Request) -> JSONResponse:
    if kind not in ("client", "device", "site", "group", "ssid", "rf"):
        return _err(404, "Unknown detail type.")
    conn, err = _dash_conn(request, flavor)
    if err:
        return err
    fn = _DASH[flavor].get(kind)
    if fn is None:
        return _err(404, f"{_FLAVOR_LABEL[flavor]} has no {kind} details.")
    data = await fn(conn["host"], conn["access_token"], ident)
    if data is None:
        return _err(404, f"No {kind} found for '{ident}'.")
    return JSONResponse({"detail": data})


@app.get("/api/topology/{flavor}/{site_id}")
async def topology(flavor: str, site_id: str, request: Request) -> JSONResponse:
    conn, err = _dash_conn(request, flavor)
    if err:
        return err
    data = await _DASH[flavor]["topology"](conn["host"], conn["access_token"], site_id)
    if data is None:
        return _err(502, "Central did not return topology for this site.")
    return JSONResponse(data)


# --------------------------------------------------------------------------- #
# Configuration writes (Classic Central only)
# --------------------------------------------------------------------------- #
@app.get("/api/config/{flavor}/groups")
async def config_groups(flavor: str, request: Request) -> JSONResponse:
    conn, err = _dash_conn(request, flavor)
    if err:
        return err
    if flavor != "classic":
        return JSONResponse({"groups": []})
    hdr = {"Authorization": f"Bearer {conn['access_token']}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=45.0) as cx:
        names, _sc = await _classic_group_names(cx, conn["host"], hdr)
    return JSONResponse({"groups": sorted(names or [], key=str.lower)})


_GROUP_DEV_TYPES = {"AccessPoints", "Gateways", "Switches"}
_GROUP_SW_TYPES = {"AOS_S", "AOS_CX"}


@app.post("/api/config/{flavor}/group")
async def config_group_create(flavor: str, request: Request) -> JSONResponse:
    """Create a new AP/config group (Classic). Groups are always created with
    the AOS-8 / Instant architecture — Central's API does not allow choosing or
    changing it, so an AOS-10 group must be made in the Central UI."""
    conn, err = _dash_conn(request, flavor)
    if err:
        return err
    if flavor != "classic":
        return _err(400, "Group creation is only available for Classic Central.")
    b = await request.json()
    name = (b.get("name") or "").strip()
    password = (b.get("password") or "").strip()
    dev_types = [t for t in (b.get("devTypes") or []) if t in _GROUP_DEV_TYPES] or ["AccessPoints"]
    sw_types = [t for t in (b.get("swTypes") or []) if t in _GROUP_SW_TYPES]
    ap_role = b.get("apRole") if b.get("apRole") in ("Standard", "Microbranch") else "Standard"
    if not re.fullmatch(r"[A-Za-z0-9 _.\-]{1,32}", name):
        return _err(400, "Group name: letters, numbers, spaces, . _ - only (max 32).")
    if len(password) < 6:
        return _err(400, "Group password must be at least 6 characters.")

    hdr = {"Authorization": f"Bearer {conn['access_token']}",
           "Content-Type": "application/json", "Accept": "application/json"}
    base = f"https://{conn['host']}"
    async with httpx.AsyncClient(timeout=45.0) as cx:
        r = await cx.post(f"{base}/configuration/v1/groups", headers=hdr, json={
            "group": name,
            "group_attributes": {"template_group": False, "group_password": password},
        })
        if not (200 <= r.status_code < 300):
            return _err(502, f"Central rejected the group ({r.status_code}): {(r.text or '')[:300]}")
        props: dict[str, Any] = {"AllowedDevTypes": dev_types, "ApNetworkRole": ap_role}
        if "Switches" in dev_types and sw_types:
            props["AllowedSwitchTypes"] = sw_types
        pr = await cx.patch(
            f"{base}/configuration/v2/groups/{quote(name, safe='')}/properties",
            headers=hdr, json={"properties": props})
        props_ok = 200 <= pr.status_code < 300
    _ap_cli_cache_clear(conn["host"])
    return JSONResponse({
        "ok": True, "name": name, "architecture": "Instant (AOS-8)",
        "propertiesApplied": props_ok,
        "note": "" if props_ok else f"group created; properties not applied ({pr.status_code})",
    })


@app.delete("/api/config/{flavor}/group/{name}")
async def config_group_delete(flavor: str, name: str, request: Request) -> JSONResponse:
    conn, err = _dash_conn(request, flavor)
    if err:
        return err
    if flavor != "classic":
        return _err(400, "Group deletion is only available for Classic Central.")
    hdr = {"Authorization": f"Bearer {conn['access_token']}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=45.0) as cx:
        r = await cx.delete(
            f"https://{conn['host']}/configuration/v1/groups/{quote(name, safe='')}", headers=hdr)
    if not (200 <= r.status_code < 300):
        return _err(502, f"Central rejected the deletion ({r.status_code}): {(r.text or '')[:300]}")
    _ap_cli_cache_clear(conn["host"])
    return JSONResponse({"ok": True, "name": name})


# Short-lived cache so the SSID and RF-profile sweeps (both hit ap_cli for
# every group, and run concurrently during overview load) don't hammer the
# rate-limity config API and lose groups to 429s. Keyed by (host, group).
_AP_CLI_CACHE: dict[tuple[str, str], tuple[float, list[str]]] = {}
_AP_CLI_TTL = 120.0
# global ceiling on concurrent ap_cli calls — the SSID + RF sweeps can otherwise
# stack up and trip the config API's rate limiter
_AP_CLI_SEM = asyncio.Semaphore(4)
# serialize the all-groups ap_cli sweeps (SSID map, RF profiles) against each
# other so the second one runs entirely off the 120 s cache the first fills,
# instead of both bursting the rate-limity config API at once (which made the
# overview card and the list disagree by a group or two)
_AP_CLI_SWEEP_LOCK = asyncio.Lock()


def _ap_cli_cache_clear(host: str, group: Optional[str] = None) -> None:
    for k in [k for k in _AP_CLI_CACHE if k[0] == host and (group is None or k[1] == group)]:
        _AP_CLI_CACHE.pop(k, None)


async def _ap_cli_get(cx: httpx.AsyncClient, host: str, hdr: dict[str, str], group: str,
                      tries: int = 3, sweep: bool = False, use_cache: bool = False
                      ) -> tuple[Optional[list[str]], int, str]:
    """Read a group's full AP CLI config.

    Gateway/switch-only groups return a deterministic 500 here. Callers that
    sweep every group pass ``sweep=True`` so we still retry rate-limits /
    502-504 but not the 500, avoiding wasted retries on non-AP groups.
    ``use_cache`` serves a <=120s-old copy (used by the read-only sweeps, never
    by the read-modify-write config push).
    """
    if use_cache:
        hit = _AP_CLI_CACHE.get((host, group))
        if hit and (_now() - hit[0]) < _AP_CLI_TTL:
            return list(hit[1]), 200, ""
    retry_on = (429, 502, 503, 504) if sweep else (429, 500, 502, 503, 504)
    for path in (f"/configuration/v1/ap_cli/{quote(group, safe='')}",
                 f"/configuration/v2/ap_cli/{quote(group, safe='')}"):
        async with _AP_CLI_SEM:
            r = await _retry_get(cx, f"https://{host}{path}", hdr, tries=tries, retry_on=retry_on)
        if r is None:
            return None, 0, "request failed"
        if r.status_code == 404:
            continue
        if r.status_code != 200:
            return None, r.status_code, (r.text or "")[:300]
        body = r.json() if r.content else []
        if isinstance(body, list):
            clis = [str(x) for x in body]
        else:
            clis = [str(x) for x in (body.get("clis") or body.get("data") or [])]
        _AP_CLI_CACHE[(host, group)] = (_now(), list(clis))
        return clis, 200, ""
    return None, 404, "ap_cli endpoint not found"


def _cli_lines(text: str) -> list[str]:
    out = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        ln = raw.rstrip()
        if ln.strip():
            out.append(ln)
    return out


def _cli_blocks(lines: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = []
    cur: Optional[list[str]] = None
    for ln in lines:
        if ln and not ln[0].isspace():
            if cur:
                blocks.append(cur)
            cur = [ln]
        elif cur is not None:
            cur.append(ln)
    if cur:
        blocks.append(cur)
    return blocks


def _cli_replace_block(existing: list[str], header: str, new_block: list[str]) -> list[str]:
    out: list[str] = []
    i, done = 0, False
    while i < len(existing):
        ln = existing[i]
        if ln.strip() == header and not ln[:1].isspace():
            i += 1
            while i < len(existing) and existing[i][:1].isspace():
                i += 1
            if not done:
                out.extend(new_block)
                done = True
            continue
        out.append(ln)
        i += 1
    if not done:
        out.extend(new_block)
    return out


def _merge_cli(existing: list[str], submitted: list[str]) -> list[str]:
    merged = list(existing)
    for block in _cli_blocks(submitted):
        merged = _cli_replace_block(merged, block[0].strip(), block)
    return merged


def _cli_drop_block(existing: list[str], header: str) -> list[str]:
    """Remove the top-level block whose header matches (and its indented body)."""
    out: list[str] = []
    i = 0
    while i < len(existing):
        ln = existing[i]
        if ln.strip() == header.strip() and not ln[:1].isspace():
            i += 1
            while i < len(existing) and existing[i][:1].isspace():
                i += 1
            continue
        out.append(ln)
        i += 1
    return out


def _kw(line: str) -> str:
    """The leading keyword of a CLI sub-line (everything up to the first space)."""
    return line.strip().split(None, 1)[0] if line.strip() else ""


def _merge_cli_submerge(existing: list[str], submitted: list[str],
                        managed: list[str]) -> list[str]:
    """Merge each submitted block into the same-header existing block
    *line by line*: a submitted child replaces the existing child with the same
    leading keyword, unmanaged existing children are kept, and any ``managed``
    keyword absent from the submitted block is dropped (so unticking a field in
    the form removes it). Blocks with no existing match are appended whole.
    """
    managed_set = set(managed)
    out = list(existing)
    for block in _cli_blocks(submitted):
        header = block[0].strip()
        sub_children = block[1:]
        sub_kw = {_kw(c): c for c in sub_children}
        # locate the existing block
        start = next((i for i, ln in enumerate(out)
                      if ln.strip() == header and not ln[:1].isspace()), None)
        if start is None:
            out.extend([block[0]] + sub_children)
            continue
        end = start + 1
        while end < len(out) and out[end][:1].isspace():
            end += 1
        indent = "  "
        kept: list[str] = []
        seen: set[str] = set()
        for ln in out[start + 1:end]:
            k = _kw(ln)
            if k in sub_kw:
                kept.append(indent + sub_kw[k].strip())
                seen.add(k)
            elif k in managed_set:
                continue  # managed + not submitted -> drop
            else:
                kept.append(ln)
        for k, c in sub_kw.items():
            if k not in seen:
                kept.append(indent + c.strip())
        out = out[:start + 1] + kept + out[end:]
    return out


def _unquote(s: str) -> str:
    s = (s or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


_RF_BAND_TOKEN = {
    "2.4": "2.4 GHz", "2.4ghz": "2.4 GHz", "g": "2.4 GHz",
    "5": "5 GHz", "5.0": "5 GHz", "5ghz": "5 GHz", "a": "5 GHz",
    "6": "6 GHz", "6.0": "6 GHz", "6ghz": "6 GHz",
}


def _cli_ssid_cfg(clis: Optional[list[str]]) -> dict[str, dict[str, Any]]:
    """Parse `wlan ssid-profile` blocks -> {name/essid: {bands:set, vlan:str}}."""
    out: dict[str, dict[str, Any]] = {}
    for blk in _cli_blocks(_cli_lines("\n".join(clis or []))):
        head = blk[0].strip()
        if not head.startswith("wlan ssid-profile "):
            continue
        prof_name = _unquote(head[len("wlan ssid-profile "):])
        names: set[str] = set()
        bands: set[str] = set()
        vlan = ""
        for ln in blk[1:]:
            t = ln.strip()
            if t.startswith("essid "):
                names.add(_unquote(t[6:]))
            elif t.startswith(("rf-band ", "allowed-band ", "wifi-band ")):
                v = t.split(None, 1)[1].strip().lower()
                if v in ("all", "all-bands"):
                    bands |= {"2.4 GHz", "5 GHz", "6 GHz"}
                elif v in _RF_BAND_TOKEN:
                    bands.add(_RF_BAND_TOKEN[v])
            elif t.startswith("vlan ") and not vlan:
                vlan = _unquote(t[5:])
        if not names:
            names = {prof_name}
        for nm in names:
            if not nm:
                continue
            e = out.setdefault(nm, {"bands": set(), "vlan": ""})
            e["bands"] |= bands
            if vlan and not e["vlan"]:
                e["vlan"] = vlan
    return out


_RF_PROFILE_KIND = {
    "dot11a-radio-profile": "5 GHz",
    "dot11a-secondary-radio-profile": "5 GHz (secondary)",
    "dot11g-radio-profile": "2.4 GHz",
    "dot11-6ghz-radio-profile": "6 GHz",
    "dot11-6GHz-radio-profile": "6 GHz",
    "arm-profile": "ARM",
}
_RF_RADIO_ORDER = {"2.4 GHz": 0, "5 GHz": 1, "5 GHz (secondary)": 2, "6 GHz": 3, "ARM": 4}


_RF_FLAG_KEYS = ("spectrum-monitor", "smart-antenna", "channel-quality-aware",
                 "very-high-throughput-disable", "high-throughput-disable")
_RF_VAL_KEYS = ("max-tx-power", "min-tx-power", "max-distance",
                "free-channel-index", "disable-arm-wids-functions",
                "csa-count", "high-noise-backoff-time", "zone", "dot11h")


def _cli_rf_profiles(clis: Optional[list[str]]) -> dict[str, dict[str, Any]]:
    """`rf <kind>-radio-profile ["<name>"]` blocks.

    AOS-10 config groups usually carry one *unnamed* radio profile per band
    (the group default); some deployments define named ones. Returns
    ``{name_or_"": {"bands": {band_label: {setting: value}}}}`` — key ``""`` is
    the group default. Flag lines store ``True``; ``ch-bw-range a b`` and
    ``allowed-channels ...`` keep their raw argument.
    """
    out: dict[str, dict[str, Any]] = {}
    for blk in _cli_blocks(_cli_lines("\n".join(clis or []))):
        head = blk[0].strip()
        if not head.startswith("rf "):
            continue
        parts = head[3:].split(None, 1)
        label = _RF_PROFILE_KIND.get(parts[0])
        if not label:
            continue
        name = _unquote(parts[1]) if len(parts) == 2 else ""
        band = out.setdefault(name, {"bands": {}})["bands"].setdefault(label, {})
        for ln in blk[1:]:
            t = ln.strip()
            if not t:
                continue
            kw = t.split(None, 1)[0]
            arg = t.split(None, 1)[1] if " " in t else ""
            if kw in _RF_FLAG_KEYS and not arg:
                band[kw] = True
            elif kw in ("ch-bw-range", "allowed-channels") or kw in _RF_VAL_KEYS:
                band.setdefault(kw, arg or True)
    return out


async def _classic_rf_profiles(host: str, token: str) -> Optional[dict[str, dict[str, Any]]]:
    """RF profiles across every AP group.

    -> ``{display_name: {"bands": {label: {setting: value}}, "groups": set,
    "named": bool}}``. Unnamed group-default radio configs are keyed by their
    AP-group name (in the Classic config-group model the RF profile is per
    group).
    """
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    out: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=45.0) as client:
        names, _sc = await _classic_group_names(client, host, headers)
        if names is None:
            return None
        sem = asyncio.Semaphore(5)

        async def _grp(g: str) -> tuple[str, dict[str, dict[str, Any]]]:
            async with sem:
                clis, _sc2, _m = await _ap_cli_get(client, host, headers, g, tries=5, sweep=True, use_cache=True)
                return g, _cli_rf_profiles(clis)

        async with _AP_CLI_SWEEP_LOCK:
            swept = await asyncio.gather(*[_grp(n) for n in names])
        for g, prof in swept:
            for nm, info in prof.items():
                key = nm or g
                e = out.setdefault(key, {"bands": {}, "groups": set(), "named": bool(nm)})
                for label, settings in info["bands"].items():
                    dst = e["bands"].setdefault(label, {})
                    for k, v in settings.items():
                        dst.setdefault(k, v)
                e["groups"].add(g)
    return out


def _rf_power(s: dict[str, Any]) -> Optional[str]:
    lo, hi = s.get("min-tx-power"), s.get("max-tx-power")
    if lo and hi:
        return f"{lo}–{hi} dBm"
    if hi:
        return f"up to {hi} dBm"
    if lo:
        return f"from {lo} dBm"
    return None


def _rf_width(s: dict[str, Any], band: str) -> Optional[str]:
    raw = s.get("ch-bw-range")
    if isinstance(raw, str) and raw:
        w = [p.replace("MHz", " MHz").strip() for p in raw.split()]
        return w[0] if len(w) == 1 or w[0] == w[-1] else f"{w[0]} – {w[-1]}"
    if band == "2.4 GHz":
        return "20 MHz"
    return "Default"


def _rf_band_group(label: str, s: dict[str, Any]) -> Optional[dict[str, Any]]:
    ch = s.get("allowed-channels")
    src = {
        "power": _rf_power(s),
        "width": _rf_width(s, label),
        "channels": ch if isinstance(ch, str) and ch else "Regulatory default",
        "spectrum": "On" if s.get("spectrum-monitor") else None,
        "smart": "On" if s.get("smart-antenna") else None,
        "cqa": "On" if s.get("channel-quality-aware") else None,
        "armwids": ("Disabled" if str(s.get("disable-arm-wids-functions")).lower()
                    not in ("", "off", "none") else None),
        "maxdist": s.get("max-distance") if s.get("max-distance") not in (None, "0") else None,
        "csa": s.get("csa-count"),
        "noise": (f"{s['high-noise-backoff-time']} min" if s.get("high-noise-backoff-time") else None),
        "dot11h": "On" if s.get("dot11h") else None,
        "zone": s.get("zone") if isinstance(s.get("zone"), str) else None,
    }
    return _kv_group(f"{label} radio", src, [
        ("power", "Allowed transmit power"),
        ("width", "Channel width"),
        ("channels", "Allowed channels"),
        ("dot11h", "Advertise 802.11d/h"),
        ("spectrum", "Spectrum monitor"),
        ("smart", "Smart antenna"),
        ("cqa", "Channel quality aware"),
        ("armwids", "ARM/WIDS functions"),
        ("maxdist", "Max distance"),
        ("csa", "CSA count"),
        ("noise", "High-noise backoff"),
        ("zone", "AP zone"),
    ])


async def _classic_central_rf_detail(host: str, token: str, name: str) -> Optional[dict[str, Any]]:
    rp = await _classic_rf_profiles(host, token)
    e = (rp or {}).get(name)
    if e is None:
        return None
    order = _RF_RADIO_ORDER
    groups = [_rf_band_group(lbl, e["bands"][lbl])
              for lbl in sorted(e["bands"], key=lambda b: order.get(b, 9))]
    grp_list = ", ".join(sorted(e["groups"]))
    groups = [g for g in groups if g]
    groups.append({"label": "Applied to", "fields": [["AP groups", grp_list or "—"]]})
    groups.append({"label": "Note", "fields": [[
        "Channels & width",
        "Only values changed from the regulatory default are stored in Central's "
        "config API. Fields shown as “Regulatory default” use the default "
        "channel list / width for the AP’s country."]]})
    return {
        "title": name,
        "subtitle": "Named RF profile" if e["named"] else "Group-default RF profile",
        "status": "",
        "groups": groups,
        "devices": [],
        "meta": {"kind": "rf"},
    }


_DASH["classic"]["rf"] = _classic_central_rf_detail


SSID_TEMPLATE = (
    "wlan ssid-profile {name}\n"
    "  essid {name}\n"
    "  type employee\n"
    "  opmode wpa2-psk-aes\n"
    "  wpa-passphrase CHANGE_ME_1234\n"
    "  vlan 1\n"
    "  rf-band all\n"
    "  captive-portal disable\n"
    "  enable\n"
    "wlan access-rule {name}\n"
    "  rule any any match any any any permit\n"
)
RADIUS_TEMPLATE = (
    "wlan auth-server {name}\n"
    "  ip {ip}\n"
    "  key CHANGE_ME_SECRET\n"
    "  port 1812\n"
    "  acctport 1813\n"
    "  rfc3576\n"
    "  cppm-rfc3576-port 5999\n"
    "  rfc5997\n"
)


def _cli_block_names(lines: list[str], prefix: str) -> list[str]:
    p = prefix.strip() + " "
    out: list[str] = []
    for ln in lines:
        if ln[:1] and not ln[:1].isspace() and ln.strip().startswith(p):
            nm = ln.strip()[len(p):].strip()
            if nm and nm not in out:
                out.append(nm)
    return sorted(out, key=str.lower)


def _cli_extract_block(lines: list[str], header: str) -> list[str]:
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip() == header.strip() and not ln[:1].isspace():
            out = [ln]
            i += 1
            while i < len(lines) and lines[i][:1].isspace():
                out.append(lines[i])
                i += 1
            return out
        i += 1
    return []


@app.get("/api/config/{flavor}/cli/{group}")
async def config_cli_get(flavor: str, group: str, request: Request,
                         block: Optional[str] = None,
                         names: Optional[str] = None) -> JSONResponse:
    conn, err = _dash_conn(request, flavor)
    if err:
        return err
    if flavor != "classic":
        return _err(400, "CLI configuration is only available for Classic Central.")
    hdr = {"Authorization": f"Bearer {conn['access_token']}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=45.0) as cx:
        clis, sc, msg = await _ap_cli_get(cx, conn["host"], hdr, group)
    if clis is None:
        return _err(502, f"Could not read CLI for {group} ({sc}). {msg}")
    if names:
        return JSONResponse({"group": group, "names": _cli_block_names(clis, names)})
    if block:
        picked: list[str] = []
        for h in [x for x in block.split("||") if x.strip()]:
            picked += _cli_extract_block(clis, h)
        return JSONResponse({"group": group, "cli": "\n".join(picked), "found": bool(picked)})
    return JSONResponse({"group": group, "cli": "\n".join(clis)})


@app.post("/api/config/{flavor}/cli")
async def config_cli_push(flavor: str, request: Request) -> JSONResponse:
    conn, err = _dash_conn(request, flavor)
    if err:
        return err
    if flavor != "classic":
        return _err(400, "CLI configuration is only available for Classic Central.")
    b = await request.json()
    submitted = _cli_lines(b.get("cli") or "")
    groups = [g for g in (b.get("groups") or []) if g]
    preview = bool(b.get("preview"))
    submerge = bool(b.get("submerge"))
    managed = [str(k) for k in (b.get("managed") or [])]
    remove = [str(h).strip() for h in (b.get("remove") or []) if str(h).strip()]
    if not groups:
        return _err(400, "Select at least one AP group.")
    if not submitted and not remove:
        return _err(400, "The configuration is empty.")
    if submitted and not _cli_blocks(submitted):
        return _err(400, "The configuration must start with a top-level command (no leading spaces).")

    hdr_json = {"Authorization": f"Bearer {conn['access_token']}",
                "Content-Type": "application/json", "Accept": "application/json"}
    results = []
    async with httpx.AsyncClient(timeout=60.0) as cx:
        for g in groups:
            cur, sc, msg = await _ap_cli_get(cx, conn["host"], hdr_json, g)
            if cur is None:
                results.append({"group": g, "ok": False, "status": sc,
                                "error": f"read failed: {msg}"})
                continue
            merged = list(cur)
            for header in remove:
                merged = _cli_drop_block(merged, header)
            if submitted:
                merged = (_merge_cli_submerge(merged, submitted, managed) if submerge
                          else _merge_cli(merged, submitted))
            if preview:
                results.append({"group": g, "ok": True, "status": 200,
                                "preview": "\n".join(merged), "added": len(merged) - len(cur)})
                continue
            try:
                r = await cx.post(
                    f"https://{conn['host']}/configuration/v1/ap_cli/{quote(g, safe='')}",
                    headers=hdr_json, json={"clis": merged})
                ok = 200 <= r.status_code < 300
                if ok:
                    _ap_cli_cache_clear(conn["host"], g)
                results.append({"group": g, "ok": ok, "status": r.status_code,
                                "error": "" if ok else (r.text or "")[:300]})
            except Exception as exc:
                results.append({"group": g, "ok": False, "status": 0, "error": str(exc)[:200]})
    return JSONResponse({"results": results, "preview": preview})


@app.post("/api/refresh/{kind}")
async def refresh(kind: str, request: Request) -> JSONResponse:
    if kind not in ("classic", "new"):
        return _err(404, "Unknown environment.")
    sess = _get(request)
    conn = sess.get(kind) if sess else None
    if not conn:
        return _err(409, "Connect the environment first.")
    if conn.get("expires_at", 0) < _now():
        return _err(401, "The access token has expired — reconnect to refresh device status.")
    fn = _new_central_devices if kind == "new" else _classic_central_devices
    devices = await fn(conn["host"], conn["access_token"])
    if devices is not None:
        conn["devices"] = devices
    return JSONResponse(_state(sess))


@app.post("/api/disconnect/{kind}")
async def disconnect(kind: str, request: Request) -> JSONResponse:
    if kind not in ("classic", "new"):
        return _err(404, "Unknown environment.")
    sess = _get(request)
    if sess:
        sess[kind] = None
    return JSONResponse(_state(sess))


@app.post("/api/webhooks/{kind}")
async def webhooks(kind: str, request: Request) -> JSONResponse:
    if kind not in ("classic", "new"):
        return _err(404, "Unknown environment.")
    sess = _get(request)
    if not sess or not sess.get(kind):
        return _err(409, "Connect the environment first.")
    body = await request.json()
    conn = sess[kind]
    if "enabled" in body:
        conn["webhooks"] = bool(body["enabled"])
    if body.get("regenerate"):
        conn["webhookKey"] = "whk_" + secrets.token_urlsafe(12)
    return JSONResponse(_state(sess))


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
