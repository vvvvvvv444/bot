"""
Memecoin Scanner + Paper Trading Bot (Solana)
- Cerca nuovi token su DexScreener
- Filtra scam evidenti (liquidita', eta', holder, RugCheck)
- Simula acquisti/vendite con soldi FINTI (nessun wallet collegato)
- Alert opzionali su Telegram
"""
import json
import os
import time
from pathlib import Path

import requests

# ================= CONFIG =================
SCAN_INTERVAL = 60
MIN_LIQUIDITY_USD = 15_000
MIN_VOLUME_H1_USD = 10_000
MIN_AGE_MIN = 30
MAX_AGE_HOURS = 24
MIN_BUY_SELL_RATIO = 1.2
MAX_RUGCHECK_SCORE = 5000

PAPER_BALANCE_START = 1_000
POSITION_SIZE = 50
MAX_OPEN_POSITIONS = 5
TAKE_PROFIT = 0.50
STOP_LOSS = -0.25
MAX_DAILY_LOSS = 150
MAX_HOLD_HOURS = 6

STATE_FILE = Path("paper_state.json")
REPORT_FILE = Path("REPORT.md")
DEX = "https://api.dexscreener.com"
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


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"balance": PAPER_BALANCE_START, "positions": {}, "history": [],
            "day": time.strftime("%Y-%m-%d"), "day_pnl": 0.0, "seen": []}


def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2))


def get_json(url):
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            return r.json()
    except (requests.RequestException, ValueError):
        pass
    return None


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
    created = p.get("pairCreatedAt")
    if not created:
        return False, "eta' sconosciuta"
    age_min = (time.time() * 1000 - created) / 60000

    if liq < MIN_LIQUIDITY_USD:
        return False, f"liquidita' bassa ({liq:.0f})"
    if vol < MIN_VOLUME_H1_USD:
        return False, "volume basso"
    if age_min < MIN_AGE_MIN:
        return False, "troppo nuovo"
    if age_min > MAX_AGE_HOURS * 60:
        return False, "troppo vecchio"
    if sells == 0 or buys / max(sells, 1) < MIN_BUY_SELL_RATIO:
        return False, "rapporto buy/sell debole"
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


def close_position(s, mint, price, reason):
    pos = s["positions"].pop(mint)
    value = pos["qty"] * price
    pnl = value - pos["cost"]
    s["balance"] += value
    s["day_pnl"] += pnl
    s["history"].append({"symbol": pos["symbol"], "pnl": round(pnl, 2), "reason": reason})
    notify(f"VENDITA (paper) {pos['symbol']} | {reason} | PnL {pnl:+.2f}$ | saldo {s['balance']:.2f}$")


def manage_positions(s):
    if not s["positions"]:
        return
    pairs = pairs_for(list(s["positions"].keys()))
    for mint in list(s["positions"]):
        p = best_pair(pairs, mint)
        if not p:
            continue
        price = float(p["priceUsd"])
        pos = s["positions"][mint]
        pos["last"] = price
        change = price / pos["entry"] - 1
        held_h = (time.time() - pos["ts"]) / 3600
        if change >= TAKE_PROFIT:
            close_position(s, mint, price, f"take profit {change:+.0%}")
        elif change <= STOP_LOSS:
            close_position(s, mint, price, f"stop loss {change:+.0%}")
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
        notify(f"SEGNALE {sym}\nprezzo {price}\nliq {p['liquidity']['usd']:.0f}$\n{p['url']}")

        can_buy = (
            len(s["positions"]) < MAX_OPEN_POSITIONS
            and s["balance"] >= POSITION_SIZE
            and s["day_pnl"] > -MAX_DAILY_LOSS
        )
        if can_buy:
            s["balance"] -= POSITION_SIZE
            s["positions"][mint] = {"symbol": sym, "entry": price, "cost": POSITION_SIZE,
                                    "qty": POSITION_SIZE / price, "ts": time.time()}
            notify(f"ACQUISTO (paper) {sym} a {price} | saldo {s['balance']:.2f}$")

    s["seen"] = s["seen"][-500:]


def write_report(s):
    hist = s["history"]
    wins = [h for h in hist if h["pnl"] > 0]
    closed_pnl = sum(h["pnl"] for h in hist)
    open_val = sum(p["qty"] * p.get("last", p["entry"]) for p in s["positions"].values())
    equity = s["balance"] + open_val
    total = equity - PAPER_BALANCE_START
    winrate = len(wins) / len(hist) * 100 if hist else 0

    L = ["# Report paper trading (soldi finti)", ""]
    L.append(f"Aggiornato: {time.strftime('%Y-%m-%d %H:%M')} (ora server)")
    L.append("")
    L.append("## Riepilogo")
    L.append(f"- Capitale iniziale: {PAPER_BALANCE_START:.2f}$")
    L.append(f"- Capitale attuale (liquidi + posizioni aperte): **{equity:.2f}$**")
    L.append(f"- Risultato totale: **{total:+.2f}$** ({total / PAPER_BALANCE_START * 100:+.1f}%)")
    L.append(f"- Liquidi: {s['balance']:.2f}$")
    L.append(f"- Guadagno/perdita di oggi: {s['day_pnl']:+.2f}$")
    L.append("")
    L.append("## Operazioni chiuse")
    L.append(f"- Totale: {len(hist)} | Vinte: {len(wins)} | Perse: {len(hist) - len(wins)}")
    L.append(f"- Percentuale vittorie: {winrate:.0f}%")
    L.append(f"- Risultato operazioni chiuse: {closed_pnl:+.2f}$")
    L.append("")
    L.append("## Posizioni aperte")
    if s["positions"]:
        for p in s["positions"].values():
            last = p.get("last", p["entry"])
            ch = (last / p["entry"] - 1) * 100
            L.append(f"- {p['symbol']}: {ch:+.1f}% (investiti {p['cost']:.0f}$)")
    else:
        L.append("- Nessuna")
    L.append("")
    L.append("## Ultime 10 operazioni")
    if hist:
        for h in reversed(hist[-10:]):
            L.append(f"- {h['symbol']}: {h['pnl']:+.2f}$ ({h['reason']})")
    else:
        L.append("- Nessuna operazione chiusa ancora")
    REPORT_FILE.write_text("\n".join(L) + "\n")


def main():
    s = load_state()
    if os.getenv("RUN_ONCE"):
        manage_positions(s)
        scan(s)
        save_state(s)
        write_report(s)
        return
    notify("Bot avviato (PAPER TRADING, nessun fondo reale)")
    while True:
        try:
            manage_positions(s)
            scan(s)
            save_state(s)
            write_report(s)
        except Exception as e:
            log(f"errore: {e}")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
