import asyncio
from unittest.mock import AsyncMock

import pytest

import network_diagnostics as diagnostics


@pytest.mark.parametrize("data", [{}, {"ip": "bad"}, {"success": False, "ip": "1.1.1.1"}])
def test_invalid_ip_service_response_is_not_reported_as_success(data):
    with pytest.raises(ValueError):
        diagnostics.describe_response(data)


def test_ip_report_escapes_country():
    assert diagnostics.describe_response({"ip": "2001:db8::1", "country": "<test>"}) == (
        "IP: 2001:db8::1; страна: &lt;test&gt;")


def test_probe_error_does_not_leak_proxy_credentials():
    operation = AsyncMock(side_effect=RuntimeError("https://user:secret@proxy.example"))
    result = asyncio.run(diagnostics._probe("HTTP", operation))
    assert "RuntimeError" in result and "secret" not in result


def test_report_keeps_separate_transport_results(monkeypatch):
    monkeypatch.setattr(diagnostics, "http_report", AsyncMock(return_value="HTTP: 1.1.1.1"))
    monkeypatch.setattr(diagnostics, "browser_report", AsyncMock(return_value=[
        "Firefox: 2.2.2.2", "context.request: 3.3.3.3"]))
    report = asyncio.run(diagnostics.startup_network_report("10.0.0.1"))
    for value in ("10.0.0.1", "1.1.1.1", "2.2.2.2", "3.3.3.3", "trust_env=False"):
        assert value in report


def test_startup_report_failure_still_notifies_admin(monkeypatch):
    import vsco_bot
    monkeypatch.setattr(vsco_bot, "BOT_ADMIN_IDS", {123})
    monkeypatch.setattr(vsco_bot, "ARCHIVE_ADMIN_CHANNEL_ID", None)
    monkeypatch.setattr(vsco_bot, "startup_network_report", AsyncMock(side_effect=TimeoutError()))
    sender = AsyncMock()
    monkeypatch.setattr(vsco_bot.bot, "send_message", sender)
    asyncio.run(vsco_bot._notify_startup_ip("10.0.0.1"))
    assert sender.call_args.args[0] == 123
    assert "TimeoutError" in sender.call_args.args[1]


def test_startup_report_goes_to_configured_admin_targets(monkeypatch):
    import vsco_bot
    monkeypatch.setattr(vsco_bot, "BOT_ADMIN_IDS", {123, 456})
    monkeypatch.setattr(vsco_bot, "ARCHIVE_ADMIN_CHANNEL_ID", 789)
    monkeypatch.setattr(vsco_bot, "startup_network_report", AsyncMock(return_value="Network report"))
    sender = AsyncMock()
    monkeypatch.setattr(vsco_bot.bot, "send_message", sender)
    asyncio.run(vsco_bot._notify_startup_ip("10.0.0.1"))
    assert {call.args[0] for call in sender.call_args_list} == {123, 456, 789}
    assert all(call.args[1] == "Network report" for call in sender.call_args_list)
