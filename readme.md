# twi2dan

Download media from a Twitter/X post and sort it into a folder named after the Danbooru artist tag.

```
╭─────────────────────────────────────────────╮
│ twi2dan  Twitter/X → Danbooru artist folder │
╰─────────────────────────────────────────────╯
```

---

## How it works

1. Downloads all media from the tweet via **gallery-dl**
2. Submits each image to **IQDB** for reverse image search
3. Fetches the matched Danbooru post and extracts the artist tag
4. Falls back to searching Danbooru by the Twitter handle if IQDB finds nothing
5. Falls back to the Twitter handle itself as the folder name if all else fails
6. Moves files to `<output_dir>/<artist_name>/`

Multi-image tweets are handled correctly — all images land in the same artist folder. Subsequent downloads from the same artist accumulate in the same folder. Duplicate filenames get a `_1`, `_2` suffix.

### IQDB queue position is shown live

IQDB runs one shared queue for everyone, and step 2 can sit in it for half a minute or more when the site is busy. Rather than freeze on a single spinner line, twi2dan reads IQDB’s progress as it arrives and counts your place down:

```
⠹ IQDB: FxK9mZ3aQAA1b2C.jpg — queued, place 25…
⠸ IQDB: FxK9mZ3aQAA1b2C.jpg — queued, place 13…
⠼ IQDB: FxK9mZ3aQAA1b2C.jpg — queued, place 4…
⠴ IQDB: searching FxK9mZ3aQAA1b2C.jpg…
✓ IQDB match found (96%)
```

A falling number means it’s waiting, not hung — so let it run instead of interrupting. Restarting sends every image back to the end of the queue.

You may occasionally see `waiting on 2 other queries` instead of a place. That’s IQDB serialising *your own* requests, not queue depth; it clears on its own.

Nothing is displayed when IQDB isn’t busy, because it doesn’t queue the query at all — the search just returns.

### AI art warning

If the matched Danbooru post is tagged `ai-generated`, `ai-assisted`, `ai-generated_background`, `ai-generated_inset`, `stable_diffusion` or `nai_diffusion`, the summary shows an **AI** row and a warning. The flag is cached with the artist, so later tweets from the same account show it too.

### Artist lookups are cached per session

The first time a handle is resolved to a Danbooru artist (step 3 or 4 above), the link is remembered for the rest of the run. Every further URL from that same account goes straight to the known folder — no IQDB upload, no Danbooru query:

```
──────────────────────────────── 2/4 ────────────────────────────────

  14:22:07 @someartist
  14:22:07 artist already resolved this session → sakimichan

✓ Reusing artist sakimichan (cached this session)
```

Batch-downloading twenty tweets from one artist therefore costs one lookup instead of twenty, which keeps you well clear of IQDB and Danbooru rate limits.

Two details worth knowing:

- Handles that Danbooru has **no** artist entry for are also remembered, so that search isn’t repeated — but IQDB still runs on each tweet, since it identifies the *image* rather than the account. A late-indexed image can still resolve the artist mid-batch.
- If the artist couldn’t be identified at all and the Twitter handle was used as the folder name, nothing is cached — the next tweet gets a full lookup.

The cache lives in memory only and is empty again on the next run.

---

## Requirements

