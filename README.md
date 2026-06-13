# ICT/SMC Trading Bot — VPS Direct (MT5 Native)

**Stack:** Windows VPS (MT5 + bot) ← Cloudflare Tunnel → Replit (Dashboard) ← GitHub

No MetaApi. No third-party bridges. The bot connects to MetaTrader 5 directly via the
official `MetaTrader5` Python library, running on the same Windows machine as your MT5 terminal.
The remote dashboard is hosted on Replit and communicates with the bot through a Cloudflare Tunnel.

---

## Architecture

```
┌─────────────────────────────────────┐          ┌──────────────────────────┐
│  Windows VPS                        │          │  Replit                  │
│  ┌──────────────┐  ┌──────────────┐ │  HTTPS   │  serve_dashboard.py      │
│  │ MT5 Terminal │←→│ Python Bot   │ │ ◄──────  │  dashboard.html          │
│  │ (logged in)  │  │ + Flask API  │ │  tunnel  │  (Flask, always-on)      │
│  └──────────────┘  └──────┬───────┘ │          └──────────────────────────┘
│                            │        │
│               Cloudflare Tunnel     │
│               (cloudflared.exe)     │
└─────────────────────────────────────┘
         ↑
    GitHub (source of truth for both sides)
```

---

## File Structure

```
├── mt5_trading_bot.py      ← Runs on Windows VPS — MT5 + Flask API
├── dashboard.html          ← Single-file remote monitoring UI
├── serve_dashboard.py      ← Replit Flask entry point (serves dashboard.html)
├── requirements.txt        ← VPS dependencies (MetaTrader5, flask, flask-cors, …)
└── README.md
```

---

## Strategy Suite — 36 Total

### ICT PD Arrays (12)
Order Block · Breaker Block · Mitigation Block · Fair Value Gap (FVG) ·
Inverse FVG (iFVG) · Implied FVG · Balanced Price Range (BPR) ·
Rejection Block · Suspension Block · Propulsion Block · Vacuum Block · Inducement

### SMC / Structure (8)
BOS / CHoCH · Market Structure Shift (MSS) · CISD ·
Premium / Discount · OTE (0.62–0.79 Fib) · HTF Bias Alignment ·
IPDA Lookback (20/40/60d) · Supply & Demand

### Liquidity (5)
Liquidity Sweep (BSL/SSL) · Turtle Soup · SMT Divergence ·
New Week Opening Gap (NWOG) · New Day Opening Gap (NDOG)

### Time-Based (4)
Kill Zone Entry · Silver Bullet · Power of 3 (AMD) · ICT Macros

### Technical (4)
S/R Levels · VWAP · Fibonacci · EMA

### Price Action (3)
Engulfing · Pin Bar · Rejection Candle

---

## Detection Logic — `mt5_trading_bot.py`

### Core detectors (original)
| Function | Description |
|---|---|
| `get_market_structure` | HTF bias: BULLISH / BEARISH / NEUTRAL via HH/HL or BOS |
| `find_order_blocks` | Last opposite-direction candle before strong impulse |
| `find_fvg` | 3-candle imbalance; bullish and bearish, unfilled only |
| `find_sr_levels` | Swing-point clusters, top 10 closest levels |

### Extended ICT/SMC detectors (added)
| Function | Description |
|---|---|
| `find_breaker_blocks` | Violated OBs that have flipped polarity |
| `find_balanced_price_range` | Overlapping bull + bear FVG equilibrium zone |
| `find_rejection_blocks` | Long-wick candles (≥ 60 % wick ratio) |
| `find_suspension_blocks` | Single candle floating between two volume gaps |
| `find_propulsion_blocks` | 3 consecutive same-direction candles (≥ 10 pip move) |
| `find_vacuum_blocks` | Macro-scale FVG (≥ 50 pip void); draw-on-liquidity target |
| `find_inducement` | Unswept swing near OB (retail trap check) |
| `find_liquidity_sweeps` | Recent BSL/SSL stop-hunt wicks with close-back confirmation |
| `find_turtle_soup` | False breakout of the 2nd-to-last swing high/low |
| `find_bos_choch` | Classifies structure breaks as BOS (continuation) or CHoCH (reversal) |
| `detect_cisd` | Candle delivery state flip (close through prior candle body) |
| `get_premium_discount` | Fibonacci position in dealing range (38 % discount / 62 % premium) |
| `get_ipda_ranges` | 20 / 40 / 60 trading-day high/low reference windows |
| `find_opening_gaps` | Unfilled NWOG and NDOG gaps |
| `in_silver_bullet` | Three Silver Bullet time windows (UTC) |
| `in_ict_macro` | Eight ICT Macro algorithmic windows (minute-precision) |
| `detect_power_of_3` | AMD phase classification: Accumulation → Manipulation → Distribution |
| `detect_smt_divergence` | Correlated-pair divergence (EURUSD ↔ GBPUSD, XAUUSD ↔ XAGUSD, etc.) |

