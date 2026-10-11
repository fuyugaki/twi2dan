#!/usr/bin/env python3
"""
twi2dan.py — Download media from a Twitter/X post, sorted into a Danbooru artist folder.

Lookup chain for artist name:
  1. IQDB reverse image search → Danbooru post → artist tag  (most reliable)
  2. Danbooru artist URL search by Twitter handle              (fallback)
  3. Twitter handle as folder name                             (last resort)

Once tier 1 or 2 resolves a handle, the result is cached for the rest of the
session, so further URLs from the same account skip the lookups entirely.

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
import sys
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

# Redirected output (pipes, files, PowerShell jobs) falls back to the Windows
# ANSI codepage, which can't encode "→", "✓" etc. Force UTF-8 instead.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

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

# While a query waits, IQDB holds the response open and flushes a
# <script>queue('<place>', '<own_pending>');</script> tag each time it re-reads
# the queue. First arg is how many queries sit ahead of ours; second is how many
# other queries *we* have in flight, which IQDB displays instead of the place.
IQDB_QUEUE_RE = re.compile(r"queue\('(\d+)',\s*'(\d+)'\)")

# Bytes of already-scanned text to re-examine after a chunk with no match, so a
# queue() call split across a chunk boundary is still found. Comfortably longer
# than the ~20-character call itself.
IQDB_SCAN_OVERLAP = 64

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}

# Danbooru tags marking a post as made (wholly or partly) with generative AI.
# Only canonical names are needed — Danbooru resolves aliases such as
# "AIart" or "NovelAI" to these before they reach a post's tag_string.
AI_TAGS = (
    "ai-generated",
    "ai-assisted",
    "ai-generated_background",
    "ai-generated_inset",
    "stable_diffusion",
    "nai_diffusion",
)

# ── Session caches ────────────────────────────────────────────────────────────
# In-memory only, discarded when the process exits. Their sole purpose is to
# stop a batch of URLs from the same account hammering IQDB and Danbooru with
# identical questions — one handle should cost one lookup per session.
#
# Handles are lowercased before use as keys: Twitter treats @Artist and
# @artist as the same account, so they must not occupy separate cache entries.

# handle → Danbooru artist tag, recorded only for *confirmed* resolutions
# (tier 1 or tier 2). A hit skips IQDB and both Danbooru queries.
# The tier-3 handle fallback is deliberately never stored here — it isn't a
# real answer, and a later tweet from the account may still land an IQDB match.
ARTIST_CACHE: dict[str, str] = {}

# handle → AI tags on the Danbooru post that resolved it, stored alongside
# every ARTIST_CACHE entry (empty when the post had none, or when tier 2
# resolved the artist without a post). Since a cache hit skips IQDB, this is
# the only way later URLs from the account can still carry the AI warning.
ARTIST_AI_TAGS: dict[str, list[str]] = {}

# Handles that /artists.json has already been asked about and had no artist
# for. Only tier 2 is skipped on a hit; tier 1 (IQDB) still runs, since it
# looks at the image rather than the account.
ARTIST_URL_MISSES: set[str] = set()


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

def clean_tweet_url(url: str) -> str:
    """Drop share-tracking junk (?s=12&t=…, #fragment) so duplicates collapse."""
    return re.split(r"[?#]", url, maxsplit=1)[0]


def is_truncated_share_link(url: str) -> bool:
    """X share links end in ?s=NN&t=…; ending at ?s=NN means the shell split on '&'."""
    return bool(re.search(r"\?s=\d+$", url))


def alert_unquoted_urls():
    """Explain the unquoted-'&' mistake. Pops a dialog on Windows when output is
    hidden (PowerShell turns each '&' into a background job)."""
    msg = ("Links were cut off at '&' by the shell — nothing was downloaded.\n\n"
           "Put each URL in quotes:\n"
           '  python twi2dan.py "https://x.com/…?s=12&t=…"\n\n'
           "Or delete everything from '?' onward in each link.")
    console.print(Panel(msg, title="[bold red]✗ Unquoted URLs[/]", border_style="red"))
    if os.name == "nt" and not sys.stdout.isatty():
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, "twi2dan", 0x30)  # MB_ICONWARNING
        except Exception:
            pass


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

