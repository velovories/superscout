#!/usr/bin/env python3
"""
programs_scout.py — Bug-bounty / audit PROGRAM scorer.

Scrapes public listing pages (no login needed for the signals we use), normalizes
every program into one schema, then scores each on an EV model tuned to OUR edge
(deep smart-contract / protocol review; web is our weak axis). The single biggest
lever is `dup_pays`: standard bounties are first-reporter (a duplicate = $0), while
audit contests use a shared pool where duplicates still split money.

Primary platform = HackenProof (our home turf, parsed in full from Nuxt __NUXT_DATA__).
Secondary platforms = Immunefi / Code4rena / Sherlock (best-effort, separate section).

Outputs:
  - programs_scout.json   (full normalized + scored records)
  - CLI ranked tables (HackenProof first, then others)

Usage:
  python3 programs_scout.py                 # scan + print + write json
  python3 programs_scout.py --no-others     # HackenProof only (fast)
  python3 programs_scout.py --json-only      # no table, just refresh json (for watcher)
  python3 programs_scout.py --cache-only     # use on-disk cache, no network
"""
import json, os, re, sys, time, html, urllib.request, urllib.error, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "scout_cache")
OUT_JSON = os.path.join(HERE, "programs_scout.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ScoutBot/1.0"
POLITE_DELAY = 0.8          # seconds between network requests
CACHE_TTL = 6 * 3600        # reuse cached page if younger than this

# ──────────────────────────────────────────────────────────────────────────
#  OUR PROFILE — tune here. Drives edge_fit. (from claude_notes campaign doctrine)
# ──────────────────────────────────────────────────────────────────────────
PROFILE = {
    "our_max_reputation": 140,           # HackenProof rep we hold
    "domain_base": {                     # how well a domain fits our edge (0-100)
        "smart_contract": 82, "blockchain": 78, "protocol": 80,
        "api": 48, "web": 30, "mobile": 22,
    },
    # keyword → tech bucket (inferred from title/company/scope text)
    "strong_tech": [
        "evm", "solidity", "perp", "perpetual", "derivativ", "amm", "dex",
        "lending", "lend", "vault", "defi", "gmx", "staking", "ton", "tlb",
        "func", "tolk", "jetton", "uniswap", "yield", "margin", "futures",
    ],
    "weak_tech": [
        "sui", "move ", "aptos", "solana", "anchor", "cairo", "starknet",
        "mina", "zeko", "cosmwasm", "tact",
    ],
    "weights": {                         # EV blend (sum≈1.0)
        "payout_econ": 0.30, "edge_fit": 0.26, "freshness_opportunity": 0.16,
        "anti_saturation": 0.16, "accessibility": 0.12,
    },
}

# ──────────────────────────────────────────────────────────────────────────
#  HTTP + cache
# ──────────────────────────────────────────────────────────────────────────
def _cache_path(key):
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", key)
    return os.path.join(CACHE_DIR, safe + ".html")

def fetch(url, key=None, cache_only=False, ttl=CACHE_TTL, validate=None, retries=0):
    """Fetch with on-disk cache.

    Hardening (fixes the "27/121 parsed" flaky-fresh-fetch bug): when `validate`
    is given, a freshly fetched body is only accepted (and only overwrites the
    cache) if it passes — so a data-less SPA shell never clobbers a good copy.
    On a failed/shell fetch we retry `retries` times, then fall back to the
    last-good cached copy rather than dropping the program.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = key or url
    cp = _cache_path(key)
    cached = None
    if os.path.exists(cp):
        with open(cp, encoding="utf-8", errors="replace") as f:
            cached = f.read()
        age = time.time() - os.path.getmtime(cp)
        # serve cache when offline, or fresh-enough AND (no validator or it passes)
        if cache_only or (age < ttl and (validate is None or validate(cached))):
            return cached
    if cache_only:
        return cached  # may be None — best we can do without network
    attempt = 0
    last = None
    while True:
        time.sleep(POLITE_DELAY)
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
        try:
            data = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            data = None
        if data is not None:
            last = data
            if validate is None or validate(data):
                with open(cp, "w", encoding="utf-8") as f:
                    f.write(data)
                return data
        attempt += 1
        if attempt > retries:
            break
    # fresh fetch failed or only yielded a shell → prefer last-good cache
    if cached is not None:
        return cached
    return last  # may be None or an unvalidated body; caller decides

# ──────────────────────────────────────────────────────────────────────────
#  Nuxt __NUXT_DATA__ resolver (devalue-style indexed array)
# ──────────────────────────────────────────────────────────────────────────
class Nuxt:
    """Resolve Nuxt payload: every value is an index into a flat array; small
    ints are references, containers may be tagged ['Reactive', idx]."""
    TAGS = {"Reactive", "ShallowReactive", "Ref", "ShallowRef", "EmptyRef"}

    def __init__(self, arr):
        self.arr = arr
        self._memo = {}

    def resolve(self, idx, _path=()):
        if not isinstance(idx, int):
            return idx
        if idx in self._memo:
            return self._memo[idx]
        if idx in _path or idx < 0 or idx >= len(self.arr):
            return None                                  # cycle / oob
        v = self.arr[idx]
        path = _path + (idx,)
        if isinstance(v, dict):
            out = {k: self.resolve(val, path) for k, val in v.items()}
        elif isinstance(v, list):
            if len(v) == 2 and v[0] in self.TAGS:
                out = self.resolve(v[1], path)
            else:
                out = [self.resolve(x, path) for x in v]
        else:
            out = v
        self._memo[idx] = out
        return out

def extract_nuxt(page):
    m = re.search(r'id="__NUXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        return None
    try:
        return Nuxt(json.loads(m.group(1)))
    except Exception:
        return None

# ──────────────────────────────────────────────────────────────────────────
#  HackenProof
# ──────────────────────────────────────────────────────────────────────────
HP = "https://hackenproof.com"
# non-program paths the /programs|/audit-programs regex can spuriously match
_NOT_PROGRAMS = {"weekly", "new", "all", "popular", "trending", "page"}

def _page_has_program(h):
    """True iff the page server-rendered a real program payload (not an empty
    SPA shell). Used as the fetch() validator so flaky fresh fetches that return
    a shell are retried / fall back to cache instead of dropping the program."""
    if not h:
        return False
    nx = extract_nuxt(h)
    if not nx:
        return False
    root = nx.resolve(0) or {}
    data = root.get("data")
    return isinstance(data, dict) and isinstance(data.get("program"), dict)

def hp_list_slugs(cache_only=False, ttl=3 * 3600):
    """Enumerate program slugs across paginated listing until saturation.
    `ttl` controls listing-cache freshness — the sentinel passes a short ttl so a
    15-min poll actually re-fetches the listing instead of reusing a 3h cache."""
    seen, empty_pages = [], 0
    for page in range(1, 13):
        url = f"{HP}/programs" + (f"?page={page}" if page > 1 else "")
        try:
            h = fetch(url, key=f"hp_list_p{page}", cache_only=cache_only, ttl=ttl)
        except Exception:
            break
        if not h:
            break
        found = re.findall(r"/programs/([a-z0-9][a-z0-9-]{2,60})", h)
        new = [s for s in dict.fromkeys(found) if s not in seen and s not in _NOT_PROGRAMS]
        seen.extend(new)
        if not new:
            empty_pages += 1
            if empty_pages >= 2:
                break
        else:
            empty_pages = 0
    return seen

def _money(x):
    """'$10,000' or '10000.0' or 10000 -> float or None."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = re.sub(r"[^\d.]", "", str(x))
    try:
        return float(s) if s else None
    except ValueError:
        return None

def hp_audit_list_slugs(cache_only=False, ttl=3 * 3600):
    """Enumerate AUDIT/DualDefense contest slugs from the PUBLIC /audit-programs
    listing (no login). These are dup-pay shared-pool contests — the rep-friendly
    surface the bounty listing (/programs) doesn't include. Detail pages live at
    /audit-programs/<slug> and parse with the same Nuxt schema as bounties.
    `ttl` controls listing-cache freshness (the sentinel passes a short ttl)."""
    seen, empty_pages = [], 0
    for page in range(1, 13):
        url = f"{HP}/audit-programs" + (f"?page={page}" if page > 1 else "")
        try:
            h = fetch(url, key=f"hp_auditlist_p{page}", cache_only=cache_only, ttl=ttl)
        except Exception:
            break
        if not h:
            break
        found = re.findall(r"/audit-programs/([a-z0-9][a-z0-9-]{2,70})", h)
        new = [s for s in dict.fromkeys(found) if s not in seen and s not in _NOT_PROGRAMS]
        seen.extend(new)
        if not new:
            empty_pages += 1
            if empty_pages >= 2:
                break
        else:
            empty_pages = 0
    return seen


def hp_program(slug, cache_only=False, base="programs"):
    key = f"hp_{slug}" if base == "programs" else f"hp_ap_{slug}"
    h = fetch(f"{HP}/{base}/{slug}", key=key, cache_only=cache_only,
              validate=_page_has_program, retries=2)
    if not h:
        return None
    nx = extract_nuxt(h)
    if not nx:
        return None
    root = nx.resolve(0) or {}
    prog = None
    data = root.get("data")
    if isinstance(data, dict):
        prog = data.get("program")
    if not isinstance(prog, dict):
        return None

    # scopes / domains
    scopes = prog.get("scopes") or []
    domains, targets = set(), []
    for sc in scopes:
        if not isinstance(sc, dict):
            continue
        title = (sc.get("title") or "").strip()
        targets.append({"target": sc.get("target"), "type": title,
                        "criticality": sc.get("criticality"),
                        "oos": bool(sc.get("out_of_scope"))})
        t = title.lower()
        if "smart" in t or "contract" in t:
            domains.add("smart_contract")
        elif "web" in t:
            domains.add("web")
        elif "mobile" in t or "android" in t or "ios" in t:
            domains.add("mobile")
        elif "api" in t:
            domains.add("api")
        elif t:
            domains.add("blockchain")

    rw = prog.get("rewards") or {}
    sev = {
        "critical": [_money(rw.get("critical_min")), _money(rw.get("critical_max"))],
        "high": [_money(rw.get("high_min")), _money(rw.get("high_max"))],
        "medium": [_money(rw.get("medium_min")), _money(rw.get("medium_max"))],
        "low": [_money(rw.get("low_min")), _money(rw.get("low_max"))],
    }

    rules_text = " ".join(str(prog.get(k) or "") for k in
                          ("programRules", "eligibilityAndCoordinateDisclosure",
                           "focusArea", "description"))

    _end = prog.get("endDate")
    _ed = _parse_date(_end) if _end else None
    rec = {
        "platform": "HackenProof",
        "slug": slug,
        "url": f"{HP}/{base}/{slug}",
        "title": prog.get("title"),
        "company": prog.get("companyName"),
        "is_audit": bool(prog.get("isAudit")),
        "is_open": (_ed is None) or (_ed.date() >= datetime.date.today()),
        "is_triaged": bool(prog.get("isTriaged")),
        "is_unending": bool(prog.get("isUnending")),
        "start_date": prog.get("startDate"),
        "end_date": prog.get("endDate"),
        "status": prog.get("status"),
        "domains": sorted(domains),
        "targets": targets,
        "min_bounty": _money(prog.get("minBounty")),
        "max_bounty": _money(prog.get("maxBounty")),
        "severity_caps": sev,
        "total_rewards": _money(prog.get("totalRewards")),
        "submissions": prog.get("submittedReports"),
        "scope_reviews": prog.get("scopeReview"),
        "reputation_required": bool(prog.get("reputationRequired")),
        "min_reputation": prog.get("minReputation"),
        "poc_required": bool(prog.get("pocRequired")),
        "kyc_required": bool(prog.get("kycRequired")),
        "submission_fee": _money(prog.get("submissionFee")),
        "pool_url": prog.get("poolUrl") or "",
        "deposit_available": bool(prog.get("depositAvailable")),
        "program_of_the_week": bool(prog.get("programOfTheWeek")),
        "_rules_text": rules_text[:4000],
    }
    return rec

# ──────────────────────────────────────────────────────────────────────────
#  dup_pays classification — the dominant EV lever
# ──────────────────────────────────────────────────────────────────────────
def classify_dup_pays(rec):
    """True = shared-pool/contest (duplicates still pay); False = standard
    first-reporter bounty (a duplicate pays $0); None = can't tell.
    NOTE: `deposit_available` is NOT a pool signal — it just means the program is
    funded, and is True for plenty of first-reporter bounties (e.g. DEXX)."""
    t = (rec.get("_rules_text") or "").lower()
    pool_phrase = any(p in t for p in (
        "bounty distribution", "shared pool", "reward pool", "split among",
        "distributed among", "pool is distributed", "rewards are distributed"))

    # explicit pool / distribution language is the strongest signal
    if rec.get("pool_url") or pool_phrase:
        return True, "shared-pool / distribution model → duplicates split the pot"
    # explicit first-reporter language = standard bounty (beats weak heuristics)
    if "first reporter" in t or "first to report" in t or "first valid report" in t:
        return False, "standard bounty: first-reporter only → a duplicate pays $0"
    # flagged audit contest without first-reporter wording → pooled
    if rec.get("is_audit"):
        return True, "audit contest → pooled rewards, duplicates typically still pay"
    # time-boxed but no explicit pool signal — unclear
    if rec.get("end_date") and not rec.get("is_unending"):
        return None, "time-boxed; verify whether pooled or first-reporter"
    return None, "unclear from rules text (verify manually)"

# ──────────────────────────────────────────────────────────────────────────
#  Scoring
# ──────────────────────────────────────────────────────────────────────────
import math

def _clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))

