#!/usr/bin/env python3
"""
Single-property analyser — "should I go for this one?", NO API KEY REQUIRED.

Paste anything about one property (a homely / domain / realestate.com.au
link, a bare address, or the listing text copied off any site) and this
builds a full factor-by-factor table plus an overall verdict:

  1. Reads whatever facts are in the pasted text itself (price, land,
     beds/baths/cars, build year, status).
  2. Looks the address up internally first — in the last daily scan
     (reports/latest.json) — then live on homely.com.au: the listing's own
     page if a homely link was pasted, otherwise that suburb's for-sale
     search page, matched on street address.
  3. Benchmarks it against every other listing in the same suburb (asking
     prices — homely has no sold data), works out upfront cash, monthly
     repayments, buyer score, and driving distance to your reference
     locations.
  4. Anything no free source can tell us (build year, overlays, school
     zones, sold history) comes back as a ready-made search link, rather
     than a guess.

Used by server.py's /api/analyze; can also be run directly:
    python analyze_property.py "12 Example St, Truganina VIC 3029"
"""

import os
import re
import sys
import json
import datetime
import statistics
import urllib.parse

import scan_deals as sd

HOMELY_URL_RE = re.compile(r"https?://(?:www\.)?homely\.com\.au/homes/[^\s\"'<>]+")
DOMAIN_URL_RE = re.compile(r"https?://(?:www\.)?domain\.com\.au/([a-z0-9-]+?)-(\d{6,})\b")
REA_URL_RE = re.compile(r"https?://(?:www\.)?realestate\.com\.au/property-[a-z+]+-([a-z]{2,3})-([a-z+-]+?)-(\d{6,})\b")

STREET_TYPES = {
    "st": "street", "street": "street", "rd": "road", "road": "road",
    "ave": "avenue", "av": "avenue", "avenue": "avenue", "dr": "drive", "drive": "drive",
    "ct": "court", "crt": "court", "court": "court", "cres": "crescent", "cr": "crescent",
    "crescent": "crescent", "way": "way", "wy": "way", "bvd": "boulevard",
    "blvd": "boulevard", "boulevard": "boulevard", "pl": "place", "place": "place",
    "ln": "lane", "lane": "lane", "pde": "parade", "parade": "parade",
    "cct": "circuit", "circuit": "circuit", "cl": "close", "close": "close",
    "tce": "terrace", "terrace": "terrace", "gr": "grove", "gve": "grove", "grove": "grove",
    "rise": "rise", "walk": "walk", "loop": "loop", "hwy": "highway", "highway": "highway",
    "row": "row", "mews": "mews", "link": "link", "chase": "chase", "vista": "vista",
    "esp": "esplanade", "esplanade": "esplanade", "sq": "square", "square": "square",
    "gdns": "gardens", "gardens": "gardens", "pkwy": "parkway", "parkway": "parkway",
    "track": "track", "trl": "trail", "trail": "trail", "view": "view", "glade": "glade",
}

ADDRESS_RE = re.compile(
    r"\b((?:lot\s*)?\d+[a-z]?(?:\s*/\s*\d+[a-z]?)?\s+[a-z][a-z' -]{1,40}?\s+(?:"
    + "|".join(sorted(STREET_TYPES, key=len, reverse=True))
    + r")\b\.?,?\s+[a-z][a-z' -]{1,30}?,?\s+(?:vic|victoria)\b,?\s*(?:3\d{3})?)",
    re.I,
)
LAND_NEAR_RE = re.compile(
    r"land[^\d\n]{0,25}(\d[\d,]*(?:\.\d+)?)\s*(?:m²|m2|sqm|sq\.?\s?m|square\s*met(?:re|er)s?)", re.I
)
AREA_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:m²|m2|sqm|sq\.?\s?m|square\s*met(?:re|er)s?)\b", re.I)
BEDS_RE = re.compile(r"(\d{1,2})\s*(?:bed(?:room)?s?|br)\b|\bbed(?:room)?s?\s*[:\-]?\s*(\d{1,2})\b", re.I)
BATHS_RE = re.compile(r"(\d{1,2})\s*(?:bath(?:room)?s?|ba)\b|\bbath(?:room)?s?\s*[:\-]?\s*(\d{1,2})\b", re.I)
CARS_RE = re.compile(
    r"(\d{1,2})\s*(?:car(?:\s*spaces?)?|garage|parking)\b|\b(?:car(?:\s*spaces?)?|garage|parking)\s*[:\-]?\s*(\d{1,2})\b",
    re.I,
)
BUILT_RE = re.compile(r"(?:built|constructed|year\s*built|build\s*year|completed)\D{0,15}((?:19|20)\d{2})", re.I)
NON_PRICE_LINE_RE = re.compile(r"rent|p/?w|per\s*week|duty|deposit|rates|levy|bond|saving|commission", re.I)

