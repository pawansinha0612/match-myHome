#!/usr/bin/env python3
"""
Melbourne daily deal finder — scrapes homely.com.au (NO API KEY REQUIRED).

Runs (typically via GitHub Actions cron) once a day:
  1. Uses headless Chromium (Playwright) to load homely.com.au's for-sale
     search page for each suburb in config.yaml, one page load per suburb.
  2. homely.com.au server-renders a clean JSON payload (Next.js __NEXT_DATA__)
     into every search page — this script reads that JSON directly instead of
     scraping visible text, so it's far more reliable than regex/BeautifulSoup
     over rendered HTML.
  3. Scores each listing 0-100 as a BUYER (land/size, value vs. estimate,
     build age, availability) — not just "can I afford it" — plus a small,
     separately-shown bonus for western-Melbourne suburbs (your preference).
  4. Compares against data/seen_listings.json to flag what's NEW since the
     last run.
  5. Writes a markdown report to reports/<date>.md and reports/latest.md.

Why homely.com.au and not Domain/realestate.com.au: both of those block
scraping outright at the edge (Domain: Akamai WAF, instant 403 before any
page JS runs; realestate.com.au: Kasada JS challenge) — confirmed by testing
with full headless Chromium, stealth flags, and referer headers, all blocked.
homely.com.au was tested the same way and returns real, current listing data
with no blocking.

HONEST LIMITATIONS (inherent to what homely.com.au's data actually contains):
  - No build year field exists in the data at all — build age always scores
    as "unknown" (neutral) unless the listing is tagged new/house-and-land.
  - No independent third-party valuation (like Domain's AVM) is available,
    so "value vs. estimate" always scores neutrally (15/30) — homely doesn't
    provide one.
  - No overlay risk flags (flood/bushfire/heritage) are available.
  - Listing "price" is often marketing text, not a clean number (e.g.
    "Contact Agent", "27SQ/EASTFACING/$649K-$679K"). This script uses
    homely's own internal reference price (loanPrice) as the numeric price
    for all calculations, and shows the agent's original price text
    alongside it in the report for transparency.

Requires: PyYAML, playwright (see requirements.txt), and `playwright install
chromium` to fetch the browser binary.
"""

import os
import re
import sys
import json
import time
import html
import datetime
import webbrowser

try:
    import yaml
    from playwright.sync_api import sync_playwright
except ImportError as e:
    print(f"Missing dependency ({e}). Run: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.yaml")
# Optional, git-ignored overrides for personal settings (e.g. your own
# reference_locations or deposit_pct). Only used where the file exists, so
# the scheduled GitHub run always uses the public config.yaml.
LOCAL_CONFIG_PATH = os.path.join(HERE, "config.local.yaml")
SEEN_PATH = os.path.join(HERE, "data", "seen_listings.json")
REPORTS_DIR = os.path.join(HERE, "reports")

# Companion calculator (stamp duty / LMI / buyer score) — a separate published
# Artifact, not part of this repo. Update this if you republish it elsewhere.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_DELAY_SECONDS = 1.5  # be polite — don't hammer homely's servers
PAGE_TIMEOUT_MS = 25000

NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
RSC_CHUNK_RE = re.compile(r'self\.__next_f\.push\((\[.*?\])\)</script>', re.S)
NEW_HOME_HINTS_RE = re.compile(
    r"house\s*and\s*land|off[- ]the[- ]plan|brand\s*new|display\s*home|\bnew\s*home\b",
    re.IGNORECASE,
)
PRICE_TOKEN_RE = re.compile(r"\$\s?([\d][\d,]*(?:\.\d+)?)([kKmM])?\b|\b(\d{2,4})[kK]\b")
MIN_PLAUSIBLE_PRICE = 50000  # filters out rent-per-week asides etc. from price text
MAX_PLAUSIBLE_PRICE = 50_000_000  # generous ceiling; filters out corrupted upstream values


def extract_price_range(text):
    """Pull real $ figures out of the agent's own price text. Homely's
    internal loanPrice field can be badly stale (seen a listing marked
    "$3,250,000 - $3,450,000" by the agent with loanPrice=605000) — so the
    agent's own text is the source of truth whenever it contains a number,
    and loanPrice is only a fallback for text like "Contact Agent"."""
    if not text:
        return None, None
    values = []
    for m in PRICE_TOKEN_RE.finditer(text):
        if m.group(1):
            num = float(m.group(1).replace(",", ""))
            suffix = (m.group(2) or "").lower()
        else:
            num = float(m.group(3))
            suffix = "k"
        if suffix == "k":
            num *= 1000
        elif suffix == "m":
            num *= 1_000_000
        values.append(num)
    # Filter out anything implausible for a house sale price (e.g. a
    # "$561 rent p/week" aside, or a stray huge number) so it can't get
    # mistaken for the sale price itself.
    values = [v for v in values if MIN_PLAUSIBLE_PRICE <= v <= MAX_PLAUSIBLE_PRICE]
    if not values:
        return None, None
    return min(values), max(values)

# Western-Melbourne suburbs get a small, separately-shown scoring bonus
# (not folded into the /100 score) per your stated preference for the west.
WEST_SUBURBS = {
    "truganina", "tarneit", "point cook", "werribee", "williamstown",
    "altona", "altona north", "altona meadows", "laverton", "hoppers crossing",
    "wyndham vale", "manor lakes", "williams landing", "seabrook", "spotswood",
    "newport", "yarraville", "footscray", "maidstone", "sunshine", "st albans",
    "deer park", "caroline springs", "melton", "rockbank", "aintree",
    "plumpton", "kealba", "keilor", "sydenham", "taylors lakes", "delahey",
}
WEST_SIDE_BONUS = 5  # shown separately as "+N west-side bonus", not part of /100


def infer_is_new_home(raw):
    features = raw.get("features") or {}
    other = features.get("otherFeatures") or ""
    title = raw.get("title") or ""
    price_text = (raw.get("priceDetails") or {}).get("longDescription") or ""
    combined = f"{other} {title} {price_text}"
    return bool(NEW_HOME_HINTS_RE.search(combined))


def infer_status(raw):
    if raw.get("auction"):
        return "Auction scheduled"
    listing_details = ((raw.get("saleDetails") or {}).get("listingDetails")) or {}
    if listing_details.get("isUnderOffer"):
        return "Under offer"
    if raw.get("statusType") in ("sold", "leased", "unavailable", "offmarket"):
        return "Under contract"
    return "Available"


def parse_listing(raw):
    address = (raw.get("address") or {}).get("longAddress")
    canonical_uri = raw.get("canonicalUri") or ""
    url = f"https://www.homely.com.au{canonical_uri}" if canonical_uri else None

    land_features = raw.get("landFeatures") or {}
    land_size = land_features.get("areaSqm")
    # homely's own upstream data occasionally has garbage land sizes (e.g. a
    # "Lot" house-and-land listing reporting 342,000m²) — a standard house
    # block in these suburbs is never anywhere near 1 hectare, so treat
    # implausible values as unknown rather than let bad data dominate scoring.
    if land_size is not None and (land_size <= 0 or land_size > 10000):
        land_size = None

    features = raw.get("features") or {}
    bedrooms = features.get("bedrooms") or None
    bathrooms = features.get("bathrooms") or None
    car_spaces = features.get("cars") or None

    price_display = (raw.get("priceDetails") or {}).get("longDescription")
    price_low, price_high = extract_price_range(price_display)
    if price_low is None:
        loan_price = (raw.get("saleDetails") or {}).get("loanPrice")
        # loanPrice is itself sometimes corrupted upstream (seen values of
        # $1, $2, $3, and $350,000,000) — apply the same sanity bounds.
        if loan_price and MIN_PLAUSIBLE_PRICE <= loan_price <= MAX_PLAUSIBLE_PRICE:
            price_low = price_high = loan_price

    lat_long = (raw.get("location") or {}).get("latLong") or {}

    return {
        "address": address or (raw.get("uri") or f"listing-{raw.get('id')}"),
        "url": url,
        "price_low": price_low,
        "price_high": price_high,
        "price_display": price_display,
        "land_size_sqm": land_size,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "car_spaces": car_spaces,
        "is_new_home": infer_is_new_home(raw),
        "build_year": None,  # not present in homely's data at all
        "status": infer_status(raw),
        "overlay_flags": None,  # not available from this source
        "domain_estimate_mid": None,  # no independent valuation available
        "lat": lat_long.get("latitude"),
        "lon": lat_long.get("longitude"),
        "note": None,
        "_id": raw.get("id"),
    }


