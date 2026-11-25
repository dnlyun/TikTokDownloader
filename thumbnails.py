"""Bounded, same-origin thumbnail service. TikTok previews are best effort."""
import asyncio
import json
import re
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit
import httpx
from fastapi import HTTPException
from fastapi.responses import Response

class PreviewParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []
        self.meta = None
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script':
            self.active = attrs.get('id') == '__UNIVERSAL_DATA_FOR_REHYDRATION__'
        if tag == 'meta' and attrs.get('property', attrs.get('name')) in ('og:image', 'twitter:image'):
            self.meta = attrs.get('content')
    def handle_endtag(self, tag):
        if tag == 'script': self.active = False
    def handle_data(self, data):
        if self.active: self.parts.append(data)
    def cover(self):
        try:
            scope = json.loads(''.join(self.parts)).get('__DEFAULT_SCOPE__', {})
            for key in ('webapp.video-detail', 'webapp.photo-detail'):
                item = scope.get(key, {}).get('itemInfo', {}).get('itemStruct', {})
                video = item.get('video', {})
                if video.get('cover'): return video['cover']
                images = item.get('imagePost', {}).get('images', [])
                if images:
                    urls = images[0].get('imageURL', {}).get('urlList', [])
                    if urls: return urls[0]
        except (ValueError, AttributeError, TypeError):
            pass
        return self.meta

def safe_image(url):
    p = urlsplit(url)
    host = p.hostname or ''
    return p.scheme == 'https' and not p.username and not p.password and p.port in (None,443) and any(host == d or host.endswith('.'+d) for d in ('tiktokcdn.com','tiktokcdn-us.com','tiktokcdn-eu.com','byteoversea.com','ibytedtos.com','ibyteimg.com','tiktok.com'))

class Thumbnails:
    def __init__(self):
        self.cache = {}
        self.pending = {}
        self.slots = asyncio.Semaphore(3)
    async def get(self, url):
        entry = self.cache.get(url)
        if entry and entry[0] > time.monotonic(): return entry[1]
        if url not in self.pending:
            self.pending[url] = asyncio.create_task(self.fetch(url))
            self.pending[url].add_done_callback(lambda task: self.pending.pop(url, None))
        return await asyncio.shield(self.pending[url])
    async def fetch(self, url):
        result = None
        try:
            async with self.slots, asyncio.timeout(20):
                async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
                    # oEmbed sometimes succeeds without including a thumbnail.
                    r = await client.get('https://www.tiktok.com/oembed', params={'url':url})
                    source = r.json().get('thumbnail_url') if r.status_code == 200 else None
                    if not source:
                        current = url
                        for _ in range(4):
                            r = await client.get(current)
                            if r.is_redirect:
                                from urllib.parse import urljoin
                                current = urljoin(current, r.headers.get('location',''))
                                p = urlsplit(current)
                                if p.scheme != 'https' or p.hostname not in ('www.tiktok.com','tiktok.com','vm.tiktok.com','vt.tiktok.com') or p.username or p.port not in (None,443): break
                                continue
                            if r.status_code == 200:
                                parser = PreviewParser(); parser.feed(r.text); source = parser.cover()
                            break
                    if not source and re.search(r"/photo/\d+", url):
                        # TikTok's photo route may omit item data; the video route for
                        # that same post currently includes imagePost.images.
                        preview_url = re.sub(r"/photo/(\d+)", r"/video/\1", url)
                        r = await client.get(preview_url)
                        if r.status_code == 200:
                            parser = PreviewParser()
                            parser.feed(r.text)
                            source = parser.cover()
                    if source and safe_image(source):
                        async with client.stream('GET', source) as image:
                            kind = image.headers.get('content-type','').split(';')[0]
                            if image.status_code == 200 and kind in ('image/jpeg','image/png','image/webp','image/avif'):
                                body = bytearray()
                                async for chunk in image.aiter_bytes():
                                    body.extend(chunk)
                                    if len(body) > 5_000_000: raise ValueError('Thumbnail too large')
                                if body: result = (bytes(body),kind)
        except (httpx.HTTPError, ValueError, TypeError, TimeoutError):
            pass
        if len(self.cache) >= 32: self.cache.pop(next(iter(self.cache)))
        self.cache[url] = (time.monotonic() + (1800 if result else 20), result)
        return result
    async def response(self, url):
        result = await self.get(url)
        if not result: raise HTTPException(404, 'Preview unavailable; click to retry')
        return Response(result[0], media_type=result[1], headers={'Cache-Control':'private, max-age=1800','X-Content-Type-Options':'nosniff'})