- Python 3.10+
- [gallery-dl](https://github.com/mikf/gallery-dl)

```
pip install -r requirements.txt
```

---

## Setup

### 1. Twitter/X auth

gallery-dl needs a logged-in session to download age-restricted content.

**Option A — import browser cookies (easiest):**
```
gallery-dl --cookies-from-browser chrome
gallery-dl --cookies-from-browser firefox
```
Run once while logged into X. Make sure NSFW content is enabled in your account settings first:
> Settings → Privacy and Safety → Display of sensitive media → enable all

**Option B — `auth_token` cookie (more stable, expires less frequently):**

1. Open `x.com`, open DevTools (`F12`)
2. Go to **Application → Cookies → https://x.com**
3. Copy the value of `auth_token`
4. Add to your gallery-dl config file:

   - Windows: `%APPDATA%/gallery-dl/config.json`
   - Linux/macOS: `~/.config/gallery-dl/config.json`

```json
{
  "extractor": {
    "twitter": {
      "cookies": { "auth_token": "YOUR_TOKEN_HERE" }
    }
  }
}
```

### 2. Danbooru credentials (optional)

Not required, but recommended to avoid rate limits on the API.

Prompted for in interactive mode and optionally saved to `.twi2dan.json` next to the script.

Alternatively, set environment variables:
```
# Windows
set DANBOORU_USER=yourname
set DANBOORU_API_KEY=yourkey

# Linux/macOS
export DANBOORU_USER=yourname
export DANBOORU_API_KEY=yourkey
```

Your API key is available at: `https://danbooru.donmai.us/users/[YOUR ID]/api_keys`

---

## Usage

### Interactive mode
```
python twi2dan.py
```
Prompts for output directory, then accepts URLs one per line. Empty line starts processing.

```
Output directory (C:\Users\you\Downloads):
Paste tweet URLs one per line. Multiple URLs supported. Empty line when done. Type /help for help.
Tweet URL (empty when done): https://x.com/artist/status/123456789
Tweet URL (empty when done): https://x.com/artist/status/987654321
Tweet URL (empty when done):
```

### Command line
```
python twi2dan.py "<url>" ["<url>" ...] [--output-dir PATH]
```

Starts downloading immediately — no prompts.

- Output defaults to `~/Downloads`.
- Danbooru credentials come from env vars or `.twi2dan.json`; otherwise anonymous.
- Tracking params (`?s=12&t=…`) are stripped; duplicate links are downloaded once.

**Quote every URL.** An unquoted `&` cuts the link off (cmd) or errors (PowerShell).

```
python twi2dan.py "https://x.com/someartist/status/123456789?s=12&t=abc"
python twi2dan.py "https://x.com/a/status/111" "https://x.com/b/status/222" --output-dir D:\art
```

### In-app help
Type `/help` at the URL prompt for usage, credential setup, and library links.

---

## Output structure

```
Downloads/
├── artist_name/
│   ├── FxK9mZ3aQAA1b2C.jpg
│   ├── FxK9mZ3aQAA1b2D.jpg   ← multi-image tweet
│   └── Gy7nPq2bRAB3c4E.png   ← later download, same artist
└── another_artist/
    └── Hz8oQr3cSBC4d5F.jpg
```

---

## Troubleshooting

### `'Unavailable'` error
The tweet is age-restricted and gallery-dl couldn’t access it.

1. Make sure NSFW is enabled in your X account settings
2. Refresh cookies: `gallery-dl --cookies-from-browser chrome`
3. Or set `auth_token` directly in gallery-dl config (see Setup above)

### `HTTP Error 401` / `Unauthorized`
Your cookies have expired. Re-run:
```
gallery-dl --cookies-from-browser chrome
```

### `HTTP Error 403`
Cookies may be from a logged-out session or a restricted account. Log into X in your browser first, then re-import.

### Tweet not found (404)
The tweet was deleted or the URL is wrong.

### Artist not identified
IQDB didn’t have the image indexed (common for very new posts) and the artist’s Twitter handle wasn’t linked on their Danbooru profile. The Twitter handle is used as the folder name in this case. You can rename the folder manually once you identify the artist.

### IQDB is stuck / taking forever
Check the spinner line. If it shows a queue place, IQDB is under load and you’re waiting your turn — the number counts down and the search starts on its own. If it sits on `searching …` with no place, IQDB has your image and is working.

Interrupting and re-running is counterproductive: every image is uploaded again and rejoins the queue at the back.

### Danbooru rate limit (429)
Add your Danbooru credentials — anonymous API access is limited to 2 requests/second and can be cut off during heavy use.

---

## Files

| File | Description |
|---|---|
| `twi2dan.py` | Main script |
| `.twi2dan.json` | Saved Danbooru credentials (same folder as script) |
| `requirements.txt` | Python dependencies |

---

## Dependencies

| Library | Purpose |
|---|---|
| [gallery-dl](https://github.com/mikf/gallery-dl) | Twitter/X media downloader |
| [IQDB](https://iqdb.org) | Reverse image search for anime art |
| [Danbooru](https://danbooru.donmai.us) | Anime image board and artist database |
| [requests](https://docs.python-requests.org) | HTTP client |
| [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup) | HTML parser |
| [Rich](https://github.com/Textualize/rich) | Terminal formatting |

---

© fuyugaki, 2026 — Support your artists and developers!