def extract_rsc_ssr_data(html_content):
    """homely.com.au moved to the Next.js App Router (seen Oct 2026), which
    drops the __NEXT_DATA__ tag and instead streams the page data as React
    Server Component chunks (self.__next_f.push([1, "..."])). The same
    ssrData object ({"paging": ..., "listings": [...]}) is still embedded in
    that stream, so stitch the chunks together and decode it from there."""
    payload = ""
    for chunk in RSC_CHUNK_RE.findall(html_content):
        try:
            parts = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if len(parts) > 1 and isinstance(parts[1], str):
            payload += parts[1]
    idx = payload.find('"ssrData":{')
    if idx == -1:
        return None
    obj, _ = json.JSONDecoder().raw_decode(payload, idx + len('"ssrData":'))
    return _unescape_rsc(obj)


def _unescape_rsc(value):
    """RSC escapes any string starting with "$" by doubling it ("$$965,000"),
    since a single leading "$" marks a reference. Undo that."""
    if isinstance(value, str):
        return value[1:] if value.startswith("$$") else value
    if isinstance(value, list):
        return [_unescape_rsc(v) for v in value]
    if isinstance(value, dict):
        return {k: _unescape_rsc(v) for k, v in value.items()}
    return value


def scan_suburb_page(page, slug):
    """Fetch homely.com.au's for-sale search page for one suburb slug using an
    already-open Playwright page, and return its parsed listings (or None on
    any failure). Shared by the batch scan and the live single-suburb lookup
    server so both use identical fetch/parse logic."""
    url = f"https://www.homely.com.au/for-sale/{slug}/houses"
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        if not resp or resp.status != 200:
            print(f"  [warn] {url} returned HTTP {resp.status if resp else 'no response'}", file=sys.stderr)
            return None
        page.wait_for_timeout(500)
        html_content = page.content()
    except Exception as e:
        print(f"  [warn] fetch failed for {url}: {e}", file=sys.stderr)
        return None

    try:
        m = NEXT_DATA_RE.search(html_content)
        if m:
            raw_listings = json.loads(m.group(1))["props"]["pageProps"]["ssrData"]["listings"]
        else:
            ssr_data = extract_rsc_ssr_data(html_content)
            if ssr_data is None:
                print(f"  [warn] no listing data found for {url} (page structure may have changed)", file=sys.stderr)
                return None
            raw_listings = ssr_data["listings"]
    except (KeyError, TypeError, json.JSONDecodeError) as e:
        print(f"  [warn] unexpected page structure for {url}: {e}", file=sys.stderr)
        return None

    return [parse_listing(raw) for raw in raw_listings]


def fetch_listings(config):
    suburbs = config.get("suburbs", [])
    seen_ids = set()
    listings = []
    any_success = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT, locale="en-AU")
        page = context.new_page()
        for slug in suburbs:
            print(f"Fetching {slug} ...")
            parsed = scan_suburb_page(page, slug)
            time.sleep(REQUEST_DELAY_SECONDS)
            if parsed is None:
                continue
            any_success = True
            for l in parsed:
                lid = l.get("_id")
                if lid in seen_ids:
                    continue
                seen_ids.add(lid)
                listings.append(l)
        browser.close()

    print(f"Fetched {len(listings)} unique listing(s) across {len(suburbs)} suburb(s)")
    return listings, not any_success


GEOCODE_CACHE_PATH = os.path.join(HERE, "data", "geocode_cache.json")


def _load_geocode_cache():
    if os.path.exists(GEOCODE_CACHE_PATH):
        with open(GEOCODE_CACHE_PATH) as f:
            return json.load(f)
    return {}


