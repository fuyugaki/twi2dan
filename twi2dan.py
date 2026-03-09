#!/usr/bin/env python3
"""
twi2dan.py — Download media from a Twitter/X post, sorted into a Danbooru artist folder.

Lookup chain for artist name:
  1. IQDB reverse image search → Danbooru post → artist tag  (most reliable)
  2. Danbooru artist URL search by Twitter handle              (fallback)
  3. Twitter handle as folder name                             (last resort)

Usage:
    python twi2dan.py                    # interactive mode
    python twi2dan.py <url> [<url> ...]  # one or more URLs
    python twi2dan.py <url> [<url> ...] --output-dir PATH

Requirements:
    pip install requests gallery-dl beautifulsoup4 rich

Twitter auth (run once):
    gallery-dl --cookies-from-browser chrome

Danbooru auth (optional, avoids rate limits):
    set DANBOORU_USER=yourname
    set DANBOORU_API_KEY=yourkey
"""

import os
import re
import json
import shutil
import argparse
import requests
import subprocess
import tempfile
import time
from pathlib import Path
from bs4 import BeautifulSoup

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich import box
from rich.rule import Rule
from datetime import datetime

console = Console()


def timestamp() -> str:
    """Return a dim-styled current time string for log output."""
    return f"[dim]{datetime.now().strftime('%H:%M:%S')}[/]"

# ── Config ────────────────────────────────────────────────────────────────────

SCRIPT_DIR    = Path(__file__).resolve().parent
CONFIG_FILE   = SCRIPT_DIR / ".twi2dan.json"
DANBOORU_BASE = "https://danbooru.donmai.us"
IQDB_URL      = "https://iqdb.org/"
IQDB_MAX_SIZE = 8 * 1024 * 1024  # 8 MB — IQDB's hard limit

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}


class AppError(Exception):
    """A known, user-facing error — shown cleanly without a traceback."""

class RetryableError(Exception):
    """A transient error that should trigger a retry."""


def with_retry(fn, *, attempts: int = 3, initial_delay: float = 2.0, label: str = "", progress=None, task=None):
    """
    Call fn() up to `attempts` times, with exponential backoff on RetryableError.
    All other exceptions propagate immediately.
    """
    delay = initial_delay
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except RetryableError as e:
            if attempt == attempts:
                raise AppError(f"{label}: failed after {attempts} attempts — {e}")
            if progress and task is not None:
                progress.update(task, description=f"[yellow]⚠ {label}:[/] {e} — retrying in {delay:.0f}s ({attempt}/{attempts - 1})…")
            time.sleep(delay)
            delay *= 2
    raise AppError(f"{label}: failed after {attempts} attempts")


# ── Credentials ───────────────────────────────────────────────────────────────

def load_config() -> dict:
    saved = {}
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            console.print(f"  [yellow]⚠ Couldn’t read {CONFIG_FILE}, ignoring.[/]")
    return {
        "danbooru_user":    os.environ.get("DANBOORU_USER")    or saved.get("danbooru_user", ""),
        "danbooru_api_key": os.environ.get("DANBOORU_API_KEY") or saved.get("danbooru_api_key", ""),
    }


def save_config(user: str, api_key: str):
    try:
        CONFIG_FILE.write_text(
            json.dumps({"danbooru_user": user, "danbooru_api_key": api_key}, indent=2),
            encoding="utf-8",
        )
        try:
            CONFIG_FILE.chmod(0o600)
        except NotImplementedError:
            pass  # Windows — silently skip
    except OSError as e:
        console.print(f"  [yellow]⚠ Couldn’t save config:[/] {e}")