### Signal confluence scoring — up to 19 factors per signal
```
HTF Bias · Price in OB · FVG Overlap · S&R Proximity · Kill Zone
Silver Bullet · ICT Macro · Premium/Discount Zone · BSL/SSL Sweep
Turtle Soup · BPR Nearby · NWOG/NDOG · CISD · AMD Phase
BOS Confirmed · Rejection Block · Propulsion Block · IPDA Range · Inducement Cleared
```
Minimum score required to execute is set by `CONFIG["min_confluence"]` (default: 3).

---

## Part 1 — VPS Setup (Windows)

### 1. Install Python + dependencies
RDP into your VPS, open PowerShell:
```powershell
pip install -r requirements.txt
```

### 2. Clone the repo
```powershell
git clone https://github.com/hibothost/VPS.git
cd VPS
```

### 3. Configure the bot
Edit `CONFIG` in `mt5_trading_bot.py`, or set environment variables:
```powershell
$env:MT5_LOGIN="12345678"
$env:MT5_PASSWORD="yourpassword"
$env:MT5_SERVER="JustMarkets-Demo3"
```
If MT5 is already open and logged in on the VPS, you can skip the credentials —
the bot connects to the running terminal session directly.

### 4. Run the bot
```powershell
python mt5_trading_bot.py
```
Expected output:
```
✅  Connected  │  Just Global Markets Ltd.
💰  Balance    │  1000.00 USD
🌐  API server │  http://localhost:5000
```

### 5. Keep it running permanently (NSSM)
```powershell
# Download nssm.exe from https://nssm.cc, then:
nssm install ICTBot "C:\Python311\python.exe" "C:\VPS\mt5_trading_bot.py"
nssm set ICTBot AppDirectory "C:\VPS"
nssm start ICTBot
```

---

## Part 2 — Expose via Cloudflare Tunnel

### Quick tunnel (temporary URL — easiest to start)
```powershell
cloudflared.exe tunnel --url http://localhost:5000
```
Prints a URL like `https://random-words.trycloudflare.com` — paste this into the
dashboard's **SERVER** field.

### Permanent named tunnel (recommended)
```powershell
cloudflared.exe tunnel login
cloudflared.exe tunnel create ict-bot
cloudflared.exe tunnel route dns ict-bot bot.yourdomain.com
```
Create `config.yml`:
```yaml
tunnel: ict-bot
credentials-file: C:\Users\Administrator\.cloudflared\<tunnel-id>.json
ingress:
  - hostname: bot.yourdomain.com
    service: http://localhost:5000
  - service: http_status:404
```
```powershell
cloudflared.exe service install
cloudflared.exe tunnel run ict-bot
```
You now have a stable `https://bot.yourdomain.com` that survives reboots.

---

## Part 3 — Dashboard (Replit)

The dashboard is a single `dashboard.html` served by a tiny Flask app (`serve_dashboard.py`)
running on Replit. It connects to your bot via the Cloudflare Tunnel URL.

### First-time setup
On first load the dashboard prompts for the bot URL:
```
Enter your bot server URL
https://your-tunnel-url.trycloudflare.com
```
This is saved in `localStorage`. Click **🔗 SERVER** anytime to update it.

### Dashboard tabs
| Tab | Contents |
|---|---|
| **SIGNALS** | Live ICT/SMC signals with full confluence checklist (hit ✓ / miss ✗) |
| **CONFLUENCES** | Per-signal hit/miss grid, score bar, entry/SL/TP footer |
| **EQUITY** | Live equity curve chart |
| **HISTORY** | Last 100 closed trades |

---

## Configuration Reference (`CONFIG`)

