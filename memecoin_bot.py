"""
Memecoin Scanner + Paper Trading Bot v2 (Solana)
- Cerca nuovi token su DexScreener e li filtra (anti-scam)
- Simula acquisti/vendite con soldi FINTI (nessun wallet collegato)
- Vendita parziale a +50% e trailing stop sul resto
- Salva i grafici a candele (GeckoTerminal) e aggiorna lo stato su GitHub
"""
import json
import os
import subprocess
import time
from pathlib import Path

import requests

# ================= CONFIG =================
STRATEGY = "v2"
RUN_MINUTES = float(os.getenv("RUN_MINUTES", "0"))  # 0 = un solo giro
MANAGE_INTERVAL = 30   # secondi tra un controllo e l'altro delle posizioni
SCAN_EVERY = 2         # cerca nuovi token ogni 2 controlli (circa 60 s)
CHART_EVERY = 300      # aggiorna i grafici ogni 5 minuti
PUSH_EVERY = 600       # salva su GitHub ogni 10 minuti

MIN_LIQUIDITY_USD = 40_000
MIN_VOLUME_H1_USD = 20_000
MIN_TXNS_H1 = 100
MIN_AGE_MIN = 60
MAX_AGE_HOURS = 12
MIN_BUY_SELL_RATIO = 1.2
MAX_CHANGE_H1 = 200    # % : non comprare dopo pump enormi
MIN_CHANGE_H1 = -15    # % : non comprare token in calo
MAX_RUGCHECK_SCORE = 5000

PAPER_BALANCE_START = 1_000
POSITION_SIZE = 25
MAX_OPEN_POSITIONS = 5
STOP_LOSS = -0.25
PARTIAL_TP = 0.50      # a +50% vende meta' posizione
TRAILING = 0.25        # poi esce se scende del 25% dal massimo
MAX_DAILY_LOSS = 75
MAX_HOLD_HOURS = 6
SLIPPAGE = 0.03        # costo simulato 3% su ogni acquisto e vendita

STATE_FILE = Path("paper_state.json")
CHARTS_FILE = Path("candles.json")
DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2/networks/solana/pools"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{}/report/summary"
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")
# ==========================================


def log(msg):
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)


def notify(msg):
    log(msg)
    if TG_TOKEN and TG_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": msg},
                timeout=10,
            )
        except requests.RequestException:
            pass


def get_json(url):
    try:
        r = requests.get(url, timeout=15, headers={"Accept": "application/json"})
        if r.status_code == 200:
            return r.json()
    except (requests.RequestException, ValueError):
        pass
    return None


# ---------- stato ----------
def fresh_state():
    return {"strategy": STRATEGY, "balance": PAPER_BALANCE_START, "positions": {},
            "history": [], "day": time.strftime("%Y-%m-%d"), "day_pnl": 0.0, "seen": []}


def load_state():
    if not STATE_FILE.exists():
        return fresh_state()
    old = json.loads(STATE_FILE.read_text())
    if old.get("strategy") == STRATEGY:
        return old
    # strategia nuova: archivio il vecchio test e riparto da zero
    open_val = sum(p.get("qty", 0) * p.get("last", p.get("entry", 0))
                   for p in old.get("positions", {}).values())
    s = fresh_state()
    s["seen"] = old.get("seen", [])
    s["archive"] = {"strategy": old.get("strategy", "v1"),
                    "final_balance": round(old.get("balance", 0) + open_val, 2),
                    "trades": len(old.get("history", [])),
                    "history": old.get("history", [])}
    return s


def update_meta(s):
    s["meta"] = {
        "last_run": int(time.time()),
        "mode": "PAPER",
        "strategy": STRATEGY,
        "start_balance": PAPER_BALANCE_START,
        "position_size": POSITION_SIZE,
        "max_positions": MAX_OPEN_POSITIONS,
        "max_daily_loss": MAX_DAILY_LOSS,
        "partial_tp": PARTIAL_TP,
        "stop_loss": STOP_LOSS,
        "trailing": TRAILING,
    }


def save_all(s):
    update_meta(s)
    STATE_FILE.write_text(json.dumps(s, indent=2))
    CHARTS_FILE.write_text(json.dumps(CHARTS, separators=(",", ":")))


def git_push():
    def run(*a):
        return subprocess.run(a, capture_output=True, text=True)
    try:
        run("git", "add", "paper_state.json", "candles.json")
        if run("git", "commit", "-m", "stato").returncode != 0:
            return  # niente da salvare
        if run("git", "pull", "--rebase", "--autostash").returncode != 0:
            run("git", "rebase", "--abort")
        res = run("git", "push")
        if res.returncode != 0:
            log(f"push fallito: {res.stderr.strip()[:200]}")
    except Exception as e:
        log(f"git non disponibile: {e}")