cfg = load_config()
DANBOORU_USER    = cfg["danbooru_user"]
DANBOORU_API_KEY = cfg["danbooru_api_key"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_twitter_username(tweet_url: str) -> str | None:
    m = re.search(r"(?:twitter\.com|x\.com)/([^/?\s]+)/status/", tweet_url)
    return m.group(1) if m else None


def danbooru_session() -> requests.Session:
    s = requests.Session()
    if DANBOORU_USER and DANBOORU_API_KEY:
        s.auth = (DANBOORU_USER, DANBOORU_API_KEY)
    s.headers["User-Agent"] = "twi2dan/1.0"
    return s


def danbooru_get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    """GET with Danbooru-specific HTTP error handling."""
    try:
        resp = session.get(url, timeout=10, **kwargs)
    except requests.ConnectionError:
        raise RetryableError("no network connection")
    except requests.Timeout:
        raise RetryableError("request timed out")

    if resp.status_code == 401:
        raise AppError("Danbooru auth failed — check your username and API key.")
    if resp.status_code == 403:
        raise AppError("Danbooru returned 403 Forbidden — account may be restricted.")
    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After", "")
        raise RetryableError(f"rate limit hit{f' (retry after {retry_after}s)' if retry_after else ''}")
    if resp.status_code == 503:
        raise RetryableError("service unavailable (503)")
    resp.raise_for_status()
    return resp


# ── IQDB lookup ───────────────────────────────────────────────────────────────

def iqdb_search(image_path: Path, progress, task) -> str | None:
    size = image_path.stat().st_size
    if size > IQDB_MAX_SIZE:
        progress.update(task, description=f"[yellow]⚠ IQDB:[/] {image_path.name} too large ({size // 1024 // 1024} MB > 8 MB), skipping")
        return None

    progress.update(task, description=f"[blue]IQDB:[/] searching {image_path.name}…")

    def _iqdb_post():
        with open(image_path, "rb") as fh:
            r = requests.post(
                IQDB_URL,
                files={"file": (image_path.name, fh)},
                data={
                    "MAX_FILE_SIZE": str(IQDB_MAX_SIZE),
                    "service[]": "1",   # Danbooru only — faster lookup
                },
                timeout=(10, 90),
            )
        if r.status_code == 429:
            raise RetryableError("rate limit hit")
        if r.status_code == 503:
            raise RetryableError("service unavailable")
        if not r.ok:
            raise RetryableError(f"HTTP {r.status_code}")
        return r

    try:
        resp = with_retry(_iqdb_post, attempts=3, initial_delay=3.0, label="IQDB", progress=progress, task=task)
    except AppError as e:
        progress.update(task, description=f"[yellow]{e}[/]")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Check for IQDB error
    err = soup.select_one(".err")
    if err:
        progress.update(task, description=f"[yellow]⚠ IQDB:[/] {err.get_text(strip=True)}")
        return None

    # Parse result tables inside #pages — each child div is a match.
    # Best/additional matches appear first; we take the first Danbooru link.
    pages = soup.select_one("#pages")
    if pages:
        for div in pages.find_all("div", recursive=False):
            th = div.select_one("th")
            if not th:
                continue
            heading = th.get_text(strip=True).lower()
            if heading not in ("best match", "additional match"):
                continue
            link = div.select_one("td a[href]")
            if not link:
                continue
            href = link["href"]
            if href.startswith("//"):
                href = "https:" + href
            if "danbooru.donmai.us/posts/" in href:
                # Extract similarity from the last row
                sim_td = div.select("td")[-1] if div.select("td") else None
                sim = ""
                if sim_td:
                    sim_match = re.search(r"(\d+)% similarity", sim_td.get_text())
                    if sim_match:
                        sim = f" ({sim_match.group(1)}%)"
                progress.update(task, description=f"[green]✓ IQDB match found[/]{sim}")
                return href

    progress.update(task, description="[yellow]⚠ No Danbooru match on IQDB[/]")
    return None


def artist_from_danbooru_post(post_url: str, session: requests.Session, progress, task) -> str | None:
    m = re.search(r"/posts/(\d+)", post_url)
    if not m:
        return None
    post_id = m.group(1)

    progress.update(task, description=f"[blue]Danbooru:[/] fetching post {post_id}…")
    try:
        resp = with_retry(
            lambda: danbooru_get(session, f"{DANBOORU_BASE}/posts/{post_id}.json"),
            attempts=3, initial_delay=2.0, label="Danbooru post", progress=progress, task=task,
        )
    except AppError as e:
        progress.update(task, description=f"[yellow]⚠ Danbooru post:[/] {e}")
        return None

    try:
        post = resp.json()
    except ValueError:
        progress.update(task, description="[yellow]⚠ Danbooru:[/] invalid JSON response")
        return None

    tag_string_artist = post.get("tag_string_artist", "").strip()
    if not tag_string_artist:
        progress.update(task, description="[yellow]⚠ Danbooru:[/] post has no artist tag")
        return None

    return tag_string_artist.split()[0]


# ── Danbooru URL fallback ─────────────────────────────────────────────────────

def find_danbooru_artist_by_twitter(twitter_username: str, session: requests.Session, progress, task) -> str | None:
    for domain in ("twitter.com", "x.com"):
        url_to_match = f"https://{domain}/{twitter_username}"
        progress.update(task, description=f"[blue]Danbooru:[/] searching artist by Twitter URL…")
        try:
            resp = with_retry(
                lambda u=url_to_match: danbooru_get(session, f"{DANBOORU_BASE}/artists.json",
                                     params={"search[url_matches]": u}),
                attempts=3, initial_delay=2.0, label="Danbooru artist", progress=progress, task=task,
            )
        except AppError as e:
            progress.update(task, description=f"[yellow]⚠ Danbooru artist search:[/] {e}")
            return None

        try:
            artists = resp.json()
        except ValueError:
            continue

        if artists:
            for artist in artists:
                if not artist.get("is_banned") and not artist.get("is_deleted"):
                    return artist["name"]
            return artists[0]["name"]

    return None


# ── gallery-dl ────────────────────────────────────────────────────────────────

def check_gallery_dl():
    """Raise a clear error if gallery-dl isn't installed."""
    try:
        subprocess.run(
            ["gallery-dl", "--version"],
            capture_output=True, check=True,
        )
    except FileNotFoundError:
        raise AppError(
            "gallery-dl not found.\n"
            "  Install it with:  pip install gallery-dl\n"
            "  Then auth with:   gallery-dl --cookies-from-browser chrome"
        )
    except subprocess.CalledProcessError:
        pass  # --version exits non-zero on some builds, that's fine


def parse_gallery_dl_error(stderr: str, returncode: int) -> str:
    """Turn gallery-dl stderr into something readable."""
    if not stderr:
        return f"gallery-dl exited with code {returncode}"

    # Common patterns
    if "HTTP Error 401" in stderr or "Unauthorized" in stderr:
        return "Twitter/X auth failed — run: gallery-dl --cookies-from-browser chrome"
    if "HTTP Error 403" in stderr or "Forbidden" in stderr:
        return "Twitter/X returned 403 — your cookies may have expired, re-run auth"
    if "HTTP Error 404" in stderr or "Not Found" in stderr:
        return "Tweet not found — it may have been deleted or the URL is wrong"
    if "suspended" in stderr.lower():
        return "Twitter account is suspended"
    if "private" in stderr.lower() or "protected" in stderr.lower():
        return "Tweet is from a private/protected account"
    if "No media" in stderr or "no media" in stderr:
        return "Tweet contains no downloadable media"
    if "Unavailable" in stderr or "unavailable" in stderr:
        return "Tweet is unavailable — it may be age-restricted (ensure NSFW is enabled on your account and cookies are fresh), or deleted"

    # Return first non-empty line of stderr as a fallback
    first_line = next((l.strip() for l in stderr.splitlines() if l.strip()), stderr[:200])
    return first_line


def download_with_gallery_dl(tweet_url: str, dest_dir: Path, progress, task) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    progress.update(task, description="[blue]gallery-dl:[/] downloading media…")

    cmd = [
        "gallery-dl",
        "--dest", str(dest_dir),
        "--filename", "{filename}.{extension}",
        # Use syndication API as fallback for age-restricted tweets
        "-o", "extractor.twitter.syndication=extended",
        # Always grab the highest quality image
        "-o", "extractor.twitter.size=orig",
        tweet_url,
    ]
    TRANSIENT_PATTERNS = ("ConnectionError", "Timeout", "429", "503", "503 Service")

    def _run():
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode not in (0, 1):
            msg = parse_gallery_dl_error(r.stderr, r.returncode)
            # Only retry on transient network errors, not auth/404/etc.
            if any(p in r.stderr for p in TRANSIENT_PATTERNS):
                raise RetryableError(msg)
            raise AppError(msg)
        return r

    result = with_retry(_run, attempts=3, initial_delay=3.0, label="gallery-dl", progress=progress, task=task)

    files = [f for f in dest_dir.rglob("*") if f.is_file()]

    if not files:
        hint = parse_gallery_dl_error(result.stderr, result.returncode)
        raise AppError(f"No files downloaded — {hint}")

    return files


# ── File operations ───────────────────────────────────────────────────────────

def ensure_output_dir(path: Path):
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise AppError(f"Can’t create output directory '{path}': {e}")


def move_files_to_folder(files: list[Path], artist_folder: Path) -> list[Path]:
    ensure_output_dir(artist_folder)
    moved = []
    for src in files:
        dest = artist_folder / src.name
        if dest.exists():
            stem, suffix = src.stem, src.suffix
            i = 1
            while dest.exists():
                dest = artist_folder / f"{stem}_{i}{suffix}"
                i += 1
        try:
            shutil.move(str(src), dest)
            moved.append(dest)
        except OSError as e:
            console.print(f"  [yellow]⚠ couldn’t move {src.name}:[/] {e}")
    return moved


# ── Core pipeline ─────────────────────────────────────────────────────────────

def run(tweet_url: str, output_base: Path):
    twitter_user = parse_twitter_username(tweet_url)
    if not twitter_user:
        raise AppError(f"Can’t parse a Twitter username from: {tweet_url!r}\n  Expected format: https://x.com/username/status/123…")

    ensure_output_dir(output_base)

    console.print(f"  {timestamp()} [blue]@{twitter_user}[/]")
    console.print()

    artist_name      = None
    danbooru_post_url = None
    session = danbooru_session()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Starting…")

        with tempfile.TemporaryDirectory(prefix="twi2dan_") as tmp:
            tmp_path = Path(tmp)

            # Download
            progress.update(task, description="[blue]Downloading media…[/]")
            files = download_with_gallery_dl(tweet_url, tmp_path, progress, task)

            # IQDB reverse search
            image_files = [f for f in files if f.suffix.lower() in IMAGE_EXTENSIONS]

            if image_files:
                for img in image_files:
                    post_url = iqdb_search(img, progress, task)
                    if post_url:
                        danbooru_post_url = post_url
                        break
                    time.sleep(1)
            else:
                progress.update(task, description="[yellow]⚠ No images to reverse-search (video only?)[/]")

            # Danbooru artist lookup
            if danbooru_post_url:
                artist_name = artist_from_danbooru_post(danbooru_post_url, session, progress, task)

            if not artist_name:
                artist_name = find_danbooru_artist_by_twitter(twitter_user, session, progress, task)

            if not artist_name:
                progress.update(task, description="[yellow]⚠ Artist unknown — using Twitter handle[/]")
                artist_name = twitter_user.lower()

            # Move files
            progress.update(task, description=f"[blue]Moving files →[/] [bold]{artist_name}[/]")
            artist_folder = output_base / artist_name
            moved = move_files_to_folder(files, artist_folder)

            if not moved:
                raise AppError("No files were saved — all moves failed. Check output directory permissions.")

            progress.update(task, description=f"[green]✓ Done[/] {timestamp()}")

    # ── Summary ───────────────────────────────────────────────────────────────
    console.print()
    artist_page = f"{DANBOORU_BASE}/posts?tags={artist_name}"
    artist_cell = f"[bold green][link={artist_page}]{artist_name}[/link][/] [blue][link={artist_page}]{artist_page}[/link][/]"

    table = Table(box=box.ROUNDED, show_header=False, padding=(0, 1))
    table.add_column(style="dim")
    table.add_column()
    table.add_row("Artist", artist_cell)
    if danbooru_post_url:
        table.add_row("Post", f"[link={danbooru_post_url}]{danbooru_post_url}[/link]")
    table.add_row("Folder", str(artist_folder))
    table.add_row("Files",  str(len(moved)))
    console.print(table)
    console.print()
    for f in moved:
        console.print(f"  [dim]→[/] [green]{f.name}[/]")



# ── Help ──────────────────────────────────────────────────────────────────────

def print_help():
    console.print()
    console.print(Panel(
        "[bold]twi2dan[/] — download Twitter/X post media into a Danbooru artist folder\n",
        title="[bold]Help[/]",
        border_style="bright_blue",
        padding=(0, 1),
    ))

    console.print("[bold]Usage[/]")
    console.print("  [blue]Interactive mode[/]")
    console.print("    [dim]python twi2dan.py[/]")
    console.print("    Prompts for output folder and URLs one by one.")
    console.print("    Type [bold]/help[/] at the URL prompt to show this message.")
    console.print()
    console.print("  [blue]Command line[/]")
    console.print("    [dim]python twi2dan.py <url> [url ...][/]")
    console.print("    [dim]python twi2dan.py <url> --output-dir PATH[/]")
    console.print("    Pass one or more tweet URLs directly. Output dir defaults to ~/Downloads.")
    console.print()

    console.print("[bold]How it works[/]")
    console.print("  1. Downloads media via [bold]gallery-dl[/]")
    console.print("  2. Submits each image to [bold]IQDB[/] for reverse image search")
    console.print("  3. Looks up the Danbooru post to get the artist tag")
    console.print("  4. Falls back to searching Danbooru by Twitter handle if IQDB fails")
    console.print("  5. Falls back to the Twitter handle as folder name if all else fails")
    console.print("  6. Saves files to [bold]<output_dir>/<artist_name>/[/]")
    console.print()

    console.print("[bold]Credentials[/]")
    console.print("  Danbooru credentials are optional but recommended to avoid rate limits.")
    console.print(f"  Saved to [dim]{CONFIG_FILE}[/] when you choose to store them.")
    console.print()
    console.print("  gallery-dl requires Twitter/X auth to download age-restricted content.")
    console.print()
    console.print("  [blue]Option A — import browser cookies (easiest):[/]")
    console.print("    [dim]gallery-dl --cookies-from-browser chrome[/]")
    console.print("    [dim]gallery-dl --cookies-from-browser firefox[/]")
    console.print("    Run once while logged into X with NSFW enabled in settings.")
    console.print()
    console.print("  [blue]Option B — auth_token cookie (more stable, doesn’t expire as fast):[/]")
    console.print("    1. Open x.com in browser, open DevTools (F12)")
    console.print("    2. Go to Application → Cookies → https://x.com")
    console.print("    3. Copy the value of [bold]auth_token[/]")
    console.print("    4. Add to [dim]%APPDATA%/gallery-dl/config.json[/] (Windows) or [dim]~/.config/gallery-dl/config.json[/]:")
    console.print('    [dim]{ "extractor": { "twitter": { "cookies": { "auth_token": "YOUR_TOKEN" } } } }[/]')
    console.print()
    console.print("  [yellow]⚠[/] Also ensure X account settings have NSFW enabled:")
    console.print("    Settings → Privacy and Safety → Display of sensitive media → enable all")
    console.print()

    console.print("[bold]Libraries[/]")
    libs = [
        ("gallery-dl",      "https://github.com/mikf/gallery-dl",   "Twitter/X media downloader"),
        ("IQDB",            "https://iqdb.org",                      "Reverse image search for anime art"),
        ("Danbooru",        "https://danbooru.donmai.us",            "Anime image board and artist database"),
        ("requests",        "https://docs.python-requests.org",      "HTTP client"),
        ("BeautifulSoup4",  "https://www.crummy.com/software/BeautifulSoup", "HTML parser"),
        ("Rich",            "https://github.com/Textualize/rich",    "Terminal formatting"),
    ]
    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    t.add_column(style="bold cyan",  no_wrap=True)
    t.add_column(style="dim",        no_wrap=True)
    t.add_column(style="dim italic")
    for name, url, desc in libs:
        t.add_row(name, url, desc)
    console.print(t)

    console.print("[bold]Support[/]")
    console.print(f"  Danbooru: [link=https://danbooru.donmai.us/static/terms_of_service]Terms of Service[/link] • [link=https://danbooru.donmai.us/wiki_pages/api]API docs[/link] • [link=https://danbooru.donmai.us/users/new]Create account[/link]")
    console.print(f"  IQDB: [link=https://iqdb.org]iqdb.org[/link] • [link=https://iqdb.org/faq.html]FAQ[/link]")
    console.print(f"  gallery-dl: [link=https://github.com/mikf/gallery-dl/blob/master/docs/configuration.rst]Configuration docs[/link] • [link=https://github.com/mikf/gallery-dl/issues]Issue tracker[/link]")
    console.print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Download Twitter/X media into a Danbooru artist folder.",
        add_help=True,
    )
    parser.add_argument("tweet_urls",   nargs="*", help="One or more tweet URLs (leave blank for interactive mode)")
    parser.add_argument("--output-dir", "-o", default=None, help="Base output directory")
    args = parser.parse_args()

    console.print(Panel.fit(
        "[bold]twi2dan[/]  [dim]Twitter/X → Danbooru artist folder[/]",
        border_style="bright_blue",
    ))
    console.print()

    # Danbooru credentials
    global DANBOORU_USER, DANBOORU_API_KEY
    if not DANBOORU_USER or not DANBOORU_API_KEY:
        if Confirm.ask("[bold]Set Danbooru credentials?[/] [dim](recommended, avoids rate limits)[/]", default=False):
            DANBOORU_USER    = Prompt.ask("  [bold]Danbooru username[/]").strip()
            DANBOORU_API_KEY = Prompt.ask("  [bold]Danbooru API key[/]", password=True).strip()
            if DANBOORU_USER and DANBOORU_API_KEY:
                if Confirm.ask(f"  Save credentials to [dim]{CONFIG_FILE}[/]?", default=True):
                    save_config(DANBOORU_USER, DANBOORU_API_KEY)
                    console.print(f"  [dim]Saved → {CONFIG_FILE}[/]")
            console.print()
    else:
        source = "env" if os.environ.get("DANBOORU_USER") else CONFIG_FILE.name
        console.print(f"  [blue]Danbooru:[/] @{DANBOORU_USER} [dim](loaded from {source})[/]")
        console.print()

    # Output dir (ask once, before any URLs)
    output_dir = args.output_dir
    if not output_dir:
        default = str(Path.home() / "Downloads")
        while True:
            raw = Prompt.ask("[bold]Output directory[/]", default=default).strip().strip('"\'') or default
            if re.match(r"https?://", raw):
                console.print(f"  [yellow]⚠[/]  That looks like a URL, not a directory. Using default: [dim]{default}[/]")
                console.print(f"  [dim]Tip: set the output folder first, then paste URLs at the URL prompt.[/]")
                output_dir = default
                # Rescue all URLs accidentally pasted into this field
                if not args.tweet_urls:
                    rescued = list(dict.fromkeys(t for t in raw.split() if re.match(r"https?://", t)))
                    args.tweet_urls = rescued
                    for u in rescued:
                        console.print(f"  [blue]↪[/] [dim]Saved for processing:[/] {u}")
                break
            output_dir = raw
            break

    output_base = Path(output_dir).resolve()

    # URLs — from CLI args or interactive prompt loop
    seen = set()
    urls = []
    for u in args.tweet_urls:
        if u not in seen:
            seen.add(u)
            urls.append(u)
    if not urls:
        console.print("[dim]Paste tweet URLs (space-separated or one per line). Empty line when done. Type /help for help.[/]")
        while True:
            line = Prompt.ask("[bold]Tweet URL[/] [dim](empty when done)[/]", default="").strip()
            if not line:
                break
            if line.lower() == "/help":
                print_help()
                continue
            for u in line.split():
                if not re.match(r"https?://", u):
                    console.print(f"  [yellow]⚠ Skipping non-URL:[/] {u}")
                    continue
                if u not in seen:
                    seen.add(u)
                    urls.append(u)

    if not urls:
        console.print("[red]✗ No URLs provided.[/]")
        console.print()
        console.print("[dim]© fuyugaki, 2026[/][dim] • Support your artists and developers![/]")
        console.print()
        return

    check_gallery_dl()

    errors = []
    try:
        for i, url in enumerate(urls, 1):
            if len(urls) > 1:
                console.print()
                console.print(Rule(f"[dim]{i}/{len(urls)}[/]", style="dim"))
            else:
                console.print()
                console.print(Rule(style="dim"))
            console.print()
            try:
                run(url, output_base)
            except AppError as e:
                msg = str(e).strip("'\"")
                if msg.lower() == "unavailable":
                    console.print(f"\n{timestamp()} [bold red]✗ Tweet is age-restricted — to fix:[/]")
                    console.print("  [dim]1. Enable NSFW: X Settings → Privacy and Safety → Display of sensitive media[/]")
                    console.print("  [dim]2. Refresh cookies:  gallery-dl --cookies-from-browser chrome[/]")
                    console.print("  [dim]   Or set auth_token in gallery-dl config (see /help → Credentials)[/]")
                    errors.append((url, "age-restricted"))
                else:
                    console.print(f"\n{timestamp()} [bold red]✗ {msg}[/]")
                    errors.append((url, msg))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/]")
    finally:
        if errors:
            console.print()
            console.print(f"{timestamp()} [red]✗ {len(errors)} of {len(urls)} URL(s) failed:[/]")
            for url, msg in errors:
                console.print(f"  [dim]{url}[/]")
                console.print(f"  [red]   {msg}[/]")
        console.print()
        console.print("[dim]© fuyugaki, 2026[/][dim] • Support your artists and developers![/]")
        console.print()


if __name__ == "__main__":
    main()