#!/usr/bin/env python3
"""
contest_watch.py — poll audit-CONTEST platforms (Sherlock, Cantina, Code4rena)
and Telegram-alert when a contest OPENS for submission or is newly announced.

Companion to programs_scout_watch.py (HackenProof). Same Telegram pipeline + state
diff so it slots into superscout. Detection is WINDOW-based (start <= now <= end)
so it survives status-string changes. CodeHawks is client-rendered (no clean API);
eyeball codehawks.cyfrin.io/first-flights. See memory [[audit-contest-channel]].

  python3 contest_watch.py                 # poll + diff + alert (the scheduled mode)
  python3 contest_watch.py --seed          # snapshot current state, send no alerts
  python3 contest_watch.py --list          # human listing of OPEN + UPCOMING
  python3 contest_watch.py --test-telegram # send a test alert
"""
import json, os, sys, time, datetime, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "contest_watch_state.json")
TG_CONFIG = os.path.join(HERE, ".telegram_config.json")
JOURNAL = os.path.join(HERE, "scout_log.md")
ALERT_CAP = 10
NOW = datetime.datetime.now(datetime.timezone.utc)
UA = {"User-Agent": "Mozilla/5.0"}

DOMAIN = ("lend","borrow","perp","vault","stak","margin","collateral","amm","dex",
          "swap","liquid","yield","tranche","cdp","stable","option","restak","money market")
# non-EVM / Rust-Move ecosystems: our edge needs a tooling ramp here (see memory)
ALTVM = ("solana","stellar","cosmos","move","sui","aptos","thorchain","near","fuel",
         "sway","anchor","rust","ink!","substrate","polkadot","ton ","cairo","starknet")


def get(url):
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30).read())

def dom(txt):
    t = (txt or "").lower()
    return ",".join(sorted({k[:4] for k in DOMAIN if k in t})) or "?"

def lang(txt):
    t = (txt or "").lower()
    return "Rust/altVM" if any(k in t for k in ALTVM) else "EVM"

def ts(x):
    try: return datetime.datetime.fromtimestamp(int(x), datetime.timezone.utc)
    except Exception: return None

def iso(x):
    try: return datetime.datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    except Exception: return None

def win(s, e):
    if s and e and s <= NOW <= e: return "OPEN"
    if s and s > NOW:             return "UPCOMING"
    return "past"


# --- platform fetchers -> normalized records ---
def fetch_sherlock():
    recs, page, seen = [], 1, set()
    while page <= 3:
        j = get(f"https://audits.sherlock.xyz/api/contests?page={page}")
        for it in j.get("items", []):
            if it["id"] in seen: continue
            seen.add(it["id"])
            s, e = ts(it.get("starts_at")), ts(it.get("ends_at"))
            st = win(s, e)
            if st == "past": continue
            txt = it["title"] + " " + (it.get("short_description") or "")
            recs.append(dict(key=f"sh:{it['id']}", platform="Sherlock", title=it["title"],
                             pot=f"${it.get('prize_pool') or '?'}", st=st, start=s, end=e,
                             dom=dom(txt), lang=lang(txt),
                             url=f"https://audits.sherlock.xyz/contests/{it['id']}"))
        nxt = j.get("next_page")
        if not nxt: break
        page = nxt if isinstance(nxt, int) else page + 1
    return recs

def fetch_cantina():
    recs = []
    for c in get("https://cantina.xyz/api/v0/competitions"):
        tf = c.get("timeframe") or {}
        s, e = iso(tf.get("startDate") or tf.get("start")), iso(tf.get("endDate") or tf.get("end"))
        st = win(s, e) if (s or e) else ("OPEN" if c.get("status") not in ("complete", "judging") else "past")
        if st == "past": continue
        recs.append(dict(key=f"ca:{c['id']}", platform="Cantina", title=c["name"],
                         pot=f"{c.get('totalRewardPot')} {c.get('currencyCode')}", st=st, start=s, end=e,
                         dom=dom(c["name"]), lang=lang(c["name"]),
                         url=c.get("url") or "https://cantina.xyz/competitions"))
    return recs

def fetch_c4():
    recs = []
    for a in get("https://code4rena.com/api/v1/audits")["data"]["audits"]:
        s, e = iso(a.get("startTime")), iso(a.get("endTime"))
        st = win(s, e)
        if st == "past": continue
        txt = a["title"] + " " + (a.get("league") or "")
        recs.append(dict(key=f"c4:{a.get('slug') or a.get('uid')}", platform="Code4rena", title=a["title"],
                         pot=a.get("formattedAmount"), st=st, start=s, end=e,
                         dom=dom(txt), lang=lang(txt),
                         url=f"https://code4rena.com/audits/{a.get('slug','')}"))
    return recs

