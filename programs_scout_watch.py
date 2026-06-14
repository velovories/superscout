#!/usr/bin/env python3
"""
programs_scout_watch.py — scheduled scout + Telegram alerts.

Re-runs programs_scout, diffs against last snapshot, and pings Telegram ONLY on
high-signal changes (new +EV programs, especially smart-contract scope where
duplicates still pay, or a program flipping into +EV). Anti-spam by design —
the campaign rules everywhere punish noise, so we only fire on things worth a look.

Wire via launchd (com.ton.programscout) every ~12h. Fail-safe: never raises.
"""
import json, os, sys, datetime, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import programs_scout as ps

STATE = os.path.join(HERE, "scout_watch_state.json")
TG_CONFIG = os.path.join(HERE, ".telegram_config.json")
JOURNAL = os.path.join(HERE, "claude_notes.md")
ALERT_CAP = 8


# ── Telegram (mirrors fresh_commit_watch, kept local to avoid import side-effects) ──
def _tg_creds():
    tok = os.environ.get("TON_WATCH_TG_TOKEN")
    chat = os.environ.get("TON_WATCH_TG_CHAT")
    if tok and chat:
        return tok, chat
    if os.path.exists(TG_CONFIG):
        try:
            c = json.load(open(TG_CONFIG))
            return c.get("token"), c.get("chat_id")
        except (OSError, ValueError):
            pass
    return None, None


def notify_telegram(text):
    tok, chat = _tg_creds()
    if not tok or not chat:
        print("telegram NOT SENT — no token/chat_id")
        return False
    try:
        data = urllib.parse.urlencode({
            "chat_id": str(chat), "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        print(f"telegram notify failed: {e}")
        return False


# ── state ──
def load_state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE))
        except (OSError, ValueError):
            pass
    return {}


def save_state(snapshot):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=1)


def snap(rec):
    return {
        "ev": rec["scores"]["ev"], "verdict": rec["verdict"],
        "dup_pays": rec.get("dup_pays"), "max_bounty": rec.get("max_bounty"),
        "domains": rec.get("domains") or [], "title": rec.get("title"),
        "url": rec.get("url"), "status": rec.get("status"),
        "is_audit": rec.get("is_audit"), "is_open": rec.get("is_open"),
        "end_date": rec.get("end_date"),
    }


# ── diff / alert logic (high-signal only) ──
def interesting_new(rec):
    doms = rec.get("domains") or []
    sc = "smart_contract" in doms
    # audit/DualDefense contests are time-boxed: only alert while the window is OPEN
    if rec.get("is_audit"):
        if not rec.get("is_open"):
            return False
        # an open SC/chain audit is the rep-friendly dup-pay surface — always flag it
        if sc or "blockchain" in doms:
            return True
    if rec["verdict"] == "+EV":
        return True
    if sc and rec.get("dup_pays") is True:
        return True
    if sc and rec["verdict"] != "-EV" and any("young" in w or "fresh" in w for w in rec.get("why", [])):
        return True
    return False


def material_change(old, rec):
    # paused → live = a buy signal (and live → paused worth knowing)
    os_, ns_ = str(old.get("status") or "").upper(), str(rec.get("status") or "").upper()
    if os_ == "PAUSED" and ns_ == "LIVE":
        return "▶ RESUMED (now LIVE — submittable again)"
    if os_ == "LIVE" and ns_ == "PAUSED":
        return "⏸ paused (no longer submittable)"
    # crossed into +EV
    if old.get("verdict") != "+EV" and rec["verdict"] == "+EV":
        return "→ now +EV"
    # duplicates started paying
    if old.get("dup_pays") is not True and rec.get("dup_pays") is True:
        return "→ now pays duplicates (pool)"
    # ceiling jumped a lot
    ob, nb = old.get("max_bounty") or 0, rec.get("max_bounty") or 0
    if nb >= 2 * max(ob, 1) and nb - ob >= 50000:
        return f"→ ceiling ${ob:.0f}→${nb:.0f}"
    return None


def dup_tag(d):
    return {True: "dup✓", False: "dup✗"}.get(d, "dup?")


