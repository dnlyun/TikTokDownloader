import asyncio
import json
import sys
import unittest
from pathlib import Path
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from thumbnails import BROWSER_HEADERS, PreviewParser, Thumbnails, safe_image

class PreviewTests(unittest.TestCase):
    def test_page_cover_fallback(self):
        data = {'__DEFAULT_SCOPE__': {'webapp.video-detail': {'itemInfo': {'itemStruct': {'video': {'cover': 'https://p.tiktokcdn.com/cover.jpg'}}}}}}
        p = PreviewParser()
        p.feed('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'+json.dumps(data)+'</script>')
        self.assertEqual(p.cover(), 'https://p.tiktokcdn.com/cover.jpg')
    def test_missing_preview(self):
        p = PreviewParser(); p.feed('<html>Unavailable</html>')
        self.assertIsNone(p.cover())
    def test_image_hosts(self):
        self.assertTrue(safe_image('https://p19.tiktokcdn.com/image.jpg'))
        for url in ['http://p19.tiktokcdn.com/image','https://localhost/image','https://tiktokcdn.com.evil.com/image','https://user@p19.tiktokcdn.com/image']:
            self.assertFalse(safe_image(url))


class ThumbnailFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_thumbnail_uses_browser_headers(self):
        source = "https://p16.tiktokcdn.com/cover.jpg"
        data = {'__DEFAULT_SCOPE__': {'webapp.video-detail': {'itemInfo': {'itemStruct': {'video': {'cover': source}}}}}}
        seen_user_agent = []
        def handler(request):
            seen_user_agent.append(request.headers.get("user-agent", ""))
            if request.url.host == "www.tiktok.com" and request.url.path == "/oembed":
                return httpx.Response(200, json={})
            if request.url.host == "www.tiktok.com":
                html = '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'+json.dumps(data)+'</script>'
                return httpx.Response(200, text=html)
            return httpx.Response(200, content=b"jpeg-data", headers={"content-type": "image/jpeg"})

        thumbnails = Thumbnails()
        await thumbnails.client.aclose()
        thumbnails.client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), headers=BROWSER_HEADERS,
            follow_redirects=False,
        )
        try:
            result = await thumbnails.fetch("https://www.tiktok.com/@user/video/123")
            self.assertEqual(result, (b"jpeg-data", "image/jpeg"))
            self.assertTrue(all("Windows NT 10.0" in value for value in seen_user_agent))
        finally:
            await thumbnails.close()

    async def test_photo_thumbnail_uses_first_image_from_video_route(self):
        source = "https://p19.tiktokcdn.com/photo.jpeg"
        photo_data = {'__DEFAULT_SCOPE__': {'webapp.video-detail': {'itemInfo': {'itemStruct': {
            'imagePost': {'images': [{'imageURL': {'urlList': [source]}}]}
        }}}}}
        requested_paths = []
        def handler(request):
            requested_paths.append(request.url.path)
            if request.url.path == "/oembed": return httpx.Response(400, json={})
            if "/photo/" in request.url.path: return httpx.Response(200, text="<html></html>")
            if "/video/" in request.url.path:
                html = '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'+json.dumps(photo_data)+'</script>'
                return httpx.Response(200, text=html)
            return httpx.Response(200, content=b"photo-data", headers={"content-type": "image/jpeg"})

        thumbnails = Thumbnails()
        await thumbnails.client.aclose()
        thumbnails.client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), headers=BROWSER_HEADERS,
            follow_redirects=False,
        )
        try:
            result = await thumbnails.fetch("https://www.tiktok.com/@user/photo/456")
            self.assertEqual(result, (b"photo-data", "image/jpeg"))
            self.assertIn("/@user/video/456", requested_paths)
        finally:
            await thumbnails.close()

if __name__ == '__main__': unittest.main()
