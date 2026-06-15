#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║         ICT / SMC + S&R Trading Bot for MetaTrader 5        ║
║  Strategy: Order Blocks · FVG · Market Structure · KZ Filter ║
║  Requires Python 3.10+ on Windows with MT5 installed        ║
╚══════════════════════════════════════════════════════════════╝

Install dependencies:
    pip install MetaTrader5 pandas numpy flask flask-cors

Run:
    python mt5_trading_bot.py
    Then open dashboard.html in your browser
"""

import sys
import time
import json
import logging
import threading
from datetime import datetime, timezone

import numpy as np
import pandas as pd

try:
    import MetaTrader5 as mt5
except ImportError:
    print("MetaTrader5 not installed. Run: pip install MetaTrader5")
    sys.exit(1)

try:
    from flask import Flask, jsonify, request
    from flask_cors import CORS
except ImportError:
    print("Flask not installed. Run: pip install flask flask-cors")
    sys.exit(1)


# ══════════════════════════════════════════════════════════════
#  CONFIGURATION  ─  Edit before running
# ══════════════════════════════════════════════════════════════

CONFIG = {
    # ── MT5 credentials (leave None if already logged in to MT5)
    "login":    None,           # e.g. 12345678
    "password": None,           # e.g. "yourpassword"
    "server":   None,           # e.g. "ICMarkets-Demo"

    # ── Symbol to trade
    "symbol": "EURUSD",

    # ── Timeframes
    "htf": mt5.TIMEFRAME_H4,   # Higher TF — structure analysis
    "ltf": mt5.TIMEFRAME_M15,  # Lower TF — entry signals

    # ── Risk management
    "risk_pct":     10.0,        # % of balance risked per trade
    "min_rr":       4.0,        # Minimum Risk : Reward ratio
    "max_spread":   5000,         # Max allowed spread in points
    "max_trades":   2,          # Max concurrent bot trades

    # ── Strategy parameters
    "swing_lookback":   10,     # Bars each side for swing detection
    "ob_lookback":      50,     # Bars to scan back for Order Blocks
    "fvg_min_pips":     2.0,    # Minimum FVG size in pips
    "sr_lookback":      150,    # Bars to scan for S&R levels
    "sl_buffer_pips":   5.0,    # Buffer pips beyond OB for SL
    "min_confluence":   3,      # Min confluence score to execute

    # ── Trade monitoring & dynamic exit
    "bias_exit":       True,   # Close trade if HTF bias reverses
    "bos_exit":        True,   # Close trade on BOS/CHoCH against direction
    "trail_be_pips":   20,     # Move SL to break-even once X pips in profit (0 = off)
    "trail_be_buffer_pips": 2, # Extra pips of room beyond entry for the BE stop (avoids closing on re-touch)

    # ── Kill zones (UTC hours) — 4 standard ICT kill zones
    "kill_zones": [
        {"name": "Asian Session",    "start": 0,  "end": 3 },
        {"name": "London Open",      "start": 7,  "end": 10},
        {"name": "New York AM",      "start": 12, "end": 15},
        {"name": "London Close",     "start": 15, "end": 17},
    ],

    # ── Kill zone gate — False = execute any time confluence is met (default)
    #    True  = only execute inside a kill zone window
    "require_kill_zone": False,

    # ── Silver Bullet windows (UTC hours, non-DST / EST+5)
    "silver_bullet_windows": [
        {"name": "London SB",  "start_utc": 8,  "end_utc": 9 },
        {"name": "AM SB",      "start_utc": 15, "end_utc": 16},
        {"name": "PM SB",      "start_utc": 19, "end_utc": 20},
    ],

    # ── ICT Macro windows (minutes since midnight UTC)
    "macro_windows": [
        {"name": "0233 Macro",        "start": 153,  "end": 180 },
        {"name": "0403 Macro",        "start": 243,  "end": 270 },
        {"name": "London Open Macro", "start": 530,  "end": 550 },
        {"name": "0950 Macro",        "start": 590,  "end": 610 },
        {"name": "1050 Macro",        "start": 650,  "end": 670 },
        {"name": "NY Open Macro",     "start": 830,  "end": 850 },
        {"name": "1015 AM Macro",     "start": 915,  "end": 945 },
        {"name": "PM Macro",          "start": 1190, "end": 1210},
    ],

    # ── Vacuum Block minimum size (pips)
    "vacuum_min_pips": 50.0,

    # ── IPDA lookback periods (trading days)
    "ipda_lookback": [20, 40, 60],

    # ── SMT Divergence correlated pairs
    "smt_pairs": {
        "EURUSD": "GBPUSD",  "GBPUSD": "EURUSD",
        "USDJPY": "USDCHF",  "USDCHF": "USDJPY",
        "XAUUSD": "XAGUSD",  "XAGUSD": "XAUUSD",
        "AUDUSD": "NZDUSD",  "NZDUSD": "AUDUSD",
    },

    # ── Active strategies — controls which checks contribute to confluence score
    "active_strategies": [
        "ob","fvg","bos","breaker","liq","pd","bpr","mss","kz","silver",
        "po3","htf","sr","cisd","ipda","rejblock","propulsion","inducement",
        "turtle","macros","nwog","ndog",
    ],

    # ── Bot internals
    "scan_interval": 60,        # Seconds between analysis scans
    "magic_number":  20250101,  # Unique ID for this bot's trades
    "comment":       "ICT_SMC_v1",

    # ── Dashboard
    "port": 5000,
}


# ══════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("ICT_Bot")


# ══════════════════════════════════════════════════════════════
#  SHARED STATE  (read by Flask API)
# ══════════════════════════════════════════════════════════════

state = {
    "running":        False,
    "connected":      False,
    "market_bias":    "NEUTRAL",
    "last_scan":      None,
    "error":          None,
    "account":        {},
    "equity_curve":   [],   # [{time, equity}, …]  capped at 500 points
    "ob_count":       0,
    "fvg_count":      0,
    "breaker_count":  0,
    "bpr_count":      0,
    "sweep_count":    0,
    "pd_zone":        "UNKNOWN",
    "po3_phase":      "UNKNOWN",
    "bos_choch":      [],
    "cisd":           False,
    "ipda":           {},
    "smt_divergence": False,
    "current_price":  {},   # {symbol, bid, ask, mid, digits} — updated every scan
    "ctx_confluences": [],  # [{name, active, hint}] — contextual factors updated each scan
}

active_signals:  list[dict] = []
open_trades:     list[dict] = []
trade_history:   list[dict] = []
sr_cache:        list[dict] = []

# ── Persistent trade log ───────────────────────────────────────────
TRADE_LOG_FILE  = "trade_log.json"
EQUITY_LOG_FILE = "equity_log.json"
_prev_open_tickets: set = set()   # track ticket IDs to detect closures


def _load_json(path: str, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path: str, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        log.error(f"Failed to save {path}: {e}")


# Load persisted data on startup
persistent_trade_log: list[dict] = _load_json(TRADE_LOG_FILE, [])
persistent_equity_log: list[dict] = _load_json(EQUITY_LOG_FILE, [])

# Seed in-memory history from persisted log
trade_history = [t for t in persistent_trade_log]


def record_trade_open(trade: dict, ctx: dict, bias: str, sig: dict):
    """Append a full ML-context record when a trade opens."""
    global persistent_trade_log
    ml_record = {
        **trade,
        "status":      "OPEN",
        "close_price": None,
        "close_time":  None,
        "pips":        None,
        "exit_reason": None,
        "ml_context": {
            "bias":             bias,
            "pd_zone":          ctx.get("pd_zone", "UNKNOWN"),
            "po3_phase":        ctx.get("po3_phase", "UNKNOWN"),
            "cisd":             ctx.get("cisd", False),
            "smt_divergence":   bool(ctx.get("smt_divergence", False)),
            "bos_choch":        [b["type"] for b in ctx.get("bos_choch", [])],
            "ob_count":         len(ctx.get("obs", [])),
            "fvg_count":        len(ctx.get("fvgs", [])),
            "sweep_count":      len(ctx.get("sweeps", [])),
            "breaker_count":    len(ctx.get("breakers", [])),
            "bpr_count":        len(ctx.get("bprs", [])),
            "is_kz":            ctx.get("is_kz", False),
            "kz_name":          ctx.get("kz_name"),
            "is_sb":            ctx.get("is_sb", False),
            "is_macro":         ctx.get("is_macro", False),
            "confluence_score": sig.get("score", 0),
            "confluence_hit":   sig.get("confluence", {}).get("hit", []),
            "confluence_miss":  sig.get("confluence", {}).get("miss", []),
        },
    }
    persistent_trade_log.append(ml_record)
    _save_json(TRADE_LOG_FILE, persistent_trade_log)


def record_trade_close(ticket: int, close_price: float, pnl: float, reason: str):
    """Update the persisted record when a trade is closed."""
    global persistent_trade_log
    pip = get_pip_size(CONFIG["symbol"])
    for rec in persistent_trade_log:
        if rec.get("ticket") == ticket and rec.get("status") == "OPEN":
            rec["status"]      = "CLOSED"
            rec["close_price"] = round(close_price, 5)
            rec["close_time"]  = datetime.now(timezone.utc).isoformat()
            rec["pnl"]         = round(pnl, 2)
            rec["exit_reason"] = reason
            if rec["direction"] == "BUY":
                rec["pips"] = round((close_price - rec["entry"]) / pip, 1)
            else:
                rec["pips"] = round((rec["entry"] - close_price) / pip, 1)
            break
    _save_json(TRADE_LOG_FILE, persistent_trade_log)
    # Mirror to in-memory list
    for t in trade_history:
        if t.get("ticket") == ticket:
            t["status"]      = "CLOSED"
            t["pnl"]         = round(pnl, 2)
            t["close_price"] = round(close_price, 5)
            t["exit_reason"] = reason
            break


def _query_mt5_close(ticket: int):
    """Query MT5 deal history to get close details after TP/SL/manual close."""
    try:
        deals = mt5.history_deals_get(position=ticket) or []
        close_deals = [d for d in deals if d.entry == 1]  # entry=1 = close
        if not close_deals:
            return None, 0.0, "Closed"
        d = close_deals[-1]
        total_pnl = sum(dd.profit + dd.swap + dd.commission for dd in deals)
        reason_map = {3: "TP hit", 4: "SL hit", 0: "Manual close", 2: "Stop-out"}
        reason = reason_map.get(d.reason, f"Closed (reason {d.reason})")
        return d.price, round(total_pnl, 2), reason
    except Exception:
        return None, 0.0, "Closed"


# ══════════════════════════════════════════════════════════════
#  MT5 CONNECTION
# ══════════════════════════════════════════════════════════════

def connect_mt5(login=None, password=None, server=None) -> bool:
    """
    Connect to MT5.  When credentials are supplied they are passed directly
    to mt5.initialize() so the terminal logs in automatically — no separate
    mt5.login() call needed (avoids error -6 / Authorization failed).
    """
    mt5.shutdown()  # ensure clean state before (re)initialising

    if login and password and server:
        ok = mt5.initialize(
            login=int(login),
            password=str(password),
            server=str(server),
        )
    else:
        ok = mt5.initialize()   # uses whatever account is already open in MT5

    if not ok:
        err = mt5.last_error()
        log.error(f"MT5 initialize failed: {err}")
        state["connected"] = False
        state["error"]     = f"MT5 init failed: {err}"
        return False

    acct = mt5.account_info()
    if acct is None:
        err = mt5.last_error()
        log.error(f"MT5: Cannot get account info — {err}")
        mt5.shutdown()
        state["connected"] = False
        state["error"]     = "Not logged in to MT5. Connect via the dashboard."
        return False

    state["connected"] = True
    state["error"]     = None
    state["account"]   = _fmt_account(acct)
    log.info(f"MT5 connected │ {acct.company} │ #{acct.login} │ {acct.balance:.2f} {acct.currency}")
    return True


def _fmt_account(acct) -> dict:
    return {
        "login":       acct.login,
        "balance":     round(acct.balance, 2),
        "equity":      round(acct.equity, 2),
        "margin":      round(acct.margin, 2),
        "free_margin": round(acct.margin_free, 2),
        "profit":      round(acct.profit, 2),
        "currency":    acct.currency,
        "leverage":    acct.leverage,
        "broker":      acct.company,
    }


# ══════════════════════════════════════════════════════════════
#  MARKET DATA
# ══════════════════════════════════════════════════════════════

def ensure_symbol(symbol: str) -> bool:
    """Make sure the symbol is visible in Market Watch before fetching data."""
    info = mt5.symbol_info(symbol)
    if info is None:
        log.warning(f"Symbol {symbol} not found in MT5")
        return False
    if not info.visible:
        if not mt5.symbol_select(symbol, True):
            log.warning(f"Could not add {symbol} to Market Watch: {mt5.last_error()}")
            return False
        time.sleep(0.3)  # let MT5 subscribe and fetch initial data
    return True


def get_ohlcv(symbol: str, timeframe, n: int = 250) -> pd.DataFrame | None:
    ensure_symbol(symbol)
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, n)
    if rates is None or len(rates) == 0:
        log.warning(f"No data for {symbol} tf={timeframe}: {mt5.last_error()}")
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("time")


def get_tick(symbol: str):
    return mt5.symbol_info_tick(symbol)


def get_pip_size(symbol: str) -> float:
    info = mt5.symbol_info(symbol)
    return (info.point * 10) if info else 0.0001


# ══════════════════════════════════════════════════════════════
#  SWING POINT DETECTION
# ══════════════════════════════════════════════════════════════

def find_swings(df: pd.DataFrame, n: int = 10) -> tuple[list, list]:
    """
    Swing High: bar with the highest High in a ±n bar window.
    Swing Low : bar with the lowest  Low  in a ±n bar window.
    """
    highs, lows = [], []
    ah = df["high"].values
    al = df["low"].values
    idx = df.index

    for i in range(n, len(df) - n):
        if ah[i] == ah[i - n : i + n + 1].max():
            highs.append({"i": i, "time": str(idx[i]), "price": float(ah[i])})
        if al[i] == al[i - n : i + n + 1].min():
            lows.append({"i": i, "time": str(idx[i]), "price": float(al[i])})

    return highs, lows


# ══════════════════════════════════════════════════════════════
#  MARKET STRUCTURE (HTF)
# ══════════════════════════════════════════════════════════════

def get_market_structure(df: pd.DataFrame) -> tuple[str, list, list]:
    """
    Determine HTF bias: BULLISH | BEARISH | NEUTRAL.

    Logic:
      BULLISH  → HH + HL sequence  OR  BOS above last swing high
      BEARISH  → LH + LL sequence  OR  BOS below last swing low
    """
    n = CONFIG["swing_lookback"]
    sh, sl = find_swings(df, n)

    if len(sh) < 3 or len(sl) < 3:
        return "NEUTRAL", sh, sl

    sh = sorted(sh, key=lambda x: x["i"])
    sl = sorted(sl, key=lambda x: x["i"])

    close = float(df["close"].iloc[-1])
    last_sh = sh[-1]["price"]
    last_sl = sl[-1]["price"]

    # Break of Structure
    bos_bull = close > last_sh
    bos_bear = close < last_sl

    # Pattern: last 3 swing points
    r_sh, r_sl = sh[-3:], sl[-3:]
    hh = all(r_sh[i+1]["price"] > r_sh[i]["price"] for i in range(2))
    hl = all(r_sl[i+1]["price"] > r_sl[i]["price"] for i in range(2))
    lh = all(r_sh[i+1]["price"] < r_sh[i]["price"] for i in range(2))
    ll = all(r_sl[i+1]["price"] < r_sl[i]["price"] for i in range(2))

    if bos_bull or (hh and hl):
        return "BULLISH", sh, sl
    if bos_bear or (lh and ll):
        return "BEARISH", sh, sl
    return "NEUTRAL", sh, sl


# ══════════════════════════════════════════════════════════════
#  ORDER BLOCK DETECTION
# ══════════════════════════════════════════════════════════════

def find_order_blocks(df: pd.DataFrame, bias: str) -> list[dict]:
    """
    Bullish OB : Last bearish candle before a strong bullish impulse.
    Bearish OB : Last bullish candle before a strong bearish impulse.
    Unmitigated: price has not yet closed back through the OB.
    """
    obs = []
    lookback = CONFIG["ob_lookback"]
    sub = df.iloc[-lookback:].reset_index()
    current = float(df["close"].iloc[-1])

    for i in range(2, len(sub) - 4):
        c = sub.iloc[i]
        body = abs(c["close"] - c["open"])
        rng  = c["high"] - c["low"]
        if rng == 0 or body / rng < 0.25:
            continue  # Skip dojis

        future = sub.iloc[i+1 : i+5]

        # Bullish OB
        if bias in ("BULLISH", "NEUTRAL") and c["close"] < c["open"]:
            momentum = (future["close"].max() - c["high"]) / rng
            if momentum > 0.5:
                obs.append({
                    "type":      "BULLISH_OB",
                    "time":      str(sub.iloc[i]["time"]),
                    "high":      round(float(c["high"]), 5),
                    "low":       round(float(c["low"]),  5),
                    "mid":       round((float(c["high"]) + float(c["low"])) / 2, 5),
                    "mitigated": current < float(c["low"]),
                })

        # Bearish OB
        if bias in ("BEARISH", "NEUTRAL") and c["close"] > c["open"]:
            momentum = (c["low"] - future["close"].min()) / rng
            if momentum > 0.5:
                obs.append({
                    "type":      "BEARISH_OB",
                    "time":      str(sub.iloc[i]["time"]),
                    "high":      round(float(c["high"]), 5),
                    "low":       round(float(c["low"]),  5),
                    "mid":       round((float(c["high"]) + float(c["low"])) / 2, 5),
                    "mitigated": current > float(c["high"]),
                })

    return [ob for ob in obs if not ob["mitigated"]]


# ══════════════════════════════════════════════════════════════
#  FAIR VALUE GAP (FVG / IMBALANCE)
# ══════════════════════════════════════════════════════════════

def find_fvg(df: pd.DataFrame) -> list[dict]:
    """
    3-candle imbalance pattern.
    Bullish FVG : candle[i-1].high < candle[i+1].low
    Bearish FVG : candle[i-1].low  > candle[i+1].high
    Unfilled only (price hasn't retraced back through).
    """
    fvgs = []
    pip     = get_pip_size(CONFIG["symbol"])
    min_sz  = CONFIG["fvg_min_pips"] * pip
    lookback = 100
    sub     = df.iloc[-lookback:]
    current = float(df["close"].iloc[-1])

    for i in range(1, len(sub) - 1):
        prev = sub.iloc[i - 1]
        nxt  = sub.iloc[i + 1]

        # Bullish FVG
        top, bottom = float(nxt["low"]), float(prev["high"])
        if top > bottom and (top - bottom) >= min_sz:
            fvgs.append({
                "type":      "BULLISH_FVG",
                "time":      str(sub.index[i]),
                "top":       round(top, 5),
                "bottom":    round(bottom, 5),
                "mid":       round((top + bottom) / 2, 5),
                "size_pips": round((top - bottom) / pip, 1),
                "filled":    current < bottom,
            })

        # Bearish FVG
        top, bottom = float(prev["low"]), float(nxt["high"])
        if top > bottom and (top - bottom) >= min_sz:
            fvgs.append({
                "type":      "BEARISH_FVG",
                "time":      str(sub.index[i]),
                "top":       round(top, 5),
                "bottom":    round(bottom, 5),
                "mid":       round((top + bottom) / 2, 5),
                "size_pips": round((top - bottom) / pip, 1),
                "filled":    current > top,
            })

    return [f for f in fvgs if not f["filled"]]


# ══════════════════════════════════════════════════════════════
#  SUPPORT & RESISTANCE LEVELS
# ══════════════════════════════════════════════════════════════

def find_sr_levels(df: pd.DataFrame) -> list[dict]:
    """
    Cluster swing highs/lows into key S&R zones.
    Strength = number of touches within the cluster tolerance.
    """
    pip = get_pip_size(CONFIG["symbol"])
    tol = 10 * pip   # 10-pip cluster tolerance

    sub = df.iloc[-CONFIG["sr_lookback"]:]
    sh, sl = find_swings(sub, n=7)

    raw = sorted(p["price"] for p in sh + sl)
    if not raw:
        return []

    # Cluster
    clusters: list[list[float]] = []
    group = [raw[0]]
    for p in raw[1:]:
        if p - group[0] <= tol:
            group.append(p)
        else:
            clusters.append(group)
            group = [p]
    clusters.append(group)

    current = float(df["close"].iloc[-1])
    levels  = []
    for grp in clusters:
        lvl = float(np.mean(grp))
        levels.append({
            "price":     round(lvl, 5),
            "type":      "RESISTANCE" if lvl > current else "SUPPORT",
            "strength":  len(grp),
            "dist_pips": round(abs(lvl - current) / pip, 1),
        })

    return sorted(levels, key=lambda x: x["dist_pips"])[:10]


# ══════════════════════════════════════════════════════════════
#  BREAKER BLOCKS
# ══════════════════════════════════════════════════════════════

def find_breaker_blocks(df: pd.DataFrame, obs: list[dict]) -> list[dict]:
    """
    An Order Block that price has closed through — it flips polarity.
    Bullish OB violated by close below → becomes Bearish Breaker (resistance).
    Bearish OB violated by close above → becomes Bullish Breaker (support).
    """
    current = float(df["close"].iloc[-1])
    breakers = []
    lookback = CONFIG["ob_lookback"]
    sub = df.iloc[-lookback:]
    for ob in obs:
        oh, ol = ob["high"], ob["low"]
        if ob["type"] == "BULLISH_OB" and current < ol:
            breakers.append({**ob, "type": "BEARISH_BREAKER"})
        elif ob["type"] == "BEARISH_OB" and current > oh:
            breakers.append({**ob, "type": "BULLISH_BREAKER"})
    return breakers


# ══════════════════════════════════════════════════════════════
#  BALANCED PRICE RANGE (BPR)
# ══════════════════════════════════════════════════════════════

def find_balanced_price_range(fvgs: list[dict]) -> list[dict]:
    """
    BPR = the overlapping zone between a Bullish FVG and a Bearish FVG.
    Price is algorithmically balanced inside this zone — strong reaction area.
    """
    bull_fvgs = [f for f in fvgs if f["type"] == "BULLISH_FVG"]
    bear_fvgs = [f for f in fvgs if f["type"] == "BEARISH_FVG"]
    bprs = []
    for bf in bull_fvgs:
        for sf in bear_fvgs:
            lo = max(bf["bottom"], sf["bottom"])
            hi = min(bf["top"],    sf["top"])
            if hi > lo:
                bprs.append({
                    "type":   "BPR",
                    "top":    round(hi, 5),
                    "bottom": round(lo, 5),
                    "mid":    round((hi + lo) / 2, 5),
                })
    return bprs


# ══════════════════════════════════════════════════════════════
#  REJECTION BLOCK
# ══════════════════════════════════════════════════════════════

def find_rejection_blocks(df: pd.DataFrame, bias: str) -> list[dict]:
    """
    Long-wick candle at a key level indicating sharp institutional rejection.
    Bullish: lower wick > 60 % of total range, small body at top.
    Bearish: upper wick > 60 % of total range, small body at bottom.
    """
    blocks = []
    sub = df.iloc[-50:]
    for i in range(len(sub) - 1):
        c = sub.iloc[i]
        o, h, l, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
        rng = h - l
        if rng == 0:
            continue
        body        = abs(cl - o)
        upper_wick  = h - max(o, cl)
        lower_wick  = min(o, cl) - l

        if bias in ("BULLISH", "NEUTRAL") and lower_wick / rng > 0.60 and body / rng < 0.30:
            blocks.append({
                "type": "BULLISH_REJECTION", "time": str(sub.index[i]),
                "high": round(h, 5), "low": round(l, 5), "mid": round((h + l) / 2, 5),
                "wick_pct": round(lower_wick / rng * 100, 0),
            })
        elif bias in ("BEARISH", "NEUTRAL") and upper_wick / rng > 0.60 and body / rng < 0.30:
            blocks.append({
                "type": "BEARISH_REJECTION", "time": str(sub.index[i]),
                "high": round(h, 5), "low": round(l, 5), "mid": round((h + l) / 2, 5),
                "wick_pct": round(upper_wick / rng * 100, 0),
            })
    return blocks


# ══════════════════════════════════════════════════════════════
#  SUSPENSION BLOCK
# ══════════════════════════════════════════════════════════════

def find_suspension_blocks(df: pd.DataFrame) -> list[dict]:
    """
    A single candle suspended between a volume imbalance above AND below it.
    Bullish SB : c.low > prev.high AND nxt.low > c.high (gaps on both sides, price floated up).
    Bearish SB : prev.low > c.high AND c.low > nxt.high (gaps on both sides, price floated down).
    """
    pip = get_pip_size(CONFIG["symbol"])
    min_gap = 2 * pip
    blocks = []
    sub = df.iloc[-80:]
    for i in range(1, len(sub) - 1):
        prev, c, nxt = sub.iloc[i-1], sub.iloc[i], sub.iloc[i+1]
        ph, pl = float(prev["high"]), float(prev["low"])
        ch, cl = float(c["high"]),    float(c["low"])
        nh, nl = float(nxt["high"]),  float(nxt["low"])

        gap_below = cl - ph   # gap between prev.high and c.low  (bullish)
        gap_above = nl - ch   # gap between c.high and nxt.low   (bullish)
        gap_below_b = pl - ch # gap between c.high and prev.low  (bearish)
        gap_above_b = cl - nh # gap between nxt.high and c.low   (bearish)

        if gap_below >= min_gap and gap_above >= min_gap:
            blocks.append({
                "type": "BULLISH_SUSPENSION", "time": str(sub.index[i]),
                "high": round(ch, 5), "low": round(cl, 5), "mid": round((ch+cl)/2, 5),
                "gap_below_pips": round(gap_below/pip, 1), "gap_above_pips": round(gap_above/pip, 1),
            })
        elif gap_below_b >= min_gap and gap_above_b >= min_gap:
            blocks.append({
                "type": "BEARISH_SUSPENSION", "time": str(sub.index[i]),
                "high": round(ch, 5), "low": round(cl, 5), "mid": round((ch+cl)/2, 5),
                "gap_below_pips": round(gap_below_b/pip, 1), "gap_above_pips": round(gap_above_b/pip, 1),
            })
    return blocks


# ══════════════════════════════════════════════════════════════
#  PROPULSION BLOCK
# ══════════════════════════════════════════════════════════════

def find_propulsion_blocks(df: pd.DataFrame, bias: str) -> list[dict]:
    """
    Three consecutive same-direction candles launching strongly from a level.
    Acts as a continuation zone on retest — institutional momentum signature.
    """
    pip = get_pip_size(CONFIG["symbol"])
    blocks = []
    sub = df.iloc[-60:]
    for i in range(2, len(sub) - 1):
        c0, c1, c2 = sub.iloc[i-2], sub.iloc[i-1], sub.iloc[i]
        bull = all(float(c["close"]) > float(c["open"]) for c in (c0, c1, c2))
        bear = all(float(c["close"]) < float(c["open"]) for c in (c0, c1, c2))

        if bias in ("BULLISH", "NEUTRAL") and bull:
            move = (float(c2["close"]) - float(c0["open"])) / pip
            if move >= 10:
                blocks.append({
                    "type": "BULLISH_PROPULSION", "time": str(sub.index[i]),
                    "high": round(float(c2["high"]), 5), "low": round(float(c0["low"]), 5),
                    "mid":  round((float(c2["high"]) + float(c0["low"]))/2, 5),
                    "move_pips": round(move, 1),
                })
        elif bias in ("BEARISH", "NEUTRAL") and bear:
            move = (float(c0["open"]) - float(c2["close"])) / pip
            if move >= 10:
                blocks.append({
                    "type": "BEARISH_PROPULSION", "time": str(sub.index[i]),
                    "high": round(float(c0["high"]), 5), "low": round(float(c2["low"]), 5),
                    "mid":  round((float(c0["high"]) + float(c2["low"]))/2, 5),
                    "move_pips": round(move, 1),
                })
    return blocks


# ══════════════════════════════════════════════════════════════
#  VACUUM BLOCK
# ══════════════════════════════════════════════════════════════

def find_vacuum_blocks(df: pd.DataFrame) -> list[dict]:
    """
    Macro-scale FVG (large price void). Price moved so fast almost no
    trading occurred — acts as a draw-on-liquidity TARGET, not an entry zone.
    """
    pip = get_pip_size(CONFIG["symbol"])
    min_sz = CONFIG.get("vacuum_min_pips", 50.0) * pip
    current = float(df["close"].iloc[-1])
    vbs = []
    for i in range(1, len(df) - 1):
        prev, nxt = df.iloc[i-1], df.iloc[i+1]
        # Bullish vacuum
        top, bot = float(nxt["low"]), float(prev["high"])
        if top > bot and (top - bot) >= min_sz:
            vbs.append({
                "type": "BULLISH_VACUUM", "time": str(df.index[i]),
                "top": round(top,5), "bottom": round(bot,5), "mid": round((top+bot)/2,5),
                "size_pips": round((top-bot)/pip, 1), "filled": current < bot,
            })
        # Bearish vacuum
        top, bot = float(prev["low"]), float(nxt["high"])
        if top > bot and (top - bot) >= min_sz:
            vbs.append({
                "type": "BEARISH_VACUUM", "time": str(df.index[i]),
                "top": round(top,5), "bottom": round(bot,5), "mid": round((top+bot)/2,5),
                "size_pips": round((top-bot)/pip, 1), "filled": current > top,
            })
    return [v for v in vbs if not v["filled"]][-10:]


# ══════════════════════════════════════════════════════════════
#  INDUCEMENT
# ══════════════════════════════════════════════════════════════

def find_inducement(df: pd.DataFrame, sh: list, sl: list, bias: str) -> list[dict]:
    """
    A small liquidity pool (swing point) placed just before the real OB —
    designed to lure retail traders in the wrong direction.
    Detection: a recent swing within 30 pips of current price.
    """
    pip = get_pip_size(CONFIG["symbol"])
    current = float(df["close"].iloc[-1])
    recent_threshold = max(0, len(df) - 30)
    result = []

    if bias == "BULLISH":
        for s in (sl or []):
            if s["i"] >= recent_threshold:
                dist = abs(current - s["price"]) / pip
                if dist <= 30:
                    result.append({"type": "BULLISH_INDUCEMENT",
                                   "price": round(s["price"], 5), "dist_pips": round(dist, 1)})
    elif bias == "BEARISH":
        for s in (sh or []):
            if s["i"] >= recent_threshold:
                dist = abs(current - s["price"]) / pip
                if dist <= 30:
                    result.append({"type": "BEARISH_INDUCEMENT",
                                   "price": round(s["price"], 5), "dist_pips": round(dist, 1)})
    return result[-3:]


# ══════════════════════════════════════════════════════════════
#  LIQUIDITY SWEEPS (BSL / SSL)
# ══════════════════════════════════════════════════════════════

def find_liquidity_sweeps(df: pd.DataFrame, sh: list, sl: list) -> list[dict]:
    """
    A sweep occurs when price spikes beyond a swing high/low (stop hunt)
    then closes back inside — confirming smart-money absorption.
    """
    sweeps = []
    sub = df.iloc[-30:]

    if sh:
        lvl = sh[-1]["price"]
        for i in range(len(sub)):
            c = sub.iloc[i]
            if float(c["high"]) > lvl and float(c["close"]) < lvl:
                sweeps.append({"type": "BSL_SWEEP", "time": str(sub.index[i]),
                                "level": round(lvl, 5), "bars_ago": len(sub) - i})
    if sl:
        lvl = sl[-1]["price"]
        for i in range(len(sub)):
            c = sub.iloc[i]
            if float(c["low"]) < lvl and float(c["close"]) > lvl:
                sweeps.append({"type": "SSL_SWEEP", "time": str(sub.index[i]),
                                "level": round(lvl, 5), "bars_ago": len(sub) - i})

    return sorted(sweeps, key=lambda x: x["bars_ago"])[:5]


# ══════════════════════════════════════════════════════════════
#  TURTLE SOUP
# ══════════════════════════════════════════════════════════════

def find_turtle_soup(df: pd.DataFrame, sh: list, sl: list) -> list[dict]:
    """
    False breakout of a prior swing high/low: price spikes through,
    then reverses sharply back inside — a classic ICT stop-hunt reversal.
    """
    setups = []
    sub = df.iloc[-15:]

    if len(sh) >= 2:
        target = sh[-2]["price"]
        for i in range(len(sub) - 1):
            c = sub.iloc[i]
            if float(c["high"]) > target and float(c["close"]) < target:
                setups.append({"type": "TURTLE_SOUP_SELL", "swept": round(target,5),
                                "high": round(float(c["high"]),5), "bars_ago": len(sub)-i})
    if len(sl) >= 2:
        target = sl[-2]["price"]
        for i in range(len(sub) - 1):
            c = sub.iloc[i]
            if float(c["low"]) < target and float(c["close"]) > target:
                setups.append({"type": "TURTLE_SOUP_BUY", "swept": round(target,5),
                                "low": round(float(c["low"]),5), "bars_ago": len(sub)-i})

    return sorted(setups, key=lambda x: x["bars_ago"])[:3]


# ══════════════════════════════════════════════════════════════
#  BOS / CHoCH CLASSIFICATION
# ══════════════════════════════════════════════════════════════

def find_bos_choch(sh: list, sl: list, bias: str) -> list[dict]:
    """
    BOS  = Break of Structure — confirms trend continuation.
    CHoCH = Change of Character — first sign of potential reversal.
    """
    events = []
    if len(sh) < 2 or len(sl) < 2:
        return events
    sh_s = sorted(sh, key=lambda x: x["i"])
    sl_s = sorted(sl, key=lambda x: x["i"])

    if bias == "BULLISH":
        if sh_s[-1]["price"] > sh_s[-2]["price"]:
            events.append({"type": "BOS_BULLISH",  "price": round(sh_s[-1]["price"],5)})
        if sl_s[-1]["price"] < sl_s[-2]["price"]:
            events.append({"type": "CHOCH_BEARISH", "price": round(sl_s[-1]["price"],5)})
    elif bias == "BEARISH":
        if sl_s[-1]["price"] < sl_s[-2]["price"]:
            events.append({"type": "BOS_BEARISH",  "price": round(sl_s[-1]["price"],5)})
        if sh_s[-1]["price"] > sh_s[-2]["price"]:
            events.append({"type": "CHOCH_BULLISH", "price": round(sh_s[-1]["price"],5)})
    return events


# ══════════════════════════════════════════════════════════════
#  CISD — CHANGE IN STATE OF DELIVERY
# ══════════════════════════════════════════════════════════════

def detect_cisd(df: pd.DataFrame, bias: str) -> bool:
    """
    CISD: a candle closes through the body of the prior opposite-direction
    candle — the algorithm has shifted its delivery state.
    Returns True if confirmed within the last 5 bars.
    """
    sub = df.iloc[-8:]
    for i in range(1, len(sub)):
        curr = sub.iloc[i]
        prev = sub.iloc[i-1]
        c_bull = float(curr["close"]) > float(curr["open"])
        p_bull = float(prev["close"]) > float(prev["open"])
        if bias == "BULLISH" and c_bull and not p_bull:
            if float(curr["close"]) > max(float(prev["open"]), float(prev["close"])):
                return True
        elif bias == "BEARISH" and not c_bull and p_bull:
            if float(curr["close"]) < min(float(prev["open"]), float(prev["close"])):
                return True
    return False


# ══════════════════════════════════════════════════════════════
#  PREMIUM / DISCOUNT ZONE
# ══════════════════════════════════════════════════════════════

def get_premium_discount(sh: list, sl: list, current: float) -> str:
    """
    Fibonacci equilibrium across the last dealing range:
      > 62 % → PREMIUM   (prefer shorts)
      < 38 % → DISCOUNT  (prefer longs)
      38–62 % → EQUILIBRIUM
    """
    if not sh or not sl:
        return "UNKNOWN"
    hi  = max(s["price"] for s in sh[-3:])
    lo  = min(s["price"] for s in sl[-3:])
    rng = hi - lo
    if rng == 0:
        return "UNKNOWN"
    fib = (current - lo) / rng
    if fib > 0.62:
        return "PREMIUM"
    if fib < 0.38:
        return "DISCOUNT"
    return "EQUILIBRIUM"


# ══════════════════════════════════════════════════════════════
#  IPDA LOOKBACK RANGES
# ══════════════════════════════════════════════════════════════

def get_ipda_ranges(df_daily: pd.DataFrame) -> dict:
    """
    ICT's Interbank Price Delivery Algorithm uses 20 / 40 / 60 trading-day
    reference windows. Returns the high/low of each window.
    """
    result = {}
    for days in CONFIG.get("ipda_lookback", [20, 40, 60]):
        sub = df_daily.iloc[-days:] if len(df_daily) >= days else df_daily
        result[f"{days}d"] = {
            "high": round(float(sub["high"].max()), 5),
            "low":  round(float(sub["low"].min()),  5),
        }
    return result


# ══════════════════════════════════════════════════════════════
#  NWOG / NDOG — OPENING GAPS
# ══════════════════════════════════════════════════════════════

def find_opening_gaps(df: pd.DataFrame) -> list[dict]:
    """
    NDOG = New Day Opening Gap (5 PM → 6 PM NY daily session gap).
    NWOG = New Week Opening Gap (Friday close → Monday open).
    Detected as any open-to-previous-close gap ≥ 2 pips on H1 bars.
    Gaps act as price magnets until filled.
    """
    pip = get_pip_size(CONFIG["symbol"])
    min_gap = 2 * pip
    gaps = []
    current = float(df["close"].iloc[-1])
    sub = df.iloc[-14:]

    for i in range(1, len(sub)):
        prev_close = float(sub.iloc[i-1]["close"])
        curr_open  = float(sub.iloc[i]["open"])
        gap_size   = curr_open - prev_close
        if abs(gap_size) < min_gap:
            continue
        is_nwog = abs(gap_size) > 10 * pip
        kind    = "NWOG" if is_nwog else "NDOG"
        dirn    = "BULLISH" if gap_size < 0 else "BEARISH"
        top     = max(prev_close, curr_open)
        bot     = min(prev_close, curr_open)
        filled  = (gap_size > 0 and current < bot) or (gap_size < 0 and current > top)
        if not filled:
            gaps.append({
                "type": f"{dirn}_{kind}", "kind": kind,
                "top": round(top,5), "bottom": round(bot,5), "mid": round((top+bot)/2,5),
                "size_pips": round(abs(gap_size)/pip, 1), "time": str(sub.index[i]),
            })
    return gaps


# ══════════════════════════════════════════════════════════════
#  SILVER BULLET — TIME CHECK
# ══════════════════════════════════════════════════════════════

def in_silver_bullet() -> tuple[bool, str | None]:
    """
    Silver Bullet windows (UTC, non-DST / EST+5 offset):
      08:00–09:00  (London 3–4 AM NY)
      15:00–16:00  (AM 10–11 AM NY)
      19:00–20:00  (PM 2–3 PM NY)
    """
    h = datetime.now(timezone.utc).hour
    for w in CONFIG.get("silver_bullet_windows", []):
        if w["start_utc"] <= h < w["end_utc"]:
            return True, w["name"]
    return False, None


# ══════════════════════════════════════════════════════════════
#  ICT MACROS — TIME CHECK
# ══════════════════════════════════════════════════════════════

def in_ict_macro() -> tuple[bool, str | None]:
    """
    ICT Macro windows — precise 15–20 min algorithmic activity spikes.
    Config stores start/end as minutes since midnight UTC.
    """
    now = datetime.now(timezone.utc)
    t   = now.hour * 60 + now.minute
    for mac in CONFIG.get("macro_windows", []):
        if mac["start"] <= t < mac["end"]:
            return True, mac["name"]
    return False, None


# ══════════════════════════════════════════════════════════════
#  POWER OF 3 — AMD STRUCTURE
# ══════════════════════════════════════════════════════════════

def detect_power_of_3(df: pd.DataFrame, bias: str) -> str:
    """
    Accumulation → Manipulation → Distribution.
    Reads the last 12 bars as a session:
      - First 4 bars : tight range → Accumulation
      - Middle 4 bars: spike in one direction → Manipulation
      - Last 4 bars  : strong directional move → Distribution
    Returns the current phase: ACCUMULATION | MANIPULATION | DISTRIBUTION | UNKNOWN
    """
    if len(df) < 12:
        return "UNKNOWN"
    sub    = df.iloc[-12:]
    pip    = get_pip_size(CONFIG["symbol"])
    highs  = sub["high"].values.astype(float)
    lows   = sub["low"].values.astype(float)
    opens  = sub["open"].values.astype(float)
    closes = sub["close"].values.astype(float)

    accum_range = (highs[:4].max() - lows[:4].min()) / pip
    accum_hi    = highs[:4].max()
    accum_lo    = lows[:4].min()
    manip_hi    = highs[4:8].max()
    manip_lo    = lows[4:8].min()
    manip_up    = manip_hi > accum_hi + 5 * pip
    manip_down  = manip_lo < accum_lo - 5 * pip
    dist_move   = abs(closes[-1] - opens[8]) / pip

    if accum_range < 20:
        if dist_move > 15 and ((bias == "BULLISH" and manip_down) or (bias == "BEARISH" and manip_up)):
            return "DISTRIBUTION"
        if manip_up or manip_down:
            return "MANIPULATION"
        return "ACCUMULATION"
    return "UNKNOWN"


# ══════════════════════════════════════════════════════════════
#  SMT DIVERGENCE
# ══════════════════════════════════════════════════════════════

def detect_smt_divergence(symbol: str, df: pd.DataFrame, bias: str) -> bool:
    """
    Smart Money Tool: two correlated instruments diverge at a swing —
    one makes a new extreme while its correlated pair fails to confirm.
    EURUSD new high + GBPUSD lower high → bearish SMT (institutions selling).
    """
    corr = CONFIG.get("smt_pairs", {}).get(symbol)
    if not corr or not state["connected"]:
        return False
    try:
        df2 = get_ohlcv(corr, CONFIG["ltf"], 50)
        if df2 is None or len(df2) < 10:
            return False
        n = max(3, CONFIG["swing_lookback"] // 2)
        sh1, sl1 = find_swings(df.iloc[-50:],  n)
        sh2, sl2 = find_swings(df2.iloc[-50:], n)
        if bias == "BEARISH" and len(sh1) >= 2 and len(sh2) >= 2:
            if sh1[-1]["price"] > sh1[-2]["price"] and sh2[-1]["price"] < sh2[-2]["price"]:
                return True
        if bias == "BULLISH" and len(sl1) >= 2 and len(sl2) >= 2:
            if sl1[-1]["price"] < sl1[-2]["price"] and sl2[-1]["price"] > sl2[-2]["price"]:
                return True
    except Exception:
        pass
    return False


# ══════════════════════════════════════════════════════════════
#  KILL ZONE
# ══════════════════════════════════════════════════════════════

def in_kill_zone() -> tuple[bool, str | None]:
    h = datetime.now(timezone.utc).hour
    for kz in CONFIG["kill_zones"]:
        if kz["start"] <= h < kz["end"]:
            return True, kz["name"]
    return False, None


def kill_zone_status() -> list[dict]:
    now  = datetime.now(timezone.utc)
    h, m = now.hour, now.minute
    result = []
    for kz in CONFIG["kill_zones"]:
        active = kz["start"] <= h < kz["end"]
        if active:
            mins_left = (kz["end"] - h) * 60 - m
            result.append({**kz, "active": True, "mins_left": int(mins_left)})
        else:
            if h < kz["start"]:
                mins_until = (kz["start"] - h) * 60 - m
            else:
                mins_until = (24 - h + kz["start"]) * 60 - m
            result.append({**kz, "active": False, "mins_until": int(mins_until)})
    return result


# ══════════════════════════════════════════════════════════════
#  SIGNAL GENERATION  (ICT/SMC confluence scoring — full suite)
# ══════════════════════════════════════════════════════════════

def generate_signals(symbol: str, bias: str, ctx: dict) -> list[dict]:
    """
    Score each unmitigated Order Block against up to 22 ICT/SMC factors.
    ctx keys (produced by bot_loop):
      obs, fvgs, sr, sh, sl, breakers, bprs, rej_blocks, suspensions,
      propulsions, vacuums, inducements, sweeps, turtle_soups, bos_choch,
      cisd, pd_zone, ipda, gaps, po3_phase,
      is_kz, kz_name, is_sb, sb_name, is_macro, macro_name
    """
    tick = get_tick(symbol)
    if tick is None:
        return []

    bid, ask = float(tick.bid), float(tick.ask)
    pip = get_pip_size(symbol)
    buf = CONFIG["sl_buffer_pips"] * pip

    # Respect the strategy toggle panel — disabled strategies are skipped entirely
    # (neither hit nor miss), so the confluence score only reflects active checks.
    active_strats = set(CONFIG.get("active_strategies", []))

    obs          = ctx.get("obs", [])
    fvgs         = ctx.get("fvgs", [])
    sr           = ctx.get("sr", [])
    bprs         = ctx.get("bprs", [])
    rej_blocks   = ctx.get("rej_blocks", [])
    sweeps       = ctx.get("sweeps", [])
    turtle_soups = ctx.get("turtle_soups", [])
    bos_choch    = ctx.get("bos_choch", [])
    inducements  = ctx.get("inducements", [])
    gaps         = ctx.get("gaps", [])
    propulsions  = ctx.get("propulsions", [])
    cisd         = ctx.get("cisd", False)
    pd_zone      = ctx.get("pd_zone", "UNKNOWN")
    ipda         = ctx.get("ipda", {})
    po3_phase    = ctx.get("po3_phase", "UNKNOWN")
    is_kz        = ctx.get("is_kz", False)
    kz_name      = ctx.get("kz_name", None)
    is_sb        = ctx.get("is_sb", False)
    sb_name      = ctx.get("sb_name", None)
    is_macro     = ctx.get("is_macro", False)
    macro_name   = ctx.get("macro_name", None)

    signals = []

    for ob in obs[-20:]:
        oh, ol = ob["high"], ob["low"]
        hit: list[str] = []
        miss: list[str] = []

        # ── BULLISH setup ─────────────────────────────────────────────────
        if ob["type"] == "BULLISH_OB" and bias == "BULLISH":
            if not (ol <= ask <= oh):
                continue
            hit.append("HTF Bullish Bias")
            hit.append("Price in Bullish OB")

            # FVG overlap
            if "fvg" in active_strats:
                if any(f["type"] == "BULLISH_FVG" and f["bottom"] <= oh and f["top"] >= ol for f in fvgs):
                    hit.append("Bullish FVG overlap")
                else:
                    miss.append("No FVG confluence")

            # S&R proximity
            if "sr" in active_strats:
                near_sr = [v for v in sr[:6] if v["type"] == "SUPPORT" and v["dist_pips"] < 20]
                if near_sr:
                    hit.append(f"Support @ {near_sr[0]['price']:.5f}")
                else:
                    miss.append("No nearby S&R")

            # Kill Zone
            if "kz" in active_strats:
                if is_kz:
                    hit.append(f"Kill Zone: {kz_name}")
                else:
                    miss.append("Outside kill zone")

            # Silver Bullet
            if "silver" in active_strats:
                if is_sb:
                    hit.append(f"Silver Bullet: {sb_name}")
                else:
                    miss.append("No Silver Bullet")

            # ICT Macro
            if "macros" in active_strats:
                if is_macro:
                    hit.append(f"ICT Macro: {macro_name}")
                else:
                    miss.append("No macro window")

            # Discount zone (ideal for longs)
            if "pd" in active_strats:
                if pd_zone == "DISCOUNT":
                    hit.append("Price in Discount zone")
                elif pd_zone == "PREMIUM":
                    miss.append("Price in Premium (unfavourable for BUY)")
                else:
                    miss.append(f"Price at {pd_zone}")

            # Liquidity sweep (SSL swept → bullish)
            if "liq" in active_strats:
                ssl_sweeps = [s for s in sweeps if s["type"] == "SSL_SWEEP" and s["bars_ago"] <= 10]
                if ssl_sweeps:
                    hit.append(f"SSL swept {ssl_sweeps[0]['bars_ago']} bars ago")
                else:
                    miss.append("No recent SSL sweep")

            # Turtle Soup BUY
            if "turtle" in active_strats:
                if any(t["type"] == "TURTLE_SOUP_BUY" and t["bars_ago"] <= 8 for t in turtle_soups):
                    hit.append("Turtle Soup BUY")
                else:
                    miss.append("No Turtle Soup")

            # BPR nearby
            if "bpr" in active_strats:
                near_bpr = [b for b in bprs if b["bottom"] <= oh and b["top"] >= ol]
                if near_bpr:
                    hit.append(f"BPR @ {near_bpr[0]['mid']:.5f}")
                else:
                    miss.append("No BPR")

            # Opening Gap (NWOG/NDOG) nearby
            if "nwog" in active_strats or "ndog" in active_strats:
                near_gap = [g for g in gaps if g["type"].startswith("BULLISH") and
                            g["bottom"] <= oh + 10*pip and g["top"] >= ol - 10*pip]
                if near_gap:
                    hit.append(f"{near_gap[0]['kind']} nearby ({near_gap[0]['size_pips']} pips)")
                else:
                    miss.append("No opening gap")

            # CISD
            if "cisd" in active_strats:
                if cisd:
                    hit.append("CISD bullish confirmed")
                else:
                    miss.append("No CISD")

            # Power of 3
            if "po3" in active_strats:
                if po3_phase == "DISTRIBUTION":
                    hit.append("AMD: Distribution phase")
                elif po3_phase == "MANIPULATION":
                    miss.append("AMD: Manipulation phase")
                else:
                    miss.append(f"AMD: {po3_phase}")

            # BOS confirmed
            if "bos" in active_strats:
                if any(e["type"] == "BOS_BULLISH" for e in bos_choch):
                    hit.append("BOS confirmed bullish")
                else:
                    miss.append("No BOS")

            # Rejection Block at OB
            if "rejblock" in active_strats:
                near_rej = [r for r in rej_blocks if r["type"] == "BULLISH_REJECTION" and
                            r["low"] <= oh and r["high"] >= ol]
                if near_rej:
                    hit.append(f"Rejection Block ({near_rej[0]['wick_pct']:.0f}% wick)")
                else:
                    miss.append("No Rejection Block")

            # Propulsion block in direction
            if "propulsion" in active_strats:
                if any(p["type"] == "BULLISH_PROPULSION" for p in propulsions):
                    hit.append("Bullish Propulsion Block")
                else:
                    miss.append("No Propulsion Block")

            # IPDA range confluence (price near 20d low)
            if "ipda" in active_strats:
                ipda20 = ipda.get("20d", {})
                if ipda20 and ask <= ipda20.get("low", 0) * 1.001:
                    hit.append("Near IPDA 20d Low")
                else:
                    miss.append("No IPDA confluence")

            # Inducement nearby (cleared)
            if "inducement" in active_strats:
                if not inducements:
                    hit.append("Inducement cleared")
                else:
                    miss.append(f"Inducement unswept @ {inducements[0]['price']:.5f}")

            if len(hit) >= CONFIG["min_confluence"]:
                sl_price = ol - buf
                tp_price = ask + (ask - sl_price) * CONFIG["min_rr"]
                signals.append(_build_signal("BUY", symbol, ask, sl_price, tp_price,
                                            hit, miss, ob, is_kz, pip))
            else:
                miss.append(f"Score {len(hit)} < min {CONFIG['min_confluence']}")

        # ── BEARISH setup ─────────────────────────────────────────────────
        elif ob["type"] == "BEARISH_OB" and bias == "BEARISH":
            if not (ol <= bid <= oh):
                continue
            hit.append("HTF Bearish Bias")
            hit.append("Price in Bearish OB")

            # FVG overlap
            if "fvg" in active_strats:
                if any(f["type"] == "BEARISH_FVG" and f["bottom"] <= oh and f["top"] >= ol for f in fvgs):
                    hit.append("Bearish FVG overlap")
                else:
                    miss.append("No FVG confluence")

            # S&R proximity
            if "sr" in active_strats:
                near_sr = [v for v in sr[:6] if v["type"] == "RESISTANCE" and v["dist_pips"] < 20]
                if near_sr:
                    hit.append(f"Resistance @ {near_sr[0]['price']:.5f}")
                else:
                    miss.append("No nearby S&R")

            # Kill Zone
            if "kz" in active_strats:
                if is_kz:
                    hit.append(f"Kill Zone: {kz_name}")
                else:
                    miss.append("Outside kill zone")

            # Silver Bullet
            if "silver" in active_strats:
                if is_sb:
                    hit.append(f"Silver Bullet: {sb_name}")
                else:
                    miss.append("No Silver Bullet")

            # ICT Macro
            if "macros" in active_strats:
                if is_macro:
                    hit.append(f"ICT Macro: {macro_name}")
                else:
                    miss.append("No macro window")

            # Premium zone (ideal for shorts)
            if "pd" in active_strats:
                if pd_zone == "PREMIUM":
                    hit.append("Price in Premium zone")
                elif pd_zone == "DISCOUNT":
                    miss.append("Price in Discount (unfavourable for SELL)")
                else:
                    miss.append(f"Price at {pd_zone}")

            # Liquidity sweep (BSL swept → bearish)
            if "liq" in active_strats:
                bsl_sweeps = [s for s in sweeps if s["type"] == "BSL_SWEEP" and s["bars_ago"] <= 10]
                if bsl_sweeps:
                    hit.append(f"BSL swept {bsl_sweeps[0]['bars_ago']} bars ago")
                else:
                    miss.append("No recent BSL sweep")

            # Turtle Soup SELL
            if "turtle" in active_strats:
                if any(t["type"] == "TURTLE_SOUP_SELL" and t["bars_ago"] <= 8 for t in turtle_soups):
                    hit.append("Turtle Soup SELL")
                else:
                    miss.append("No Turtle Soup")

            # BPR nearby
            if "bpr" in active_strats:
                near_bpr = [b for b in bprs if b["bottom"] <= oh and b["top"] >= ol]
                if near_bpr:
                    hit.append(f"BPR @ {near_bpr[0]['mid']:.5f}")
                else:
                    miss.append("No BPR")

            # Opening Gap nearby
            if "nwog" in active_strats or "ndog" in active_strats:
                near_gap = [g for g in gaps if g["type"].startswith("BEARISH") and
                            g["bottom"] <= oh + 10*pip and g["top"] >= ol - 10*pip]
                if near_gap:
                    hit.append(f"{near_gap[0]['kind']} nearby ({near_gap[0]['size_pips']} pips)")
                else:
                    miss.append("No opening gap")

            # CISD
            if "cisd" in active_strats:
                if cisd:
                    hit.append("CISD bearish confirmed")
                else:
                    miss.append("No CISD")

            # Power of 3
            if "po3" in active_strats:
                if po3_phase == "DISTRIBUTION":
                    hit.append("AMD: Distribution phase")
                elif po3_phase == "MANIPULATION":
                    miss.append("AMD: Manipulation phase")
                else:
                    miss.append(f"AMD: {po3_phase}")

            # BOS confirmed
            if "bos" in active_strats:
                if any(e["type"] == "BOS_BEARISH" for e in bos_choch):
                    hit.append("BOS confirmed bearish")
                else:
                    miss.append("No BOS")

            # Rejection Block at OB
            if "rejblock" in active_strats:
                near_rej = [r for r in rej_blocks if r["type"] == "BEARISH_REJECTION" and
                            r["low"] <= oh and r["high"] >= ol]
                if near_rej:
                    hit.append(f"Rejection Block ({near_rej[0]['wick_pct']:.0f}% wick)")
                else:
                    miss.append("No Rejection Block")

            # Propulsion block in direction
            if "propulsion" in active_strats:
                if any(p["type"] == "BEARISH_PROPULSION" for p in propulsions):
                    hit.append("Bearish Propulsion Block")
                else:
                    miss.append("No Propulsion Block")

            # IPDA range confluence (price near 20d high)
            if "ipda" in active_strats:
                ipda20 = ipda.get("20d", {})
                if ipda20 and bid >= ipda20.get("high", float("inf")) * 0.999:
                    hit.append("Near IPDA 20d High")
                else:
                    miss.append("No IPDA confluence")

            # Inducement nearby (cleared)
            if "inducement" in active_strats:
                if not inducements:
                    hit.append("Inducement cleared")
                else:
                    miss.append(f"Inducement unswept @ {inducements[0]['price']:.5f}")

            if len(hit) >= CONFIG["min_confluence"]:
                sl_price = oh + buf
                tp_price = bid - (sl_price - bid) * CONFIG["min_rr"]
                signals.append(_build_signal("SELL", symbol, bid, sl_price, tp_price,
                                            hit, miss, ob, is_kz, pip))
            else:
                miss.append(f"Score {len(hit)} < min {CONFIG['min_confluence']}")

    return sorted(signals, key=lambda x: len(x["confluence"]["hit"]), reverse=True)


def _build_signal(
    direction: str, symbol: str,
    entry: float, sl: float, tp: float,
    hit: list[str], miss: list[str],
    ob: dict, is_kz: bool, pip: float,
) -> dict:
    return {
        "id":         f"{direction}_{int(time.time())}",
        "symbol":     symbol,
        "direction":  direction,
        "entry":      round(entry, 5),
        "sl":         round(sl, 5),
        "tp":         round(tp, 5),
        "risk_pips":  round(abs(entry - sl) / pip, 1),
        "rr":         CONFIG["min_rr"],
        "score":      len(hit),
        "confluence": {
            "hit":  hit,
            "miss": miss,
        },
        "ob":         ob,
        "in_kz":      is_kz,
        "time":       datetime.now(timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════
#  RISK-BASED LOT SIZING
# ══════════════════════════════════════════════════════════════

def calc_lot_size(symbol: str, entry: float, sl: float) -> float:
    acct = mt5.account_info()
    info = mt5.symbol_info(symbol)
    if not acct or not info:
        return 0.01

    risk_amt  = acct.balance * (CONFIG["risk_pct"] / 100)
    sl_pips   = abs(entry - sl) / (10 * info.point)
    pip_value = info.trade_tick_value * (info.trade_tick_size / info.point) * 10

    if sl_pips == 0 or pip_value == 0:
        return info.volume_min

    raw  = risk_amt / (sl_pips * pip_value)
    lots = round(raw / info.volume_step) * info.volume_step
    lots = max(info.volume_min, min(info.volume_max, lots))
    return round(lots, 2)


# ══════════════════════════════════════════════════════════════
#  TRADE EXECUTION
# ══════════════════════════════════════════════════════════════

def get_filling_mode(symbol: str) -> int:
    """
    Return the correct ORDER_FILLING_* constant for the symbol.

    MT5 symbol_info.filling_mode is a bitmask:
      bit 0 (value 1) → FOK supported
      bit 1 (value 2) → IOC supported
      value 0          → only RETURN is supported (common on ECN/STP brokers)

    Using an unsupported mode causes error 10030 (TRADE_RETCODE_INVALID_FILL).
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        return mt5.ORDER_FILLING_RETURN  # safest fallback
    fm = info.filling_mode
    if fm == 0:
        return mt5.ORDER_FILLING_RETURN
    if fm & 2:   # IOC bit
        return mt5.ORDER_FILLING_IOC
    if fm & 1:   # FOK bit
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def place_order(sig: dict) -> dict | None:
    symbol = sig["symbol"]
    info   = mt5.symbol_info(symbol)

    if not info or not info.visible:
        mt5.symbol_select(symbol, True)
        time.sleep(0.5)
        info = mt5.symbol_info(symbol)
    if not info:
        log.error(f"Symbol {symbol} not found")
        return None

    if info.spread > CONFIG["max_spread"]:
        log.warning(f"Spread {info.spread} > {CONFIG['max_spread']} pts. Skipping.")
        return None

    positions = mt5.positions_get(symbol=symbol) or []
    bot_pos   = [p for p in positions if p.magic == CONFIG["magic_number"]]
    if len(bot_pos) >= CONFIG["max_trades"]:
        log.info(f"Max trades ({CONFIG['max_trades']}) reached. Skipping.")
        return None

    tick  = get_tick(symbol)
    if not tick:
        return None
    price = tick.ask if sig["direction"] == "BUY" else tick.bid
    lots  = calc_lot_size(symbol, price, sig["sl"])

    req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       lots,
        "type":         mt5.ORDER_TYPE_BUY if sig["direction"] == "BUY" else mt5.ORDER_TYPE_SELL,
        "price":        price,
        "sl":           sig["sl"],
        "tp":           sig["tp"],
        "deviation":    20,
        "magic":        CONFIG["magic_number"],
        "comment":      CONFIG["comment"],
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": get_filling_mode(symbol),
    }

    res = mt5.order_send(req)
    if res.retcode != mt5.TRADE_RETCODE_DONE:
        log.error(f"Order failed [{res.retcode}]: {res.comment}")
        return None

    trade = {
        "ticket":    res.order,
        "symbol":    symbol,
        "direction": sig["direction"],
        "entry":     round(price, 5),
        "sl":        sig["sl"],
        "tp":        sig["tp"],
        "lots":      lots,
        "pnl":       0.0,
        "status":    "OPEN",
        "time":      datetime.now(timezone.utc).isoformat(),
        "confluence": sig["confluence"],
    }
    trade_history.append(trade)
    # Persist with full ML context (ctx injected by bot_loop via sig)
    record_trade_open(trade, sig.get("_ctx", {}), sig.get("_bias", "NEUTRAL"), sig)
    
    # ── TERMINAL: Show executed trade with confluence checklist ──
    print(f"\n  ✅ TRADE EXECUTED")
    print(f"     {sig['direction']:4s} {symbol} @ {price:.5f}")
    print(f"     SL: {sig['sl']:.5f} | TP: {sig['tp']:.5f} | {lots} lots")
    print(f"     Confluence ({len(sig['confluence']['hit'])}/{len(sig['confluence']['hit'])+len(sig['confluence']['miss'])}):")
    for item in sig["confluence"]["hit"]:
        print(f"       ✓ {item}")
    for item in sig["confluence"]["miss"]:
        print(f"       ✗ {item}")
    print()
    
    return trade


