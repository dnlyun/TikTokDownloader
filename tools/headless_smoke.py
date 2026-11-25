"""Live checks using the real downloader, isolated from your queue and numbered files."""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ssstik import SsstikDownloader

EXAMPLES = [
    ("video", "https://www.tiktok.com/@alqa_2408/video/7315846202510134534"),
    ("slideshow", "https://www.tiktok.com/@might.be.ellen/photo/7703064171126033695"),
]

async def run(args):
    os.environ["SSSTIK_HEADLESS"] = "0" if args.headed else "1"
    os.environ["SSSTIK_BROWSER_CHANNEL"] = args.channel
    output = Path(args.output) / time.strftime("%Y%m%d-%H%M%S")
    output.mkdir(parents=True, exist_ok=True)
    downloader = SsstikDownloader(output)
    results = []
    # Save screenshots before the production downloader closes a failed tab.
    original = downloader._do_download
    async def observed(page, url, number, callback):
        try:
            return await original(page, url, number, callback)
        except Exception:
            try:
                await page.screenshot(path=str(output / f"failure-{number}.png"), timeout=5000)
            except Exception:
                pass
            raise
    downloader._do_download = observed
    try:
        await downloader.start_browser()
        for number, (kind, url) in enumerate(EXAMPLES, 1):
            started = time.monotonic()
            async def progress(percent, message):
                print(f"{kind}: {percent}% {message}", flush=True)
            try:
                filename = await downloader.download_url(url, number, progress)
                results.append({"kind":kind,"status":"saved","file":filename,"bytes":(output/filename).stat().st_size,"seconds":round(time.monotonic()-started,1)})
            except Exception as error:
                results.append({"kind":kind,"status":"failed","error":str(error).splitlines()[0]})
            print(results[-1], flush=True)
            if number < len(EXAMPLES): await asyncio.sleep(12)
    except Exception as error:
        results.append({"status":"startup_failed","error":str(error).splitlines()[0]})
        print(results[-1], flush=True)
    finally:
        await downloader.close_browser()
        report = {"headless":not args.headed,"channel":args.channel,"results":results}
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Report: {output.resolve() / 'report.json'}")
    return 0 if len(results)==2 and all(r["status"]=="saved" for r in results) else 1

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", choices=["chromium","msedge","chrome"], default="chromium")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--output", default=str(Path(__file__).resolve().parents[1] / "headless-checks"))
    sys.exit(asyncio.run(run(parser.parse_args())))