def _iqdb_read_stream(resp, image_name: str, progress, task) -> str:
    """
    Consume IQDB's chunked response, reporting queue position as it arrives.

    Under load IQDB queues the query and keeps the connection open, flushing an
    inline queue() <script> tag each time it re-reads the queue (see
    IQDB_QUEUE_RE). Those tags are meant for a browser to execute as they land,
    so the position is only useful mid-flight — once the request completes it is
    stale history, which is why the body has to be read incrementally rather
    than with a plain buffered `resp.text`.

    `resp` must come from a stream=True request. Returns the whole decoded
    document, so callers parse exactly what a buffered read would have given.
    """
    # Decode as Response.text would: requests sets ISO-8859-1 for text/html
    # with no charset, so streaming doesn't change how the page is interpreted.
    # iter_content decodes incrementally and holds back partial multi-byte
    # sequences, so a character split across two chunks survives intact.
    resp.encoding = resp.encoding or "utf-8"

    buf = ""
    scan_from = 0      # start of the region not yet matched against
    last_seen = None   # last (place, own_pending) rendered, to skip idle redraws
    searching = False  # results began — the queue line is no longer true

    try:
        for chunk in resp.iter_content(chunk_size=1024, decode_unicode=True):
            if not chunk:  # keep-alive chunks carry no payload
                continue
            buf += chunk

            # Take the *last* queue() in the new region, not each one: these are
            # position updates, so if several arrived together only the newest
            # is still accurate. Older ones would only flicker the spinner.
            latest = None
            for m in IQDB_QUEUE_RE.finditer(buf, scan_from):
                latest = m

            if latest:
                scan_from = latest.end()
                place, own_pending = int(latest.group(1)), int(latest.group(2))
                # IQDB re-sends an unchanged position when nothing drained
                # between reads; don't redraw for that.
                if (place, own_pending) != last_seen:
                    last_seen = (place, own_pending)
                    if own_pending:
                        # Our own queries are serialised, so IQDB reports that
                        # backlog instead of a queue place — mirror its wording.
                        others = "query" if own_pending == 1 else "queries"
                        detail = f"waiting on {own_pending} other {others}"
                    else:
                        detail = f"queued, place {place}"
                    progress.update(task, description=f"[blue]IQDB:[/] {image_name} — {detail}…")
            elif len(buf) - scan_from > IQDB_SCAN_OVERLAP:
                # No match: skip ahead so the scan stays O(chunk) instead of
                # re-reading the whole page, but leave a tail behind in case a
                # queue() call straddles this chunk boundary and completes next.
                scan_from = len(buf) - IQDB_SCAN_OVERLAP

            # Past the queue and into the results: stop claiming we're waiting.
            if last_seen and not searching and "Search results" in buf:
                searching = True
                progress.update(task, description=f"[blue]IQDB:[/] searching {image_name}…")
    except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
        # A drop mid-stream leaves a truncated document. Retry it — parsing the
        # partial page would report "no match" and mis-name the artist folder.
        raise RetryableError("connection lost mid-search")

    return buf


