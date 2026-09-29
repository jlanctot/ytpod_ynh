import os
import tempfile
import unittest
from pathlib import Path


class YTPodTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["YTPOD_CONFIG"] = str(Path(cls.tmp.name) / "feeds.toml")
        os.environ["YTPOD_DATA"] = str(Path(cls.tmp.name) / "data")
        os.environ["YTPOD_BASE_URL"] = "https://pod.example.test/ytpod"
        os.environ["YTPOD_YTDLP"] = "/bin/false"
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        import ytpod
        cls.app = ytpod

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_normalize_feed_generates_token(self):
        feed = self.app.normalize_feed({
            "id": "news-1",
            "name": "News",
            "source": "https://www.youtube.com/@example/videos",
            "keep_last": 20,
        })
        self.assertEqual(feed["id"], "news-1")
        self.assertGreaterEqual(len(feed["token"]), 24)

    def test_media_url(self):
        raw = self.app.normalize_feed({
            "id": "news",
            "name": "News",
            "source": "https://www.youtube.com/playlist?list=abc",
            "token": "A" * 32,
        })
        feed = self.app.Feed(**raw)
        url = self.app.media_url(feed, "news-abc_123.m4a")
        self.assertEqual(url, "https://pod.example.test/ytpod/media/news/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/news-abc_123.m4a")

    def test_rss_contains_podcast_enclosure(self):
        raw = self.app.normalize_feed({
            "id": "news",
            "name": "The <News>",
            "source": "https://www.youtube.com/@example/videos",
            "token": "B" * 32,
        })
        feed = self.app.Feed(**raw)
        self.app.ensure_directories()
        self.app.save_state("news", {
            "items": {
                "abc_123": {
                    "id": "abc_123",
                    "title": "A & B",
                    "description": "Desc",
                    "published": "2026-01-02T03:04:05+00:00",
                    "duration": 61,
                    "file": "news-abc_123.m4a",
                    "size": 12345,
                }
            }
        })
        rss = self.app.build_rss(feed)
        self.assertIn("audio/mp4", rss)
        self.assertIn("news-abc_123.m4a", rss)
        self.assertIn("A &amp; B", rss)
        self.assertIn("The &lt;News&gt;", rss)

    def test_csrf(self):
        token = self.app.csrf_token("admin")
        self.assertTrue(self.app.valid_csrf("admin", token))
        self.assertFalse(self.app.valid_csrf("admin2", token))


if __name__ == "__main__":
    unittest.main()
