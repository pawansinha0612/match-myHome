#!/usr/bin/env python3
"""
Local live-search server — companion to scan_deals.py, NO API KEY REQUIRED.

scan_deals.py does the daily batch scan across your configured suburb list
and writes reports/latest.html. This server adds one more thing on top: a
"search any suburb right now" box in that same page. Typing a suburb and
hitting Enter calls homely.com.au's own suburb-autocomplete API to resolve
it to a slug, then scrapes that suburb live using the exact same fetch/parse
logic as the daily scan — no AI, no third-party API key, just homely.com.au.

Run:
    python server.py

Then use the page it opens (http://127.0.0.1:5001/) as normal — the daily
scan's results are already there; the search box now also works live.
"""

import os
import re
import sys
import threading
import webbrowser

try:
    from flask import Flask, jsonify, request, send_from_directory
except ImportError as e:
    print(f"Missing dependency ({e}). Run: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

import scan_deals as sd

PORT = 5001

STATE_ABBR = {
    "new south wales": "NSW",
    "victoria": "VIC",
    "queensland": "QLD",
    "south australia": "SA",
    "tasmania": "TAS",
    "western australia": "WA",
    "northern territory": "NT",
    "australian capital territory": "ACT",
}

app = Flask(__name__)


@app.route("/")
def index():
    return send_from_directory(sd.REPORTS_DIR, "latest.html")


@app.route("/reports/<path:filename>")
def reports_files(filename):
    return send_from_directory(sd.REPORTS_DIR, filename)


@app.route("/api/scan")
def api_scan():
    query = request.args.get("suburb", "").strip()
    state_filter = request.args.get("state", "").strip() or None
    price_max_param = request.args.get("price_max", "").strip()
    if not query:
        return jsonify({"error": "Type a suburb name first."}), 400
    if price_max_param:
        try:
            price_max = float(price_max_param)
        except ValueError:
            return jsonify({"error": f'Invalid price max "{price_max_param}".'}), 400
    else:
        price_max = None  # blank = no budget cap for this live search

    resolved = sd.resolve_suburb_slug(query, state_filter)
    if not resolved:
        scope = f" in {state_filter}" if state_filter else ""
        return jsonify({"error": f'Could not find a suburb matching "{query}"{scope} on homely.com.au.'}), 404
    slug, name, state = resolved

    config = sd.load_config()
    live_config = dict(config)
    live_config["max_listings_per_run"] = None  # show every result for this one suburb, no top-N cap
    live_config["budget_max"] = price_max if price_max is not None else float("inf")

    seen_before = set(sd.load_seen().keys())

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=sd.USER_AGENT, locale="en-AU")
        page = context.new_page()
        parsed = sd.scan_suburb_page(page, slug)
        browser.close()

    if parsed is None:
        return jsonify({
            "error": f"Fetch failed for {name} ({slug}) — homely.com.au may have blocked the request or changed its page structure."
        }), 502

    # homely.com.au pads out low-inventory suburbs (e.g. a small VIC border
    # town) with nearby listings that can be just across the state line, so
    # "VIC only" needs to filter the actual returned addresses, not just
    # disambiguate which suburb slug gets fetched.
    if state_filter:
        abbr = STATE_ABBR.get(state_filter.lower())
        if abbr:
            parsed = [l for l in parsed if re.search(rf"\b{abbr}\b", l.get("address") or "")]

    scored = sd.build_scored_listings(live_config, parsed, seen_before)
    weights = live_config["score_weights"]
    results = [
        sd.scored_entry_to_dict(i, score, bonus, l, land_ok, budget_ok, breakdown, costs, weights)
        for i, (score, bonus, l, land_ok, budget_ok, breakdown, costs, is_new) in enumerate(scored)
    ]
    return jsonify({"suburb": name, "state": state, "slug": slug, "count": len(results), "results": results})


@app.route("/api/distances")
def api_distances():
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"error": "lat/lon required"}), 400

    config = sd.load_config()

    out = []
    for ref in config.get("reference_locations", []):
        origin = sd.geocode_address(ref["address"])
        if not origin:
            out.append({"name": ref["name"], "address": ref["address"], "error": "Could not geocode this address"})
            continue
        route = sd.driving_distance(origin, (lat, lon))
        if not route:
            out.append({"name": ref["name"], "address": ref["address"], "error": "Routing failed"})
            continue
        km, minutes = route
        out.append({"name": ref["name"], "address": ref["address"], "km": km, "minutes": minutes})
    return jsonify({"distances": out})


def main():
    latest_html = os.path.join(sd.REPORTS_DIR, "latest.html")
    if not os.path.exists(latest_html):
        print("No reports/latest.html found yet — run `python scan_deals.py` at least once first.", file=sys.stderr)
        sys.exit(1)

    url = f"http://127.0.0.1:{PORT}/"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"Serving {latest_html} at {url} (Ctrl+C to stop)")
    app.run(port=PORT, debug=False)


if __name__ == "__main__":
    main()
