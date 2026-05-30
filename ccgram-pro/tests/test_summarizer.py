from __future__ import annotations

from typing import Any

from ccgram_pro.output_pipeline import summarizer


class _StubClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


async def test_post_summary_attaches_view_diff_and_settings_buttons() -> None:
    client = _StubClient()
    await summarizer._post_summary_message(
        client=client,
        chat_id=1,
        thread_id=2,
        summary="done",
        link_url="https://x/view/t",
        diff_url="https://x/diff/t",
        window_id="@5",
    )
    assert len(client.calls) == 1
    markup = client.calls[0]["reply_markup"]
    flat = [b for row in markup.inline_keyboard for b in row]
    urls = [b.url for b in flat if b.url]
    cbs = [b.callback_data for b in flat if b.callback_data]
    assert "https://x/view/t" in urls
    assert "https://x/diff/t" in urls
    assert any(c.startswith("ccgrampro:set:open:@5") for c in cbs)


async def test_post_summary_no_settings_without_window_id() -> None:
    client = _StubClient()
    await summarizer._post_summary_message(
        client=client,
        chat_id=1,
        thread_id=2,
        summary="done",
        link_url="https://x/view/t",
        diff_url=None,
        window_id=None,
    )
    markup = client.calls[0]["reply_markup"]
    cbs = [
        b.callback_data
        for row in markup.inline_keyboard
        for b in row
        if b.callback_data
    ]
    assert cbs == []


def test_no_llm_summarizer_reference() -> None:
    import inspect

    src = inspect.getsource(summarizer)
    assert "summarize_completion" not in src
    assert "_safe_llm_summary" not in src