def iqdb_search(image_path: Path, progress, task) -> str | None:
    size = image_path.stat().st_size
    if size > IQDB_MAX_SIZE:
        progress.update(task, description=f"[yellow]⚠ IQDB:[/] {image_path.name} too large ({size // 1024 // 1024} MB > 8 MB), skipping")
        return None

    progress.update(task, description=f"[blue]IQDB:[/] searching {image_path.name}…")

    def _iqdb_post():
        with open(image_path, "rb") as fh:
            # stream=True so the queue updates can be read while IQDB is still
            # working; the body is identical either way, so this costs IQDB
            # nothing extra. The `with` guarantees the connection is released
            # even when a status check below bails out early.
            with requests.post(
                IQDB_URL,
                files={"file": (image_path.name, fh)},
                data={
                    "MAX_FILE_SIZE": str(IQDB_MAX_SIZE),
                    "service[]": "1",   # Danbooru only — faster lookup
                },
                timeout=(10, 90),
                stream=True,
            ) as r:
                if r.status_code == 429:
                    raise RetryableError("rate limit hit")
                if r.status_code == 503:
                    raise RetryableError("service unavailable")
                if not r.ok:
                    raise RetryableError(f"HTTP {r.status_code}")
                return _iqdb_read_stream(r, image_path.name, progress, task)

    try:
        html = with_retry(_iqdb_post, attempts=3, initial_delay=3.0, label="IQDB", progress=progress, task=task)
    except AppError as e:
        progress.update(task, description=f"[yellow]{e}[/]")
        return None

    soup = BeautifulSoup(html, "html.parser")

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


def artist_from_danbooru_post(post_url: str, session: requests.Session, progress, task) -> tuple[str | None, list[str]]:
    """Return (artist tag, AI tags found on the post)."""
    m = re.search(r"/posts/(\d+)", post_url)
    if not m:
        return None, []
    post_id = m.group(1)

    progress.update(task, description=f"[blue]Danbooru:[/] fetching post {post_id}…")
    try:
        resp = with_retry(
            lambda: danbooru_get(session, f"{DANBOORU_BASE}/posts/{post_id}.json"),
            attempts=3, initial_delay=2.0, label="Danbooru post", progress=progress, task=task,
        )
    except AppError as e:
        progress.update(task, description=f"[yellow]⚠ Danbooru post:[/] {e}")
        return None, []

    try:
        post = resp.json()
    except ValueError:
        progress.update(task, description="[yellow]⚠ Danbooru:[/] invalid JSON response")
        return None, []

    post_tags = set(post.get("tag_string", "").split())
    ai_tags   = [t for t in AI_TAGS if t in post_tags]

    tag_string_artist = post.get("tag_string_artist", "").strip()
    if not tag_string_artist:
        progress.update(task, description="[yellow]⚠ Danbooru:[/] post has no artist tag")
        return None, ai_tags

    return tag_string_artist.split()[0], ai_tags


# ── Danbooru URL fallback ─────────────────────────────────────────────────────