def collect():
    recs = []
    for f in (fetch_sherlock, fetch_cantina, fetch_c4):
        try: recs += f()
        except Exception as e: print(f"{f.__name__} ERR: {type(e).__name__}: {e}")
    return recs


# --- Telegram (same creds/pipeline as programs_scout_watch.py) ---
def _tg_creds():
    tok, chat = os.environ.get("TON_WATCH_TG_TOKEN"), os.environ.get("TON_WATCH_TG_CHAT")
    if tok and chat: return tok, chat
    if os.path.exists(TG_CONFIG):
        try:
            c = json.load(open(TG_CONFIG)); return c.get("token"), c.get("chat_id")
        except (OSError, ValueError): pass
    return None, None

def notify_telegram(text):
    tok, chat = _tg_creds()
    if not tok or not chat:
        print("telegram NOT SENT - no token/chat_id"); return False
    try:
        data = urllib.parse.urlencode({"chat_id": str(chat), "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        print(f"telegram notify failed: {e}"); return False


# --- state ---
def load_state():
    if os.path.exists(STATE):
        try: return json.load(open(STATE))
        except (OSError, ValueError): pass
    return {}

def save_state(recs):
    snap = {r["key"]: {"st": r["st"], "title": r["title"], "platform": r["platform"]} for r in recs}
    json.dump(snap, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def _fmt(r):
    end = r["end"].strftime("%Y-%m-%d") if r["end"] else "?"
    flag = "✅" if r["lang"] == "EVM" else "⚠️"
    tag = "🟢 OPEN" if r["st"] == "OPEN" else "🔜 upcoming"
    return (f"• {tag} · <b>{r['platform']}</b> · {flag}{r['lang']} · {r['dom']} · {r['pot']} · ends {end}\n"
            f"  <a href=\"{r['url']}\">{r['title'][:60]}</a>")


def run():
    recs = collect()
    state = load_state()
    fresh = []  # newly OPEN or newly announced, or upcoming->open transition
    for r in recs:
        old = state.get(r["key"])
        if old is None:
            if r["st"] in ("OPEN", "UPCOMING"): fresh.append(r)
        elif old.get("st") == "UPCOMING" and r["st"] == "OPEN":
            fresh.append(r)
    save_state(recs)

    if not fresh:
        print(f"[{NOW:%Y-%m-%d %H:%M}] contest_watch: no new contests "
              f"({len(recs)} open/upcoming tracked)")
        return

    # EVM/our-domain first
    fresh.sort(key=lambda r: (r["lang"] != "EVM", r["dom"] == "?"))
    lines = ["🏆 <b>Audit contest alert</b> — new / now-open"]
    for r in fresh[:ALERT_CAP]:
        lines.append(_fmt(r))
    sent = notify_telegram("\n".join(lines))
    print(f"[{NOW:%Y-%m-%d %H:%M}] contest_watch ALERT: {len(fresh)} new · TG sent={sent}")
    try:
        with open(JOURNAL, "a", encoding="utf-8") as f:
            f.write(f"\n[{NOW:%Y-%m-%d %H:%M}] contest_watch: {len(fresh)} new: "
                    + "; ".join(f"{r['platform']}/{r['title']}" for r in fresh[:3]) + "\n")
    except OSError:
        pass


def seed():
    recs = collect()
    save_state(recs)
    print(f"seeded {len(recs)} open/upcoming contests into "
          f"{os.path.basename(STATE)} (no alerts sent)")


def list_mode():
    recs = collect()
    openr = [r for r in recs if r["st"] == "OPEN"]
    upc = [r for r in recs if r["st"] == "UPCOMING"]
    print(f"# contest_watch {NOW:%Y-%m-%d %H:%M}Z — OPEN {len(openr)} | UPCOMING {len(upc)}\n")
    for r in sorted(openr + upc, key=lambda r: (r["st"] != "OPEN", r["end"] or NOW)):
        end = r["end"].strftime("%Y-%m-%d") if r["end"] else "?"
        print(f"  [{r['st']:8}] {r['platform']:9} {r['lang']:9} {r['dom']:14} {str(r['pot'])[:18]:18} "
              f"ends {end}  {r['title'][:34]}")
    if not recs:
        print("  nothing open or announced — between waves.")


if __name__ == "__main__":
    if "--test-telegram" in sys.argv:
        print("TG test:", notify_telegram("🏆 contest_watch: test alert ✅"))
    elif "--seed" in sys.argv:
        seed()
    elif "--list" in sys.argv:
        list_mode()
    else:
        try:
            run()
        except Exception as e:
            notify_telegram(f"⚠️ <b>contest_watch crashed</b>\n{type(e).__name__}: {e}")
            raise
