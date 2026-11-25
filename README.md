# TikTok downloader — SSSTik branch

Python 3.11+ required. On Windows 11 (Google Chrome installed):

```powershell
py -m pip install -r requirements.txt
py main.py
```

Paste or drop multiple TikTok post links. Videos use SSSTik HD only; photo posts use its MP4 conversion. On Windows, the downloader targets the installed Google Chrome channel by default and keeps the window visible so ad playback is not background-throttled. Set `SSSTIK_BROWSER_CHANNEL=msedge` or `chromium` to override it. Set `SSSTIK_HEADLESS=1` only if your environment supports the ad flow headlessly.

Ad handling is state-driven: no-ad downloads proceed immediately, while skip, dismiss, and close-ad controls are detected across nested frames and popup tabs whenever they appear. There is no assumed ad duration. Popup tabs are closed after their job. Failed attempts save a sanitized JSON report and, when possible, a screenshot under `downloads/diagnostics/`.

Tuning environment variables:

```powershell
$env:SSSTIK_MAX_CONCURRENT="3"       # default 3
$env:SSSTIK_LAUNCH_INTERVAL="12"     # seconds between new submissions
$env:SSSTIK_NAVIGATION_TIMEOUT="45"
$env:SSSTIK_RESULT_TIMEOUT="90"
$env:SSSTIK_FLOW_TIMEOUT="600"       # conversion plus any ad playback
$env:SSSTIK_SAVE_TIMEOUT="180"
```

Concurrency and launch pacing are independent. Keep the 12-second launch interval unless live testing shows SSSTik accepts a faster rate. The default is three overlapping jobs because rewarded ads become unreliable when too many Chrome tabs are backgrounded at once. Chrome background throttling is disabled for the downloader context.

Completed post history persists in `queue.sqlite3`. Refreshing reconnects to progress. Restarting clears failed and unfinished work; a fully validated file left by an interrupted final save is still recovered as completed. Enter the first output number in the dashboard before starting a batch. New batches do not read `counter.txt`, database maxima, or existing filenames to choose that number; an existing target filename is rejected instead of overwritten. Adding links during a running batch continues after that batch's highest assigned number. Canonical post IDs prevent repeated downloads; short links are deduplicated by their URL until resolved by SSSTik.

By default, at most three jobs run concurrently, with 12 seconds between starts to respect SSSTik's current request pacing. Retries use exponential backoff; rate limits pause new launches globally. Downloads are saved to a temporary file, checked for an MP4 header and minimum size, then renamed atomically. Startup recovery and archive reuse apply the same validation. There is no fallback to standard quality. Use the app's **Open downloads folder** button rather than Chrome's download bubble, which refers to Playwright's temporary download entry.

Keep `queue.sqlite3` with the downloads folder. Run only one server process per data directory. SSSTik can change its UI or ad behavior; failed HD generation is reported rather than silently reducing quality.

Validation:

```powershell
py -m pip install httpx
py tests/regression.py
```

Live SSSTik controls were checked on 2026-11-25 using the supplied video and slideshow. Both browser download events completed. The video file was verified with ffprobe (H.264/AAC, 576×1024, 14.28 seconds). Full execution of the Python browser automation could not be tested in the development shell because its sandbox blocks Chromium startup. Regression checks use a fake downloader and temporary data directories.

Thumbnails are fetched by the local server with a Windows Chrome user agent and a bounded connection/cache pool. If oEmbed omits a cover, the server checks the post page; photo previews try the same post ID through TikTok's video route and use the first photo. Unavailable previews show a clickable cache-bypassing retry placeholder. TikTok may still withhold preview data during anti-bot throttling. Multi-post drops read HTML anchors as well as URL/plain-text formats; a selection containing only captions cannot supply post URLs.

Failed cards expose **Open TikTok** and **Copy link** actions. The batch toolbar can copy every failed URL or save them as a newline-delimited text file before retrying.

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