def close_position(ticket: int) -> bool:
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        return False
    p = pos[0]
    t = get_tick(p.symbol)
    if not t:
        return False
    price = t.bid if p.type == 0 else t.ask
    req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       p.symbol,
        "volume":       p.volume,
        "type":         mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY,
        "position":     ticket,
        "price":        price,
        "deviation":    20,
        "magic":        CONFIG["magic_number"],
        "comment":      "Bot close",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": get_filling_mode(p.symbol),
    }
    res = mt5.order_send(req)
    return res.retcode == mt5.TRADE_RETCODE_DONE


# ══════════════════════════════════════════════════════════════
#  REFRESH OPEN TRADES FROM MT5
# ══════════════════════════════════════════════════════════════

def _modify_sl(ticket: int, symbol: str, new_sl: float):
    """Modify the stop-loss of an open position."""
    info = mt5.symbol_info(symbol)
    digits = info.digits if info else 5
    req = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "symbol":   symbol,
        "sl":       round(new_sl, digits),
    }
    mt5.order_send(req)


def monitor_open_trades(ctx: dict, bias: str):
    """
    Check each open bot trade for dynamic exit conditions:
      1. HTF bias reversal
      2. BOS / CHoCH against trade direction
      3. Trail SL to break-even once profit >= trail_be_pips
    """
    global _prev_open_tickets
    positions = mt5.positions_get() or []
    bot_pos   = [p for p in positions if p.magic == CONFIG["magic_number"]]
    bos_choch = ctx.get("bos_choch", [])

    for p in bot_pos:
        direction = "BUY" if p.type == 0 else "SELL"
        ticket    = p.ticket
        pip       = get_pip_size(p.symbol)
        profit_pips = ((p.price_current - p.price_open) / pip
                       if direction == "BUY"
                       else (p.price_open - p.price_current) / pip)
        exit_reason = None

        # ── 1. Bias reversal exit ──────────────────────────────────
        if CONFIG.get("bias_exit", True):
            if direction == "BUY"  and bias == "BEARISH":
                exit_reason = "Bias reversed to BEARISH"
            elif direction == "SELL" and bias == "BULLISH":
                exit_reason = "Bias reversed to BULLISH"

        # ── 2. BOS / CHoCH structural break against position ──────
        if not exit_reason and CONFIG.get("bos_exit", True):
            if direction == "BUY":
                if any(b["type"] in ("BEARISH_BOS", "BEARISH_CHOCH") for b in bos_choch):
                    exit_reason = "Bearish BOS/CHoCH — structure broken"
            else:
                if any(b["type"] in ("BULLISH_BOS", "BULLISH_CHOCH") for b in bos_choch):
                    exit_reason = "Bullish BOS/CHoCH — structure broken"

        # ── 3. Trail to break-even ────────────────────────────────
        trail_be = CONFIG.get("trail_be_pips", 0)
        if trail_be > 0 and profit_pips >= trail_be:
            sym_info  = mt5.symbol_info(p.symbol)
            digits    = sym_info.digits if sym_info else 5
            # Apply a small buffer so the SL sits a few pips beyond entry
            # rather than exactly at it — prevents closing on a mere re-touch
            be_buf    = CONFIG.get("trail_be_buffer_pips", 2) * pip
            if direction == "BUY":
                be_price = round(p.price_open - be_buf, digits)
                if p.sl == 0 or p.sl < be_price:
                    _modify_sl(ticket, p.symbol, be_price)
                    log.info(f"Trail BE: moved SL → {be_price:.5f} on #{ticket}")
            else:
                be_price = round(p.price_open + be_buf, digits)
                if p.sl == 0 or p.sl > be_price:
                    _modify_sl(ticket, p.symbol, be_price)
                    log.info(f"Trail BE: moved SL → {be_price:.5f} on #{ticket}")

        # ── Auto-close if exit condition met ─────────────────────
        if exit_reason:
            log.info(f"⚡ Auto-close #{ticket} ({direction} {p.symbol}): {exit_reason}")
            tick = get_tick(p.symbol)
            close_px = (tick.bid if p.type == 0 else tick.ask) if tick else p.price_current
            if close_position(ticket):
                record_trade_close(ticket, close_px, p.profit, f"Bot: {exit_reason}")
                log.info(f"  ✓ Closed #{ticket} | PnL: {p.profit:.2f}")
            else:
                log.error(f"  ✗ Failed to close #{ticket}")