GOOD, OK, BAD, INFO, UNKNOWN = "good", "ok", "bad", "info", "unknown"


# --------------------------------------------------------------------------
# 1. What's in the pasted text itself
# --------------------------------------------------------------------------
def _first_int(regex, text):
    m = regex.search(text)
    if not m:
        return None
    return int(next(g for g in m.groups() if g))


def parse_pasted_text(text):
    """Pull whatever facts the pasted text states outright. Every field is
    optional — a bare link or address just yields an address."""
    facts = {}

    m = HOMELY_URL_RE.search(text)
    if m:
        facts["homely_url"] = m.group(0).rstrip(".,)")

    # Domain links carry the full street address in the slug; REA links only
    # carry the suburb.
    m = DOMAIN_URL_RE.search(text)
    if m:
        facts["address_guess"] = m.group(1).replace("-", " ")
        facts["source_url"] = m.group(0)
    m = REA_URL_RE.search(text)
    if m:
        facts["suburb_guess"] = m.group(2).replace("+", " ").replace("-", " ")
        facts["source_url"] = m.group(0)

    # Strip URLs before looking for facts so slugs/ids don't read as numbers.
    body = re.sub(r"https?://\S+", " ", text)

    m = ADDRESS_RE.search(body)
    if m:
        facts["address_guess"] = re.sub(r"\s+", " ", m.group(1)).strip(" ,")

    price_lines = [ln for ln in body.splitlines() if "$" in ln and not NON_PRICE_LINE_RE.search(ln)]
    lo, hi = sd.extract_price_range(" ".join(price_lines))
    if lo:
        facts["price_low"], facts["price_high"] = lo, hi

    m = LAND_NEAR_RE.search(body)
    if m:
        land = float(m.group(1).replace(",", ""))
    else:
        # No "land" label: the biggest area quoted is almost always the block
        # (house size is smaller than the lot in these suburbs).
        areas = [float(a.replace(",", "")) for a in AREA_RE.findall(body)]
        land = max([a for a in areas if 80 <= a <= 10000], default=None)
    if land:
        facts["land_size_sqm"] = round(land)

    for key, regex in (("bedrooms", BEDS_RE), ("bathrooms", BATHS_RE), ("car_spaces", CARS_RE)):
        v = _first_int(regex, body)
        if v is not None and v < 20:
            facts[key] = v

    m = BUILT_RE.search(body)
    if m and 1900 <= int(m.group(1)) <= datetime.date.today().year + 2:
        facts["build_year"] = int(m.group(1))
    if sd.NEW_HOME_HINTS_RE.search(body):
        facts["is_new_home"] = True

    low = body.lower()
    if re.search(r"under contract|\bsold\b", low):
        facts["status"] = "Under contract"
    elif "under offer" in low:
        facts["status"] = "Under offer"
    elif "auction" in low:
        facts["status"] = "Auction scheduled"

    return facts


# --------------------------------------------------------------------------
# 2. Address matching
# --------------------------------------------------------------------------
def _norm_tokens(s):
    tokens = re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split()
    return [STREET_TYPES.get(t, t) for t in tokens]


