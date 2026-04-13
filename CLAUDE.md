# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Static site generator for **Terminal de Ofertas** — a Brazilian deal aggregator that fetches products from the Lomadee affiliate API and Shopee CSV exports, then generates a static `index.html` deployed via GitHub Pages to `terminaldeofertas.com.br`.

## Commands

```bash
# Install Python dependencies
pip install -r requirements.txt

# Generate index.html and sitemap.xml
python3 generator.py

# Full automation: git pull → generate → git commit → git push
./run.sh
```

## Architecture

Everything is driven by a single Python script (`generator.py`) that produces two output files:

```
generator.py → index.html + sitemap.xml → GitHub Pages
```

**Data sources:**
- **Lomadee API** — affiliate network; fetches products by search term and campaign. Brand IDs are cached in-memory to avoid redundant API calls. Affiliate short URLs are generated per product.
- **Shopee CSV** — local file or S3-hosted. Reservoir sampling ensures unbiased random selection. Filtered by discount %, item/shop rating, and price range.

**Output sections in `index.html`:**
1. Campaigns (Lomadee campaigns)
2. Shopee Offers (CSV-sourced)
3. Other Offers (Lomadee search-term products)

The HTML and CSS are fully inline — generated as strings in `generator.py` with no external templates or build tools.

## Configuration

Copy `.env.example` to `.env` and fill in:

| Variable | Purpose |
|---|---|
| `LOMADEE_TOKEN` | Lomadee API authentication key |
| `SOURCE_ID` | Lomadee source/affiliate identifier |
| `SITE_URL` | Canonical base URL |
| `CSV_LOCAL_PATH` | Path to local Shopee CSV (optional if S3 is set) |
| `S3_BUCKET` / `S3_KEY` | AWS S3 location for Shopee CSV |
| `MIN_DISCOUNT` | Minimum discount % to include (default: 15) |
| `MIN_ITEM_RATING` / `MIN_SHOP_RATING` | Rating thresholds for Shopee |
| `MIN_PRICE` / `MAX_PRICE` | Price range filter |

## Deployment

The site is hosted on GitHub Pages. Pushing to `main` triggers deployment. `run.sh` automates the full cycle. The `CNAME` file sets the custom domain. `ads.txt` contains the Google AdSense publisher ID.