def refresh_open_trades():
    global open_trades, _prev_open_tickets
    positions   = mt5.positions_get() or []
    bot_pos_map = {p.ticket: p for p in positions if p.magic == CONFIG["magic_number"]}
    cur_tickets = set(bot_pos_map.keys())

    # Detect trades that were open last cycle but are now gone (TP/SL/manual)
    closed_tickets = _prev_open_tickets - cur_tickets
    for ticket in closed_tickets:
        close_px, pnl, reason = _query_mt5_close(ticket)
        if close_px is not None:
            record_trade_close(ticket, close_px, pnl, reason)
            log.info(f"📋 Trade #{ticket} recorded as CLOSED — {reason} | PnL: {pnl:.2f}")

    _prev_open_tickets = cur_tickets
    open_trades = [
        {
            "ticket":       p.ticket,
            "symbol":       p.symbol,
            "direction":    "BUY" if p.type == 0 else "SELL",
            "entry":        round(p.price_open, 5),
            "current":      round(p.price_current, 5),
            "sl":           round(p.sl, 5),
            "tp":           round(p.tp, 5),
            "lots":         p.volume,
            "pnl":          round(p.profit, 2),
            "swap":         round(p.swap, 2),
            "time":         datetime.fromtimestamp(p.time, tz=timezone.utc).isoformat(),
            "comment":      p.comment,
        }
        for p in bot_pos_map.values()
    ]