def split_address(address):
    """("12 smith street", "truganina") from any of "12 Smith St, Truganina
    VIC 3029" / "12 smith street truganina vic 3029" / a domain URL slug."""
    tokens = _norm_tokens(address)
    if "lot" in tokens[:1]:
        tokens = tokens[1:]
    # drop state + postcode off the end
    while tokens and (tokens[-1] in ("vic", "victoria", "australia") or re.fullmatch(r"\d{4}", tokens[-1])):
        tokens.pop()
    street_type_words = set(STREET_TYPES.values())
    cut = None
    for i, t in enumerate(tokens):
        if i > 0 and t in street_type_words:
            cut = i
    if cut is None:
        return " ".join(tokens), ""
    return " ".join(tokens[:cut + 1]), " ".join(tokens[cut + 1:])


def same_address(a, b):
    sa, ua = split_address(a)
    sb, ub = split_address(b)
    return bool(sa) and sa == sb and (not ua or not ub or ua == ub)


# --------------------------------------------------------------------------
# 3. Data lookups — internal scan first, then homely live
# --------------------------------------------------------------------------
def load_internal_listings():
    path = os.path.join(sd.REPORTS_DIR, "latest.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f).get("listings", [])


def _feed_entry_to_listing(e):
    """latest.json entry → the parse_listing() shape used by the scorer."""
    return {
        "address": e.get("address"), "url": e.get("url"),
        "price_low": e.get("price_low"), "price_high": e.get("price_high"),
        "price_display": e.get("price_display"),
        "land_size_sqm": e.get("land"), "bedrooms": e.get("beds"),
        "bathrooms": e.get("baths"), "car_spaces": e.get("cars"),
        "is_new_home": e.get("is_new_home"), "build_year": e.get("build_year"),
        "status": e.get("status"), "lat": e.get("lat"), "lon": e.get("lon"),
    }


def _extract_detail_listing(html_content):
    """A homely listing page streams the listing as RSC chunks, like the
    search page does — but as a single "listing":{...} object."""
    payload = ""
    for chunk in sd.RSC_CHUNK_RE.findall(html_content):
        try:
            parts = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if len(parts) > 1 and isinstance(parts[1], str):
            payload += parts[1]
    idx = payload.find('"listing":{"id"')
    if idx == -1:
        m = sd.NEXT_DATA_RE.search(html_content)
        if not m:
            return None
        try:
            return json.loads(m.group(1))["props"]["pageProps"]["listing"]
        except (KeyError, TypeError, json.JSONDecodeError):
            return None
    obj, _ = json.JSONDecoder().raw_decode(payload, idx + len('"listing":'))
    return sd._unescape_rsc(obj)


def fetch_homely_detail(page, url):
    """Returns (parsed listing, extras) for one homely listing URL, or (None, {})."""
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=sd.PAGE_TIMEOUT_MS)
        if not resp or resp.status != 200:
            return None, {}
        page.wait_for_timeout(500)
        raw = _extract_detail_listing(page.content())
    except Exception as e:
        print(f"  [warn] listing fetch failed for {url}: {e}", file=sys.stderr)
        return None, {}
    if not raw:
        return None, {}

    listing = sd.parse_listing(raw)
    addr = raw.get("address") or {}
    if listing.get("lat") is None and addr.get("latitude") is not None:
        listing["lat"], listing["lon"] = addr["latitude"], addr["longitude"]

    extras = {
        "tags": (raw.get("features") or {}).get("tags") or [],
        "title": raw.get("title"),
        "council_rates": raw.get("councilRates"),
        "listed_on": ((raw.get("saleDetails") or {}).get("listingDetails") or {}).get("listedOn"),
        "property_type": raw.get("propertyTypeDescription"),
    }
    desc = raw.get("description") or ""
    m = BUILT_RE.search(desc)
    if m:
        listing["build_year"] = int(m.group(1))
    return listing, extras


