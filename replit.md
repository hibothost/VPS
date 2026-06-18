# ICT/SMC Trading Bot Dashboard

A monitoring dashboard for an ICT/Smart Money Concepts (SMC) algorithmic trading bot running on MetaTrader 5.

## Architecture

This Replit project hosts **only the web dashboard**. The actual trading bot (`mt5_trading_bot.py`) runs separately on a Windows VPS with MetaTrader 5 installed. The two communicate via a configurable server URL (set in the dashboard UI via the 🔗 SERVER button).

- **Dashboard server**: `serve_dashboard.py` — Flask app serving `dashboard.html`
- **Trading bot**: `mt5_trading_bot.py` — runs on Windows VPS (not on Replit)

## Running

The dashboard server starts automatically on port 5000. Visit the preview pane to use it.

## Configuration

In the dashboard UI, click the 🔗 SERVER button to configure the URL of your Windows VPS bot (exposed via Cloudflare Tunnel or similar).

## User preferences

- Keep `MetaTrader5`, `numpy`, and `pandas` out of `requirements.txt` — those only run on the Windows VPS, not on Replit (Linux).