# ══════════════════════════════════════════════════════════════
#  MAIN BOT LOOP
# ══════════════════════════════════════════════════════════════

def bot_loop():
    global active_signals, sr_cache
    log.info("═" * 55)
    log.info("  Bot loop started  │  Strategy: ICT/SMC + S&R")
    log.info("═" * 55)

    while state["running"]:
        try:
            symbol = CONFIG["symbol"]

            # Ensure symbol is in Market Watch before fetching data
            ensure_symbol(symbol)

            # Refresh account
            acct = mt5.account_info()
            if acct:
                state["account"] = _fmt_account(acct)
                state["equity_curve"].append({
                    "time":   datetime.now(timezone.utc).isoformat(),
                    "equity": acct.equity,
                })
                state["equity_curve"] = state["equity_curve"][-500:]

            # Fetch candles
            df_htf = get_ohlcv(symbol, CONFIG["htf"], 250)
            df_ltf = get_ohlcv(symbol, CONFIG["ltf"], 250)
            if df_htf is None or df_ltf is None:
                log.warning("Market data unavailable — retrying in 30s")
                time.sleep(30)
                continue

            # ── Core analysis ─────────────────────────────────────────────
            bias, sh, sl = get_market_structure(df_htf)
            state["market_bias"] = bias

            obs  = find_order_blocks(df_ltf, bias)
            fvgs = find_fvg(df_ltf)
            sr   = find_sr_levels(df_htf)

            # ── Extended ICT/SMC detections ───────────────────────────────
            breakers    = find_breaker_blocks(df_ltf, obs)
            bprs        = find_balanced_price_range(fvgs)
            rej_blocks  = find_rejection_blocks(df_ltf, bias)
            suspensions = find_suspension_blocks(df_ltf)
            propulsions = find_propulsion_blocks(df_ltf, bias)
            vacuums     = find_vacuum_blocks(df_htf)
            inducements = find_inducement(df_ltf, sh, sl, bias)
            sweeps      = find_liquidity_sweeps(df_ltf, sh, sl)
            turtle_soups= find_turtle_soup(df_ltf, sh, sl)
            bos_choch   = find_bos_choch(sh, sl, bias)
            cisd        = detect_cisd(df_ltf, bias)
            pd_zone     = get_premium_discount(sh, sl, float(df_ltf["close"].iloc[-1]))
            po3_phase   = detect_power_of_3(df_ltf, bias)
            gaps        = find_opening_gaps(df_ltf)
            smt_div     = detect_smt_divergence(symbol, df_ltf, bias)

            # IPDA uses daily-equivalent bars (HTF is H4 → 6 bars/day)
            ipda = get_ipda_ranges(df_htf)

            # Time-based filters
            is_kz, kz_name     = in_kill_zone()
            is_sb, sb_name     = in_silver_bullet()
            is_macro, mac_name = in_ict_macro()

            # ── Update shared state counts ─────────────────────────────────
            state["ob_count"]       = len(obs)
            state["fvg_count"]      = len(fvgs)
            state["breaker_count"]  = len(breakers)
            state["bpr_count"]      = len(bprs)
            state["sweep_count"]    = len(sweeps)
            state["pd_zone"]        = pd_zone
            state["po3_phase"]      = po3_phase
            state["bos_choch"]      = [e["type"] for e in bos_choch]
            state["cisd"]           = cisd
            state["ipda"]           = ipda
            state["smt_divergence"] = smt_div
            sr_cache = sr

            # ── Contextual confluence meter ────────────────────────────────
            bias_ok = bias in ("BULLISH", "BEARISH")
            pd_ok   = (bias == "BULLISH" and pd_zone == "DISCOUNT") or \
                       (bias == "BEARISH" and pd_zone == "PREMIUM") or \
                       (pd_zone not in ("UNKNOWN", "EQUILIBRIUM"))
            state["ctx_confluences"] = [
                {"name": "Directional Bias",  "active": bias_ok,             "hint": bias if bias_ok else "NEUTRAL"},
                {"name": "Kill Zone",         "active": is_kz,               "hint": kz_name or "—"},
                {"name": "Silver Bullet",     "active": is_sb,               "hint": sb_name or "—"},
                {"name": "ICT Macro",         "active": is_macro,            "hint": mac_name or "—"},
                {"name": "CISD",              "active": bool(cisd),          "hint": "confirmed" if cisd else "—"},
                {"name": "P/D Zone",          "active": pd_ok,               "hint": pd_zone},
                {"name": "AMD Phase",         "active": po3_phase != "UNKNOWN", "hint": po3_phase},
                {"name": "SMT Divergence",    "active": bool(smt_div),       "hint": "confirmed" if smt_div else "—"},
                {"name": "Liquidity Sweep",   "active": len(sweeps) > 0,     "hint": f"{len(sweeps)} recent"},
                {"name": "BOS / CHoCH",       "active": len(bos_choch) > 0,  "hint": bos_choch[0]["type"] if bos_choch else "—"},
                {"name": "Breaker Block",     "active": len(breakers) > 0,   "hint": f"{len(breakers)} found"},
                {"name": "Order Block",       "active": len(obs) > 0,        "hint": f"{len(obs)} unmitigated"},
                {"name": "Fair Value Gap",    "active": len(fvgs) > 0,       "hint": f"{len(fvgs)} gaps"},
                {"name": "BPR",               "active": len(bprs) > 0,       "hint": f"{len(bprs)} found"},
            ]

            # ── Build context dict for signal engine ──────────────────────
            ctx = {
                "obs": obs, "fvgs": fvgs, "sr": sr,
                "sh": sh, "sl": sl,
                "breakers": breakers, "bprs": bprs,
                "rej_blocks": rej_blocks, "suspensions": suspensions,
                "propulsions": propulsions, "vacuums": vacuums,
                "inducements": inducements, "sweeps": sweeps,
                "turtle_soups": turtle_soups, "bos_choch": bos_choch,
                "cisd": cisd, "pd_zone": pd_zone, "ipda": ipda,
                "gaps": gaps, "po3_phase": po3_phase,
                "is_kz": is_kz, "kz_name": kz_name,
                "is_sb": is_sb, "sb_name": sb_name,
                "is_macro": is_macro, "macro_name": mac_name,
            }

            # ── Monitor open trades — dynamic exit / trailing BE ──────────
            monitor_open_trades(ctx, bias)

            # ── Generate signals with full confluence scoring ──────────────
            sigs = generate_signals(symbol, bias, ctx)
            active_signals = sigs

            # Execute — respect kill-zone gate (configurable) and min confluence
            if sigs:
                best = sigs[0]
                kz_ok = (not CONFIG.get("require_kill_zone", True)) or is_kz
                if kz_ok and best["score"] >= CONFIG["min_confluence"]:
                    # Inject context for ML record-keeping
                    best["_ctx"]  = ctx
                    best["_bias"] = bias
                    place_order(best)
                else:
                    reason = "outside kill zone" if not kz_ok else f"score {best['score']} < {CONFIG['min_confluence']}"
                    log.info(f"Signal queued ({reason}): {best['direction']} {symbol}")

            # Refresh open positions (also detects TP/SL closures)
            refresh_open_trades()

            # Cache live price so dashboard ticker stays current on any symbol
            tick = get_tick(symbol)
            if tick:
                info = mt5.symbol_info(symbol)
                digits = info.digits if info else 5
                state["current_price"] = {
                    "symbol": symbol,
                    "bid":    round(float(tick.bid), digits),
                    "ask":    round(float(tick.ask), digits),
                    "mid":    round((float(tick.bid) + float(tick.ask)) / 2, digits),
                    "digits": digits,
                }

            state["last_scan"] = datetime.now(timezone.utc).isoformat()
            state["error"]     = None

            sb_str    = f"SB:{sb_name}"      if is_sb    else "SB:—"
            macro_str = f"MAC:{mac_name}"    if is_macro else "MAC:—"
            log.info(
                f"Bias:{bias:8s} │ OBs:{len(obs):2d} │ FVGs:{len(fvgs):2d} │ "
                f"SR:{len(sr):2d} │ BRK:{len(breakers)} │ BPR:{len(bprs)} │ "
                f"SWP:{len(sweeps)} │ PD:{pd_zone[:3]} │ AMD:{po3_phase[:3]} │ "
                f"SMT:{'✓' if smt_div else '—'} │ CISD:{'✓' if cisd else '—'} │ "
                f"KZ:{'✓ '+kz_name if is_kz else '—'} │ {sb_str} │ Sigs:{len(sigs)}"
            )

            time.sleep(CONFIG["scan_interval"])

        except Exception as exc:
            log.exception(f"Bot error: {exc}")
            state["error"] = str(exc)
            time.sleep(30)

    log.info("Bot loop stopped.")


