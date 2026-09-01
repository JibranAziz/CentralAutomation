"""Aruba Central Automation — thin backend.

Sessions live only in this process's memory, keyed by a per-browser cookie.
Nothing is written to disk and nothing persists a restart. A different browser
(no cookie) always starts a fresh session.
"""
from __future__ import annotations

import asyncio
import os
import re
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
    try:
        r = await client.get(url, headers=headers, params=params or {})
        if r.status_code == 200:
            return r.json().get("total")
    except Exception:
        pass
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
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            clients_total, sites_total, subs_total, dev_totals = await asyncio.gather(
                _get_total(client, f"https://{host}/network-monitoring/v1/clients",
                           headers, {"limit": "1"}),
                _get_total(client, f"https://{host}/network-monitoring/v1/sites-health",
                           headers, {"limit": "1"}),
                _get_total(client, "https://global.api.greenlake.hpe.com/subscriptions/v1/subscriptions",
                           headers, {"limit": "1"}),
                _new_central_device_totals(client, host, headers),
            )
        out["clients"] = clients_total
        out["sites"] = sites_total
        out["subscriptions"] = subs_total
        if dev_totals:
            out.update(dev_totals)
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
            "connType": c.get("clientConnectionType"),
            "band": c.get("wirelessBand"),
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
        else:
            return None, 0
    return rows, total if total is not None else len(rows)


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


@app.get("/api/overview/new")
async def overview_new(request: Request) -> JSONResponse:
    sess = _get(request)
    conn = sess.get("new") if sess else None
    if not conn:
        return _err(409, "Connect New Central first.")
    if conn.get("expires_at", 0) < _now():
        return _err(401, "The access token has expired — reconnect New Central.")
    data = await _new_central_overview(conn["host"], conn["access_token"])
    return JSONResponse({"overview": data})


@app.get("/api/list/new/{entity}")
async def list_new(entity: str, request: Request) -> JSONResponse:
    valid = {"clients", "access-points", "switches", "gateways", "sites", "subscriptions"}
    if entity not in valid:
        return _err(404, "Unknown entity.")
    sess = _get(request)
    conn = sess.get("new") if sess else None
    if not conn:
        return _err(409, "Connect New Central first.")
    if conn.get("expires_at", 0) < _now():
        return _err(401, "The access token has expired — reconnect New Central.")
    rows, _ = await _new_central_list(conn["host"], conn["access_token"], entity)
    if rows is None:
        return _err(502, f"Central did not return {entity.replace('-', ' ')} for this API client.")
    return JSONResponse({"rows": rows, "total": len(rows)})


@app.get("/api/detail/new/{kind}/{ident}")
async def detail_new(kind: str, ident: str, request: Request) -> JSONResponse:
    if kind not in ("client", "device", "site"):
        return _err(404, "Unknown detail type.")
    sess = _get(request)
    conn = sess.get("new") if sess else None
    if not conn:
        return _err(409, "Connect New Central first.")
    if conn.get("expires_at", 0) < _now():
        return _err(401, "The access token has expired — reconnect New Central.")
    if kind == "client":
        data = await _new_central_client_detail(conn["host"], conn["access_token"], ident)
    elif kind == "site":
        data = await _new_central_site_detail(conn["host"], conn["access_token"], ident)
    else:
        data = await _new_central_device_detail(conn["host"], conn["access_token"], ident)
    if data is None:
        return _err(404, f"No {kind} found for '{ident}'.")
    return JSONResponse({"detail": data})


@app.get("/api/topology/new/{site_id}")
async def topology_new(site_id: str, request: Request) -> JSONResponse:
    sess = _get(request)
    conn = sess.get("new") if sess else None
    if not conn:
        return _err(409, "Connect New Central first.")
    if conn.get("expires_at", 0) < _now():
        return _err(401, "The access token has expired — reconnect New Central.")
    data = await _new_central_topology(conn["host"], conn["access_token"], site_id)
    if data is None:
        return _err(502, "Central did not return topology for this site.")
    return JSONResponse(data)


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
