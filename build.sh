set -e

cd "$(dirname "$0")"

pip install --quiet pyinstaller

pyinstaller \
    --name tiktok-downloader \
    --onefile \
    --add-data "static:static" \
    --add-data "downloader.py:." \
    --add-data "slideshow.py:." \
    --add-data "app.py:." \
    --hidden-import uvicorn.logging \
    --hidden-import uvicorn.loops \
    --hidden-import uvicorn.loops.auto \
    --hidden-import uvicorn.protocols \
    --hidden-import uvicorn.protocols.http \
    --hidden-import uvicorn.protocols.http.auto \
    --hidden-import uvicorn.protocols.websockets \
    --hidden-import uvicorn.protocols.websockets.auto \
    --hidden-import uvicorn.lifespan \
    --hidden-import uvicorn.lifespan.on \
    --hidden-import uvicorn.lifespan.off \
    --hidden-import uvloop \
    --hidden-import httptools \
    --hidden-import websockets \
    --hidden-import yt_dlp \
    --hidden-import aiohttp \
    --collect-all yt_dlp \
    --noconfirm \
    main.py

echo ""
echo "Build complete: dist/tiktok-downloader"
echo "Run it with: ./dist/tiktok-downloader"