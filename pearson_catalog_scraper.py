#!/usr/bin/env python3
"""
pearson_catalog_scraper.py
~~~~~~~~~~~~~~~~~~~~~~~~~~
Scrapes the Pearson public book catalog by subject, in batches of 3 subjects.
Outputs results to pearson_catalog.csv.

Setup (one-time):
    pip install playwright
    playwright install chromium

Usage:
    python pearson_catalog_scraper.py

The script opens a real Chromium browser window (visible, not headless) which
helps bypass Cloudflare bot protection. It processes subjects 3 at a time and
pauses between batches waiting for you to press ENTER — giving you control over
pacing and letting you resume if anything goes wrong.

Results are appended to pearson_catalog.csv as each batch completes, so
partial runs are never lost.
"""

import asyncio
import csv
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

from playwright.async_api import async_playwright, Browser, Page

# ── Configuration ──────────────────────────────────────────────────────────────

SUBJECTS = [
    "Arts",
    "Business & Economics",
    "Careers & Trades",
    "Communication",
    "Computer Science",
    "Engineering",
    "English",
    "Health Professions",
    "Helping Professions",
    "Humanities",
    "Information Technology",
    "Mathematics",
    "Nursing",
    "Personal & Professional Development",
    "Science",
    "Social Sciences",
    "Statistics",
    "Teacher Education",
    "World Languages",
]

# (URL param value, human label, classification column value)
LEARNING_STAGES = [
    ("For School",   "For School",   "K12"),
    ("For College",  "For College",  "Higher Ed"),
    ("For Work",     "For Work",     "Professional"),
]

BATCH_SIZE       = 3
RESULTS_PER_PAGE = 72
OUTPUT_FILE      = "pearson_catalog.csv"
BASE_SEARCH_URL  = "https://www.pearson.com/en-us/search.html"

# Polite delays (seconds) — keeps traffic indistinguishable from normal browsing
DELAY_BETWEEN_PAGES    = 2.0
DELAY_BETWEEN_SUBJECTS = 1.5

CSV_HEADERS = [
    "title",
    "edition",
    "authors",
    "cover_image_url",
    "subject",
    "learning_stage",
    "classification",    # K12 | Higher Ed | Professional
    "is_international",  # True if title/authors suggest a Global/International edition
    "product_url",
]

# ── URL helpers ────────────────────────────────────────────────────────────────

def search_url(subject: str, stage: str, start: int = 0) -> str:
    params = {
        "q":              "",
        "discipline":     subject,
        "learning-stage": stage,
        "count":          RESULTS_PER_PAGE,
        "start":          start,
    }
    return f"{BASE_SEARCH_URL}?{urlencode(params)}"


INTL_KEYWORDS = ("global edition", "international edition", "global ed.", "intl. ed")

def flag_international(title: str, authors: str) -> bool:
    combined = (title + " " + authors).lower()
    return any(kw in combined for kw in INTL_KEYWORDS)

# ── Data extraction ────────────────────────────────────────────────────────────

