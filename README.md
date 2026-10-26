# match-myHome — Melbourne Deal Finder

A Playwright scraper that scans Melbourne houses for sale every morning,
scores each one as a buyer would, and publishes the results to a live web
page. It needs no API keys, no paid services and no server.

**Live results:** https://pawansinha.com.au/projects/deal-finder/

[![Daily scan](https://github.com/pawansinha0612/match-myHome/actions/workflows/daily-scan.yml/badge.svg)](https://github.com/pawansinha0612/match-myHome/actions/workflows/daily-scan.yml)

---

## What it does

- **Scans** the for-sale search results on [homely.com.au](https://www.homely.com.au)
  for each suburb listed in `config.yaml`, using headless Chromium.
- **Scores** every listing out of 100 on land size, value, build age and
  availability, plus a small separate bonus for western suburbs. It doesn't
  simply rank by price.
- **Estimates upfront cash** for each listing: deposit, VIC stamp duty,
  lenders mortgage insurance (LMI) and other purchase costs.
- **Flags new listings** since the previous scan, and marks anything under
  offer or under contract.
- **Publishes** `reports/latest.json`, which the live page reads in the
  browser and lets visitors filter by price, land, bedrooms, suburb and status.

## How it works

```
 GitHub Actions (7am Melbourne, daily)
        │
        ▼
 scan_deals.py ── Playwright/Chromium ──► homely.com.au search pages
        │                                   (reads the JSON embedded in each page)
        ▼
 parse → sanity-check → score → cost estimate → compare with seen listings
        │
        ▼
 reports/latest.json · latest.html · latest.md      committed back to this repo
        │
        ▼
 pawansinha.com.au/projects/deal-finder/   fetches latest.json and filters it in the browser
```

The scraper reads the structured data that homely embeds in each search page
rather than scraping the visible text, which makes it much less fragile.
homely has used two formats, and both are supported:

- the classic Next.js `__NEXT_DATA__` script tag
- React Server Component chunks (`self.__next_f.push(...)`), which homely
  switched to in October 2026. These are decoded by `extract_rsc_ssr_data()`.

## Quick start

Requires Python 3.11 or later.

```bash
git clone https://github.com/pawansinha0612/match-myHome.git
cd match-myHome
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### Run a scan

```bash
python scan_deals.py
```

This writes the reports to `reports/` and opens `reports/latest.html` in your
browser. The auto-open is skipped when `CI` or `GITHUB_ACTIONS` is set.

### Live search server (optional, local only)

```bash
python server.py
```

Then open http://127.0.0.1:5001/. You get the same report, plus:

- a search box that scrapes **any** suburb live, using homely's own suburb
  lookup to find it
- a compare view showing driving distance and time from each property to
  your `reference_locations`, using free OSRM routing

## Configuration

| File | Committed? | Purpose |
|---|---|---|
| `config.yaml` | Yes | Public search criteria. The daily GitHub run uses this file. |
| `config.local.yaml` | **No** (git-ignored) | Your personal overrides, used only on your machine. |

Every top-level key in `config.local.yaml` replaces the same key in
`config.yaml`. Keep anything personal there, for example:

```yaml
# config.local.yaml
deposit_pct: 10
reference_locations:
  - name: "Work"
    address: "1 Example St, Melbourne, VIC 3000"
feasibility_analyzer_url: "https://..."   # optional link shown in the HTML report
```

Main settings in `config.yaml`:

| Key | Meaning |
|---|---|
| `budget_min`, `budget_max` | Price range. Listings above `budget_max` are left out of the report. |
| `min_land_sqm`, `min_bedrooms` | Hard filters for the report |
| `deposit_pct` | Deposit assumed for the LVR, LMI and upfront cash estimates |
| `max_listings_per_run` | Number of top listings in the HTML and Markdown reports |
| `score_weights` | Buyer score weights (must add up to 100) |
| `suburbs` | homely URL slugs, e.g. `truganina-vic-3029`. Each one costs one page load per run. |

`reports/latest.json` always contains **every** scanned listing, whatever
the filters, so the live page can apply its own.

## Daily automation

`.github/workflows/daily-scan.yml` runs the scan at 20:00 UTC, which is 7am in
Melbourne during daylight saving (6am otherwise). It commits the updated
`reports/latest.*` and `data/seen_listings.json` back to the repo.

To run it on demand: **Actions → Match My-Home → Run workflow**.

## Buyer score

| Factor | Points | Rewards |
|---|---|---|
| Land and size | 30 | More land above `min_land_sqm`, capped at +250 m² |
| Value vs. independent estimate | 30 | Neutral (15) for now, because homely provides no valuation |
| Build age and condition | 20 | New builds score highest. An unknown build year scores neutral. |
| Availability | 20 | Available scores full. Under offer scores low; under contract scores zero. |
| West-side bonus | +5 | Shown separately, not part of the /100 |

## Output files

| Path | Contents |
|---|---|
| `reports/latest.json` | Every listing with score, breakdown, estimated upfront cost and new flag. Feeds the live page. |
| `reports/latest.html` | Styled report of the top matches |
| `reports/latest.md` | The same report in Markdown |
| `data/seen_listings.json` | First and last date each address was seen; used to flag new listings |

Dated copies (`reports/YYYY-MM-DD.*`) and the geocoding cache are written
locally but git-ignored.

## Project structure

```
scan_deals.py          scraper, parser, scoring, cost model, report writers
server.py              optional local Flask server: live suburb search + distances
config.yaml            public search criteria
config.local.yaml      your private overrides (git-ignored, create it yourself)
.github/workflows/     daily scheduled scan
reports/               generated reports (latest.* committed)
data/                  seen-listings history (committed), geocode cache (ignored)
```

## Known limitations

- **No build year.** homely's data has no build year field, so build age
  scores neutral unless a listing is tagged as a new build or house and land.
- **No independent valuation**, so the value score is always neutral.
- **No overlay data** (flood, bushfire, heritage).
- **Agent prices are often marketing text** ("Contact Agent",
  "$649K–$679K"). The scraper takes real dollar figures from the text first,
  and only falls back to homely's internal reference price. Sanity bounds
  reject obvious junk, such as $1 prices or blocks over one hectare.
- **Only page 1** of each suburb's results (about 25 listings) is read.
- **Cost estimates are approximate.** Stamp duty uses the VIC
  first-home-buyer concession bands, and LMI uses typical premium rates.
- Domain and realestate.com.au block headless browsers at the edge (Akamai
  and Kasada), which is why this project uses homely.
- Scraping is inherently fragile. If homely changes its pages again, see below.

## If parsing breaks

1. Check `reports/latest.md` for the warning banner, or `fetch_failed` in
   `latest.json`. The live page shows a notice when it's set.
2. Look at `NEXT_DATA_RE`, `RSC_CHUNK_RE`, `extract_rsc_ssr_data()` and
   `parse_listing()` in `scan_deals.py`.
3. A broken field becomes `null` instead of crashing the run, so a partial
   change in homely's pages won't stop the daily reports.

## Responsible use

This project reads public search pages once a day, with a delay between
requests. It's for personal home-buying research, not bulk collection or
commercial use. Check homely.com.au's Terms of Use before reusing it. It is
not financial advice, and it doesn't replace a conveyancer, building
inspector or buyer's agent.