# ---------- grafici ----------
def load_charts():
    if CHARTS_FILE.exists():
        try:
            return json.loads(CHARTS_FILE.read_text())
        except ValueError:
            pass
    return {}


CHARTS = load_charts()


def fetch_candles(pair):
    if not pair:
        return None
    data = get_json(f"{GECKO}/{pair}/ohlcv/minute?aggregate=5&limit=120")
    try:
        rows = data["data"]["attributes"]["ohlcv_list"]
        return sorted(([int(c[0]), c[1], c[2], c[3], c[4]] for c in rows), key=lambda c: c[0])
    except (TypeError, KeyError, IndexError, ValueError):
        return None


def touch_chart(mint, pos):
    ch = CHARTS.setdefault(mint, {})
    ch.update({"symbol": pos["symbol"], "open": True, "entry": pos["entry_mkt"],
               "entry_ts": int(pos["ts"]), "exit": None})
    candles = fetch_candles(pos.get("pair"))
    if candles:
        ch["candles"] = candles


def update_open_charts(s):
    for mint, pos in s["positions"].items():
        touch_chart(mint, pos)


def prune_charts():
    closed = sorted((k for k, v in CHARTS.items() if not v.get("open")),
                    key=lambda k: CHARTS[k].get("exit_ts", 0), reverse=True)
    for k in closed[8:]:
        del CHARTS[k]


# ---------- ricerca token ----------
def new_solana_tokens():
    data = get_json(f"{DEX}/token-profiles/latest/v1") or []
    return [d["tokenAddress"] for d in data if d.get("chainId") == "solana"]


def pairs_for(addresses):
    out = []
    for i in range(0, len(addresses), 30):
        chunk = ",".join(addresses[i:i + 30])
        data = get_json(f"{DEX}/tokens/v1/solana/{chunk}") or []
        out.extend(data)
    return out


def best_pair(pairs, mint):
    mine = [p for p in pairs if p["baseToken"]["address"] == mint]
    return max(mine, key=lambda p: (p.get("liquidity") or {}).get("usd", 0), default=None)


def passes_filters(p):
    liq = (p.get("liquidity") or {}).get("usd", 0)
    vol = (p.get("volume") or {}).get("h1", 0)
    tx = (p.get("txns") or {}).get("h1", {})
    buys, sells = tx.get("buys", 0), tx.get("sells", 0)
    change = (p.get("priceChange") or {}).get("h1")
    created = p.get("pairCreatedAt")
    if not created or not p.get("priceUsd"):
        return False, "dati mancanti"
    age_min = (time.time() * 1000 - created) / 60000

    if liq < MIN_LIQUIDITY_USD:
        return False, f"liquidita' bassa ({liq:.0f})"
    if vol < MIN_VOLUME_H1_USD:
        return False, "volume basso"
    if buys + sells < MIN_TXNS_H1:
        return False, "poche transazioni"
    if age_min < MIN_AGE_MIN:
        return False, "troppo nuovo"
    if age_min > MAX_AGE_HOURS * 60:
        return False, "troppo vecchio"
    if sells == 0 or buys / max(sells, 1) < MIN_BUY_SELL_RATIO:
        return False, "rapporto buy/sell debole"
    if change is not None and not (MIN_CHANGE_H1 <= change <= MAX_CHANGE_H1):
        return False, f"variazione 1h fuori limiti ({change}%)"
    return True, "ok"


def rugcheck_ok(mint):
    data = get_json(RUGCHECK.format(mint))
    if not data:
        return False, "rugcheck non disponibile"
    if data.get("score", 0) > MAX_RUGCHECK_SCORE:
        return False, f"score rischio alto ({data.get('score')})"
    for risk in data.get("risks", []):
        if risk.get("level") == "danger":
            return False, f"rischio: {risk.get('name')}"
    return True, "ok"


# ---------- operazioni ----------
def sell_half(s, mint, price):
    pos = s["positions"][mint]
    qty = pos["qty"] / 2
    cost = pos["cost"] / 2
    value = qty * price * (1 - SLIPPAGE)
    pnl = value - cost
    pos["qty"] -= qty
    pos["cost"] -= cost
    pos["realized"] = pos.get("realized", 0.0) + pnl
    pos["partial"] = True
    s["balance"] += value
    s["day_pnl"] += pnl
    notify(f"VENDITA PARZIALE (paper) {pos['symbol']} | +{PARTIAL_TP:.0%} | incassati {pnl:+.2f}$")