def _parse_date(s):
    if not s:
        return None
    for fmt in ("%d %b %Y", "%Y-%m-%d", "%d %B %Y"):
        try:
            return datetime.datetime.strptime(s.strip(), fmt)
        except (ValueError, TypeError):
            continue
    return None

def score(rec):
    reasons = []

    # program age (used by several components) — None if unknown
    _d = _parse_date(rec.get("start_date"))
    age_days = (datetime.datetime.now() - _d).days if _d else None
    young = age_days is not None and age_days <= 75

    # ---- edge_fit -----------------------------------------------------------
    doms = rec.get("domains") or []
    base = max((PROFILE["domain_base"].get(d, 40) for d in doms), default=45)
    blob = " ".join([str(rec.get("title") or ""), str(rec.get("company") or ""),
                     str(rec.get("slug") or ""),
                     " ".join(t.get("type", "") for t in rec.get("targets") or [])]).lower()
    strong = any(k in blob for k in PROFILE["strong_tech"])
    weak = any(k in blob for k in PROFILE["weak_tech"])
    edge = base + (12 if strong else 0) - (16 if (weak and not strong) else 0)
    edge = _clamp(edge)
    if "smart_contract" in doms:
        reasons.append("smart-contract scope (our edge)")
    if "web" in doms and "smart_contract" not in doms:
        reasons.append("web-only (our weak axis)")
    if weak and not strong:
        reasons.append("ecosystem outside our stack (Sui/Move/Mina/etc.)")

    # ---- payout_econ --------------------------------------------------------
    ceil = rec.get("max_bounty") or 0
    cap_score = _clamp(20 + 28 * math.log10(max(ceil, 10) / 10))   # $10→20, $10k→~104→clamp
    subs = rec.get("submissions") or 0
    total = rec.get("total_rewards") or 0
    ratio = (total / subs) if subs else None
    dup_pays, dup_reason = classify_dup_pays(rec)

    econ = cap_score
    if ratio is not None and subs >= 8:
        # realized payout per submission, a hard reality check
        if ratio >= 250:
            econ += 12; reasons.append(f"strong realized payout (${ratio:.0f}/sub)")
        elif ratio >= 80:
            econ += 4
        elif ratio < 30:
            if young:
                econ -= 7; reasons.append(f"low payout so far (${ratio:.0f}/sub) but young program")
            else:
                econ -= 22; reasons.append(f"poor realized payout (${ratio:.0f}/sub over {subs} subs)")
        elif ratio < 60:
            econ -= (4 if young else 10)
    # duplicate economics × saturation = originality risk
    if dup_pays is True:
        econ += 10; reasons.append("duplicates still pay (pool)")
    elif dup_pays is False:
        sat = (rec.get("scope_reviews") or 0)
        if subs >= 40 or sat >= 8000:
            econ -= 18; reasons.append("first-reporter + picked-over → high dup-risk ($0)")
        else:
            econ -= 6; reasons.append("first-reporter (duplicate = $0)")
    fee = rec.get("submission_fee") or 0
    if fee and ceil:
        if ceil / max(fee, 1) < 50:
            econ -= 6
    econ = _clamp(econ)

    # ---- anti_saturation ----------------------------------------------------
    sr = rec.get("scope_reviews") or 0
    sat_pen = _clamp(math.log10(max(sr, 1) + 1) * 22)              # 25k reviews → ~97
    anti_sat = _clamp(100 - sat_pen)
    if sr >= 15000:
        reasons.append("heavily reviewed (saturated)")

    # ---- freshness_opportunity ---------------------------------------------
    fresh = 50
    if age_days is not None:
        if age_days <= 30:
            fresh = 90; reasons.append("freshly launched (<30d, less picked-over)")
        elif age_days <= 90:
            fresh = 72
        elif age_days <= 180:
            fresh = 55
        else:
            fresh = 38
    if rec.get("program_of_the_week"):
        fresh = max(fresh, 65)

    # ---- accessibility ------------------------------------------------------
    acc = 80
    minrep = rec.get("min_reputation") or 0
    if rec.get("reputation_required") and minrep:
        if minrep > PROFILE["our_max_reputation"]:
            acc -= 45; reasons.append(f"rep gate {minrep} > ours (~{PROFILE['our_max_reputation']})")
        elif minrep > PROFILE["our_max_reputation"] * 0.7:
            acc -= 12
    if rec.get("kyc_required"):
        acc -= 18; reasons.append("KYC required")
    acc = _clamp(acc)

    # ---- blend --------------------------------------------------------------
    w = PROFILE["weights"]
    ev = (w["payout_econ"] * econ + w["edge_fit"] * edge +
          w["freshness_opportunity"] * fresh + w["anti_saturation"] * anti_sat +
          w["accessibility"] * acc)
    ev = _clamp(ev)

    # ---- status: a program you cannot submit to right now has no live EV ----
    status = (rec.get("status") or "").upper()
    is_live = status == "LIVE"
    if status == "PAUSED":
        ev *= 0.55; reasons.insert(0, "⏸ PAUSED — not submittable now (watch for resume)")
    elif status and not is_live:
        ev *= 0.35; reasons.insert(0, f"status {status} — likely not submittable")
    ev = round(ev, 1)
    verdict = "+EV" if ev >= 60 else ("neutral" if ev >= 45 else "-EV")

    rec["is_live"] = is_live
    rec["scores"] = {"ev": ev, "edge_fit": round(edge, 1), "payout_econ": round(econ, 1),
                     "anti_saturation": round(anti_sat, 1), "freshness": round(fresh, 1),
                     "accessibility": round(acc, 1)}
    rec["dup_pays"] = dup_pays
    rec["dup_reason"] = dup_reason
    rec["realized_ratio"] = round(ratio, 1) if ratio is not None else None
    rec["verdict"] = verdict
    rec["why"] = reasons[:5]
    return rec

