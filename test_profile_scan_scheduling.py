import pytest

import vsco_bot


@pytest.mark.asyncio
async def test_maybe_schedule_profile_scans_runs_even_with_existing_media(monkeypatch):
    scheduled_jobs = []

    async def fake_enqueue(job):
        scheduled_jobs.append(job)
        return True

    monkeypatch.setattr(vsco_bot, "_enqueue_profile_scan", fake_enqueue)

    records = [
        {
            "url": "https://vsco.co/example/gallery",
            "username": "example",
            "image_url": "https://images.example.com/media1.jpg",
        }
    ]

    await vsco_bot._maybe_schedule_profile_scans(  # pylint: disable=protected-access
        chat_id=123,
        records=records,
        new_links=["https://vsco.co/example/gallery"],
        added_by="tester",
        source="unit-test",
    )

    assert len(scheduled_jobs) == 1
    assert scheduled_jobs[0].username == "example"
    assert scheduled_jobs[0].profile_url == "https://vsco.co/example/gallery"
