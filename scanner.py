#!/usr/bin/env python3
"""Early-rise crypto scanner (spot, long-only). Scores each coin in coins.json on
six signals using Coinbase public candles, prints a table, and sends a Pushover
alert when a coin reaches BUY status.

  python scanner.py            scan + alert
  python scanner.py --dry      scan only, no alerts
  python scanner.py --test     send a test notification
"""
import json, os, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).parent
API = "https://api.exchange.coinbase.com"
GRAN = int(os.getenv("GRANULARITY", "3600"))          # 3600 = 1h, 900 = 15m
COOLDOWN_H = float(os.getenv("COOLDOWN_HOURS", "6"))  # don't re-alert a coin sooner
BUY_SCORE, WATCH_SCORE = 4, 3                         # signals needed (of 6)
STATE = HERE / "state.json"


def get_json(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "crypto-screener"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(1.5 * (i + 1)); continue
            if e.code == 404:
                return None
            time.sleep(1)
        except Exception:
            time.sleep(1)
    return None


def fetch_bars(sym):
    data = get_json(f"{API}/products/{sym}-USD/candles?granularity={GRAN}")
    if not data:
        return None
    now = time.time()
    bars = [b for b in reversed(data) if b[0] + GRAN <= now]  # oldest->newest, closed only
    return bars  # [time, low, high, open, close, volume]


# ---- indicators (identical logic in index.html) ----
def ema(v, n):
    k = 2 / (n + 1); out = []; p = v[0]
    for i, x in enumerate(v):
        p = x if i == 0 else x * k + p * (1 - k)
        out.append(p)
    return out


def rsi(c, n=14):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    g = l = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        if d >= 0: g += d
        else: l -= d
    g /= n; l /= n
    out[n] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def atr(h, l, c, n=14):
    trs = [h[i] - l[i] if i == 0 else max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
           for i in range(len(c))]
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def bandwidth(c, n=20):
    out = []
    for i in range(n - 1, len(c)):
        w = c[i - n + 1:i + 1]; m = sum(w) / n
        sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
        out.append(4 * sd / m if m else 0)
    return out


def analyze(bars, gran=GRAN):
    if not bars or len(bars) < 120:
        return None
    l = [b[1] for b in bars]; h = [b[2] for b in bars]
    c = [b[4] for b in bars]; v = [b[5] for b in bars]
    price = c[-1]
    avg = lambda a: sum(a) / len(a)
    r = rsi(c)

    base = avg(v[-23:-3])
    vol_ratio = avg(v[-3:]) / base if base > 0 else 0
    chg3 = c[-1] / c[-4] - 1
    sig_vol = vol_ratio >= 1.8 and chg3 < 0.04

    bw = bandwidth(c); rec = bw[-100:]
    rank = lambda x: sum(1 for y in rec if y < x) / len(rec)
    sig_sq = rank(bw[-1]) <= 0.25 or (min(rank(x) for x in bw[-6:]) <= 0.20 and bw[-1] > bw[-2])

    cross50 = any(r[-k - 1] < 50 <= r[-k] for k in (1, 2, 3))
    bounce = min(r[-6:]) < 38 and r[-1] - r[-4] >= 8
    sig_rsi = 40 <= r[-1] <= 68 and (cross50 or bounce)

    e12, e26 = ema(c, 12), ema(c, 26)
    line = [a - b for a, b in zip(e12, e26)]
    sg = ema(line, 9)
    hist = [a - b for a, b in zip(line, sg)]
    macd_cross = any(hist[-k - 1] <= 0 < hist[-k] for k in (1, 2, 3))
    macd_turn = hist[-1] < 0 and hist[-1] > hist[-2] > hist[-3]
    sig_macd = macd_cross or macd_turn

    e20 = ema(c, 20)
    crossed = any(c[-k - 1] <= e20[-k - 1] and c[-k] > e20[-k] for k in (1, 2, 3))
    sig_ema = crossed and e20[-1] > e20[-4] and c[-1] > e20[-1]

    sig_bo = price > max(h[-25:-1])

    per_day = 86400 // gran
    chg24 = price / c[-1 - per_day] - 1
    extended = chg24 > 0.12 or r[-1] > 75

    a = atr(h, l, c)
    signals = {"volume": sig_vol, "squeeze": sig_sq, "rsi": sig_rsi,
               "macd": sig_macd, "ema": sig_ema, "breakout": sig_bo}
    score = sum(signals.values())
    status = "extended" if extended and score >= WATCH_SCORE else (
        "buy" if score >= BUY_SCORE and not extended else
        "watch" if score >= WATCH_SCORE and not extended else "none")
    return {"price": price, "chg24": chg24, "rsi": r[-1], "volRatio": vol_ratio,
            "score": score, "signals": signals, "status": status,
            "stop": price - 1.5 * a, "target": price + 3 * a}


def beta_vs(bars, ref, gran=GRAN):
    """Beta and correlation of bars' returns vs ref's returns (matched by timestamp)."""
    rc = {b[0]: b[4] for b in ref}
    x, y = [], []
    for i in range(1, len(bars)):
        if bars[i][0] - bars[i - 1][0] != gran:
            continue
        a, b = rc.get(bars[i][0]), rc.get(bars[i - 1][0])
        if a and b:
            x.append(a / b - 1); y.append(bars[i][4] / bars[i - 1][4] - 1)
    if len(x) < 100:
        return None
    mx, my = sum(x) / len(x), sum(y) / len(y)
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y))
    vx = sum((a - mx) ** 2 for a in x); vy = sum((b - my) ** 2 for b in y)
    if vx <= 0 or vy <= 0:
        return None
    return cov / vx, cov / (vx * vy) ** 0.5


