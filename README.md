# ICT/SMC + S&R Trading Bot — VPS Direct (MT5 Native)

**Stack:** AWS EC2 Windows VPS (MT5 + bot) ← Cloudflare Tunnel → Render (Dashboard) ← GitHub

No MetaApi. No third-party bridges. The bot talks to MT5 directly via the
official `MetaTrader5` Python library, running on the same Windows machine
as your MT5 terminal.

---

## Architecture

```
┌──────────────────────────────────┐         ┌─────────────────────┐
│  AWS EC2 Windows VPS              │         │  Render             │
│  ┌──────────────┐  ┌────────────┐│  HTTPS  │  dashboard.html     │
│  │ MT5 Terminal │←→│ Python Bot ││ ◄─────  │  (static, free)     │
│  │ (logged in)  │  │ + Flask API││ tunnel  │                     │
│  └──────────────┘  └─────┬──────┘│         └─────────────────────┘
│                            │       │
│                  Cloudflare Tunnel │
│                  (cloudflared.exe) │
└──────────────────────────────────┘
        ↑
   GitHub (source for both)
```

---

## Part 1 — VPS Setup (AWS EC2 Windows)

### 1. Install Python + dependencies
RDP into your VPS, open PowerShell:
```powershell
# Install Python 3.11+ from python.org if not already installed
pip install -r requirements.txt
```

### 2. Clone your repo
```powershell
git clone https://github.com/YOUR_USERNAME/ICT.git
cd ICT
```

### 3. Configure the bot
Either edit `CONFIG` directly in `mt5_trading_bot.py`, or set environment variables (PowerShell):
```powershell
$env:MT5_LOGIN="12345678"
$env:MT5_PASSWORD="yourpassword"
$env:MT5_SERVER="JustMarkets-Demo3"
$env:TRADING_SYMBOL="EURUSD"
```

> If MT5 terminal is already open and logged in, you can leave these unset —
> the bot connects to the running terminal session directly.

### 4. Run the bot
```powershell
python mt5_trading_bot.py
```
You should see:
```
✅  Connected  │  Just Global Markets Ltd.
💰  Balance    │  20.00 USD
🌐  Dashboard  │  http://localhost:5000
```

### 5. Keep it running permanently
Use **NSSM** (Non-Sucking Service Manager) to run the bot as a Windows service
so it survives reboots and RDP disconnects:
```powershell
# Download nssm.exe from https://nssm.cc, then:
nssm install ICTBot "C:\Python311\python.exe" "C:\ICT\mt5_trading_bot.py"
nssm set ICTBot AppDirectory "C:\ICT"
nssm start ICTBot
```

---

## Part 2 — Expose the Bot via Cloudflare Tunnel

This gives your bot a free, secure `https://` URL — no port forwarding,
no AWS security group changes, no SSL certificates to manage.

### 1. Install cloudflared
Download `cloudflared.exe` from:
https://github.com/cloudflare/cloudflared/releases (Windows amd64)

### 2. Run a quick tunnel (easiest — temporary URL)
```powershell
cloudflared.exe tunnel --url http://localhost:5000
```
This prints a URL like:
```
https://random-words-here.trycloudflare.com
```
This URL changes every time you restart the tunnel.

### 3. (Recommended) Permanent named tunnel
```powershell
cloudflared.exe tunnel login
cloudflared.exe tunnel create ict-bot
cloudflared.exe tunnel route dns ict-bot bot.yourdomain.com
```
Then create `config.yml`:
```yaml
tunnel: ict-bot
credentials-file: C:\Users\Administrator\.cloudflared\<tunnel-id>.json
ingress:
  - hostname: bot.yourdomain.com
    service: http://localhost:5000
  - service: http_status:404
```
Run it as a service:
```powershell
cloudflared.exe service install
cloudflared.exe tunnel run ict-bot
```
You now have a stable `https://bot.yourdomain.com` that never changes.

> Don't have a domain? The free `trycloudflare.com` quick tunnel works fine —
> just update the dashboard's server URL each time you restart it.

---

## Part 3 — Deploy the Dashboard to Render

1. Push these files to GitHub:
   ```
   ICT/
   ├── mt5_trading_bot.py       ← runs on VPS
   ├── dashboard.html           ← served by Render
   ├── serve_dashboard.py       ← Render entrypoint
   ├── requirements.txt          ← VPS deps (MetaTrader5 etc.)
   ├── requirements-render.txt   ← Render deps (just flask)
   ├── render.yaml
   └── README.md
   ```

2. Render → New → Web Service → connect repo
3. Render reads `render.yaml` automatically (free plan, just serves static dashboard)
4. Deploy → visit your Render URL

### First-time dashboard setup
On first load, the dashboard will prompt:
```
Enter your bot server URL
https://your-tunnel-url.trycloudflare.com
```
Paste your Cloudflare Tunnel URL. It's saved in your browser (localStorage) —
click the **🔗 SERVER** button anytime to change it.

---

## API Endpoints (unchanged)

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/api/status` | Bot state, signals, open trades, current price |
| GET | `/api/symbols` | Broker instrument list, grouped |
| GET | `/api/history` | Last 100 closed trades |
| GET | `/api/equity` | Equity curve |
| POST | `/api/start` | Start bot loop |
| POST | `/api/stop` | Stop bot loop |
| GET/POST | `/api/config` | Read/update settings |
| POST | `/api/reconnect` | Re-init MT5 connection |
| POST | `/api/close/<ticket>` | Close a position |
| POST | `/api/closeall` | Close all bot positions |

---

## Why This Is Better Than MetaApi

| | MetaApi | VPS Direct |
|---|---|---|
| Connection reliability | Cloud bridge, can desync | Native, same machine as MT5 |
| Historical data | Sometimes fails | Always available via MT5 terminal |
| Latency | Extra network hop | Local — fastest possible |
| Cost | Subscription after free tier | Free (just your existing VPS) |
| Token security | JWT in logs/URLs | No tokens — local connection |
| Dependency | Third-party SDK updates break things | Stable official MetaTrader5 lib |

---

## Troubleshooting

**Bot shows "Connected: false"**
→ Make sure MT5 terminal is open and logged into your account on the VPS.

**Dashboard shows DEMO / can't reach bot**
→ Check the 🔗 SERVER URL matches your current Cloudflare Tunnel address.
→ Quick tunnels (`trycloudflare.com`) change on every restart — use a named tunnel for stability.

**"Algo Trading" disabled error**
→ In MT5: Tools → Options → Expert Advisors → check "Allow Algorithmic Trading"
→ Also click the "Algo Trading" button in the toolbar (should be green/on)

---

## ⚠️ Risk Warning

Test on demo first. Start with `RISK_PCT=0.5`. Never risk money you can't afford to lose.