```python
# Risk
"risk_pct":     10.0,   # % of balance risked per trade
"min_rr":       4.0,    # Minimum R:R ratio
"max_spread":   50,     # Max spread in points
"max_trades":   2,      # Max concurrent bot trades
"min_confluence": 3,    # Min score to fire a signal

# Detection tuning
"swing_lookback":   10,   # Bars each side for swing detection
"ob_lookback":      50,   # Bars back to scan for OBs
"fvg_min_pips":     2.0,  # Minimum FVG size (pips)
"sr_lookback":      150,  # Bars back for S&R clustering
"sl_buffer_pips":   5.0,  # Buffer beyond OB for SL placement
"vacuum_min_pips":  50.0, # Minimum size to qualify as Vacuum Block

# IPDA lookback windows
"ipda_lookback": [20, 40, 60],  # trading days

# SMT correlated pairs
"smt_pairs": {
    "EURUSD": "GBPUSD",
    "USDJPY": "USDCHF",
    "XAUUSD": "XAGUSD",
    ...
},

# Kill zones (UTC hours)
"kill_zones": [
    {"name": "London Open",   "start": 7,  "end": 10},
    {"name": "New York Open", "start": 12, "end": 15},
],

# Silver Bullet windows (UTC hours)
"silver_bullet_windows": [
    {"name": "London SB", "start_utc": 8,  "end_utc": 9 },
    {"name": "AM SB",     "start_utc": 15, "end_utc": 16},
    {"name": "PM SB",     "start_utc": 19, "end_utc": 20},
],

# ICT Macro windows (minutes since midnight UTC)
"macro_windows": [
    {"name": "London Open Macro", "start": 530, "end": 550},
    {"name": "NY Open Macro",     "start": 830, "end": 850},
    ...  # 8 macros total
],
```

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/api/status` | Full bot state (see State Fields below) |
| GET | `/api/symbols` | Broker instrument list, grouped by asset class |
| GET | `/api/history` | Last 100 closed trades |
| GET | `/api/equity` | Equity curve `[{time, equity}, …]` |
| POST | `/api/start` | Start the bot loop |
| POST | `/api/stop` | Stop the bot loop |
| GET/POST | `/api/config` | Read or update editable config fields |
| GET/POST | `/api/broker` | Read or set MT5 login credentials |
| POST | `/api/reconnect` | Re-initialise MT5 connection |
| POST | `/api/close/<ticket>` | Close one position |
| POST | `/api/closeall` | Close all bot positions |

### `/api/status` — State Fields

```jsonc
{
  "running": true,
  "connected": true,
  "market_bias": "BULLISH",       // BULLISH | BEARISH | NEUTRAL
  "last_scan": "2025-01-01T…",
  "ob_count": 4,
  "fvg_count": 2,
  "breaker_count": 1,
  "bpr_count": 1,
  "sweep_count": 2,
  "pd_zone": "DISCOUNT",          // PREMIUM | DISCOUNT | EQUILIBRIUM
  "po3_phase": "MANIPULATION",    // ACCUMULATION | MANIPULATION | DISTRIBUTION
  "bos_choch": ["BOS_BULLISH"],
  "cisd": false,
  "smt_divergence": false,
  "ipda": {
    "20d": { "high": 1.09500, "low": 1.07800 },
    "40d": { "high": 1.10200, "low": 1.07100 },
    "60d": { "high": 1.11000, "low": 1.06500 }
  },
  "active_signals": [ … ],
  "open_trades":    [ … ],
  "kill_zones":     [ … ],
  "sr_levels":      [ … ],
  "current_price":  { "symbol": "EURUSD", "bid": …, "ask": …, "mid": … }
}
```

---

## Troubleshooting

**Bot shows "Connected: false"**
→ Ensure MT5 is open and logged in on the VPS. Tools → Options → Expert Advisors → enable "Allow Algorithmic Trading".

**Dashboard stuck on DEMO / can't reach bot**
→ Check the **🔗 SERVER** URL matches your current Cloudflare Tunnel address.
→ Quick tunnels change URL on every restart — use a named tunnel for stability.

**"Algo Trading" button greyed out**
→ Click the "Algo Trading" button in the MT5 toolbar until it turns green.

**Order error 10030 (Invalid Fill)**
→ The bot auto-detects the correct fill mode (FOK / IOC / RETURN) per symbol — this should not occur. Check broker requirements if it persists.

**SMT Divergence never fires**
→ The correlated symbol must be available on your broker. Add it to Market Watch in MT5.

---

## ⚠️ Risk Warning

Test on a **demo account first**. Start with `risk_pct` at 0.5–1 %. These are
detection algorithms, not financial advice. Never risk capital you cannot afford to lose.