# ══════════════════════════════════════════════════════════════
#  FLASK API
# ══════════════════════════════════════════════════════════════

app = Flask(__name__)
CORS(app)


@app.route("/")
def index():
    return (
        "<h2 style='font-family:monospace'>ICT/SMC Bot API (VPS / MT5 direct)</h2>"
        f"<p>Dashboard is hosted separately on Render.</p>"
        f"<p>Status: <b>{'Running' if state['running'] else 'Idle'}</b></p>"
        f"<p>MT5 Connected: <b>{state['connected']}</b></p>"
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok", "connected": state["connected"], "running": state["running"]})


@app.route("/api/status")
def api_status():
    return jsonify({
        **state,
        "symbol":         CONFIG["symbol"],
        "open_trades":    open_trades,
        "active_signals": active_signals,
        "kill_zones":     kill_zone_status(),
        "sr_levels":      sr_cache[:6],
    })


@app.route("/api/history")
def api_history():
    return jsonify({"trades": trade_history[-100:]})


@app.route("/api/trade-log")
def api_trade_log():
    """Full persisted trade log — all trades with ML context."""
    return jsonify({"trades": persistent_trade_log})


@app.route("/api/export-csv")
def api_export_csv():
    """Download all trades as CSV for ML / spreadsheet analysis."""
    import io, csv as csv_mod
    fields = ["ticket","symbol","direction","entry","close_price","sl","tp",
              "lots","pnl","pips","status","exit_reason","time","close_time"]
    ml_fields = ["bias","pd_zone","po3_phase","cisd","smt_divergence",
                 "ob_count","fvg_count","sweep_count","breaker_count",
                 "bpr_count","is_kz","kz_name","confluence_score"]
    buf = io.StringIO()
    w = csv_mod.writer(buf)
    w.writerow(fields + ml_fields)
    for t in persistent_trade_log:
        ml = t.get("ml_context", {})
        row = [t.get(f, "") for f in fields] + [ml.get(f, "") for f in ml_fields]
        w.writerow(row)
    buf.seek(0)
    from flask import Response
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=trade_log.csv"},
    )


