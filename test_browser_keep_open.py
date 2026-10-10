import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from browser_config import managed_async_browser, managed_sync_browser


class BrowserKeepOpenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.page = MagicMock()
        self.page.is_closed.return_value = False
        self.browser = MagicMock()
        self.browser.contexts = [SimpleNamespace(pages=[self.page])]
        self.browser.is_connected.return_value = True
        self.browser.close = AsyncMock()
        self.driver = MagicMock()
        self.launch = AsyncMock(return_value=self.browser)
        self.driver.__aenter__.return_value = SimpleNamespace(firefox=SimpleNamespace(launch=self.launch))
        for target, value in (("browser_config.browser_headless", False), ("browser_config.browser_keep_open", True)):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("playwright.async_api.async_playwright", return_value=self.driver)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def close_window(self, delay):
        self.browser.close.assert_not_awaited()
        self.driver.__aexit__.assert_not_awaited()
        self.page.is_closed.return_value = True

    async def test_success_waits_before_closing_driver(self):
        with patch("browser_config.asyncio.sleep", new=AsyncMock(side_effect=self.close_window)) as wait:
            async with managed_async_browser() as browser:
                self.assertIs(browser, self.browser)
            wait.assert_awaited_once()
        self.browser.close.assert_awaited_once()
        self.driver.__aexit__.assert_awaited_once()

    async def test_error_keeps_window_and_preserves_exception(self):
        error = ValueError("navigation failed")
        with patch("browser_config.asyncio.sleep", new=AsyncMock(side_effect=self.close_window)) as wait:
            with self.assertLogs("browser_config", level="ERROR"):
                with self.assertRaises(ValueError) as raised:
                    async with managed_async_browser():
                        raise error
            self.assertIs(raised.exception, error)
            wait.assert_awaited_once()
        self.browser.close.assert_awaited_once()

    async def test_disabled_or_headless_never_waits(self):
        for headless, keep in ((False, False), (True, True)):
            with patch("browser_config.browser_headless", return_value=headless), patch("browser_config.browser_keep_open", return_value=keep):
                with patch("browser_config.asyncio.sleep", new_callable=AsyncMock) as wait:
                    async with managed_async_browser():
                        pass
                    wait.assert_not_awaited()

    async def test_cancellation_during_task_closes_immediately(self):
        with patch("browser_config.asyncio.sleep", new_callable=AsyncMock) as wait:
            with self.assertRaises(asyncio.CancelledError):
                async with managed_async_browser():
                    raise asyncio.CancelledError()
            wait.assert_not_awaited()
        self.browser.close.assert_awaited_once()

    async def test_cancellation_during_wait_still_closes(self):
        with patch("browser_config.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                async with managed_async_browser():
                    pass
        self.browser.close.assert_awaited_once()

    async def test_already_closed_window_does_not_wait(self):
        self.page.is_closed.return_value = True
        with patch("browser_config.asyncio.sleep", new_callable=AsyncMock) as wait:
            async with managed_async_browser():
                pass
            wait.assert_not_awaited()

    async def test_launch_failure_does_not_wait(self):
        self.launch.side_effect = RuntimeError("browser missing")
        with patch("browser_config.asyncio.sleep", new_callable=AsyncMock) as wait:
            with self.assertRaises(RuntimeError):
                async with managed_async_browser():
                    self.fail("Launch must fail")
            wait.assert_not_awaited()


class SyncBrowserKeepOpenTests(unittest.TestCase):
    def test_success_and_error_pump_events_before_driver_closes(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                page = MagicMock()
                page.is_closed.return_value = False
                browser = MagicMock()
                browser.contexts = [SimpleNamespace(pages=[page])]
                browser.is_connected.return_value = True
                driver = MagicMock()
                driver.__enter__.return_value.firefox.launch.return_value = browser

                def close_window(delay):
                    browser.close.assert_not_called()
                    driver.__exit__.assert_not_called()
                    page.is_closed.return_value = True

                page.wait_for_timeout.side_effect = close_window
                with patch("playwright.sync_api.sync_playwright", return_value=driver), patch("browser_config.browser_headless", return_value=False), patch("browser_config.browser_keep_open", return_value=True):
                    if fail:
                        with self.assertLogs("browser_config", level="ERROR"), self.assertRaises(ValueError):
                            with managed_sync_browser():
                                raise ValueError("request failed")
                    else:
                        with managed_sync_browser():
                            pass
                page.wait_for_timeout.assert_called_once_with(250)
                browser.close.assert_called_once()
                driver.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