def suburb_of(address):
    _, suburb = split_address(address)
    return suburb


# --------------------------------------------------------------------------
# 4. Maths helpers
# --------------------------------------------------------------------------
def monthly_repayment(loan, rate_pct, years=30):
    r = rate_pct / 100 / 12
    n = years * 12
    if loan <= 0:
        return 0
    if r == 0:
        return loan / n
    return loan * r / (1 - (1 + r) ** -n)


def money(v):
    return f"${v:,.0f}" if v is not None else "N/A"


def percentile_rank(value, values):
    """Share of `values` strictly below `value`, 0–100."""
    if not values:
        return None
    return round(100 * sum(1 for v in values if v < value) / len(values))


def comparables_stats(subject, pool):
    """Benchmark against same-suburb listings, preferring similar bedroom
    counts. Asking prices only — homely publishes no sold prices."""
    priced = [p for p in pool if sd._price_mid(p) and not same_address(p.get("address"), subject.get("address"))]
    beds = subject.get("bedrooms")
    similar = [p for p in priced if beds and p.get("bedrooms") and abs(p["bedrooms"] - beds) <= 1]
    comps = similar if len(similar) >= 3 else priced
    if not comps:
        return None
    prices = [sd._price_mid(p) for p in comps]
    lands = [p["land_size_sqm"] for p in comps if p.get("land_size_sqm")]
    per_sqm = [sd._price_mid(p) / p["land_size_sqm"] for p in comps if p.get("land_size_sqm")]
    return {
        "n": len(comps),
        "similar_beds": comps is similar,
        "median_price": statistics.median(prices),
        "median_land": statistics.median(lands) if lands else None,
        "median_per_sqm": statistics.median(per_sqm) if per_sqm else None,
        "prices": prices,
        "per_sqm": per_sqm,
    }


# --------------------------------------------------------------------------
# 5. The analysis itself
# --------------------------------------------------------------------------
def analyze(text, config=None):
    config = config or sd.load_config()
    facts = parse_pasted_text(text)
    sources = []
    listing = {}
    extras = {}
    suburb_pool = []

    address = facts.get("address_guess")
    internal = load_internal_listings()

    # (a) internal: today's daily scan
    if address:
        hit = next((e for e in internal if same_address(e.get("address"), address)), None)
        if hit:
            listing.update({k: v for k, v in _feed_entry_to_listing(hit).items() if v is not None})
            sources.append("Found in today's daily scan (reports/latest.json)")

    # (b) live homely: listing page and/or the suburb's search page
    homely_url = facts.get("homely_url") or listing.get("url")
    suburb_name = suburb_of(listing.get("address") or address or "") or facts.get("suburb_guess")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(user_agent=sd.USER_AGENT, locale="en-AU").new_page()

        if homely_url:
            detail, extras = fetch_homely_detail(page, homely_url)
            if detail:
                listing.update({k: v for k, v in detail.items() if v is not None and not k.startswith("_")})
                sources.append("Listing page on homely.com.au (fetched live)")
                suburb_name = suburb_of(detail["address"]) or suburb_name

        if suburb_name:
            resolved = sd.resolve_suburb_slug(suburb_name, "Victoria")
            if resolved:
                slug, nice_name, _ = resolved
                suburb_name = nice_name
                parsed = sd.scan_suburb_page(page, slug) or []
                suburb_pool = [l for l in parsed if re.search(r"\bVIC\b", l.get("address") or "")]
                target = listing.get("address") or address
                hit = next((l for l in suburb_pool if target and same_address(l.get("address"), target)), None)
                if hit:
                    # search-page data fills gaps only (e.g. land size, which
                    # the listing page sometimes leaves null)
                    for k, v in hit.items():
                        if v is not None and listing.get(k) is None and not k.startswith("_"):
                            listing[k] = v
                    sources.append(f"Matched in {nice_name}'s live for-sale results on homely.com.au")
        browser.close()

    # (c) what you pasted wins over anything scraped — it's what you're looking at
    pasted_keys = ["price_low", "price_high", "land_size_sqm", "bedrooms", "bathrooms",
                   "car_spaces", "build_year", "is_new_home", "status"]
    pasted_used = [k for k in pasted_keys if k in facts]
    for k in pasted_used:
        listing[k] = facts[k]
    if pasted_used:
        sources.append("Details you pasted (" + ", ".join(k.replace("_", " ") for k in pasted_used) + ")")

    listing.setdefault("address", address or (f"Property in {suburb_name.title()}" if suburb_name else "Unknown address"))
    listing.setdefault("url", facts.get("homely_url") or facts.get("source_url"))
    status_assumed = "status" not in listing
    if status_assumed:
        # nothing said otherwise, and you're looking at it because it's for sale
        listing["status"] = "Available"

    # benchmark pool = live suburb results + same-suburb rows from the daily scan
    pool = list(suburb_pool)
    if suburb_name:
        seen_addrs = {l.get("address") for l in pool}
        for e in internal:
            if (e.get("suburb") or "").lower() == suburb_name.lower() and e.get("address") not in seen_addrs:
                pool.append(_feed_entry_to_listing(e))

    if not any(listing.get(k) for k in ("price_low", "land_size_sqm", "bedrooms")) and not suburb_pool:
        return {
            "error": "Couldn't find this property or read any details from what you pasted. "
                     "Try a homely.com.au link, a full address like \"12 Smith St, Truganina VIC 3029\", "
                     "or paste the listing text (price, beds, land size)."
        }

    approx_location = False
    if listing.get("lat") is None:
        coords = sd.geocode_address(listing["address"]) if address else None
        if not coords and suburb_name:
            coords = sd.geocode_address(f"{suburb_name.title()}, VIC, Australia")
            approx_location = bool(coords)
        if coords:
            listing["lat"], listing["lon"] = coords

    extras["status_assumed"] = status_assumed
    extras["approx_location"] = approx_location
    return build_report(config, listing, extras, pool, suburb_name, sources)