def _save_geocode_cache(cache):
    os.makedirs(os.path.dirname(GEOCODE_CACHE_PATH), exist_ok=True)
    with open(GEOCODE_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


def geocode_address(address):
    """Geocode a free-text address to (lat, lon) via OpenStreetMap's free
    Nominatim service — no API key. Cached to disk since Nominatim's usage
    policy caps requests at ~1/sec and asks callers not to hammer it for
    repeat lookups of the same address."""
    import urllib.request
    import urllib.parse

    cache = _load_geocode_cache()
    if address in cache:
        return tuple(cache[address]) if cache[address] else None

    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": address, "format": "json", "limit": 1}
    )
    req = urllib.request.Request(url, headers={"User-Agent": "melbourne-deal-finder/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"  [warn] geocoding failed for {address!r}: {e}", file=sys.stderr)
        return None

    result = (float(data[0]["lat"]), float(data[0]["lon"])) if data else None
    cache[address] = list(result) if result else None
    _save_geocode_cache(cache)
    return result


def driving_distance(origin, dest):
    """Free-flow driving distance/duration between two (lat, lon) points via
    OSRM's public demo router — no API key. Returns (km, minutes) or None.
    This is free-flow time (no traffic), not a transit/commute estimate."""
    import urllib.request

    url = (
        f"https://router.project-osrm.org/route/v1/driving/"
        f"{origin[1]},{origin[0]};{dest[1]},{dest[0]}?overview=false"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "melbourne-deal-finder/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            route = json.loads(resp.read())
        km = route["routes"][0]["distance"] / 1000
        minutes = route["routes"][0]["duration"] / 60
        return round(km, 1), round(minutes)
    except Exception as e:
        print(f"  [warn] OSRM routing failed: {e}", file=sys.stderr)
        return None


def resolve_suburb_slug(query, state_filter=None):
    """Resolve free-text suburb input (e.g. "truganina") to homely.com.au's
    slug format (e.g. "truganina-vic-3029") via homely's own suburb search
    API — the same lookup their own search box uses. If state_filter is set
    (e.g. "Victoria"), only matches in that state are considered — this
    disambiguates suburb names that exist in multiple states (e.g. Richmond,
    Brighton). Returns None if no matching suburb is found."""
    import urllib.request
    import urllib.parse

    url = "https://api.homely.com.au/search/locations?q=" + urllib.parse.quote(query.strip())
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"  [warn] suburb lookup failed for {query!r}: {e}", file=sys.stderr)
        return None

    for loc in data.get("locations", []):
        if not loc.get("slug"):
            continue
        state_name = (loc.get("state") or {}).get("name")
        if state_filter and state_name and state_filter.lower() != state_name.lower():
            continue
        return loc["slug"], loc.get("name"), state_name
    return None


# --------------------------------------------------------------------------
# VIC stamp duty / LMI — mirrors the interactive Purchase Feasibility
# Analyzer artifact, so both tools always agree.
# --------------------------------------------------------------------------
def standard_duty(price):
    if price <= 25000:
        return price * 0.014
    if price <= 130000:
        return 350 + (price - 25000) * 0.024
    if price <= 960000:
        return 2870 + (price - 130000) * 0.06
    if price <= 2000000:
        return price * 0.055
    return 110000 + (price - 2000000) * 0.065


def fhb_duty(price):
    std = standard_duty(price)
    if price <= 600000:
        return 0.0, std
    if price <= 750000:
        fraction = min(1.0, max(0.0, (750000 - price) / 150000))
        return std * (1 - fraction), std
    return std, std


def lmi_rate(lvr):
    if lvr <= 80: return 0.0
    if lvr <= 85: return 0.010
    if lvr <= 88: return 0.015
    if lvr <= 90: return 0.020
    if lvr <= 92: return 0.027
    if lvr <= 95: return 0.035
    return 0.045


def compute_costs(price, deposit_pct):
    price = float(price or 0)
    deposit = price * (deposit_pct / 100.0)
    loan = price - deposit
    lvr = (loan / price * 100.0) if price > 0 else 0.0
    duty, std_duty = fhb_duty(price)
    lmi_base = loan * lmi_rate(lvr)
    lmi_duty = lmi_base * 0.10
    lmi_total = lmi_base + lmi_duty
    conveyancing, building_pest, title_rego, loan_rego = 1600, 500, 220, 130
    total_upfront = deposit + duty + lmi_total + conveyancing + building_pest + title_rego + loan_rego
    return {
        "deposit": round(deposit), "loan": round(loan), "lvr": round(lvr, 1),
        "stamp_duty": round(duty), "standard_duty": round(std_duty),
        "lmi_total": round(lmi_total),
        "other_costs": conveyancing + building_pest + title_rego + loan_rego,
        "total_upfront": round(total_upfront),
    }


def _price_mid(listing):
    lo, hi = listing.get("price_low"), listing.get("price_high")
    if lo and hi:
        return (lo + hi) / 2
    return lo or hi


def west_side_bonus(listing):
    address = (listing.get("address") or "").lower()
    return WEST_SIDE_BONUS if any(suburb in address for suburb in WEST_SUBURBS) else 0


def quality_score(listing, min_land, weights):
    land = listing.get("land_size_sqm") or 0
    land_score = max(0, min(weights["land_and_size"], ((land - min_land) / 250) * weights["land_and_size"]))

    build_year = listing.get("build_year")
    is_new = listing.get("is_new_home")
    this_year = datetime.date.today().year
    age = (this_year - build_year) if build_year else None
    bmax = weights["build_age_condition"]
    if is_new: build_score = bmax
    elif age is None: build_score = bmax * 0.5
    elif age <= 5: build_score = bmax * 0.95
    elif age <= 10: build_score = bmax * 0.8
    elif age <= 15: build_score = bmax * 0.6
    elif age <= 20: build_score = bmax * 0.4
    else: build_score = bmax * 0.25

    price_mid = _price_mid(listing)
    est = listing.get("domain_estimate_mid")
    vmax = weights["value_vs_estimate"]
    if not est or not price_mid:
        value_score = vmax * 0.5
    else:
        discount_pct = (est - price_mid) / est
        value_score = max(0, min(vmax, vmax / 2 + discount_pct * vmax * 5))

    status = (listing.get("status") or "").lower()
    smax = weights["availability_status"]
    if status == "available": status_score = smax
    elif status == "auction scheduled": status_score = smax * 0.8
    elif status == "under offer": status_score = smax * 0.2
    elif status == "under contract": status_score = 0
    else: status_score = smax * 0.5

    total = round(land_score + build_score + value_score + status_score)
    return total, {
        "land": round(land_score, 1), "build": round(build_score, 1),
        "value": round(value_score, 1), "status": round(status_score, 1),
    }


def load_seen():
    if os.path.exists(SEEN_PATH):
        with open(SEEN_PATH, "r") as f:
            return json.load(f)
    return {}


def save_seen(seen):
    os.makedirs(os.path.dirname(SEEN_PATH), exist_ok=True)
    with open(SEEN_PATH, "w") as f:
        json.dump(seen, f, indent=2, sort_keys=True)


def status_badge(status):
    return {
        "Available": "✅ Available", "Auction scheduled": "🔨 Auction scheduled",
        "Under offer": "⚠️ Under offer", "Under contract": "⛔ Under contract",
    }.get(status, "❔ Status unknown")


def build_badge(listing):
    if listing.get("is_new_home"): return "🆕 New build"
    if listing.get("build_year"): return f"🏗️ Built {listing['build_year']}"
    return "🏗️ Build year unknown"


def scored_entry_to_dict(idx, score, bonus, l, land_ok, budget_ok, breakdown, costs, weights):
    """Serialize one scored listing to the JSON shape used by both the batch
    report's compare feature and the live-search API, so they never drift."""
    return {
        "idx": idx,
        "address": l.get("address", "Unknown address"),
        "url": l.get("url"),
        "score": score, "bonus": bonus, "total": score + bonus,
        "price_low": l.get("price_low"), "price_high": l.get("price_high"),
        "price_display": l.get("price_display"),
        "land": l.get("land_size_sqm"),
        "beds": l.get("bedrooms"), "baths": l.get("bathrooms"), "cars": l.get("car_spaces"),
        "status": l.get("status") or "Unknown",
        "build_year": l.get("build_year"), "is_new_home": l.get("is_new_home"),
        "breakdown": breakdown, "weights": weights,
        "upfront": costs["total_upfront"],
        "fits": bool(land_ok and budget_ok),
        "lat": l.get("lat"), "lon": l.get("lon"),
    }


def build_scored_listings(config, listings, seen_before):
    scored = []
    for l in listings:
        land_ok = (l.get("land_size_sqm") or 0) >= config["min_land_sqm"]
        beds_ok = (l.get("bedrooms") or 0) >= config["min_bedrooms"]
        price_mid = _price_mid(l)
        budget_ok = price_mid is not None and price_mid <= config["budget_max"]
        # Hard requirements, not preferences — a listing under the minimum
        # land size, bedroom count, or over budget_max is dropped entirely
        # rather than shown with a "outside criteria" tag.
        if not (land_ok and beds_ok and budget_ok):
            continue
        score, breakdown = quality_score(l, config["min_land_sqm"], config["score_weights"])
        bonus = west_side_bonus(l)
        costs = compute_costs(price_mid or 0, config["deposit_pct"])
        is_new_listing = l.get("address") not in seen_before
        scored.append((score, bonus, l, land_ok, budget_ok, breakdown, costs, is_new_listing))
    scored.sort(key=lambda x: x[0] + x[1], reverse=True)

    max_shown = config.get("max_listings_per_run")
    if max_shown:
        scored = scored[:max_shown]
    return scored


def write_report(config, scored, today_str, fetch_failed):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    lines = [
        f"# Melbourne deal scan — {today_str}",
        "",
        f"Criteria: {config['min_bedrooms']}+ bed house, {config['min_land_sqm']}m²+ land, "
        f"${config['budget_min']:,}–${config['budget_max']:,} price range "
        f"(costs assume a {config['deposit_pct']}% deposit). "
        f"Source: homely.com.au, {len(config.get('suburbs', []))} suburbs scanned "
        f"(preference for western suburbs, marked with a bonus below).",
        "",
    ]
    if fetch_failed:
        lines.append(
            "⚠️ **All suburb fetches failed this run.** homely.com.au's page layout may have "
            "changed, or requests were blocked. See README.md → 'If parsing breaks'. "
            "No listings could be scanned today.\n"
        )
    if not scored:
        lines.append("_No listings found this run._")
    else:
        for score, bonus, l, land_ok, budget_ok, breakdown, costs, is_new_listing in scored:
            addr = l.get("address", "Unknown address")
            price_lo, price_hi = l.get("price_low"), l.get("price_high")
            price_str = (
                f"${price_lo:,.0f}–${price_hi:,.0f}" if price_lo and price_hi
                else (f"${(price_lo or price_hi):,.0f}" if (price_lo or price_hi) else "Price N/A")
            )
            if l.get("price_display"):
                price_str += f" (agent: “{l['price_display']}”)"
            new_tag = " 🆕 **NEW SINCE LAST RUN**" if is_new_listing else ""
            fit_tag = "✅ fits criteria" if (land_ok and budget_ok) else "❌ outside criteria"
            bonus_str = f" (+{bonus} west-side bonus)" if bonus else ""

            lines += [
                f"## {addr}{new_tag}",
                f"[View listing]({l.get('url','')})",
                f"**Score: {score}/100{bonus_str}** · {price_str} · {status_badge(l.get('status'))} · {build_badge(l)} · {fit_tag}",
                "",
                f"- Land: {l.get('land_size_sqm', 'N/A')}m² (need {config['min_land_sqm']}m²+)",
                f"- Beds/baths/car: {l.get('bedrooms','?')} / {l.get('bathrooms','?')} / {l.get('car_spaces','?')}",
                f"- Independent value estimate: {'${:,.0f}'.format(l['domain_estimate_mid']) if l.get('domain_estimate_mid') else 'Not available from this source'}",
                f"- Overlay flags: {l.get('overlay_flags') or 'Not available from this source'}",
                "",
                f"**Score breakdown (points, not facts)** — "
                f"land/size {breakdown['land']}/{config['score_weights']['land_and_size']}, "
                f"value vs estimate {breakdown['value']}/{config['score_weights']['value_vs_estimate']}, "
                f"build condition {breakdown['build']}/{config['score_weights']['build_age_condition']}"
                f"{' (build year unknown)' if not l.get('build_year') and not l.get('is_new_home') else ''}, "
                f"availability {breakdown['status']}/{config['score_weights']['availability_status']}",
                "",
                f"**Est. upfront cash needed**: ${costs['total_upfront']:,} "
                f"(deposit ${costs['deposit']:,} + stamp duty ${costs['stamp_duty']:,} "
                f"+ LMI ${costs['lmi_total']:,} + other costs ${costs['other_costs']:,})",
                "", "---", "",
            ]

    report = "\n".join(lines)
    with open(os.path.join(REPORTS_DIR, f"{today_str}.md"), "w") as f:
        f.write(report)
    with open(os.path.join(REPORTS_DIR, "latest.md"), "w") as f:
        f.write(report)
    print(f"Report written: reports/{today_str}.md")


STATUS_COLORS = {
    "Available": "#2f6b3f",
    "Auction scheduled": "#a67c27",
    "Under offer": "#9b4530",
    "Under contract": "#9b4530",
}


def write_html_report(config, scored, today_str, fetch_failed):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    esc = html.escape
    weights = config["score_weights"]
    analyzer_url = config.get("feasibility_analyzer_url")
    analyzer_link = (
        f' · <a href="{esc(analyzer_url)}" target="_blank" rel="noopener">Purchase Feasibility Analyzer ↗</a>'
        " (stamp duty / LMI calculator)"
    ) if analyzer_url else ""

    cards = []
    compare_data = []
    for idx, (score, bonus, l, land_ok, budget_ok, breakdown, costs, is_new_listing) in enumerate(scored):
        addr = esc(l.get("address", "Unknown address"))
        price_lo, price_hi = l.get("price_low"), l.get("price_high")
        price_str = (
            f"${price_lo:,.0f}–${price_hi:,.0f}" if price_lo and price_hi
            else (f"${(price_lo or price_hi):,.0f}" if (price_lo or price_hi) else "Price N/A")
        )
        agent_text = f'<div class="agent-text">agent: “{esc(l["price_display"])}”</div>' if l.get("price_display") else ""
        new_tag = '<span class="tag new">NEW</span>' if is_new_listing else ""
        fit_tag = (
            '<span class="tag fit">fits criteria</span>' if (land_ok and budget_ok)
            else '<span class="tag nofit">outside criteria</span>'
        )
        bonus_str = f'<span class="tag bonus">+{bonus} west</span>' if bonus else ""
        status = l.get("status") or "Unknown"
        status_color = STATUS_COLORS.get(status, "#7c7565")
        url = esc(l.get("url") or "#")
        build_known = bool(l.get("build_year") or l.get("is_new_home"))
        build_note = "" if build_known else " (build year unknown)"

        cards.append(f"""
        <div class="card" data-idx="{idx}" data-address="{addr.lower()}">
          <div class="card-head">
            <label class="compare-check">
              <input type="checkbox" class="cmp" data-idx="{idx}"> compare
            </label>
            <h2><a href="{url}" target="_blank" rel="noopener">{addr}</a> {new_tag}</h2>
            <div class="score">{score}<span class="score-max">/100</span> {bonus_str}</div>
          </div>
          <div class="tags">
            <span class="tag status" style="--c:{status_color}">{esc(status)}</span>
            {fit_tag}
            <span class="tag price">{price_str}</span>
          </div>
          {agent_text}
          <div class="details">
            <div>Land: <b>{l.get('land_size_sqm') or 'N/A'}m²</b> (need {config['min_land_sqm']}m²+)</div>
            <div>Beds/baths/car: <b>{l.get('bedrooms','?')} / {l.get('bathrooms','?')} / {l.get('car_spaces','?')}</b></div>
            <div>Est. upfront cash needed: <b>${costs['total_upfront']:,}</b></div>
          </div>
          <div class="breakdown">
            score breakdown (points, not facts) — land/size {breakdown['land']}/{weights['land_and_size']} ·
            value vs estimate {breakdown['value']}/{weights['value_vs_estimate']} ·
            build condition {breakdown['build']}/{weights['build_age_condition']}{build_note} ·
            availability {breakdown['status']}/{weights['availability_status']}
          </div>
        </div>""")

        compare_data.append(scored_entry_to_dict(
            idx, score, bonus, l, land_ok, budget_ok, breakdown, costs, weights
        ))

    warning = ""
    if fetch_failed:
        warning = (
            '<div class="warning">⚠️ All suburb fetches failed this run. '
            "homely.com.au's page layout may have changed, or requests were blocked. "
            "See README.md → 'If parsing breaks'.</div>"
        )
    empty = '<p class="empty">No listings found this run.</p>' if not scored else ""

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Melbourne Deal Scan — {today_str}</title>
<style>
  :root {{
    --bg: #ffffff; --surface: #f7f8fa; --border: #e3e6ea;
    --text: #1a1d23; --text-soft: #5b6270; --text-faint: #8a919e;
    --accent: #2563eb; --accent-soft: #eaf1ff;
    --green: #15803d; --green-soft: #eefaf1;
    --red: #b42318; --red-soft: #fdeeed;
    --amber: #b45309; --amber-soft: #fef4e6;
    --dark: #1a1d23;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 2.5rem 1.5rem 5rem; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-variant-numeric: tabular-nums;
    -webkit-font-smoothing: antialiased;
  }}
  :focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
  .wrap {{ max-width: 1120px; margin: 0 auto; }}
  h1 {{ font-size: 1.7rem; font-weight: 700; margin: 0 0 0.3rem; letter-spacing: -0.01em; }}
  .subtitle {{ color: var(--text-soft); font-size: 0.9rem; margin-bottom: 1.5rem; }}
  .subtitle a {{ color: var(--accent); text-decoration: none; font-weight: 600; }}
  .subtitle a:hover {{ text-decoration: underline; }}
  .warning {{ background: var(--amber-soft); border-left: 4px solid var(--amber); color: #7a3d05; padding: 0.8rem 1rem; border-radius: 6px; margin-bottom: 1.5rem; font-size: 0.9rem; }}
  .empty {{ color: var(--text-soft); font-style: italic; }}

  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 1rem; }}
  .card {{
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 1.1rem 1.3rem;
  }}
  .card-head {{ display: flex; justify-content: space-between; align-items: baseline; gap: 0.75rem; flex-wrap: wrap; position: relative; }}
  .card h2 {{ font-size: 1rem; font-weight: 600; margin: 0; line-height: 1.35; }}
  .card h2 a {{ color: var(--text); text-decoration: none; }}
  .card h2 a:hover {{ color: var(--accent); text-decoration: underline; }}
  .score {{ font-size: 1.25rem; font-weight: 700; color: var(--accent); white-space: nowrap; }}
  .score-max {{ font-size: 0.85rem; color: var(--text-faint); font-weight: 400; }}
  .tags {{ margin: 0.55rem 0; display: flex; gap: 0.4rem; flex-wrap: wrap; }}
  .tag {{ font-size: 0.72rem; font-weight: 600; padding: 0.2rem 0.6rem; border-radius: 999px; }}
  .tag.status {{ background: color-mix(in srgb, var(--c) 12%, white); color: var(--c); }}
  .tag.fit {{ background: var(--green-soft); color: var(--green); }}
  .tag.nofit {{ background: var(--red-soft); color: var(--red); }}
  .tag.new {{ background: var(--accent); color: #fff; }}
  .tag.bonus {{ background: var(--amber-soft); color: var(--amber); }}
  .tag.price {{ background: var(--border); color: var(--text); }}
  .tag.live {{ background: var(--dark); color: #fff; }}
  .agent-text {{ font-size: 0.8rem; font-style: italic; color: var(--text-faint); margin-bottom: 0.5rem; }}
  .details {{ display: flex; gap: 1.25rem; flex-wrap: wrap; font-size: 0.85rem; color: var(--text-soft); margin-bottom: 0.5rem; }}
  .details b {{ color: var(--text); font-weight: 600; }}
  .breakdown {{ font-size: 0.75rem; color: var(--text-faint); border-top: 1px solid var(--border); padding-top: 0.55rem; }}
  footer {{ margin-top: 2.5rem; font-size: 0.78rem; color: var(--text-faint); text-align: center; line-height: 1.6; }}

  .toolbar {{ display: flex; gap: 0.6rem; align-items: center; margin-bottom: 0.7rem; flex-wrap: wrap; }}
  .toolbar input[type="text"] {{
    flex: 1; min-width: 220px; font: inherit; font-size: 0.95rem; padding: 0.55rem 0.9rem;
    border: 1.5px solid var(--border); border-radius: 8px; background: #fff; color: var(--text);
  }}
  .toolbar input[type="text"]:focus {{ outline: none; border-color: var(--accent); }}
  .no-results {{ color: var(--text-soft); font-style: italic; display: none; }}
  .state-check {{ font-size: 0.85rem; display: flex; align-items: center; gap: 0.35rem; white-space: nowrap; color: var(--text-soft); }}
  .price-toolbar {{ margin-bottom: 0.6rem; }}
  .price-toolbar label {{ font-size: 0.85rem; display: flex; align-items: center; gap: 0.4rem; color: var(--text-soft); }}
  .price-toolbar input[type="number"] {{
    width: 9rem; font: inherit; font-size: 0.9rem; padding: 0.4rem 0.6rem;
    border: 1.5px solid var(--border); border-radius: 8px; background: #fff; color: var(--text);
  }}
  .price-toolbar input[type="number"]:focus {{ outline: none; border-color: var(--accent); }}

  .compare-check {{ font-size: 0.72rem; font-weight: 600; color: var(--text-soft); display: flex; align-items: center; gap: 0.3rem; order: -1; }}

  #compareBar {{
    position: fixed; bottom: 1.2rem; right: 1.2rem; left: auto; top: auto; z-index: 50;
    background: var(--dark); color: #fff; box-shadow: 0 10px 30px rgba(0,0,0,0.25);
    padding: 0.6rem 0.6rem 0.6rem 1.1rem; border-radius: 999px; font-size: 0.85rem; max-width: calc(100vw - 2.4rem);
    display: none; align-items: center; justify-content: space-between; gap: 1rem;
  }}
  #compareBar.show {{ display: flex; }}
  #compareBar button {{
    font: inherit; font-size: 0.8rem; font-weight: 600; background: var(--accent); color: #fff; border: none;
    padding: 0.5rem 1.1rem; border-radius: 999px; cursor: pointer;
  }}
  #compareBar button:disabled {{ opacity: 0.5; cursor: not-allowed; }}

  #compareTable {{
    display: none; background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 1.2rem; margin-bottom: 1.5rem;
  }}
  #compareTable.show {{ display: block; }}
  .compare-table-scroll {{ overflow-x: auto; }}
  #compareTable table {{ border-collapse: collapse; width: 100%; font-size: 0.87rem; table-layout: fixed; }}
  #compareTable th, #compareTable td {{
    text-align: left; padding: 0.55rem 0.6rem; border-bottom: 1px solid var(--border);
    white-space: normal; word-break: break-word; vertical-align: top;
  }}
  #compareTable thead th:first-child, #compareTable td:first-child, #compareTable th:first-child {{ width: 26%; }}
  #compareTable thead th {{ font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-faint); font-weight: 600; }}
  #compareTable tbody th {{ font-weight: 600; color: var(--text-soft); font-size: 0.82rem; }}
  #compareTable td.winner {{ color: var(--green); font-weight: 700; }}
  #verdict {{ font-size: 0.9rem; margin-top: 1.1rem; padding-top: 0.9rem; border-top: 2px solid var(--dark); line-height: 1.6; }}

  .toolbar button#liveSearchBtn {{
    font: inherit; font-size: 0.88rem; font-weight: 600; background: var(--dark); color: #fff; border: none;
    padding: 0.55rem 1.1rem; border-radius: 8px; cursor: pointer; white-space: nowrap;
  }}
  .toolbar button#liveSearchBtn:disabled {{ opacity: 0.6; cursor: wait; }}
  .toolbar-hint {{ font-size: 0.78rem; color: var(--text-faint); margin: -0.6rem 0 1.2rem; }}
  .toolbar-hint code {{ background: var(--surface); padding: 0.1rem 0.35rem; border-radius: 4px; }}
  #liveResults {{ margin-bottom: 1.5rem; }}
  #liveResults .live-heading {{ font-size: 0.95rem; margin-bottom: 0.7rem; color: var(--text-soft); }}
  #liveResults .live-error {{
    background: var(--red-soft); border-left: 4px solid var(--red); color: #7a1a12;
    padding: 0.7rem 1rem; border-radius: 6px; font-size: 0.85rem;
  }}
  #liveResults .live-loading {{ color: var(--text-soft); font-style: italic; font-size: 0.85rem; }}
  #liveResults .grid {{ margin-top: 0.7rem; }}

  #analyzer {{
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 1rem 1.2rem; margin-bottom: 1.5rem;
  }}
  #analyzer h2 {{ font-size: 1rem; margin: 0 0 0.3rem; }}
  #analyzer .hint {{ font-size: 0.8rem; color: var(--text-soft); margin: 0 0 0.6rem; }}
  #analyzeInput {{
    width: 100%; min-height: 4.5rem; resize: vertical; font: inherit; font-size: 0.9rem; padding: 0.6rem 0.8rem;
    border: 1.5px solid var(--border); border-radius: 8px; background: #fff; color: var(--text);
  }}
  #analyzeInput:focus {{ outline: none; border-color: var(--accent); }}
  #analyzeBtn {{
    margin-top: 0.5rem; font: inherit; font-size: 0.88rem; font-weight: 600; background: var(--accent); color: #fff;
    border: none; padding: 0.55rem 1.2rem; border-radius: 8px; cursor: pointer;
  }}
  #analyzeBtn:disabled {{ opacity: 0.6; cursor: wait; }}
  #analyzeResult {{ margin-top: 1rem; }}
  #analyzeResult .an-verdict {{ border-radius: 8px; padding: 0.8rem 1rem; margin-bottom: 0.9rem; font-size: 0.9rem; line-height: 1.5; }}
  #analyzeResult .an-verdict b.label {{ font-size: 1.05rem; display: block; margin-bottom: 0.2rem; }}
  .an-verdict.good {{ background: var(--green-soft); color: #0f5a2b; }}
  .an-verdict.ok {{ background: var(--amber-soft); color: #7a3d05; }}
  .an-verdict.bad {{ background: var(--red-soft); color: #7a1a12; }}
  #analyzeResult table {{ border-collapse: collapse; width: 100%; font-size: 0.85rem; }}
  #analyzeResult th, #analyzeResult td {{ text-align: left; padding: 0.5rem 0.55rem; border-bottom: 1px solid var(--border); vertical-align: top; }}
  #analyzeResult thead th {{ font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-faint); }}
  #analyzeResult tbody th {{ font-weight: 600; color: var(--text-soft); white-space: nowrap; }}
  #analyzeResult .note {{ color: var(--text-faint); font-size: 0.78rem; }}
  .pill {{ font-size: 0.7rem; font-weight: 700; padding: 0.15rem 0.55rem; border-radius: 999px; white-space: nowrap; }}
  .pill.good {{ background: var(--green-soft); color: var(--green); }}
  .pill.ok {{ background: var(--amber-soft); color: var(--amber); }}
  .pill.bad {{ background: var(--red-soft); color: var(--red); }}
  .pill.info, .pill.unknown {{ background: var(--border); color: var(--text-soft); }}
  #analyzeResult .an-meta {{ font-size: 0.78rem; color: var(--text-faint); margin: 0.8rem 0 0.4rem; }}
  #analyzeResult .an-checks {{ display: flex; flex-wrap: wrap; gap: 0.4rem 1rem; font-size: 0.82rem; }}
  #analyzeResult .an-checks a {{ color: var(--accent); text-decoration: none; }}
  #analyzeResult .an-actions {{ margin-top: 0.8rem; }}
  #analyzeResult .an-actions button {{
    font: inherit; font-size: 0.82rem; font-weight: 600; background: var(--dark); color: #fff;
    border: none; padding: 0.45rem 1rem; border-radius: 8px; cursor: pointer;
  }}
  .an-scroll {{ overflow-x: auto; }}

  @media (max-width: 640px) {{
    #compareTable {{ padding: 0.8rem; }}
    #compareBar {{ left: 1.2rem; right: 1.2rem; }}
  }}
</style>
</head>
<body>
  <div class="wrap">
    <h1>Melbourne Deal Scan</h1>
    <div class="subtitle">
      {today_str} · {config['min_bedrooms']}+ bed house, {config['min_land_sqm']}m²+ land,
      ${config['budget_min']:,}–${config['budget_max']:,} price range · homely.com.au,
      {len(config.get('suburbs', []))} suburbs scanned{analyzer_link}
    </div>
    {warning}

    <section id="analyzer">
      <h2>Should I buy this one?</h2>
      <p class="hint">Paste a homely / domain / realestate.com.au link, an address, or the listing text copied
      from any site. You get every factor rated against your criteria and the suburb, plus a verdict.
      Needs <code>python server.py</code> running.</p>
      <textarea id="analyzeInput" placeholder="e.g. 12 Smith St, Truganina VIC 3029 — or paste the whole listing (price, beds, land size…)"></textarea>
      <button id="analyzeBtn">Analyse property</button>
      <div id="analyzeResult"></div>
    </section>

    <div class="toolbar">
      <input type="text" id="suburbFilter" placeholder="Filter these results, or type a suburb and press Enter to scan it live…">
      <label class="state-check"><input type="checkbox" id="vicOnly" checked> VIC only</label>
      <button id="liveSearchBtn">Scan live</button>
    </div>
    <div class="toolbar price-toolbar">
      <label>Price min <input type="number" id="priceMin" placeholder="e.g. 600000" step="10000"></label>
      <label>Price max <input type="number" id="priceMax" placeholder="e.g. 750000" step="10000"></label>
    </div>
    <p class="toolbar-hint">Typing/price filters the list below instantly. "Scan live" fetches that suburb from
    homely.com.au right now (unchecking "VIC only" searches all states, leaving "Price max" blank searches
    any price instead of the ${config['budget_max']:,} cap) — requires <code>python server.py</code>
    to be running.</p>
    <p class="no-results" id="noResults">No listings match that filter.</p>

    <div id="liveResults"></div>

    <div id="compareBar">
      <span id="compareCount">0 selected</span>
      <button id="compareBtn" disabled>Compare</button>
    </div>
    <div id="compareTable"></div>

    {empty}
    <div class="grid">
    {''.join(cards)}
    </div>
    <footer>Generated locally by scan_deals.py — not financial advice. Score breakdowns are points against
    your criteria, not verified facts about the property — always confirm details (especially build year)
    against the real listing.</footer>
  </div>

<script id="listing-data" type="application/json">{json.dumps(compare_data)}</script>
<script>
(function() {{
  // mutable: live-search results get appended here with new indices, so
  // they can be selected for comparison alongside the batch-scanned ones
  let LISTINGS = JSON.parse(document.getElementById('listing-data').textContent);

  // --- suburb/address text filter + price range filter (combined, AND'd) ---
  const filterInput = document.getElementById('suburbFilter');
  const priceMinInput = document.getElementById('priceMin');
  const priceMaxInput = document.getElementById('priceMax');
  const noResults = document.getElementById('noResults');

  function applyFilters() {{
    const q = filterInput.value.trim().toLowerCase();
    const min = parseFloat(priceMinInput.value);
    const max = parseFloat(priceMaxInput.value);
    let visible = 0;
    document.querySelectorAll('.card[data-idx]').forEach(card => {{
      const item = LISTINGS[parseInt(card.dataset.idx, 10)];
      // Live-search cards are already scoped to the suburb you searched for
      // (homely resolved it, possibly fuzzy-matching a typo) — re-checking
      // the typed text against their address would wrongly hide results
      // homely itself just found. Only the batch-scanned cards need the
      // text match; live cards only get the price filter.
      const textMatch = card.dataset.live === '1' || !q || card.dataset.address.includes(q);
      const mid = item ? priceMid(item) : null;
      const minOk = isNaN(min) || (mid !== null && mid >= min);
      const maxOk = isNaN(max) || (mid !== null && mid <= max);
      const match = textMatch && minOk && maxOk;
      card.style.display = match ? '' : 'none';
      if (match) visible++;
    }});
    noResults.style.display = visible === 0 ? 'block' : 'none';
  }}

  filterInput.addEventListener('input', applyFilters);
  priceMinInput.addEventListener('input', applyFilters);
  priceMaxInput.addEventListener('input', applyFilters);

  // --- live suburb search (talks to server.py's /api/scan) ---
  const liveBtn = document.getElementById('liveSearchBtn');
  const liveResults = document.getElementById('liveResults');

  function renderLiveCard(item, idx) {{
    const priceStr = fmtMoney(priceMid(item)) === 'N/A' ? 'Price N/A'
      : '$' + (item.price_low || 0).toLocaleString('en-AU') + '–$' + (item.price_high || 0).toLocaleString('en-AU');
    const agentText = item.price_display
      ? '<div class="agent-text">agent: “' + item.price_display.replace(/</g, '&lt;') + '”</div>' : '';
    const fitTag = item.fits
      ? '<span class="tag fit">fits criteria</span>' : '<span class="tag nofit">outside criteria</span>';
    const buildNote = (!item.build_year && !item.is_new_home) ? ' (build year unknown)' : '';
    return '<div class="card" data-idx="' + idx + '" data-live="1" data-address="' + item.address.toLowerCase().replace(/"/g, '') + '">' +
      '<div class="card-head">' +
      '<label class="compare-check"><input type="checkbox" class="cmp" data-idx="' + idx + '"> compare</label>' +
      '<h2><a href="' + item.url + '" target="_blank" rel="noopener">' +
      item.address.replace(/</g, '&lt;') + '</a> <span class="tag live">LIVE</span></h2>' +
      '<div class="score">' + item.score + '<span class="score-max">/100</span>' +
      (item.bonus ? ' <span class="tag bonus">+' + item.bonus + ' west</span>' : '') + '</div></div>' +
      '<div class="tags"><span class="tag status">' + item.status + '</span>' + fitTag +
      '<span class="tag price">' + priceStr + '</span></div>' + agentText +
      '<div class="details"><div>Land: <b>' + (item.land || 'N/A') + 'm²</b></div>' +
      '<div>Beds/baths/car: <b>' + (item.beds ?? '?') + ' / ' + (item.baths ?? '?') + ' / ' + (item.cars ?? '?') + '</b></div>' +
      '<div>Est. upfront cash needed: <b>$' + Math.round(item.upfront).toLocaleString('en-AU') + '</b></div></div>' +
      '<div class="breakdown">score breakdown (points, not facts) — land/size ' + item.breakdown.land + '/' + item.weights.land_and_size +
      ' · value vs estimate ' + item.breakdown.value + '/' + item.weights.value_vs_estimate +
      ' · build condition ' + item.breakdown.build + '/' + item.weights.build_age_condition + buildNote +
      ' · availability ' + item.breakdown.status + '/' + item.weights.availability_status + '</div></div>';
  }}

  const vicOnly = document.getElementById('vicOnly');

  function runLiveSearch() {{
    const q = filterInput.value.trim();
    if (!q) return;
    liveBtn.disabled = true;
    liveResults.innerHTML = '<p class="live-loading">Scanning ' + q + ' on homely.com.au…</p>';
    const stateParam = vicOnly.checked ? '&state=Victoria' : '';
    const priceMaxVal = priceMaxInput.value.trim();
    const priceMaxParam = priceMaxVal ? '&price_max=' + encodeURIComponent(priceMaxVal) : '';
    fetch('/api/scan?suburb=' + encodeURIComponent(q) + stateParam + priceMaxParam)
      .then(r => r.json().then(body => ({{ ok: r.ok, body }})))
      .then(({{ ok, body }}) => {{
        if (!ok) {{
          liveResults.innerHTML = '<div class="live-error">' + body.error + '</div>';
          return;
        }}
        if (body.count === 0) {{
          liveResults.innerHTML = '<div class="live-heading">No listings found for ' + body.suburb + ', ' + body.state + ' right now.</div>';
          return;
        }}
        const heading = '<div class="live-heading"><b>' + body.count + ' live result(s) for ' +
          body.suburb + ', ' + body.state + '</b> — fetched just now from homely.com.au, sorted by score.</div>';
        const startIdx = LISTINGS.length;
        LISTINGS = LISTINGS.concat(body.results);
        const cardsHtml = body.results.map((item, i) => renderLiveCard(item, startIdx + i)).join('');
        liveResults.innerHTML = heading + '<div class="grid">' + cardsHtml + '</div>';
        applyFilters(); // respect any price range already set
      }})
      .catch(() => {{
        liveResults.innerHTML = '<div class="live-error">Live search needs the local server running — ' +
          'start it with <code>python server.py</code> instead of opening this file directly.</div>';
      }})
      .finally(() => {{ liveBtn.disabled = false; }});
  }}

  liveBtn.addEventListener('click', runLiveSearch);
  filterInput.addEventListener('keydown', e => {{ if (e.key === 'Enter') runLiveSearch(); }});

  // --- compare selection ---
  // Uses event delegation (not a cached checkbox list) so checkboxes added
  // later by live search results are picked up automatically.
  const MAX_COMPARE = 3;
  const compareBar = document.getElementById('compareBar');
  const compareCount = document.getElementById('compareCount');
  const compareBtn = document.getElementById('compareBtn');
  const compareTable = document.getElementById('compareTable');

  function selectedIdxs() {{
    return Array.from(document.querySelectorAll('.cmp:checked')).map(c => parseInt(c.dataset.idx, 10));
  }}

  function updateBar() {{
    const n = selectedIdxs().length;
    compareBar.classList.toggle('show', n > 0);
    compareCount.textContent = n + ' selected (max ' + MAX_COMPARE + ')';
    compareBtn.disabled = n < 2;
  }}

  document.addEventListener('change', e => {{
    if (!e.target.classList.contains('cmp')) return;
    if (selectedIdxs().length > MAX_COMPARE) {{
      e.target.checked = false;
      alert('You can compare up to ' + MAX_COMPARE + ' properties at a time.');
    }}
    updateBar();
  }});

  function fmtMoney(v) {{
    return (v === null || v === undefined) ? 'N/A' : '$' + Math.round(v).toLocaleString('en-AU');
  }}

  function priceMid(l) {{
    if (l.price_low && l.price_high) return (l.price_low + l.price_high) / 2;
    return l.price_low || l.price_high || null;
  }}

  function buildLabel(l) {{
    if (l.is_new_home) return 'New / off-the-plan';
    if (l.build_year) return 'Built ' + l.build_year;
    const q = encodeURIComponent('"' + l.address + '" built year');
    return 'Unknown — <a href="https://www.google.com/search?q=' + q +
      '" target="_blank" rel="noopener">check build year ↗</a>';
  }}

  function renderCompare() {{
    const idxs = selectedIdxs();
    if (idxs.length < 2) {{ compareTable.classList.remove('show'); return; }}
    const items = idxs.map(i => LISTINGS[i]);

    const rows = [
      ['Score (incl. bonus)', l => l.total + '/105', l => l.total],
      ['Price (mid-point)', l => fmtMoney(priceMid(l)), l => -(priceMid(l) || Infinity)],
      ['Land size', l => (l.land ? l.land + 'm²' : 'N/A'), l => l.land || 0],
      ['Beds / baths / cars', l => (l.beds ?? '?') + ' / ' + (l.baths ?? '?') + ' / ' + (l.cars ?? '?'), null],
      ['Status', l => l.status, null],
      ['Build year', l => buildLabel(l), null],
      ['Fits your criteria', l => l.fits ? 'Yes' : 'No', l => l.fits ? 1 : 0],
      ['Est. upfront cash', l => fmtMoney(l.upfront), l => -l.upfront],
    ];

    let html = '<div class="compare-table-scroll"><table><thead><tr><th>Criteria</th>';
    items.forEach(l => {{ html += '<th>' + l.address.replace(/</g, '&lt;') + '</th>'; }});
    html += '</tr></thead><tbody>';
    rows.forEach(([label, fmt, rank]) => {{
      html += '<tr><th>' + label + '</th>';
      let bestVal = null, bestI = -1;
      if (rank) {{
        items.forEach((l, i) => {{
          const v = rank(l);
          if (bestVal === null || v > bestVal) {{ bestVal = v; bestI = i; }}
        }});
      }}
      items.forEach((l, i) => {{
        html += '<td' + (i === bestI ? ' class="winner"' : '') + '>' + fmt(l) + '</td>';
      }});
      html += '</tr>';
    }});
    html += '<tr id="distanceLoadingRow"><th>Distance to your reference locations</th>' +
      '<td colspan="' + items.length + '">Fetching driving distance (no traffic, no API key)…</td></tr>';
    html += '</tbody></table></div>';

    // --- verdict: rank by total score, explain the deciding factors ---
    const sorted = items.slice().sort((a, b) => b.total - a.total);
    const winner = sorted[0], runnerUp = sorted[1];
    const parts = [];
    ['land', 'value', 'build', 'status'].forEach(k => {{
      const wv = winner.breakdown[k], rv = runnerUp.breakdown[k];
      if (wv - rv >= 3) {{
        const names = {{land: 'land size', value: 'value vs. estimate', build: 'build condition', status: 'availability'}};
        parts.push(names[k]);
      }}
    }});
    let verdict = '<b>' + winner.address + '</b> scores highest overall (' + winner.total + ' vs ' +
      runnerUp.total + ')';
    verdict += parts.length ? ', mainly on ' + parts.join(' and ') + '.' : ', by a narrow margin.';
    if (!winner.fits) {{
      verdict += ' Note: it does NOT fit your hard budget/land criteria, so weigh that against the score.';
    }}
    const unknownBuild = items.filter(l => !l.build_year && !l.is_new_home).map(l => l.address);
    if (unknownBuild.length) {{
      verdict += ' Build year is unknown for ' + unknownBuild.length + ' of these — that portion of the ' +
        'score is neutral, not a real assessment of age. Verify build year yourself before deciding.';
    }}
    html += '<div id="verdict">' + verdict + '</div>';

    compareTable.innerHTML = html;
    compareTable.classList.add('show');
    compareTable.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
    fetchAndRenderDistances(items);
  }}

  function fetchAndRenderDistances(items) {{
    Promise.all(items.map(l => {{
      if (l.lat == null || l.lon == null) return Promise.resolve(null);
      return fetch('/api/distances?lat=' + l.lat + '&lon=' + l.lon)
        .then(r => r.json()).catch(() => null);
    }})).then(results => {{
      const loadingRow = document.getElementById('distanceLoadingRow');
      if (!loadingRow) return;
      if (results.every(r => !r)) {{
        loadingRow.innerHTML = '<th>Distance to your reference locations</th>' +
          '<td colspan="' + items.length + '">Could not fetch distances — is <code>python server.py</code> running?</td>';
        return;
      }}
      // reference locations are the same list for every item; use the first successful response for names
      const refNames = (results.find(r => r) || {{ distances: [] }}).distances.map(d => d.name);
      const rowsHtml = refNames.map((name, refIdx) => {{
        let bestVal = null, bestI = -1;
        const cells = items.map((l, i) => {{
          const r = results[i] && results[i].distances[refIdx];
          const v = r && !r.error ? r.km : null;
          if (v !== null && (bestVal === null || v < bestVal)) {{ bestVal = v; bestI = i; }}
          return r ? (r.error ? r.error : r.km + ' km · ' + r.minutes + ' min drive (no traffic)') : 'N/A';
        }});
        return '<tr><th>Distance to ' + name + '</th>' + cells.map((c, i) =>
          '<td' + (i === bestI ? ' class="winner"' : '') + '>' + c + '</td>').join('') + '</tr>';
      }}).join('');
      loadingRow.outerHTML = rowsHtml;
    }});
  }}

  compareBtn.addEventListener('click', renderCompare);

  // --- single-property analyser (talks to server.py's /api/analyze) ---
  const analyzeInput = document.getElementById('analyzeInput');
  const analyzeBtn = document.getElementById('analyzeBtn');
  const analyzeResult = document.getElementById('analyzeResult');
  const RATING_LABEL = {{ good: 'Good', ok: 'OK', bad: 'Concern', info: 'Info', unknown: 'Unknown' }};

  function esc(s) {{
    return String(s ?? '').replace(/[&<>"']/g, c => ({{'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}}[c]));
  }}

  function renderAnalysis(r) {{
    const v = r.verdict;
    let html = '<div class="an-verdict ' + v.rating + '"><b class="label">' + esc(v.label) + '</b>' + esc(v.summary);
    if (v.pros.length) html += '<br><b>For:</b> ' + v.pros.map(esc).join(' · ');
    if (v.cons.length) html += '<br><b>Against:</b> ' + v.cons.map(esc).join(' · ');
    html += '</div>';
    const title = r.url ? '<a href="' + esc(r.url) + '" target="_blank" rel="noopener">' + esc(r.address) + '</a>' : esc(r.address);
    html += '<div class="live-heading"><b>' + title + '</b></div>';
    html += '<div class="an-scroll"><table><thead><tr><th>Factor</th><th>This property</th><th>Compared with</th><th>Rating</th></tr></thead><tbody>';
    r.rows.forEach(row => {{
      html += '<tr><th>' + esc(row.factor) + '</th><td>' + esc(row.value) +
        (row.note ? '<div class="note">' + esc(row.note) + '</div>' : '') + '</td><td>' + esc(row.benchmark) +
        '</td><td><span class="pill ' + row.rating + '">' + RATING_LABEL[row.rating] + '</span></td></tr>';
    }});
    html += '</tbody></table></div>';
    html += '<div class="an-meta">Data from: ' + (r.sources.length ? r.sources.map(esc).join(' · ') : 'suburb results only') +
      '. Suburb comparisons use asking prices, not sold prices.</div>';
    html += '<div class="an-meta">Check yourself (no free source covers these):</div><div class="an-checks">' +
      r.checks.map(c => '<a href="' + esc(c.url) + '" target="_blank" rel="noopener">' + esc(c.label) + ' ↗</a>').join('') + '</div>';
    html += '<div class="an-actions"><button id="analyzeToCompare">Add to compare</button></div>';
    analyzeResult.innerHTML = html;

    document.getElementById('analyzeToCompare').addEventListener('click', e => {{
      const idx = LISTINGS.length;
      const item = Object.assign({{}}, r.listing, {{ idx, url: r.listing.url || '#' }});
      LISTINGS = LISTINGS.concat([item]);
      const holder = document.createElement('div');
      holder.className = 'grid';
      holder.style.marginTop = '0.8rem';
      holder.innerHTML = renderLiveCard(item, idx);
      analyzeResult.appendChild(holder);
      const box = holder.querySelector('.cmp');
      box.checked = true;
      box.dispatchEvent(new Event('change', {{ bubbles: true }}));
      e.target.disabled = true;
      e.target.textContent = 'Added — tick other cards and hit Compare';
    }});
  }}

  function runAnalysis() {{
    const text = analyzeInput.value.trim();
    if (!text) return;
    analyzeBtn.disabled = true;
    analyzeResult.innerHTML = '<p class="live-loading">Looking it up — daily scan, then homely.com.au live, then maps (takes ~10–20s)…</p>';
    fetch('/api/analyze', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify({{ text }}) }})
      .then(r => r.json().then(body => ({{ ok: r.ok, body }})))
      .then(({{ ok, body }}) => {{
        if (!ok) {{ analyzeResult.innerHTML = '<div class="live-error">' + esc(body.error) + '</div>'; return; }}
        renderAnalysis(body);
      }})
      .catch(() => {{
        analyzeResult.innerHTML = '<div class="live-error">The analyser needs the local server running — ' +
          'start it with <code>python server.py</code> instead of opening this file directly.</div>';
      }})
      .finally(() => {{ analyzeBtn.disabled = false; }});
  }}

  analyzeBtn.addEventListener('click', runAnalysis);
  analyzeInput.addEventListener('keydown', e => {{ if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) runAnalysis(); }});
}})();
</script>
</body>
</html>"""

    with open(os.path.join(REPORTS_DIR, f"{today_str}.html"), "w") as f:
        f.write(page)
    html_path = os.path.join(REPORTS_DIR, "latest.html")
    with open(html_path, "w") as f:
        f.write(page)
    return html_path


SUBURB_RE = re.compile(r",\s*([A-Za-z' -]+?)\s+VIC\s+\d{4}\s*$")


def write_json_feed(config, listings, seen_before, today_str, fetch_failed):
    """Write reports/latest.json — every fetched listing (not just the ones
    passing the hard criteria) plus the configured criteria as defaults, so
    the public showcase page can filter client-side however the viewer likes."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    weights = config["score_weights"]
    entries = []
    for idx, l in enumerate(listings):
        land_ok = (l.get("land_size_sqm") or 0) >= config["min_land_sqm"]
        price_mid = _price_mid(l)
        budget_ok = price_mid is not None and price_mid <= config["budget_max"]
        score, breakdown = quality_score(l, config["min_land_sqm"], weights)
        costs = compute_costs(price_mid or 0, config["deposit_pct"])
        entry = scored_entry_to_dict(idx, score, west_side_bonus(l), l, land_ok, budget_ok, breakdown, costs, weights)
        m = SUBURB_RE.search(l.get("address") or "")
        entry["suburb"] = m.group(1).title() if m else None
        entry["price_mid"] = price_mid
        entry["is_new"] = l.get("address") not in seen_before
        entries.append(entry)
    entries.sort(key=lambda e: e["total"], reverse=True)

    feed = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "date": today_str,
        "fetch_failed": fetch_failed,
        "criteria": {
            "budget_min": config["budget_min"],
            "budget_max": config["budget_max"],
            "min_land_sqm": config["min_land_sqm"],
            "min_bedrooms": config["min_bedrooms"],
            "suburbs": config.get("suburbs", []),
        },
        "weights": weights,
        "listings": entries,
    }
    with open(os.path.join(REPORTS_DIR, "latest.json"), "w") as f:
        json.dump(feed, f, indent=1)


def load_config():
    """config.yaml, with any top-level keys in config.local.yaml replacing it."""
    with open(CONFIG_PATH, "r") as f:
        config = yaml.safe_load(f)
    if os.path.exists(LOCAL_CONFIG_PATH):
        with open(LOCAL_CONFIG_PATH, "r") as f:
            config.update(yaml.safe_load(f) or {})
    return config


def main():
    config = load_config()

    today_str = datetime.date.today().isoformat()
    seen = load_seen()
    seen_before = set(seen.keys())

    listings, fetch_failed = fetch_listings(config)

    for l in listings:
        addr = l.get("address")
        if addr:
            seen[addr] = {"first_seen": seen.get(addr, {}).get("first_seen", today_str), "last_seen": today_str}
    save_seen(seen)

    scored = build_scored_listings(config, listings, seen_before)
    write_report(config, scored, today_str, fetch_failed)
    html_path = write_html_report(config, scored, today_str, fetch_failed)
    write_json_feed(config, listings, seen_before, today_str, fetch_failed)
    print(f"HTML report written: reports/{today_str}.html")

    if not os.environ.get("GITHUB_ACTIONS") and not os.environ.get("CI"):
        webbrowser.open(f"file://{html_path}")


if __name__ == "__main__":
    main()
