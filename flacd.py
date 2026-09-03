"""
FlacDownloader Artist Music Downloader
========================================
Uses Playwright to automate flacdownloader.com:
  1. Searches for the artist by name
  2. Crawls and collects all track metadata
  3. Automatically downloads each track in lossless FLAC format
  4. Saves files to local storage under ./flac_downloads/<Artist>/

Usage:
    python flac_downloader.py "Artist Name" [--output-dir ./flac_downloads] [--max-pages 5] [--headless]

Examples:
    python flac_downloader.py "Adele"
    python flac_downloader.py "The Beatles" --max-pages 3
"""

import argparse
import asyncio
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus

# Force UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from playwright.async_api import async_playwright, Page, BrowserContext


BASE_URL = "https://flacdownloader.com"
DEFAULT_OUTPUT_DIR = Path("./flac_downloads")
DEFAULT_MAX_PAGES = 5
DOWNLOAD_TIMEOUT_MS = 180_000  # 3 minutes per track for large FLAC files


def sanitize_filename(name: str) -> str:
    """Remove characters that are illegal in Windows/Linux filenames."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    name = name.strip('. ')
    return name or "unknown"


def print_status(msg: str, level: str = "info"):
    symbols = {"info": "[i]", "ok": "[+]", "warn": "[!]", "error": "[x]", "dl": "[>]"}
    sym = symbols.get(level, "[-]")
    print(f"     {sym} {msg}")


class FlacArtistDownloader:
    def __init__(self, artist: str, output_dir: Path, max_pages: int, headless: bool):
        self.artist = artist.strip()
        self.output_dir = output_dir
        self.max_pages = max_pages
        self.headless = headless
        self.downloaded = 0
        self.failed = 0
        self.skipped = 0

    async def run(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        profile_dir = Path("./browser_profile")
        profile_dir.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as p:
            print_status("Launching browser automation...")
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir.resolve()),
                headless=self.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--host-resolver-rules=MAP *.challenges.cloudflare.com 104.18.94.41, MAP challenges.cloudflare.com 104.18.94.41",
                ],
                viewport={"width": 1280, "height": 800},
                accept_downloads=True,
            )

            page = context.pages[0] if context.pages else await context.new_page()

            try:
                # 1. Search and collect tracks
                tracks = await self._search_artist_tracks(page)
                if not tracks:
                    print_status(f'No tracks found for artist "{self.artist}".', "warn")
                    return

                print(f"\n  Found {len(tracks)} track(s) by {self.artist}. Starting downloads...\n")

                # 2. Iterate and download each track
                for i, track in enumerate(tracks, 1):
                    title = track.get("title", "Unknown Title")
                    artist_name = track.get("artist", self.artist)
                    if isinstance(artist_name, dict):
                        artist_name = artist_name.get("name", self.artist)

                    clean_name = sanitize_filename(f"{artist_name} - {title}")
                    expected_file = self.output_dir / f"{clean_name}.flac"

                    print(f"  [{i}/{len(tracks)}] {artist_name} - {title}")

                    # Check if already downloaded
                    if expected_file.exists() and expected_file.stat().st_size > 1024 * 1024:
                        size_mb = expected_file.stat().st_size / (1024 * 1024)
                        print_status(f"Already downloaded ({size_mb:.2f} MB), skipping.", "ok")
                        self.skipped += 1
                        continue

                    # Download track in FLAC
                    success = await self._download_flac_track(page, track, clean_name)
                    if success:
                        self.downloaded += 1
                    else:
                        self.failed += 1

                    # Short delay between songs
                    await asyncio.sleep(2)

            finally:
                await context.close()

        print("\n  ========================================")
        print(f"  [+] Downloaded : {self.downloaded}")
        print(f"  [!] Skipped    : {self.skipped}")
        print(f"  [x] Failed     : {self.failed}")
        print("  ========================================")
        print(f"  Files saved in: {self.output_dir.resolve()}\n")

    async def _search_artist_tracks(self, page: Page) -> list:
        """Search all available pages for artist's tracks."""
        print_status(f'Searching for "{self.artist}" on flacdownloader.com...')
        await page.goto(f"{BASE_URL}/en", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        all_tracks = []
        seen_ids = set()
        artist_lower = self.artist.lower()

        for page_idx in range(self.max_pages):
            res = await page.evaluate("""
                async ([query, index]) => {
                    try {
                        const r = await fetch(`/search?q=${encodeURIComponent(query)}&index=${index}`);
                        if (!r.ok) return null;
                        return await r.json();
                    } catch(e) {
                        return null;
                    }
                }
            """, [self.artist, page_idx])

            if not res or not isinstance(res, dict):
                break

            tracks = res.get("tracks", res.get("data", []))
            if not tracks:
                break

            new_count = 0
            for t in tracks:
                tid = t.get("id")
                if tid and tid in seen_ids:
                    continue

                t_artist = t.get("artist", "")
                if isinstance(t_artist, dict):
                    t_artist = t_artist.get("name", "")

                # Keep track if the artist name matches
                if artist_lower in str(t_artist).lower() or not t_artist:
                    if tid:
                        seen_ids.add(tid)
                    all_tracks.append(t)
                    new_count += 1

            print_status(f"Page {page_idx + 1}: Found {new_count} tracks (Total: {len(all_tracks)})")

            if not res.get("has_more") or len(tracks) < 5:
                break

            await asyncio.sleep(1)

        return all_tracks

    async def _download_flac_track(self, page: Page, track: dict, clean_name: str) -> bool:
        """Downloads a single track via the UI FLAC flow."""
        try:
            # 1. Set track state in localStorage
            await page.evaluate("""
                (t) => {
                    localStorage.setItem("dl_track", JSON.stringify({
                        track: t,
                        source: "deezer",
                        lang: "en"
                    }));
                }
            """, track)

            # 2. Navigate to download page
            await page.goto(f"{BASE_URL}/en/download", wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)

            # 3. Locate FLAC button
            flac_btn = page.locator("button:has-text('FLAC')").first
            if not await flac_btn.is_visible(timeout=6000):
                print_status("FLAC button not available for this track.", "warn")
                return False

            print_status("Requesting FLAC audio from server...", "dl")

            # 4. Trigger download and await file delivery
            async with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
                await flac_btn.click()

            download = await dl_info.value
            suggested = download.suggested_filename
            ext = Path(suggested).suffix if suggested else ".flac"
            if not ext.lower().endswith("flac"):
                ext = ".flac"

            save_file = self.output_dir / f"{clean_name}{ext}"
            await download.save_as(str(save_file))

            size_mb = save_file.stat().st_size / (1024 * 1024)
            print_status(f"Saved: {save_file.name} ({size_mb:.2f} MB)", "ok")
            return True

        except asyncio.TimeoutError:
            print_status("Download timed out waiting for server processing.", "error")
            return False
        except Exception as e:
            print_status(f"Error during download: {e}", "error")
            return False


def main():
    parser = argparse.ArgumentParser(
        description="Automated artist music downloader in FLAC format from flacdownloader.com"
    )
    parser.add_argument("artist", help="Name of the artist to search and download")
    parser.add_argument(
        "--output-dir", "-o",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"Directory to save downloads (default: {DEFAULT_OUTPUT_DIR})"
    )
    parser.add_argument(
        "--max-pages", "-p",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help=f"Max search pages to crawl (default: {DEFAULT_MAX_PAGES})"
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode (default: False for optimal bot challenge passing)"
    )

    args = parser.parse_args()

    artist_clean = sanitize_filename(args.artist)
    artist_out_dir = Path(args.output_dir) / artist_clean

    print("\n  +------------------------------------------")
    print("  |  FLAC Downloader - Artist Automation")
    print("  +------------------------------------------")
    print(f"  |  Artist     : {args.artist}")
    print(f"  |  Format     : Lossless FLAC")
    print(f"  |  Output Dir : {artist_out_dir.resolve()}")
    print(f"  |  Max Pages  : {args.max_pages}")
    print("  +------------------------------------------\n")

    downloader = FlacArtistDownloader(
        artist=args.artist,
        output_dir=artist_out_dir,
        max_pages=args.max_pages,
        headless=args.headless,
    )

    asyncio.run(downloader.run())


if __name__ == "__main__":
    main()