# ──────────────────────────────────────────────────────────────────────────
#  Secondary platforms — best-effort, graceful degradation
# ──────────────────────────────────────────────────────────────────────────
def others_scout(cache_only=False):
    out = []
    out += _immunefi(cache_only)
    return out

def _immunefi(cache_only=False):
    """Immunefi bounties listing (Next.js). Best-effort: degrade silently."""
    try:
        h = fetch("https://immunefi.com/bug-bounty/", key="immunefi_list",
                  cache_only=cache_only, ttl=12 * 3600)
    except Exception:
        return []
    if not h:
        return []
    recs = []
    # Next data blob: look for project objects with maxBounty
    for m in re.finditer(r'"(?:id|slug)":"([a-z0-9-]{3,50})"[^}]{0,400}?"maximumReward(?:Usd)?":\s*"?(\d[\d.]*)', h):
        slug, mx = m.group(1), m.group(2)
        recs.append({"platform": "Immunefi", "slug": slug,
                     "url": f"https://immunefi.com/bug-bounty/{slug}/",
                     "title": slug.replace("-", " ").title(),
                     "company": slug, "domains": ["smart_contract"],
                     "max_bounty": _money(mx), "min_bounty": None,
                     "is_audit": False, "is_unending": True,
                     "submissions": None, "total_rewards": None, "scope_reviews": None,
                     "reputation_required": False, "min_reputation": 0,
                     "poc_required": True, "kyc_required": False, "submission_fee": 0,
                     "start_date": None, "targets": [], "severity_caps": {},
                     "_rules_text": "", "pool_url": ""})
    # de-dup by slug
    seen, dedup = set(), []
    for r in recs:
        if r["slug"] in seen:
            continue
        seen.add(r["slug"]); dedup.append(r)
    return dedup[:60]

