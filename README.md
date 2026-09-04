# Aruba Central Automation

A lightweight web GUI for **Aruba Central** (Classic *and* New Central). You paste
your own API credentials and browse your tenant — devices, clients, sites,
subscriptions, SSIDs, AP groups, RF profiles — with search, filters, CSV export
and an interactive topology view. Classic Central also gets a **configuration**
section: push SSID / RADIUS / RF-profile config to selected AP groups, and
create or delete AP groups.

No database. No stored credentials. Each browser session holds its own connection
in the server's memory only; a different browser starts fresh, and everything is
gone when the service restarts.

> Not affiliated with or endorsed by HPE / Aruba. You supply your own API
> credentials and talk directly to your own Central tenant.

---

## What you need

- A **Linux machine** to run it on — a spare PC, a VM, a Raspberry Pi, or a
  cloud server. **Debian or Ubuntu** is what the installer targets. It can be the
  same machine you're sitting at.
- About **10 minutes**.
- Your Aruba Central **API credentials** (see [below](#getting-your-api-credentials)) —
  you can also get these later, after it's running.

You do **not** need a domain name, a public IP, or a paid certificate.

---

## Install (with HTTPS)

Run every command in a terminal on the Linux machine. Lines starting with `sudo`
will ask for your password.

### 1. Install the tools the installer needs

```bash
sudo apt update
sudo apt install -y git python3-venv nginx openssl rsync curl
```

### 2. Get the code

```bash
cd ~
git clone https://github.com/JibranAziz/CentralAutomation.git
cd CentralAutomation
```

> **No git / prefer a download?** Open
> <https://github.com/JibranAziz/CentralAutomation>, click the green **Code**
> button → **Download ZIP**, unzip it, and `cd` into the unzipped folder instead
> of cloning.

### 3. Run the installer

Give it the address people will use to reach the app — a hostname **or** just the
machine's IP address:

```bash
sudo ./deploy/install.sh central.lan
# ...or an IP address:
sudo ./deploy/install.sh 192.168.1.50
```

Not sure what your IP is? Run `hostname -I` and use the first number.

The installer sets everything up: a Python environment, a self-signed HTTPS
certificate, a background service that starts on boot, and the web server in
front of it.

### 4. Open it

Go to **`https://central.lan/`** (or `https://192.168.1.50/`) in your browser.

Because the certificate is self-signed, the browser shows a **"Not secure" /
"Your connection is not private"** warning the first time. This is expected —
click **Advanced → Proceed / Continue**. The connection is still encrypted; the
browser just can't verify a certificate you generated yourself.

That's it. The app is running and will restart automatically if the machine
reboots.

---

## Updating to a newer version

```bash
cd ~/CentralAutomation
git pull
sudo ./deploy/install.sh central.lan      # same address you used the first time
```

Re-running the installer is safe — it copies the new code in, reinstalls
dependencies and restarts the service. (Downloaded the ZIP instead of cloning?
Download the new ZIP, unzip, and run the same command.)

---

## Just want to try it on your own laptop?

No install, no HTTPS, no admin rights — runs at `http://127.0.0.1:8080`:

```bash
git clone https://github.com/JibranAziz/CentralAutomation.git
cd CentralAutomation
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8080
```

Then open <http://127.0.0.1:8080>. Press `Ctrl+C` to stop.

---

## Getting your API credentials

Open the **Connect Central** tab in the app and fill in one or both:

- **Classic Central** — in Aruba Central: *Account Home → API Gateway → System
  Apps & Tokens*. Create an app/token, then copy the **Client ID**, **Client
  Secret** and **Refresh Token**, and pick your region's **API gateway base URL**
  from the dropdown. Classic refresh tokens expire — regenerate and re-paste when
  the connection drops. Tick **"Remember Client ID & Secret"** so you only have
  to re-enter the refresh token.
- **New Central** — in New Central: *Menu → API Gateway → API client
  credentials*. Copy the **Client ID** and **Client Secret**, and pick your
  **cluster** from the dropdown. No refresh token needed.

Credentials are used only to open your browser session. They are never written to
the server's disk. "Remember" stores the Client ID/Secret in *your browser* only.

---

## Advanced

### Use a real (trusted) certificate

To get rid of the browser warning, use a certificate from a real authority (e.g.
Let's Encrypt) instead of the self-signed one. Get your certificate however you
normally would, then edit `/etc/nginx/sites-available/aruba-central-automation`
and point these two lines at your certificate files:

```nginx
ssl_certificate     /etc/ssl/aruba-central-automation/fullchain.pem;
ssl_certificate_key /etc/ssl/aruba-central-automation/privkey.pem;
```

Then `sudo nginx -t && sudo systemctl reload nginx`.

### Regenerate the self-signed cert (e.g. after adding another hostname)

```bash
sudo ./deploy/gen-cert.sh central.lan central.example.com 192.168.1.50
sudo systemctl reload nginx
```

### Where things live after install

| | |
|---|---|
| App code | `/opt/aruba-central-automation/` |
| Service | `systemctl {status,restart,stop} aruba-central-automation` |
| Logs | `journalctl -u aruba-central-automation -f` |
| Web server config | `/etc/nginx/sites-available/aruba-central-automation` |
| Self-signed cert | `/etc/ssl/aruba-central-automation/` |

## Stack

- **Backend** — FastAPI + Uvicorn (`app/main.py`), `httpx` for the Central APIs.
  In-memory per-cookie sessions, no persistence, no build step.
- **Frontend** — one static page (`app/static/index.html`), vanilla JS.

See [HANDOVER.md](HANDOVER.md) for the full technical write-up.

## License

MIT — see [LICENSE](LICENSE).