def fmt_beta(b):
    return f"{b[0]:.2f}" if b else "n/a"


def fmt_price(p):
    return f"{p:,.2f}" if p >= 1 else f"{p:.6f}".rstrip("0")


def pushover(title, message):
    tok, usr = os.getenv("PUSHOVER_TOKEN"), os.getenv("PUSHOVER_USER")
    if not tok or not usr:
        print("  (Pushover keys not set; skipping notification)")
        return
    body = urllib.parse.urlencode({"token": tok, "user": usr, "title": title,
                                   "message": message}).encode()
    try:
        urllib.request.urlopen("https://api.pushover.net/1/messages.json", body, timeout=15)
    except Exception as e:
        print("  Pushover error:", e)


def main():
    if "--test" in sys.argv:
        pushover("Screener test", "Notifications are working.")
        return
    dry = "--dry" in sys.argv
    coins = json.loads((HERE / "coins.json").read_text())
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    now = time.time(); results = []
    ref_btc, ref_eth = fetch_bars("BTC"), fetch_bars("ETH")
    for sym in coins:
        bars = fetch_bars(sym)
        res = analyze(bars)
        time.sleep(0.2)
        if res:
            res["sym"] = sym
            res["btc"] = beta_vs(bars, ref_btc) if ref_btc else None
            res["eth"] = beta_vs(bars, ref_eth) if ref_eth else None
            results.append(res)
        else:
            print(f"{sym}: no data")
    results.sort(key=lambda x: (-{"buy": 2, "watch": 1}.get(x["status"], 0), -x["score"]))
    print(f"{'COIN':7}{'PRICE':>12}{'24h':>8}{'RSI':>6}{'VOL x':>7}{'SCORE':>6}{'B-BTC':>7}{'B-ETH':>7}  STATUS")
    for x in results:
        print(f"{x['sym']:7}{fmt_price(x['price']):>12}{x['chg24']*100:>7.1f}%"
              f"{x['rsi']:>6.0f}{x['volRatio']:>7.1f}{x['score']:>6}"
              f"{fmt_beta(x['btc']):>7}{fmt_beta(x['eth']):>7}  {x['status']}")
        if x["status"] == "buy" and not dry:
            if now - state.get(x["sym"], 0) >= COOLDOWN_H * 3600:
                on = ", ".join(k for k, ok in x["signals"].items() if ok)
                pushover(f"BUY signal: {x['sym']}",
                         f"Price {fmt_price(x['price'])} ({x['chg24']*100:+.1f}% 24h)\n"
                         f"Score {x['score']}/6: {on}\n"
                         f"Beta to BTC {fmt_beta(x['btc'])}\n"
                         f"Ref. stop {fmt_price(x['stop'])} / target {fmt_price(x['target'])}")
                state[x["sym"]] = now
    if not dry:
        STATE.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