# ──────────────────────────────────────────────────────────────────────────
#  Orchestration + rendering
# ──────────────────────────────────────────────────────────────────────────
def run(include_others=True, cache_only=False):
    hp = []
    seen_slugs = set()
    slugs = hp_list_slugs(cache_only=cache_only)
    for s in slugs:
        try:
            rec = hp_program(s, cache_only=cache_only)
        except Exception as e:
            rec = None
        if rec:
            hp.append(score(rec))
            seen_slugs.add(s)
    # PUBLIC audit/DualDefense contests (/audit-programs) — dup-pay, no login needed
    for s in hp_audit_list_slugs(cache_only=cache_only):
        if s in seen_slugs:
            continue
        try:
            rec = hp_program(s, cache_only=cache_only, base="audit-programs")
        except Exception:
            rec = None
        if rec:
            hp.append(score(rec))
            seen_slugs.add(s)
    others = []
    if include_others:
        for rec in others_scout(cache_only=cache_only):
            try:
                others.append(score(rec))
            except Exception:
                pass
    hp.sort(key=lambda r: r["scores"]["ev"], reverse=True)
    others.sort(key=lambda r: r["scores"]["ev"], reverse=True)
    payload = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "profile": {"our_max_reputation": PROFILE["our_max_reputation"]},
        "hackenproof": hp,
        "others": others,
    }
    # strip bulky internal text before persisting
    for grp in (hp, others):
        for r in grp:
            r.pop("_rules_text", None)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return payload