def run():
    payload = ps.run(include_others=False, cache_only=False)  # HackenProof primary
    hp = payload["hackenproof"]
    state = load_state()
    prev_count = len(state)

    # degraded-scrape heartbeat: if we parsed far fewer than the healthy baseline,
    # the scrape likely choked (network / SPA-shell). Alert, and DO NOT clobber the
    # dedup baseline with a tiny snapshot (else next run sees the rest as "new" → spam).
    if prev_count >= 50 and len(hp) < 0.6 * prev_count:
        notify_telegram(
            f"⚠️ <b>Program scout degraded</b>\n"
            f"parsed only {len(hp)} of ~{prev_count} programs this run — likely a "
            f"scrape/network issue. Baseline preserved; will retry next cycle.")
        print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M}] scout DEGRADED: "
              f"{len(hp)}/{prev_count} parsed — state preserved, no diff")
        return

    new_hits, changed_hits = [], []
    for rec in hp:
        slug = rec["slug"]
        old = state.get(slug)
        if old is None:
            if interesting_new(rec):
                new_hits.append(rec)
        else:
            note = material_change(old, rec)
            if note:
                changed_hits.append((rec, note))

    # rebuild full snapshot
    save_state({rec["slug"]: snap(rec) for rec in hp})

    if not new_hits and not changed_hits:
        print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M}] scout: no high-signal changes "
              f"({len(hp)} programs tracked)")
        return

    new_hits.sort(key=lambda r: r["scores"]["ev"], reverse=True)
    lines = ["🧭 <b>Program scout</b> — new opportunities"]
    for rec in new_hits[:ALERT_CAP]:
        dom = ",".join(d[:4] for d in (rec.get("domains") or [])) or "?"
        tag = f"🧪 AUDIT (ends {rec.get('end_date')}) · " if rec.get("is_audit") else ""
        lines.append(
            f"• {tag}<b>EV {rec['scores']['ev']}</b> {rec['verdict']} · {dup_tag(rec.get('dup_pays'))} · "
            f"{dom} · ≤${(rec.get('max_bounty') or 0):.0f}\n"
            f"  <a href=\"{rec['url']}\">{rec.get('title') or rec['slug']}</a>")
    for rec, note in changed_hits[:ALERT_CAP]:
        lines.append(
            f"• <b>Δ</b> {note} · EV {rec['scores']['ev']} · {dup_tag(rec.get('dup_pays'))}\n"
            f"  <a href=\"{rec['url']}\">{rec.get('title') or rec['slug']}</a>")
    msg = "\n".join(lines)
    sent = notify_telegram(msg)
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M}] scout alert: "
          f"{len(new_hits)} new, {len(changed_hits)} changed · TG sent={sent}")

    # one-line journal trace, matching fresh_commit_watch convention
    try:
        with open(JOURNAL, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.datetime.now():%Y-%m-%d %H:%M}] 🧭 scout: "
                    f"{len(new_hits)} new +EV-ish, {len(changed_hits)} changed "
                    f"(of {len(hp)} HP programs). Top: "
                    + "; ".join(f"{r.get('title')} (EV {r['scores']['ev']})" for r in new_hits[:3])
                    + "\n")
    except OSError:
        pass


def seed():
    """Arm the watcher silently: snapshot current state, send NO alerts.
    Uses cache so it is instant. After this, run() only fires on real deltas."""
    payload = ps.run(include_others=False, cache_only=True)
    hp = payload["hackenproof"]
    save_state({rec["slug"]: snap(rec) for rec in hp})
    print(f"seeded {len(hp)} programs into {os.path.basename(STATE)} (no alerts sent)")


def sentinel():
    """Fast launch-detector (run every ~15min): poll ONLY the listing pages for
    NEW slugs (cheap — a handful of requests, not all ~131 detail pages). On a
    genuinely new program/audit, fetch just that one detail page, score it, fold it
    into the shared baseline, and Telegram-alert if interesting (open audits + new
    +EV bounties). The full deep scrape with EV/dup scoring stays on the 3h run()."""
    state = load_state()
    known = set(state.keys())
    SHORT_TTL = 600  # so a 15-min poll re-fetches the listing instead of reusing 3h cache
    try:
        bounty = ps.hp_list_slugs(cache_only=False, ttl=SHORT_TTL)
        audit = ps.hp_audit_list_slugs(cache_only=False, ttl=SHORT_TTL)
    except Exception as e:
        notify_telegram(f"⚠️ <b>Scout sentinel error</b>\n{type(e).__name__}: {e}")
        return
    new = ([(s, "programs") for s in bounty if s not in known]
           + [(s, "audit-programs") for s in audit if s not in known])
    if not new:
        print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M}] sentinel: no new slugs ({len(known)} known)")
        return

    hits = []
    for slug, base in new:
        try:
            rec = ps.hp_program(slug, cache_only=False, base=base)
        except Exception:
            rec = None
        if not rec:
            continue
        rec = ps.score(rec)
        state[slug] = snap(rec)            # fold into baseline so the 3h run won't re-alert
        if interesting_new(rec):
            hits.append(rec)
    save_state(state)

    if not hits:
        print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M}] sentinel: {len(new)} new slug(s), none interesting")
        return
    hits.sort(key=lambda r: r["scores"]["ev"], reverse=True)
    lines = ["⚡ <b>Scout sentinel</b> — fresh launch detected"]
    for rec in hits[:ALERT_CAP]:
        dom = ",".join(d[:4] for d in (rec.get("domains") or [])) or "?"
        tag = f"🧪 AUDIT (ends {rec.get('end_date')}) · " if rec.get("is_audit") else ""
        lines.append(
            f"• {tag}<b>EV {rec['scores']['ev']}</b> {rec['verdict']} · {dup_tag(rec.get('dup_pays'))} · "
            f"{dom} · ≤${(rec.get('max_bounty') or 0):.0f}\n"
            f"  <a href=\"{rec['url']}\">{rec.get('title') or rec['slug']}</a>")
    sent = notify_telegram("\n".join(lines))
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M}] sentinel ALERT: {len(hits)} new interesting · TG sent={sent}")
    try:
        with open(JOURNAL, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.datetime.now():%Y-%m-%d %H:%M}] ⚡ sentinel: {len(hits)} fresh launch(es): "
                    + "; ".join(f"{r.get('title')} (EV {r['scores']['ev']})" for r in hits[:3]) + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    if "--test-telegram" in sys.argv:
        print("TG test:", notify_telegram("🧭 scout watcher: test alert ✅"))
    elif "--seed" in sys.argv:
        seed()
    elif "--sentinel" in sys.argv:
        try:
            sentinel()
        except Exception as e:
            notify_telegram(f"⚠️ <b>Scout sentinel crashed</b>\n{type(e).__name__}: {e}")
            raise
    else:
        try:
            run()
        except Exception as e:
            # crash heartbeat: a hard failure must not be silent
            notify_telegram(f"⚠️ <b>Program scout crashed</b>\n{type(e).__name__}: {e}")
            raise