async def extract_page(page: Page, subject: str, stage_label: str, classification: str):
    """
    Return (books: list[dict], total_results: int).
    Tries __NEXT_DATA__ JSON first; falls back to DOM scraping.
    """
    books = []
    total = 0

    # ── Strategy 1: __NEXT_DATA__ (Next.js SSR) ───────────────────────────────
    next_data_text = await page.evaluate("""
        () => {
            const el = document.getElementById('__NEXT_DATA__');
            return el ? el.textContent : null;
        }
    """)

    if next_data_text:
        try:
            data      = json.loads(next_data_text)
            page_props = data.get("props", {}).get("pageProps", {})

            # Pearson's search page may nest results under several possible keys
            search_obj = (
                page_props.get("searchResults") or
                page_props.get("searchData") or
                page_props.get("data") or
                {}
            )
            results = (
                search_obj.get("results") or
                search_obj.get("products") or
                search_obj.get("items") or
                page_props.get("results") or
                []
            )
            total = int(
                search_obj.get("totalResults") or
                search_obj.get("total") or
                page_props.get("totalResults") or
                0
            )

            for item in results:
                title = str(item.get("title") or "").strip()
                if not title:
                    continue

                edition = str(item.get("edition") or item.get("editionNumber") or "").strip()

                raw_authors = item.get("authors") or item.get("author") or []
                if isinstance(raw_authors, list):
                    authors_str = "; ".join(
                        str(a.get("name") or a.get("fullName") or a).strip()
                        for a in raw_authors
                    )
                else:
                    authors_str = str(raw_authors).strip()

                cover = str(
                    item.get("coverImageUrl") or
                    item.get("imageUrl") or
                    item.get("thumbnail") or
                    ""
                )

                href = str(item.get("url") or item.get("productUrl") or "")
                product_url = (
                    "https://www.pearson.com" + href
                    if href and href.startswith("/")
                    else href
                )

                books.append({
                    "title":           title,
                    "edition":         edition,
                    "authors":         authors_str,
                    "cover_image_url": cover,
                    "subject":         subject,
                    "learning_stage":  stage_label,
                    "classification":  classification,
                    "is_international": flag_international(title, authors_str),
                    "product_url":     product_url,
                })

            if books:
                return books, total

        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass  # fall through to DOM scraping

    # ── Strategy 2: DOM scraping ───────────────────────────────────────────────
    # Wait for at least one product card to appear (up to 12 s)
    try:
        await page.wait_for_selector(
            '[data-testid="product-card"], .product-card, .search-result-item, article',
            timeout=12000,
        )
    except Exception:
        # No cards found — page may be empty or structure is different
        return books, total

    cards = await page.query_selector_all(
        '[data-testid="product-card"], .product-card, .search-result-item, article'
    )

    for card in cards:
        try:
            title_el = await card.query_selector(
                'h2, h3, [data-testid="product-title"], .product-title'
            )
            title = (await title_el.inner_text()).strip() if title_el else ""
            if not title:
                continue

            edition_el = await card.query_selector(
                '[data-testid="edition"], .edition, .product-edition, [class*="edition"]'
            )
            edition = (await edition_el.inner_text()).strip() if edition_el else ""

            author_els = await card.query_selector_all(
                '[data-testid="author"], .author, .product-author, [class*="author"]'
            )
            authors_str = "; ".join(
                (await a.inner_text()).strip() for a in author_els
            )

            img_el  = await card.query_selector("img")
            cover   = (await img_el.get_attribute("src") or "") if img_el else ""

            link_el = await card.query_selector("a[href]")
            href    = (await link_el.get_attribute("href") or "") if link_el else ""
            product_url = (
                "https://www.pearson.com" + href
                if href.startswith("/")
                else href
            )

            books.append({
                "title":           title,
                "edition":         edition,
                "authors":         authors_str,
                "cover_image_url": cover,
                "subject":         subject,
                "learning_stage":  stage_label,
                "classification":  classification,
                "is_international": flag_international(title, authors_str),
                "product_url":     product_url,
            })
        except Exception:
            continue

    # Try to read total result count from DOM
    if not total:
        try:
            count_el = await page.query_selector(
                '[data-testid="result-count"], .result-count, [class*="results-count"], [class*="total-results"]'
            )
            if count_el:
                text = await count_el.inner_text()
                nums = re.findall(r"[\d,]+", text)
                if nums:
                    total = int(nums[-1].replace(",", ""))
        except Exception:
            pass

    return books, total


# ── Per-subject scraping ───────────────────────────────────────────────────────

