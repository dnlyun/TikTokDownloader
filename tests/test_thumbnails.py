import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from thumbnails import PreviewParser, safe_image

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

if __name__ == '__main__': unittest.main()
