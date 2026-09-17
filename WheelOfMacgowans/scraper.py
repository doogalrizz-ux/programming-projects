"""
Wheel of Macgowans — Puzzle Scraper
Scrapes all 43 seasons from buyavowel.boards.net and saves to puzzles.csv.
Run this once (or any time you want to refresh the puzzle database):
    python scraper.py
"""

import csv
import re
import time
import sys
import subprocess

from bs4 import BeautifulSoup

BASE_URL = "https://buyavowel.boards.net/page/compendium{}"
OUTPUT_FILE = "puzzles.csv"
TOTAL_SEASONS = 43

# Only keep regular rounds (R1-R5). Skip toss-ups (T), bonus rounds (BR), etc.
REGULAR_ROUND_PATTERN = re.compile(r'^R\d+\*?$', re.IGNORECASE)


def fetch_page(season: int) -> str | None:
    url = BASE_URL.format(season)
    try:
        result = subprocess.run(
            [
                "curl", "-s", "--max-time", "20",
                "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "-H", "Accept-Language: en-US,en;q=0.9",
                "-L",   # follow redirects
                url,
            ],
            capture_output=True,
            text=True,
            timeout=25,
        )
        if result.returncode != 0 or not result.stdout.strip():
            print(f"  WARNING: curl failed for season {season}")
            return None
        return result.stdout
    except Exception as e:
        print(f"  WARNING: Could not fetch season {season}: {e}")
        return None


def parse_puzzles(html: str, season: int) -> list[dict]:
    """
    Parse puzzles from a season page.
    The site uses a ProBoards forum — content is inside the post body.
    Tries HTML table parsing first, then falls back to text/regex parsing.
    """
    soup = BeautifulSoup(html, "html.parser")
    puzzles = []

    # --- Attempt 1: standard <table> with <tr>/<td> ---
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) >= 5:
                result = _extract_from_cells(
                    [c.get_text(strip=True) for c in cells]
                )
                if result:
                    puzzles.append(result)

    if puzzles:
        return puzzles

    # --- Attempt 2: look for the post body text and parse line by line ---
    # ProBoards stores post content in divs with class "post-body" or "entry-content"
    content_divs = soup.find_all(
        "div",
        class_=re.compile(r"(post.?body|entry.?content|content|body)", re.I)
    )

    raw_text = ""
    for div in content_divs:
        raw_text += div.get_text("\n") + "\n"

    if not raw_text.strip():
        # Last resort: grab all visible text
        raw_text = soup.get_text("\n")

    puzzles = _parse_text_table(raw_text)
    return puzzles


def _extract_from_cells(cells: list[str]) -> dict | None:
    """Extract a puzzle record from a list of cell strings."""
    if len(cells) < 5:
        return None
    puzzle, category, date, ep, round_code = (
        cells[0], cells[1], cells[2], cells[3], cells[4]
    )
    # Skip header rows
    if puzzle.upper() in ("PUZZLE", ""):
        return None
    # Only regular rounds
    round_code = round_code.strip().rstrip("*").upper()
    if not re.match(r'^R\d+$', round_code):
        return None
    puzzle = puzzle.strip().upper()
    category = category.strip().title()
    date = date.strip()
    if not puzzle or not category:
        return None
    return {"puzzle": puzzle, "category": category, "date": date}


def _parse_text_table(text: str) -> list[dict]:
    """
    Parse puzzles from plain text / pipe-delimited / whitespace-aligned format.
    Handles formats like:
      PUZZLE TEXT         Category         9/19/83   #1   R1
      PUZZLE TEXT | Category | 9/19/83 | #1 | R1
    """
    puzzles = []
    lines = text.splitlines()

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Try pipe-delimited first
        if "|" in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 5:
                result = _extract_from_cells(parts)
                if result:
                    puzzles.append(result)
            continue

        # Try whitespace / tab-delimited
        # Pattern: everything up to the date (M/D/YY or M/D/YYYY)
        # Date pattern: 1 or 2 digit month/day, 2 or 4 digit year
        date_match = re.search(
            r'\b(\d{1,2}/\d{1,2}/\d{2,4})\b', line
        )
        if not date_match:
            continue

        date_str = date_match.group(1)
        before_date = line[:date_match.start()].strip()
        after_date = line[date_match.end():].strip()

        # Round code is typically at the end after ep# like "#1234  R2"
        round_match = re.search(r'\bR(\d+)\*?$', after_date, re.IGNORECASE)
        if not round_match:
            continue

        round_code = f"R{round_match.group(1)}"

        # Split before_date into puzzle and category
        # Usually the last "word group" before date is category
        # Heuristic: category is typically 1-4 words, all title-case
        # Try splitting on 2+ spaces
        parts = re.split(r'\s{2,}', before_date)
        if len(parts) >= 2:
            puzzle = parts[0].strip().upper()
            category = parts[-1].strip().title()
        else:
            # Can't reliably split — skip
            continue

        if not puzzle or not category:
            continue

        puzzles.append({
            "puzzle": puzzle,
            "category": category,
            "date": date_str,
        })

    return puzzles


def scrape_all_seasons() -> list[dict]:
    all_puzzles = []
    seen = set()  # deduplicate by (puzzle, category)

    for season in range(1, TOTAL_SEASONS + 1):
        print(f"  Scraping season {season:2d}/{TOTAL_SEASONS}...", end=" ", flush=True)
        html = fetch_page(season)
        if html is None:
            print("SKIPPED")
            continue

        puzzles = parse_puzzles(html, season)

        added = 0
        for p in puzzles:
            key = (p["puzzle"], p["category"])
            if key not in seen:
                seen.add(key)
                all_puzzles.append(p)
                added += 1

        print(f"{added} puzzles")

        # Be polite — small delay between requests
        time.sleep(0.5)

    return all_puzzles


def save_csv(puzzles: list[dict], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["puzzle", "category", "date"])
        writer.writeheader()
        writer.writerows(puzzles)


def main():
    print("=" * 60)
    print("  Wheel of Macgowans — Puzzle Scraper")
    print("=" * 60)
    print(f"\nScraping {TOTAL_SEASONS} seasons from buyavowel.boards.net...")
    print("(This may take 2-5 minutes. Please wait.)\n")

    puzzles = scrape_all_seasons()

    if not puzzles:
        print("\nERROR: No puzzles were found. Check your internet connection")
        print("and try again. If the problem persists, the site structure")
        print("may have changed.")
        sys.exit(1)

    save_csv(puzzles, OUTPUT_FILE)

    print(f"\n{'=' * 60}")
    print(f"  Done! {len(puzzles):,} puzzles saved to {OUTPUT_FILE}")
    print(f"{'=' * 60}")
    print("\nYou can now run:  python server.py")


if __name__ == "__main__":
    main()