def build_report(config, l, extras, pool, suburb_name, sources):
    rows = []

    def row(factor, value, benchmark, rating, note=""):
        rows.append({"factor": factor, "value": value, "benchmark": benchmark, "rating": rating, "note": note})

    price_mid = sd._price_mid(l)
    weights = config["score_weights"]
    comps = comparables_stats(l, pool) if pool else None
    suburb_label = (suburb_name or "this suburb").title()

    # Use the suburb's comparable median as the "estimate" so the value
    # part of the score means something here (the daily scan has no estimate).
    if comps and price_mid:
        l["domain_estimate_mid"] = comps["median_price"]
    score, breakdown = sd.quality_score(l, config["min_land_sqm"], weights)
    bonus = sd.west_side_bonus(l)

    # --- price vs budget
    bmin, bmax = config["budget_min"], config["budget_max"]
    if price_mid is None:
        row("Price vs your budget", l.get("price_display") or "Not stated", f"{money(bmin)}–{money(bmax)}", UNKNOWN,
            "No price advertised — ask the agent for a price guide / Statement of Information.")
    else:
        shown = money(l["price_low"]) if l.get("price_low") == l.get("price_high") else f"{money(l.get('price_low'))}–{money(l.get('price_high'))}"
        if price_mid <= bmax and price_mid >= bmin:
            row("Price vs your budget", shown, f"{money(bmin)}–{money(bmax)}", GOOD, "Inside your range.")
        elif price_mid < bmin:
            row("Price vs your budget", shown, f"{money(bmin)}–{money(bmax)}", GOOD,
                "Under your range — check why (size, condition, location) before celebrating.")
        elif price_mid <= bmax * 1.05:
            row("Price vs your budget", shown, f"{money(bmin)}–{money(bmax)}", OK,
                f"{money(price_mid - bmax)} over budget at the mid-point — may be negotiable.")
        else:
            row("Price vs your budget", shown, f"{money(bmin)}–{money(bmax)}", BAD,
                f"{money(price_mid - bmax)} over your budget cap.")

    # --- price vs suburb
    if comps and price_mid:
        diff = (price_mid - comps["median_price"]) / comps["median_price"] * 100
        bench = f"Median {money(comps['median_price'])} across {comps['n']} {suburb_label} listings" + (
            " with similar beds" if comps["similar_beds"] else "")
        rating = GOOD if diff <= -5 else BAD if diff >= 10 else OK
        rank = percentile_rank(price_mid, comps["prices"])
        row("Price vs suburb (asking prices)", f"{diff:+.0f}% vs median", bench, rating,
            f"Cheaper than {100 - rank}% of comparables." if rank is not None and diff < 0
            else f"Dearer than {rank}% of comparables." if rank is not None else "")
    else:
        row("Price vs suburb (asking prices)", "—", "No comparable listings found", UNKNOWN, "")

    # --- land
    land = l.get("land_size_sqm")
    min_land = config["min_land_sqm"]
    if land:
        bench = f"Your minimum {min_land}m²" + (f" · suburb median {comps['median_land']:.0f}m²" if comps and comps["median_land"] else "")
        if land < min_land:
            row("Land size", f"{land}m²", bench, BAD, f"{min_land - land}m² below your minimum.")
        elif comps and comps["median_land"] and land >= comps["median_land"] * 1.1:
            row("Land size", f"{land}m²", bench, GOOD, "Bigger block than most in the suburb — land is what appreciates.")
        else:
            row("Land size", f"{land}m²", bench, GOOD if land >= min_land + 100 else OK, "Meets your minimum.")
        if comps and comps["median_per_sqm"] and price_mid:
            per = price_mid / land
            d = (per - comps["median_per_sqm"]) / comps["median_per_sqm"] * 100
            row("Price per m² of land", f"{money(per)}/m²", f"Suburb median {money(comps['median_per_sqm'])}/m²",
                GOOD if d <= -8 else BAD if d >= 15 else OK,
                f"{abs(d):.0f}% {'below' if d < 0 else 'above'} the suburb's land rate.")
    else:
        row("Land size", "Not stated", f"Your minimum {min_land}m²", UNKNOWN,
            "Check the title plan / Section 32 for the exact lot size.")

    # --- rooms
    beds = l.get("bedrooms")
    min_beds = config["min_bedrooms"]
    if beds:
        row("Bedrooms", str(beds), f"Your minimum {min_beds}", GOOD if beds >= min_beds else BAD,
            "" if beds >= min_beds else f"{min_beds - beds} short of what you need.")
    else:
        row("Bedrooms", "Not stated", f"Your minimum {min_beds}", UNKNOWN, "")
    baths, cars = l.get("bathrooms"), l.get("car_spaces")
    row("Bathrooms / car spaces", f"{baths if baths is not None else '?'} / {cars if cars is not None else '?'}",
        "2+ / 2+ is typical for a family house", GOOD if (baths or 0) >= 2 and (cars or 0) >= 2 else OK if baths else UNKNOWN, "")

    # --- build
    if l.get("is_new_home"):
        row("Build age", "New / house-and-land", "Newer = lower maintenance", GOOD,
            "Check builder reputation, completion date and whether the price includes landscaping/driveway.")
    elif l.get("build_year"):
        age = datetime.date.today().year - l["build_year"]
        row("Build age", f"Built {l['build_year']} ({age} yrs)", "Newer = lower maintenance",
            GOOD if age <= 10 else OK if age <= 20 else BAD, "")
    else:
        row("Build age", "Unknown", "Newer = lower maintenance", UNKNOWN,
            "homely has no build-year data — use the \"Build year / builder\" search below.")

    # --- availability / time on market
    status = l.get("status") or "Unknown"
    srating = {"Available": GOOD, "Auction scheduled": OK, "Under offer": BAD, "Under contract": BAD}.get(status, UNKNOWN)
    snote = {"Auction scheduled": "Auction — no cooling-off, get finance and a building inspection done before the day.",
             "Under offer": "Someone already has an offer in — you'd be competing.",
             "Under contract": "Effectively gone unless the contract falls over."}.get(status, "")
    if extras.get("status_assumed"):
        snote = "Assumed — nothing you pasted or homely said otherwise. Confirm with the agent."
    row("Status", status, "Available is best", srating, snote)

    if extras.get("listed_on"):
        try:
            listed = datetime.datetime.fromisoformat(extras["listed_on"].replace("Z", "+00:00")).date()
            days = (datetime.date.today() - listed).days
            row("Days on market", f"{days} days (listed {listed:%d %b %Y})", "Under ~30 = fresh, 60+ = stale",
                GOOD if days >= 60 else OK,
                "Long time listed — the vendor may take a lower offer." if days >= 60
                else "Fresh listing — less room to negotiate." if days < 30 else "")
        except ValueError:
            pass

    # --- money
    if price_mid:
        costs = sd.compute_costs(price_mid, config["deposit_pct"])
        rate = config.get("interest_rate_pct", 6.0)
        repay = monthly_repayment(costs["loan"], rate)
        row("Upfront cash needed", money(costs["total_upfront"]),
            f"{config['deposit_pct']}% deposit {money(costs['deposit'])} + duty {money(costs['stamp_duty'])}"
            f" + LMI {money(costs['lmi_total'])} + fees {money(costs['other_costs'])}", INFO,
            "Stamp duty assumes first-home-buyer concession (full standard duty would be "
            f"{money(costs['standard_duty'])}).")
        row("Loan & repayments", f"{money(repay)}/month", f"{money(costs['loan'])} loan · {rate}% · 30 yrs · LVR {costs['lvr']}%",
            INFO, "Principal & interest estimate — check against your lender's real rate.")
    else:
        costs = sd.compute_costs(0, config["deposit_pct"])

    if extras.get("council_rates"):
        row("Council rates", str(extras["council_rates"]), "", INFO, "")

    # --- location
    max_km = config.get("max_drive_km", 35)
    if l.get("lat") is not None:
        for ref in config.get("reference_locations", []):
            origin = sd.geocode_address(ref["address"])
            route = sd.driving_distance(origin, (l["lat"], l["lon"])) if origin else None
            if route:
                km, mins = route
                row(f"Drive to {ref['name']}", f"{'~' if extras.get('approx_location') else ''}{km} km · {mins} min",
                    f"Your limit {max_km} km (no traffic)",
                    GOOD if km <= max_km * 0.8 else OK if km <= max_km else BAD,
                    ("From the suburb centre — the street address couldn't be mapped. " if extras.get("approx_location") else "")
                    + "Free-flow time; add 30–60% at peak.")
            else:
                row(f"Drive to {ref['name']}", "Couldn't route", "", UNKNOWN, "")
    else:
        row("Location", "Couldn't place the address on a map", "", UNKNOWN, "")
    row("West-side preference", "Yes (+%d bonus)" % bonus if bonus else "No", "Your preference: western suburbs",
        GOOD if bonus else OK, "")

    if extras.get("tags"):
        nice = ", ".join(re.sub(r"(?<!^)([A-Z])", r" \1", t).lower() for t in extras["tags"])
        row("Listed features", nice, "", INFO, "")

    row("Buyer score", f"{score}/100" + (f" (+{bonus} west)" if bonus else ""),
        f"land {breakdown['land']}/{weights['land_and_size']} · value {breakdown['value']}/{weights['value_vs_estimate']}"
        f" · build {breakdown['build']}/{weights['build_age_condition']} · availability {breakdown['status']}/{weights['availability_status']}",
        GOOD if score >= 70 else OK if score >= 55 else BAD,
        "Value is scored against the suburb's asking-price median." if l.get("domain_estimate_mid") else "")

    verdict = make_verdict(config, l, rows, score, price_mid, comps)
    checks = check_links(l, suburb_name)

    land_ok = (land or 0) >= min_land
    budget_ok = price_mid is not None and price_mid <= bmax
    card = sd.scored_entry_to_dict(0, score, bonus, l, land_ok, budget_ok, breakdown, costs, weights)

    return {
        "address": l["address"], "url": l.get("url"), "suburb": suburb_label,
        "sources": sources, "rows": rows, "verdict": verdict, "checks": checks, "listing": card,
    }