async def scrape_subject_stage(
    page: Page, subject: str, stage_param: str, stage_label: str, classification: str
) -> list[dict]:
    """Paginate through all results for one subject + learning stage."""
    all_books: list[dict] = []
    start  = 0
    total  = None

    print(f"    [{classification}] {stage_label} ...", end="", flush=True)

    while True:
        url = search_url(subject, stage_param, start)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(DELAY_BETWEEN_PAGES)
        except Exception as e:
            print(f"\n      ERROR loading {url}: {e}")
            break

        books, page_total = await extract_page(page, subject, stage_label, classification)

        if total is None:
            total = page_total

        if not books:
            break

        all_books.extend(books)

        next_start = start + RESULTS_PER_PAGE
        if total and next_start >= total:
            break
        if len(books) < RESULTS_PER_PAGE:
            # Fewer results than a full page means we're done
            break

        start = next_start
        await asyncio.sleep(DELAY_BETWEEN_PAGES)

    count_str = f"{len(all_books)}" + (f"/{total}" if total else "")
    print(f" {count_str} books")
    return all_books


async def scrape_subject(browser: Browser, subject: str) -> list[dict]:
    """Open a fresh browser context per subject and scrape all learning stages."""
    all_books: list[dict] = []

    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
        locale="en-US",
    )
    page = await context.new_page()

    for stage_param, stage_label, classification in LEARNING_STAGES:
        books = await scrape_subject_stage(
            page, subject, stage_param, stage_label, classification
        )
        all_books.extend(books)
        await asyncio.sleep(DELAY_BETWEEN_SUBJECTS)

    await context.close()
    return all_books


# ── CSV helpers ────────────────────────────────────────────────────────────────

def save_books(books: list[dict], filepath: str):
    """Append records to CSV; write header row only on first write."""
    is_new = not Path(filepath).exists()
    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        writer.writerows(books)


# ── Batch control ──────────────────────────────────────────────────────────────

def pause_between_batches(batch_num: int, total_batches: int):
    """Print a summary and wait for ENTER before continuing."""
    print(f"\n{'─' * 60}")
    print(f"  Batch {batch_num} of {total_batches} complete.")
    print(f"  Results saved → {OUTPUT_FILE}")

    if batch_num < total_batches:
        subjects_left = SUBJECTS[(batch_num * BATCH_SIZE):]
        next_batch    = subjects_left[:BATCH_SIZE]
        print(f"\n  Next batch: {', '.join(next_batch)}")
        print(f"\n  Press ENTER to continue, or Ctrl+C to stop here.")
        print(f"{'─' * 60}\n")
        try:
            input()
        except (KeyboardInterrupt, EOFError):
            print("\nStopped. Partial results are saved.")
            sys.exit(0)
    else:
        print(f"\n  All {total_batches} batches complete!")
        print(f"{'─' * 60}\n")


# ── Entry point ────────────────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("  Pearson Catalog Scraper")
    print(f"  Subjects : {len(SUBJECTS)}")
    print(f"  Batch size: {BATCH_SIZE}  |  Batches: {-(-len(SUBJECTS) // BATCH_SIZE)}")
    print(f"  Output   : {OUTPUT_FILE}")
    print("=" * 60)
    print()

    batches       = [SUBJECTS[i : i + BATCH_SIZE] for i in range(0, len(SUBJECTS), BATCH_SIZE)]
    total_batches = len(batches)

    async with async_playwright() as pw:
        # headless=False — real visible browser bypasses Cloudflare bot detection
        browser = await pw.chromium.launch(headless=False)

        for batch_num, batch in enumerate(batches, start=1):
            print(f"BATCH {batch_num}/{total_batches}: {', '.join(batch)}")
            print("-" * 60)

            for subject in batch:
                print(f"  Subject: {subject}")
                books = await scrape_subject(browser, subject)
                if books:
                    save_books(books, OUTPUT_FILE)
                    print(f"    → {len(books)} total books saved for '{subject}'")
                else:
                    print(f"    → No books found for '{subject}'")

            pause_between_batches(batch_num, total_batches)

        await browser.close()

    print(f"Done.  Full catalog → {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