@app.route("/api/equity")
def api_equity():
    return jsonify({"curve": state["equity_curve"]})


@app.route("/api/start", methods=["POST"])
def api_start():
    if not state["connected"]:
        return jsonify({"error": "MT5 not connected"}), 400
    if state["running"]:
        return jsonify({"message": "Already running"})
    state["running"] = True
    threading.Thread(target=bot_loop, daemon=True).start()
    return jsonify({"message": "Bot started"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    state["running"] = False
    return jsonify({"message": "Bot stopping…"})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        data = request.get_json(force=True)
        editable = ["risk_pct", "min_rr", "max_trades", "symbol",
                    "scan_interval", "min_confluence", "fvg_min_pips",
                    "active_strategies", "require_kill_zone",
                    "trail_be_pips", "trail_be_buffer_pips"]
        for k in editable:
            if k in data:
                CONFIG[k] = data[k]
        if "symbol" in data and state["connected"]:
            mt5.symbol_select(CONFIG["symbol"], True)
        return jsonify({"message": "Config updated", "config": {k: CONFIG[k] for k in editable if k in CONFIG}})
    safe = {k: v for k, v in CONFIG.items() if k not in ("password",)}
    return jsonify(safe)


@app.route("/api/symbols")
def api_symbols():
    """Return broker symbol list, grouped by asset class, directly from MT5."""
    if not state["connected"]:
        return jsonify({"symbols": [], "grouped": {}, "source": "disconnected"})
    try:
        all_syms = mt5.symbols_get()
        if all_syms is None:
            return jsonify({"symbols": [], "grouped": {}, "error": str(mt5.last_error()), "source": "error"})

        # FIXED: Removed 'if s.visible' filter to pull EVERY symbol the broker offers
        names = sorted({s.name for s in all_syms})

        groups: dict[str, list[str]] = {
            "Forex": [], "Metals": [], "Indices": [], "Crypto": [], "Energy": [], "Other": [],
        }
        forex_currencies = {"EUR","GBP","USD","JPY","CHF","AUD","NZD","CAD","SGD","HKD","NOK","SEK","DKK","MXN","ZAR","TRY"}
        for sym in names:
            up = sym.upper()
            if any(x in up for x in ("XAU","XAG","XPT","XPD","GOLD","SILVER")):
                groups["Metals"].append(sym)
            elif any(x in up for x in ("BTC","ETH","LTC","XRP","ADA","DOT","BNB","SOL","DOGE")):
                groups["Crypto"].append(sym)
            elif any(x in up for x in ("OIL","WTI","BRENT","NGAS","XBR","XTI")):
                groups["Energy"].append(sym)
            elif any(x in up for x in ("US30","SPX","NAS","DAX","FTSE","CAC","ASX","NIK","DOW",
                                        "SP500","NDX","US500","USTEC","UK100","GER","FRA","JPN")):
                groups["Indices"].append(sym)
            elif len(sym) >= 6 and sym[:3] in forex_currencies and sym[3:6] in forex_currencies:
                groups["Forex"].append(sym)
            else:
                groups["Other"].append(sym)

        grouped = {k: v for k, v in groups.items() if v}
        return jsonify({"symbols": names, "grouped": grouped, "source": "broker", "count": len(names)})

    except Exception as exc:
        log.warning(f"api_symbols: {exc}")
        return jsonify({"symbols": [], "grouped": {}, "error": str(exc), "source": "error"})


@app.route("/api/broker", methods=["GET", "POST"])
def api_broker():
    """Get or set MT5 broker login credentials and (re)connect."""
    if request.method == "POST":
        data = request.get_json(force=True)
        login    = data.get("login")
        password = data.get("password")
        server   = data.get("server")

        if not (login and password and server):
            return jsonify({"error": "login, password and server are all required"}), 400

        # Store credentials (password kept in memory only — never returned by GET)
        CONFIG["login"]    = str(login)
        CONFIG["password"] = str(password)
        CONFIG["server"]   = str(server)

        mt5.shutdown()
        ok = connect_mt5(CONFIG["login"], CONFIG["password"], CONFIG["server"])
        if ok:
            return jsonify({"message": "Connected", "account": state["account"]})
        return jsonify({"error": f"Login failed: {mt5.last_error()}"}), 400

    # GET — never return the password
    return jsonify({
        "login":      CONFIG.get("login") or "",
        "server":     CONFIG.get("server") or "",
        "connected":  state["connected"],
        "account":    state.get("account", {}),
    })


@app.route("/api/reconnect", methods=["POST"])
def api_reconnect():
    """Re-initialise MT5 connection (e.g. after terminal restart)."""
    ok = connect_mt5(CONFIG.get("login"), CONFIG.get("password"), CONFIG.get("server"))
    if ok:
        return jsonify({"message": "Reconnected to MT5", "account": state["account"]})
    return jsonify({"error": f"Reconnect failed: {mt5.last_error()}"}), 400


@app.route("/api/logout", methods=["POST"])
def api_logout():
    """Disconnect from MT5 and clear stored credentials."""
    state["running"]   = False
    state["connected"] = False
    state["account"]   = {}
    state["error"]     = "Logged out. Connect via the dashboard."
    CONFIG["login"]    = None
    CONFIG["password"] = None
    CONFIG["server"]   = None
    mt5.shutdown()
    log.info("MT5 disconnected (user logout)")
    return jsonify({"message": "Disconnected"})


@app.route("/api/close/<int:ticket>", methods=["POST"])
def api_close(ticket):
    if close_position(ticket):
        refresh_open_trades()
        return jsonify({"message": f"#{ticket} closed"})
    return jsonify({"error": "Close failed — check MT5"}), 400


@app.route("/api/closeall", methods=["POST"])
def api_close_all():
    closed = sum(1 for t in list(open_trades) if close_position(t["ticket"]))
    refresh_open_trades()
    return jsonify({"message": f"Closed {closed} trade(s)"})


# ══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import os

    print("""
╔══════════════════════════════════════════════════════╗
║        ICT / SMC + S&R Trading Bot for MT5          ║
║   Order Blocks · FVG · Structure · Kill Zones       ║
╚══════════════════════════════════════════════════════╝
    """)

    # ── Load credentials from environment variables if not set in CONFIG ──
    if not CONFIG.get("login"):
        CONFIG["login"]    = os.getenv("MT5_LOGIN")
    if not CONFIG.get("password"):
        CONFIG["password"] = os.getenv("MT5_PASSWORD")
    if not CONFIG.get("server"):
        CONFIG["server"]   = os.getenv("MT5_SERVER")

    # ── Try initial MT5 connection (non-fatal if it fails) ──
    connected = connect_mt5(
        CONFIG.get("login"),
        CONFIG.get("password"),
        CONFIG.get("server"),
    )

    if connected:
        a = state["account"]
        print(f"  ✅  Connected  │  {a.get('broker','—')}")
        print(f"  💰  Balance    │  {a.get('balance',0):.2f} {a.get('currency','')}")
        print(f"  📊  Symbol     │  {CONFIG['symbol']}")
    else:
        print("  ⚠️   MT5 not connected at startup.")
        print("  ➜  Open MetaTrader 5 on this machine, then connect via the")
        print("     dashboard ACCOUNT → CONNECT (login / password / server).")
        print("  ➜  You can also set MT5_LOGIN / MT5_PASSWORD / MT5_SERVER")
        print("     as environment variables and restart the bot.")

    print(f"\n  🌐  API server  │  http://localhost:{CONFIG['port']}")
    print(f"  ℹ️   Bot will start trading once connected + started from dashboard.\n")

    # ── Always start the Flask API so the dashboard can connect ──
    app.run(
        host="0.0.0.0",
        port=CONFIG["port"],
        debug=False,
        use_reloader=False,
    )
