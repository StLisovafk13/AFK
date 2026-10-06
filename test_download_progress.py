import asyncio
import logging
import importlib
import sys
from types import SimpleNamespace

import pytest

from download_progress import diagnostic_hint, parse_progress, progress_details, run_step
from vsco_downloader import download_all_via_context, collect_image_urls


def events(caplog):
    return [event for record in caplog.records if (event := parse_progress(record.getMessage()))]


def test_progress_parser_and_html():
    event = parse_progress('2026 | INFO | progress_event {"stage":"download",'
                           '"detail":"HTTP <403>","total":2,"completed":2,'
                           '"downloaded":1,"failed":1,"bytes":1048576}')
    text = progress_details(event, 42, 31)
    assert 'HTTP &lt;403&gt;' in text
    assert '2/2' in text and 'ошибок: 1' in text and '1.0 МБ' in text
    assert 'Нет новых событий 31 с' in text
    for line in ['progress_event invalid', 'progress_event []',
                 'progress_event {"stage":"download","detail":"test","total":-1}',
                 'progress_event {"stage":"download","detail":"test","downloaded":true}']:
        assert parse_progress(line) is None


def test_certificate_error_explains_trust_configuration():
    hint = diagnostic_hint('profile', 'Page.goto: SEC_ERROR_UNKNOWN_ISSUER')
    assert 'сертификату CA прокси' in hint
    assert 'TLS не отключайте' in hint


def test_waiting_step_reports_then_times_out(caplog):
    async def check():
        logger = logging.getLogger('test.wait')
        cancelled = asyncio.Event()

        async def blocked():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with pytest.raises(TimeoutError):
            await run_step(logger, 'browser', 'Запуск Firefox', blocked(), .03, heartbeat=.01)
        assert cancelled.is_set()
        before = len(caplog.records)
        await asyncio.sleep(.03)
        assert len(caplog.records) == before  # no orphan heartbeat task

    caplog.set_level(logging.INFO)
    asyncio.run(check())
    details = [event['detail'] for event in events(caplog)]
    assert any('ожидание' in detail for detail in details)
    assert 'тайм-аут' in details[-1]


def test_download_progress_counts_retries_and_releases_responses(tmp_path, caplog, monkeypatch):
    class Response:
        def __init__(self, status):
            self.status = status
            self.disposed = False

        async def body(self):
            return b'x' * 1024

        async def dispose(self):
            self.disposed = True

    class Request:
        def __init__(self):
            self.calls = {}
            self.responses = []

        async def get(self, url, **kwargs):
            self.calls[url] = self.calls.get(url, 0) + 1
            status = 403 if 'blocked' in url else 429 if self.calls[url] == 1 else 200
            response = Response(status)
            self.responses.append(response)
            return response

    # Avoid retry backoff in this deterministic transport test.
    async def no_wait(seconds):
        pass

    monkeypatch.setattr('vsco_downloader.asyncio.sleep', no_wait)
    caplog.set_level(logging.INFO)
    request = Request()
    results = asyncio.run(download_all_via_context(logging.getLogger('test.download'), request,
        ['https://cdn.example/retry.jpg', 'https://cdn.example/blocked.jpg'], tmp_path,
        'https://vsco.co/user/gallery', 2, 1, 0, {}))
    assert request.calls == {'https://cdn.example/retry.jpg': 2, 'https://cdn.example/blocked.jpg': 3}
    assert all(response.disposed for response in request.responses)
    assert sum(item['ok'] for item in results) == 1
    assert 'error' not in next(item for item in results if item['ok'])
    assert 'HTTP 403' in next(item for item in results if not item['ok'])['error']
    progress = events(caplog)
    assert any('уменьшите --concurrency' in event['detail'] for event in progress)
    assert any('cookies' in event['detail'] for event in progress)
    final = progress[-1]
    assert (final['total'], final['completed'], final['downloaded'], final['failed'], final['bytes']) == (2, 2, 1, 1, 1024)


def test_scan_reports_stagnation_and_stop_reason(caplog):
    class Button:
        @property
        def first(self):
            return self

        async def count(self):
            return 0

    class Page:
        url = 'https://vsco.co/user/gallery'

        async def content(self):
            return '<img src="https://cdn.example/photo.jpg">'

        async def evaluate(self, script):
            return 100

        async def wait_for_timeout(self, milliseconds):
            pass

        def locator(self, selector):
            return Button()

    caplog.set_level(logging.INFO)
    urls = asyncio.run(collect_image_urls(Page(), logging.getLogger('test.scan'), 0, 1, 0, 2048))
    assert len(urls) == 1
    progress = events(caplog)
    assert len(progress) == 5
    assert 'без роста: 5/5' in progress[-1]['detail']
    assert 'кнопка: недоступна' in progress[-1]['detail']
    assert any('конец ленты' in record.getMessage() for record in caplog.records)


def test_bot_captures_downloader_errors_without_sending_stale_artifacts(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', '123:TESTTOKEN')
    monkeypatch.setenv('BOT_DB_PATH', str(tmp_path / 'test.db'))
    monkeypatch.setenv('BOT_WORKDIR', str(tmp_path / 'work'))
    monkeypatch.setenv('BOT_LOGDIR', str(tmp_path / 'logs'))
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED', '1')
    sys.modules.pop('vsco_bot', None)
    sys.modules.pop('zip_profile', None)
    module = importlib.import_module('vsco_bot')
    original_bot = module.bot
    replies = []
    edits = []

    class Bot:
        async def send_message(self, chat_id, text, **kwargs):
            replies.append(text)
            return SimpleNamespace(message_id=10)

        async def edit_message_text(self, **kwargs):
            edits.append(kwargs['text'])

        async def send_document(self, *args, **kwargs):
            pytest.fail('Failed downloader must not send old artifacts')

    async def check():
        reader = asyncio.StreamReader()
        reader.feed_data(b'2026 | WARNING | HTTP 403\n')
        reader.feed_data(('progress_event {"stage":"download","detail":"CDN <403>",'
                          '"total":2,"completed":2,"downloaded":1,"failed":1,"bytes":1024}\n').encode())
        reader.feed_eof()

        async def wait():
            return 5

        async def create_process(*args, **kwargs):
            return SimpleNamespace(stdout=reader, wait=wait)

        monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', create_process)
        monkeypatch.setattr(module, 'bot', Bot())
        queue = asyncio.Queue()
        monkeypatch.setattr(module, '_DL_QUEUE', queue)
        out = tmp_path / 'old-output'
        (out / 'user').mkdir(parents=True)
        (out / 'user' / 'old.zip').write_bytes(b'old archive')
        await queue.put(module.DLJob(1, 123, 'https://vsco.co/user?x=<test>', [], out))
        task = asyncio.create_task(module._dl_worker())
        try:
            await asyncio.wait_for(queue.join(), 5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await original_bot.session.close()

    caplog.set_level(logging.INFO)
    asyncio.run(check())
    assert len(replies) == 1  # initial status only, no false success message
    assert '&lt;test&gt;' in replies[0]
    assert 'кодом 5' in edits[-1] and 'CDN &lt;403&gt;' in edits[-1]
    assert 'ошибок: 1' in edits[-1]
    assert 'Job #1 downloader: 2026 | WARNING | HTTP 403' in (tmp_path / 'logs' / 'vsco-bot.log').read_text()