# ---- pretty CLI ----
C = {"g": "\033[32m", "y": "\033[33m", "r": "\033[31m", "b": "\033[1m",
     "d": "\033[2m", "c": "\033[36m", "x": "\033[0m"}
def _dup_cell(d):
    if d is True:  return f"{C['g']}dup✓{C['x']}"
    if d is False: return f"{C['r']}dup✗{C['x']}"
    return f"{C['y']}dup?{C['x']}"
def _verdict_cell(v):
    return {"+EV": C["g"] + "+EV" + C["x"], "neutral": C["y"] + "neu" + C["x"],
            "-EV": C["r"] + "-EV" + C["x"]}.get(v, v)

def render(payload):
    def table(title, rows):
        print(f"\n{C['b']}{title}{C['x']}  {C['d']}({len(rows)} programs){C['x']}")
        print(f"{C['d']}{'EV':>4}  {'verdict':<8} {'dup':<6} {'domain':<14} "
              f"{'ceiling':>8} {'$/sub':>7} {'rep':>4}  title{C['x']}")
        for r in rows:
            sc = r["scores"]; ceil = r.get("max_bounty") or 0
            ratio = r.get("realized_ratio")
            dom = ",".join(d[:4] for d in (r.get("domains") or [])) or "?"
            rep = r.get("min_reputation") or 0
            print(f"{sc['ev']:>4}  {_verdict_cell(r['verdict']):<17} {_dup_cell(r.get('dup_pays')):<15} "
                  f"{dom:<14} ${ceil:>7.0f} "
                  f"{('$'+format(ratio,'.0f')) if ratio is not None else '—':>7} "
                  f"{rep:>4}  {'' if r.get('is_live', True) else C['y']+'⏸ '+C['x']}"
                  f"{C['c']}{(r.get('title') or r['slug'])[:34]}{C['x']}")
            if r.get("why"):
                print(f"        {C['d']}{' · '.join(r['why'])}{C['x']}")
    print(f"{C['d']}generated {payload['generated_at']} · profile rep≈{payload['profile']['our_max_reputation']}{C['x']}")
    table("🏆 HACKENPROOF (primary)", payload["hackenproof"])
    if payload["others"]:
        table("🌐 OTHER PLATFORMS", payload["others"])

def main(argv):
    include_others = "--no-others" not in argv
    cache_only = "--cache-only" in argv
    json_only = "--json-only" in argv
    payload = run(include_others=include_others, cache_only=cache_only)
    if not json_only:
        render(payload)
    print(f"\n{C['d']}→ wrote {OUT_JSON}{C['x']}")

if __name__ == "__main__":
    main(sys.argv[1:])
