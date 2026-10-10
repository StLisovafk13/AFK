import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from gallery_diagnostics import GalleryLoadError, GalleryLoadMonitor
from vsco_downloader import collect_image_urls


class GalleryDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.logger = MagicMock()
        self.monitor = GalleryLoadMonitor(self.logger)

    def response(self, host="vsco.co", path="/api/3.0/medias/profile", status=403):
        return SimpleNamespace(
            url=f"https://{host}{path}?cursor=PRIVATE_CURSOR",
            status=status,
            headers={"cf-ray": "test-ray", "set-cookie": "PRIVATE_COOKIE"},
        )

    def test_successful_first_page_then_forbidden_cursor_is_failure(self):
        self.monitor.on_response(self.response(status=200))
        self.monitor.check("<main>Gallery</main>")
        self.monitor.on_response(self.response())
        with self.assertRaisesRegex(GalleryLoadError, "HTTP 403") as error:
            self.monitor.check("<main>First page still visible</main>")
        self.assertIn("test-ray", str(error.exception))
        self.assertNotIn("PRIVATE", str(error.exception))
        self.assertNotIn("PRIVATE", str(self.logger.mock_calls))

    def test_grpc_preflight_failure_is_reported(self):
        self.monitor.on_response(self.response("media-grpc-api.vsco.co", "/media.Media/FetchImages"))
        with self.assertRaisesRegex(GalleryLoadError, "FetchImages"):
            self.monitor.check("<main>Gallery</main>")

    def test_unrelated_telemetry_failure_is_ignored(self):
        self.monitor.on_response(self.response("cantor-lite-api.vsco.co", "/events.CantorLite/SendJavaScript"))
        self.monitor.check("<main>Gallery</main>")

    def test_content_error_and_cloudflare_block_are_not_end_of_gallery(self):
        for html in (
            '<span class="css-1guoi39 e15pim120">Error Loading Content</span>',
            '<div>Sorry, you have been blocked</div>',
        ):
            with self.subTest(html=html), self.assertRaises(GalleryLoadError):
                self.monitor.check(html)

    def test_error_strings_in_scripts_or_templates_are_ignored(self):
        self.monitor.check('''<script>const error = "Error Loading Content";</script>
            <template><span>Error Loading Content</span></template><main>Gallery</main>''')


class GalleryCollectorTests(unittest.IsolatedAsyncioTestCase):
    def page(self, second_html):
        self.first_html = self.gallery(16)
        page = MagicMock()
        page.url = "https://vsco.co/example/gallery"
        page.content = AsyncMock(side_effect=[self.first_html, second_html])
        page.evaluate = AsyncMock(return_value=100)
        page.wait_for_timeout = AsyncMock()
        page.locator.return_value.first.count = AsyncMock(return_value=0)
        return page

    def gallery(self, count):
        return "".join(f'<img src="https://im.vsco.co/example/{i}.jpg">' for i in range(count))

    async def test_first_sixteen_images_are_not_returned_as_complete_after_error(self):
        page = self.page('<span>Error Loading Content</span>')
        with self.assertRaises(GalleryLoadError):
            await collect_image_urls(page, logging.getLogger("test"), 0.4, 30, 0, 2048)
        self.assertEqual(page.content.await_count, 2)

    async def test_healthy_gallery_can_grow_beyond_sixteen(self):
        page = self.page(self.gallery(32))
        urls = await collect_image_urls(page, logging.getLogger("test"), 0.4, 30, 32, 2048)
        self.assertEqual(len(urls), 32)


if __name__ == "__main__":
    unittest.main()
