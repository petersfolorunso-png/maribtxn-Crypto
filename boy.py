"""
Crypto alert bot -> Telegram. Free APIs only.
Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Run every ~5 min (GitHub Actions cron). State is kept in state.json.
"""
import os, json, time, html
from urllib.parse import quote
import requests, feedparser

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE = "state.json"

MAX_AGE_H = 6          # ignore news older than this
MAX_MSGS = 15          # cap per run so you never get flooded
PM_MOVE = 0.10         # Polymarket: alert on 10+ point swing between runs
PM_MIN_VOL = 50_000    # Polymarket: min 24h volume
HL_FUNDING = 0.0002    # Hyperliquid: hourly funding (0.02%/hr) alert level
HL_OI_JUMP = 0.15      # Hyperliquid: +15% open interest between runs
HL_MIN_OI = 2_000_000  # ignore tiny markets (USD)


def gnews(q):
    return f"https://news.google.com/rss/search?q={quote(q)}+when:1d&hl=en-US&gl=US&ceid=US:en"


# Edit freely. Verify any direct feed URL opens in your browser.
FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "The Block": "https://www.theblock.co/rss.xml",
    "Decrypt": "https://decrypt.co/feed",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Rekt": "https://rekt.news/rss.xml",
    "GNews: hacks": gnews("crypto hack OR exploit OR drained"),
    "GNews: policy": gnews("crypto SEC OR CFTC OR stablecoin OR regulation OR ban"),
    "GNews: perps/pred": gnews("Hyperliquid OR Polymarket OR Kalshi OR perp DEX"),
}

HOT = ["hack", "exploit", "drained", "stolen", "sec ", "cftc", "ban", "lawsuit",
       "etf", "approved", "arrest", "bankrupt", "depeg", "outage", "halt"]


def load():
    try:
        with open(STATE_FILE) as f:
            return json.load(f), False
    except Exception:
        return {"seen": [], "pm": {}, "hl_oi": {}, "hl_flag": {}}, True


def save(st):
    st["seen"] = st["seen"][-3000:]
    with open(STATE_FILE, "w") as f:
        json.dump(st, f)


def send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": False},
        timeout=20,
    )
    if not r.ok:
        print("telegram error", r.text)


def news(st, first):
    out = []
    cutoff = time.time() - MAX_AGE_H * 3600
    for name, url in FEEDS.items():
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print("feed fail", name, e)
            continue
        for e in feed.entries[:25]:
            uid = e.get("id") or e.get("link")
            if not uid or uid in st["seen"]:
                continue
            st["seen"].append(uid)
            if first:
                continue
            ts = e.get("published_parsed")
            if ts and time.mktime(ts) < cutoff:
                continue
            title = e.get("title", "")
            hot = any(k in title.lower() for k in HOT)
            tag = "🚨" if hot else "📰"
            out.append((0 if hot else 1,
                        f"{tag} <b>{html.escape(title)}</b>\n{html.escape(name)}\n{e.get('link','')}"))
    out.sort(key=lambda x: x[0])
    return [m for _, m in out]


def polymarket(st, first):
    out = []
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active": "true", "closed": "false", "order": "volume24hr",
                    "ascending": "false", "limit": 50},
            timeout=20,
        )
        markets = r.json()
    except Exception as e:
        print("polymarket fail", e)
        return out
    for m in markets:
        try:
            price = float(json.loads(m["outcomePrices"])[0])
        except Exception:
            continue
        mid = str(m.get("id"))
        old = st["pm"].get(mid)
        st["pm"][mid] = price
        if first or old is None or float(m.get("volume24hr") or 0) < PM_MIN_VOL:
            continue
        if abs(price - old) >= PM_MOVE:
            ev = (m.get("events") or [{}])[0].get("slug") or m.get("slug")
            arrow = "📈" if price > old else "📉"
            out.append(f"{arrow} <b>Polymarket swing</b>\n{html.escape(m.get('question',''))}\n"
                       f"Yes: {old:.0%} → {price:.0%}\nhttps://polymarket.com/event/{ev}")
    return out


def hyperliquid(st, first):
    out = []
    try:
        meta, ctxs = requests.post("https://api.hyperliquid.xyz/info",
                                   json={"type": "metaAndAssetCtxs"}, timeout=20).json()
    except Exception as e:
        print("hyperliquid fail", e)
        return out
    for asset, c in zip(meta["universe"], ctxs):
        coin = asset["name"]
        try:
            funding = float(c["funding"])
            oi_usd = float(c["openInterest"]) * float(c["markPx"])
        except Exception:
            continue
        old_oi = st["hl_oi"].get(coin)
        st["hl_oi"][coin] = oi_usd
        flagged = st["hl_flag"].get(coin, False)
        if abs(funding) >= HL_FUNDING and oi_usd >= HL_MIN_OI:
            if not flagged and not first:
                out.append(f"🔥 <b>{coin} funding spike</b> (Hyperliquid)\n"
                           f"{funding*100:.4f}%/hr (~{funding*24*365*100:.0f}% APR)\n"
                           f"OI ${oi_usd/1e6:.1f}M")
            st["hl_flag"][coin] = True
        else:
            st["hl_flag"][coin] = False
        if (not first and old_oi and oi_usd >= HL_MIN_OI
                and oi_usd / old_oi - 1 >= HL_OI_JUMP):
            out.append(f"💥 <b>{coin} open interest surge</b> (Hyperliquid)\n"
                       f"${old_oi/1e6:.1f}M → ${oi_usd/1e6:.1f}M "
                       f"(+{(oi_usd/old_oi-1)*100:.0f}%)")
    return out


def main():
    st, first = load()
    msgs = polymarket(st, first) + hyperliquid(st, first) + news(st, first)
    save(st)
    for m in msgs[:MAX_MSGS]:
        send(m)
        time.sleep(1)
    print(f"sent {min(len(msgs), MAX_MSGS)} of {len(msgs)} (first_run={first})")


if __name__ == "__main__":
    main()