def close_position(s, mint, price, reason):
    pos = s["positions"].pop(mint)
    value = pos["qty"] * price * (1 - SLIPPAGE)
    pnl = value - pos["cost"]
    total = pos.get("realized", 0.0) + pnl
    now = int(time.time())
    s["balance"] += value
    s["day_pnl"] += pnl
    s["history"].append({"symbol": pos["symbol"], "pnl": round(total, 2),
                         "reason": reason, "ts": now})
    ch = CHARTS.pop(mint, {"symbol": pos["symbol"], "entry": pos["entry_mkt"],
                           "entry_ts": int(pos["ts"])})
    ch.update({"open": False, "exit": price, "exit_ts": now, "pnl": round(total, 2)})
    candles = fetch_candles(pos.get("pair"))
    if candles:
        ch["candles"] = candles
    CHARTS[f"{mint}:{now}"] = ch
    prune_charts()
    notify(f"CHIUSA (paper) {pos['symbol']} | {reason} | totale {total:+.2f}$ | saldo {s['balance']:.2f}$")


def manage_positions(s):
    if not s["positions"]:
        return
    pairs = pairs_for(list(s["positions"].keys()))
    now = time.time()
    for mint in list(s["positions"]):
        pos = s["positions"][mint]
        p = best_pair(pairs, mint)
        if not p or not p.get("priceUsd"):
            if now - pos.get("last_seen", pos["ts"]) > 3600:
                close_position(s, mint, 0.0, "token sparito (-100%)")
            continue
        price = float(p["priceUsd"])
        pos["last"] = price
        pos["last_seen"] = now
        pos["peak"] = max(pos.get("peak", price), price)
        change = price / pos["entry_mkt"] - 1
        held_h = (now - pos["ts"]) / 3600

        if pos.get("partial"):
            if price <= pos["peak"] * (1 - TRAILING) or price <= pos["entry_mkt"]:
                close_position(s, mint, price, f"trailing stop ({change:+.0%})")
            elif held_h >= MAX_HOLD_HOURS:
                close_position(s, mint, price, f"tempo massimo ({change:+.0%})")
        elif change <= STOP_LOSS:
            close_position(s, mint, price, f"stop loss {change:+.0%}")
        elif change >= PARTIAL_TP:
            sell_half(s, mint, price)
        elif held_h >= MAX_HOLD_HOURS:
            close_position(s, mint, price, f"tempo massimo ({change:+.0%})")


def scan(s):
    today = time.strftime("%Y-%m-%d")
    if s["day"] != today:
        s["day"], s["day_pnl"] = today, 0.0

    candidates = [m for m in new_solana_tokens() if m not in s["seen"] and m not in s["positions"]]
    if not candidates:
        return
    pairs = pairs_for(candidates)

    for mint in candidates:
        s["seen"].append(mint)
        p = best_pair(pairs, mint)
        if not p:
            continue
        ok, why = passes_filters(p)
        if not ok:
            continue
        ok, why = rugcheck_ok(mint)
        if not ok:
            log(f"scartato {p['baseToken']['symbol']}: {why}")
            continue

        sym = p["baseToken"]["symbol"]
        price = float(p["priceUsd"])
        can_buy = (
            len(s["positions"]) < MAX_OPEN_POSITIONS
            and s["balance"] >= POSITION_SIZE
            and s["day_pnl"] > -MAX_DAILY_LOSS
        )
        notify(f"SEGNALE {sym}\nprezzo {price}\nliq {p['liquidity']['usd']:.0f}$\n{p['url']}")
        if can_buy:
            entry = price * (1 + SLIPPAGE)
            s["balance"] -= POSITION_SIZE
            pos = {"symbol": sym, "entry": entry, "entry_mkt": price,
                   "cost": POSITION_SIZE, "qty": POSITION_SIZE / entry,
                   "ts": time.time(), "last": price, "last_seen": time.time(),
                   "peak": price, "pair": p.get("pairAddress"),
                   "partial": False, "realized": 0.0}
            s["positions"][mint] = pos
            touch_chart(mint, pos)
            notify(f"ACQUISTO (paper) {sym} a {price} | saldo {s['balance']:.2f}$")

    s["seen"] = s["seen"][-500:]


def main():
    s = load_state()
    deadline = time.time() + RUN_MINUTES * 60
    if RUN_MINUTES > 0:
        notify(f"Bot {STRATEGY} avviato (PAPER TRADING, nessun fondo reale)")
    last_push = last_chart = 0.0
    n = 0
    while True:
        try:
            manage_positions(s)
            if n % SCAN_EVERY == 0:
                scan(s)
            if time.time() - last_chart >= CHART_EVERY:
                update_open_charts(s)
                last_chart = time.time()
            save_all(s)
            if time.time() - last_push >= PUSH_EVERY:
                git_push()
                last_push = time.time()
        except Exception as e:
            log(f"errore: {e}")
        n += 1
        if RUN_MINUTES <= 0 or time.time() + MANAGE_INTERVAL >= deadline:
            break
        time.sleep(MANAGE_INTERVAL)
    try:
        update_open_charts(s)
        save_all(s)
    except Exception as e:
        log(f"errore finale: {e}")
    git_push()


if __name__ == "__main__":
    main()
