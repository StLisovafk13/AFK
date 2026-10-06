"""Progress events shared by the downloader and its Telegram worker."""

import asyncio
import contextlib
import json
import time
from html import escape

PREFIX = "progress_event "
STAGES = {"browser", "profile", "scan", "probe", "download", "archive", "finish"}


def emit_progress(logger, stage, detail, **counts):
    logger.info("%s%s", PREFIX, json.dumps(
        {"stage": stage, "detail": detail, **counts}, ensure_ascii=False))


def parse_progress(line):
    _, separator, payload = line.partition(PREFIX)
    if not separator:
        return None
    try:
        event = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict) or event.get("stage") not in STAGES:
        return None
    if not isinstance(event.get("detail"), str):
        return None
    for key in ("total", "downloaded", "failed", "bytes", "completed", "zip_parts"):
        if key in event and (type(event[key]) is not int or event[key] < 0):
            return None
    return event


def progress_details(event, elapsed, silent_for):
    lines = [f"⏱ Прошло: {int(elapsed)} с"]
    if event:
        lines.insert(0, escape(event["detail"][:500]))
        if "completed" in event and "total" in event:
            lines.append(f"Обработано: {event['completed']}/{event['total']}; "
                         f"успешно: {event.get('downloaded', 0)}; ошибок: {event.get('failed', 0)}")
        if "bytes" in event:
            lines.append(f"Сохранено: {event['bytes'] / 1048576:.1f} МБ")
    if silent_for >= 30:
        lines.append(f"⚠️ Нет новых событий {int(silent_for)} с; этап ещё выполняется.")
    return "\n".join(lines)


def diagnostic_hint(stage, error):
    if any(marker in error for marker in ('SEC_ERROR_UNKNOWN_ISSUER', 'CERTIFICATE_VERIFY_FAILED',
                                          'ERR_CERT_AUTHORITY_INVALID')):
        return 'Настройте доверие к сертификату CA прокси в браузере; проверку TLS не отключайте.'
    if 'profile folder' in error:
        return 'Проверьте доступ Firefox к каталогу профиля и ограничения файловой песочницы.'
    if stage == 'browser':
        return 'Проверьте запуск Firefox, доступ к временному каталогу и ограничения среды.'
    return 'Проверьте сеть, доступ к VSCO/CDN и установленный тайм-аут.'


async def run_step(logger, stage, detail, operation, timeout, *, heartbeat=10):
    """Report a blocking async operation and bound how long it may wait."""
    started = time.monotonic()
    emit_progress(logger, stage, detail)

    async def report_wait():
        while True:
            await asyncio.sleep(heartbeat)
            emit_progress(logger, stage, f"{detail}: ожидание {int(time.monotonic() - started)} с "
                          f"(лимит {timeout:g} с)")

    reporter = asyncio.create_task(report_wait())
    try:
        return await asyncio.wait_for(operation, timeout=timeout)
    except Exception as exc:
        reason = "истёк тайм-аут" if isinstance(exc, TimeoutError) else type(exc).__name__
        detail = f"{detail}: {reason}. {diagnostic_hint(stage, str(exc))} {str(exc)[:300]}"
        logger.error(detail)
        emit_progress(logger, stage, detail)
        raise
    finally:
        reporter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reporter