def find_danbooru_artist_by_twitter(twitter_username: str, session: requests.Session, progress, task) -> str | None:
    """
    Tier 2: find a Danbooru artist whose profile links to this Twitter handle.

    Danbooru stores artist URLs under both twitter.com and x.com, so both are
    tried. A confirmed miss is remembered in ARTIST_URL_MISSES so the same
    fruitless pair of queries isn't repeated for every URL from the account.
    """
    handle = twitter_username.lower()
    if handle in ARTIST_URL_MISSES:
        progress.update(task, description="[dim]Danbooru: no artist for this handle (already checked this session)[/]")
        return None

    # Distinguishes "Danbooru says no" from "we never got a usable answer".
    # Only the former is worth caching — a network blip must not permanently
    # rule the handle out for the rest of the session.
    complete = True
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
            complete = False  # unreadable answer — this domain proved nothing
            continue

        if artists:
            # Prefer a live artist entry; fall back to the first match if every
            # candidate is banned or deleted (the tag itself is still valid).
            for artist in artists:
                if not artist.get("is_banned") and not artist.get("is_deleted"):
                    return artist["name"]
            return artists[0]["name"]

    # Reached only when both domains answered and neither had an artist.
    if complete:
        ARTIST_URL_MISSES.add(handle)
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

    # Looked up once, up front: `cached_artist` decides whether this URL needs
    # the full resolution pipeline or can reuse an answer from an earlier URL.
    cache_key         = twitter_user.lower()
    cached_artist     = ARTIST_CACHE.get(cache_key)
    artist_name       = None
    danbooru_post_url = None
    ai_tags           = ARTIST_AI_TAGS.get(cache_key, []) if cached_artist else []
    download_error    = None
    session = danbooru_session()

    if cached_artist:
        console.print(f"  {timestamp()} [dim]artist already resolved this session →[/] [bold green]{cached_artist}[/]")
        console.print()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Starting…")

        with tempfile.TemporaryDirectory(prefix="twi2dan_") as tmp:
            tmp_path = Path(tmp)

            # Download — a failure here is non-fatal: we still create the
            # artist folder so the user has somewhere to drop files manually.
            progress.update(task, description="[blue]Downloading media…[/]")
            try:
                files = download_with_gallery_dl(tweet_url, tmp_path, progress, task)
            except AppError as e:
                download_error = str(e).strip("'\"")
                files = []
                progress.update(task, description=f"[yellow]⚠ Download failed:[/] {download_error}")

            if cached_artist:
                # An earlier URL in this batch already established the
                # handle → artist link, so neither IQDB nor Danbooru has
                # anything left to tell us. Media is still downloaded, it just
                # goes straight into the folder we already know the name of.
                artist_name = cached_artist
                progress.update(task, description=f"[green]✓ Reusing artist[/] [bold]{cached_artist}[/] [dim](cached this session)[/]")
            else:
                # Tier 1 — IQDB reverse search. Stops at the first image that
                # matches a Danbooru post; the 1s pause between images is
                # IQDB politeness, not a technical requirement.
                image_files = [f for f in files if f.suffix.lower() in IMAGE_EXTENSIONS]

                if image_files:
                    for img in image_files:
                        post_url = iqdb_search(img, progress, task)
                        if post_url:
                            danbooru_post_url = post_url
                            break
                        time.sleep(1)
                elif not download_error:
                    progress.update(task, description="[yellow]⚠ No images to reverse-search (video only?)[/]")

                # Danbooru artist lookup — without images, IQDB can't run, so we
                # fall back to the Twitter-handle searches to name the folder.
                if danbooru_post_url:
                    artist_name, ai_tags = artist_from_danbooru_post(danbooru_post_url, session, progress, task)

                # Tier 2 — ask Danbooru which artist claims this handle.
                if not artist_name:
                    artist_name = find_danbooru_artist_by_twitter(twitter_user, session, progress, task)

                # Anything reaching here came from tier 1 or 2, i.e. Danbooru
                # itself vouched for the name — safe to reuse for this handle.
                if artist_name:
                    ARTIST_CACHE[cache_key]   = artist_name
                    ARTIST_AI_TAGS[cache_key] = ai_tags

            # Tier 3 — no confirmed artist. Not cached: the next tweet from
            # this account may well be indexed on IQDB even though this one
            # wasn't, and that answer should win over the bare handle.
            if not artist_name:
                progress.update(task, description="[yellow]⚠ Artist unknown — using Twitter handle[/]")
                artist_name = twitter_user.lower()

            # Create the artist folder and move any downloaded files into it.
            artist_folder = output_base / artist_name
            if files:
                progress.update(task, description=f"[blue]Moving files →[/] [bold]{artist_name}[/]")
                moved = move_files_to_folder(files, artist_folder)
                if not moved:
                    raise AppError("No files were saved — all moves failed. Check output directory permissions.")
            else:
                # No media downloaded — still create the (empty) artist folder.
                progress.update(task, description=f"[blue]Creating folder →[/] [bold]{artist_name}[/]")
                ensure_output_dir(artist_folder)
                moved = []

            if download_error:
                progress.update(task, description=f"[yellow]⚠ Folder created, download failed[/] {timestamp()}")
            else:
                progress.update(task, description=f"[green]✓ Done[/] {timestamp()}")

    # ── Summary ───────────────────────────────────────────────────────────────
    console.print()
    artist_page = f"{DANBOORU_BASE}/posts?tags={artist_name}"
    artist_cell = f"[bold green][link={artist_page}]{artist_name}[/link][/] [blue][link={artist_page}]{artist_page}[/link][/]"
    if cached_artist:
        artist_cell += " [dim](reused from earlier in this session)[/]"

    table = Table(box=box.ROUNDED, show_header=False, padding=(0, 1))
    table.add_column(style="dim")
    table.add_column()
    table.add_row("Artist", artist_cell)
    if danbooru_post_url:
        table.add_row("Post", f"[link={danbooru_post_url}]{danbooru_post_url}[/link]")
    if ai_tags:
        ai_cell = f"[bold red]⚠ {', '.join(ai_tags)}[/]"
        if cached_artist:
            ai_cell += " [dim](from an earlier post this session)[/]"
        table.add_row("AI", ai_cell)
    table.add_row("Folder", str(artist_folder))
    table.add_row("Files",  str(len(moved)))
    if download_error:
        table.add_row("Download", f"[yellow]failed — {download_error}[/]")
    console.print(table)
    console.print()
    if ai_tags:
        if cached_artist:
            console.print(f"  [bold red]⚠ An earlier Danbooru match from this account is tagged as AI art ({', '.join(ai_tags)}).[/]")
        else:
            console.print(f"  [bold red]⚠ The matched Danbooru post is tagged as AI art ({', '.join(ai_tags)}).[/]")
        console.print()
    if download_error:
        console.print(f"  [yellow]⚠ No media downloaded — folder created empty for manual use.[/]")
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
    console.print("    Pass one or more tweet URLs to start downloading right away — no prompts.")
    console.print("    Output dir defaults to ~/Downloads; Danbooru credentials come from env/config only.")
    console.print("    [yellow]⚠[/] Wrap URLs containing [bold]&[/] in quotes, or the shell cuts them off:")
    console.print('    [dim]python twi2dan.py "https://x.com/user/status/123?s=12&t=abc"[/]')
    console.print()

    console.print("[bold]How it works[/]")
    console.print("  1. Downloads media via [bold]gallery-dl[/]")
    console.print("  2. Submits each image to [bold]IQDB[/] for reverse image search")
    console.print("  3. Looks up the Danbooru post to get the artist tag")
    console.print("  4. Falls back to searching Danbooru by Twitter handle if IQDB fails")
    console.print("  5. Falls back to the Twitter handle as folder name if all else fails")
    console.print("  6. Saves files to [bold]<output_dir>/<artist_name>/[/]")
    console.print("  Once a handle → artist link is found, it’s reused for the rest of the session")
    console.print("  [dim](no repeated IQDB or Danbooru lookups for further URLs from the same account).[/]")
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

    # URLs on the command line mean "just download these": no prompts, any
    # missing setting falls back to its default.
    direct = bool(args.tweet_urls)

    if any(is_truncated_share_link(u) for u in args.tweet_urls):
        alert_unquoted_urls()
        sys.exit(2)

    console.print(Panel.fit(
        "[bold]twi2dan[/]  [dim]Twitter/X → Danbooru artist folder[/]",
        border_style="bright_blue",
    ))
    console.print()

    # Danbooru credentials
    global DANBOORU_USER, DANBOORU_API_KEY
    if not DANBOORU_USER or not DANBOORU_API_KEY:
        if direct:
            console.print("  [blue]Danbooru:[/] anonymous [dim](run without URLs to set credentials)[/]")
            console.print()
        elif Confirm.ask("[bold]Set Danbooru credentials?[/] [dim](recommended, avoids rate limits)[/]", default=False):
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
        if direct:
            output_dir = default
        while not output_dir:
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
    if direct:
        console.print(f"  [blue]Output:[/] {output_base}")

    # URLs — from CLI args or interactive prompt loop
    seen = set()
    urls = []
    for u in args.tweet_urls:
        u = clean_tweet_url(u)
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
                u = clean_tweet_url(u)
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