def make_verdict(config, l, rows, score, price_mid, comps):
    pros = [f"{r['factor']}: {r['value']}" for r in rows if r["rating"] == GOOD and r["factor"] != "Buyer score"]
    cons = [f"{r['factor']}: {r['value']}" for r in rows if r["rating"] == BAD and r["factor"] != "Buyer score"]
    unknowns = [r["factor"] for r in rows if r["rating"] == UNKNOWN]

    deal_breakers = []
    if price_mid and price_mid > config["budget_max"] * 1.05:
        deal_breakers.append("over budget")
    if l.get("land_size_sqm") and l["land_size_sqm"] < config["min_land_sqm"]:
        deal_breakers.append("land below your minimum")
    if l.get("bedrooms") and l["bedrooms"] < config["min_bedrooms"]:
        deal_breakers.append("not enough bedrooms")
    if l.get("status") == "Under contract":
        deal_breakers.append("already under contract")

    if deal_breakers:
        label, rating = "Skip", BAD
        summary = "Fails your hard criteria: " + ", ".join(deal_breakers) + "."
    elif score >= 60 and len(pros) - 2 * len(cons) >= 6:
        label, rating = "Go for it — book an inspection", GOOD
        summary = "Ticks your criteria and stacks up well against the suburb."
    elif len(pros) - 2 * len(cons) >= 2:
        label, rating = "Worth a look", OK
        summary = "Meets your criteria but nothing stands out — inspect and negotiate on the weak points."
    else:
        label, rating = "Probably pass", BAD
        summary = "Meets the basics, but more factors work against it than for it."
    if len(unknowns) >= 3:
        summary += f" {len(unknowns)} factors are unknown, so treat this as provisional."
    return {"label": label, "rating": rating, "summary": summary, "pros": pros, "cons": cons, "unknowns": unknowns}


