# TikTok downloader — SSSTik branch

Python 3.11+ required. On Windows:

```powershell
py -m pip install -r requirements.txt
py -m playwright install chromium
py main.py
```

Paste or drop multiple TikTok post links. Videos use SSSTik HD only; photo posts use its MP4 conversion. Browser windows remain visible for ad playback. Set `SSSTIK_HEADLESS=1` only if your environment supports the ad flow headlessly.

The queue and completed post history persist in `queue.sqlite3`. Refreshing reconnects to progress; restarting resumes unfinished work. Retry failed jobs from the UI. Adding links during a batch appends them. Canonical post IDs prevent repeated downloads; short links are deduplicated by their URL until resolved by SSSTik. Existing numbered MP4 files and counter are preserved; earlier files without URL history cannot be automatically matched to posts.

At most three jobs run concurrently, with 12 seconds between starts to respect SSSTik's current request pacing. Retries use exponential backoff; rate limits pause new launches globally. Each attempt has a six-minute deadline. Downloads are saved to a temporary file, checked for an MP4 header, then renamed atomically. There is no fallback to standard quality.

Keep `queue.sqlite3` with the downloads folder. Run only one server process per data directory. SSSTik can change its UI or ad behavior; failed HD generation is reported rather than silently reducing quality.

Validation:

```powershell
py -m pip install httpx
py tests/regression.py
```

Live SSSTik controls were checked on 2026-11-25 using the supplied video and slideshow. Both browser download events completed. The video file was verified with ffprobe (H.264/AAC, 576×1024, 14.28 seconds). Full execution of the Python browser automation could not be tested in the development shell because its sandbox blocks Chromium startup. Regression checks use a fake downloader and temporary data directories.

Thumbnails are fetched by the local server with a bounded cache. If oEmbed omits a cover, the server checks the post page; photo previews also try the same post ID through TikTok's video route. Unavailable previews show a clickable retry placeholder (failed lookups are cached for 20 seconds). Multi-post drops read HTML anchors as well as URL/plain-text formats; a selection containing only captions cannot supply post URLs.

Additional offline checks:

```powershell
py -m unittest discover -s tests -p "test_*.py"
node tests/drop.test.js
```

Node is only needed for the JavaScript checks, not for running the app. Keep the new `thumbnails.py`, `requirements.txt`, and tests in the commit along with the modified files.

## Headless trial (not yet verified against SSSTik)

`SSSTIK_HEADLESS=1` now uses full Chromium's new headless mode (`channel="chromium"`), not Playwright's separate headless shell. Headed mode remains the default. Install the full browser using `py -m playwright install chromium`.

Run both supplied examples without touching the queue or normal downloads:

```powershell
py tools/headless_smoke.py
py tools/headless_smoke.py --channel msedge
```

The second command uses your installed Microsoft Edge, in an isolated browser context. It does not use your personal browser profile. Reports, downloaded MP4s and failure screenshots are saved under ignored `headless-checks/`. Compare with `py tools/headless_smoke.py --channel msedge --headed` if needed. Check that both files play and retain the expected quality before using this mode for a batch.

If the trial succeeds:

```powershell
$env:SSSTIK_HEADLESS="1"
$env:SSSTIK_BROWSER_CHANNEL="msedge"
py main.py
```

Use `chromium` for the bundled browser. Remove those environment variables to restore defaults. There is no automatic fallback to headed mode or lower quality. The application dashboard still opens normally; only the downloader browser is headless. Both headless engines were blocked at browser startup by the development sandbox, so their SSSTik ad behavior remains unverified.