def check_links(l, suburb_name):
    """Things no free scriptable source answers reliably — hand them over as searches."""
    addr = l["address"]
    sub = (suburb_name or "").title()
    g = lambda q: "https://www.google.com/search?q=" + urllib.parse.quote_plus(q)
    links = [
        {"label": "Past sales of this address", "url": g(f'"{addr}" sold')},
        {"label": f"Recent sold prices in {sub}", "url": g(f"{sub} VIC houses sold last 3 months")},
        {"label": "Flood / bushfire / heritage overlays (VicPlan)", "url": "https://mapshare.vic.gov.au/vicplan/"},
        {"label": "School zones (findmyschool.vic.gov.au)", "url": "https://www.findmyschool.vic.gov.au/"},
        {"label": f"Crime statistics for {sub}", "url": g(f"{sub} crime statistics agency victoria")},
        {"label": "Build year / builder", "url": g(f'"{addr}" built year builder')},
    ]
    if l.get("lat") is not None:
        links.append({"label": "Street view / surroundings",
                      "url": f"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={l['lat']},{l['lon']}"})
    return links


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python analyze_property.py "<address, link or listing text>"', file=sys.stderr)
        sys.exit(1)
    result = analyze(" ".join(sys.argv[1:]))
    if "error" in result:
        print(result["error"], file=sys.stderr)
        sys.exit(1)
    print(f"\n{result['address']}\n" + "-" * len(result["address"]))
    for r in result["rows"]:
        print(f"[{r['rating']:>7}] {r['factor']}: {r['value']}  ({r['benchmark']})")
    v = result["verdict"]
    print(f"\nVerdict: {v['label']} — {v['summary']}")
