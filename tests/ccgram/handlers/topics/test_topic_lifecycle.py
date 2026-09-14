import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Bot
from telegram.error import BadRequest

from ccgram.window_view import WindowView

from ccgram.handlers.topics.topic_lifecycle import (
    _reset_probe_confirmations_for_testing,
    check_autoclose_timers,
    check_unbound_window_ttl,
    classify_probe_error,
    probe_topic_existence,
    prune_stale_state,
)
from ccgram.handlers.polling.polling_state import (
    lifecycle_strategy,
    terminal_poll_state,
)


@pytest.fixture(autouse=True)
def _clean_strategy_state():
    terminal_poll_state._states.clear()
    lifecycle_strategy._states.clear()
    lifecycle_strategy._dead_notified.clear()
    _reset_probe_confirmations_for_testing()
    yield
    terminal_poll_state._states.clear()
    lifecycle_strategy._states.clear()
    lifecycle_strategy._dead_notified.clear()
    _reset_probe_confirmations_for_testing()


class TestCheckAutocloseTimers:
    async def test_no_topics_is_noop(self):
        bot = AsyncMock(spec=Bot)
        await check_autoclose_timers(bot)
        bot.delete_forum_topic.assert_not_called()

    async def test_expired_done_topic_gets_closed(self):
        bot = AsyncMock(spec=Bot)
        bot.delete_forum_topic = AsyncMock()
        user_id, thread_id = 1, 100
        lifecycle_strategy.start_autoclose_timer(
            user_id, thread_id, "done", time.monotonic() - 99999
        )
        with (
            patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.clear_topic_state",
                new_callable=AsyncMock,
            ),
        ):
            mock_config.autoclose_done_minutes = 1
            mock_router.resolve_chat_id.return_value = 42
            mock_router.get_window_for_thread.return_value = "@0"
            await check_autoclose_timers(bot)
        bot.delete_forum_topic.assert_called_once()

    async def test_not_yet_expired_topic_stays(self):
        bot = AsyncMock(spec=Bot)
        user_id, thread_id = 1, 100
        lifecycle_strategy.start_autoclose_timer(
            user_id, thread_id, "done", time.monotonic()
        )
        with patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config:
            mock_config.autoclose_done_minutes = 60
            await check_autoclose_timers(bot)
        bot.delete_forum_topic.assert_not_called()

    async def test_expired_dead_topic_with_live_window_is_not_deleted(self):
        # Defense-in-depth: a "dead" autoclose must re-verify the window is gone
        # before deleting the topic. If the window is alive (false dead), cancel
        # the autoclose + clear the dead mark instead of destroying the session.
        bot = AsyncMock(spec=Bot)
        bot.delete_forum_topic = AsyncMock()
        user_id, thread_id = 1, 100
        lifecycle_strategy.start_autoclose_timer(
            user_id, thread_id, "dead", time.monotonic() - 99999
        )
        lifecycle_strategy.mark_dead_notified(user_id, thread_id, "@0")
        with (
            patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch("ccgram.handlers.topics.topic_lifecycle.tmux_manager") as mock_tm,
        ):
            mock_config.autoclose_dead_minutes = 1
            mock_router.get_window_for_thread.return_value = "@0"
            mock_tm.find_window_by_id = AsyncMock(return_value=MagicMock())  # alive
            mock_tm.window_exists = AsyncMock(return_value=True)
            await check_autoclose_timers(bot)
        bot.delete_forum_topic.assert_not_called()
        assert lifecycle_strategy.is_dead_notified(user_id, thread_id, "@0") is False

    async def test_expired_dead_topic_with_gone_window_is_deleted(self):
        bot = AsyncMock(spec=Bot)
        bot.delete_forum_topic = AsyncMock()
        user_id, thread_id = 1, 100
        lifecycle_strategy.start_autoclose_timer(
            user_id, thread_id, "dead", time.monotonic() - 99999
        )
        with (
            patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch("ccgram.handlers.topics.topic_lifecycle.tmux_manager") as mock_tm,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.clear_topic_state",
                new_callable=AsyncMock,
            ),
        ):
            mock_config.autoclose_dead_minutes = 1
            mock_router.resolve_chat_id.return_value = 42
            mock_router.get_window_for_thread.return_value = "@0"
            mock_tm.find_window_by_id = AsyncMock(return_value=None)  # really gone
            mock_tm.window_exists = AsyncMock(return_value=False)
            await check_autoclose_timers(bot)
        bot.delete_forum_topic.assert_called_once()


def _window_view(origin: str) -> WindowView:
    return WindowView(
        window_id="@0",
        cwd="/tmp",
        provider_name="claude",
        approval_mode="normal",
        batch_mode="batched",
        tool_call_visibility="default",
        transcript_path=None,
        window_name="test",
        session_id="s1",
        external=False,
        origin=origin,
    )


class TestCheckUnboundWindowTtl:
    async def test_no_timeout_is_noop(self):
        with patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config:
            mock_config.autoclose_done_minutes = 0
            await check_unbound_window_ttl([])

    async def test_bound_window_timer_cleared(self):
        ws = terminal_poll_state.get_state("@0")
        ws.unbound_timer = time.monotonic() - 100
        mock_window = MagicMock(window_id="@0", window_name="test")
        with (
            patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
        ):
            mock_config.autoclose_done_minutes = 1
            mock_router.iter_thread_bindings.return_value = [(1, 100, "@0")]
            await check_unbound_window_ttl([mock_window])
        assert ws.unbound_timer is None

    async def test_manual_unbound_window_is_not_killed(self):
        ws = terminal_poll_state.get_state("@0")
        ws.unbound_timer = time.monotonic() - 100
        mock_window = MagicMock(window_id="@0", window_name="test")
        with (
            patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch("ccgram.handlers.topics.topic_lifecycle.window_query") as mock_wq,
            patch("ccgram.handlers.topics.topic_lifecycle.tmux_manager") as mock_tmux,
        ):
            mock_config.autoclose_done_minutes = 1
            mock_router.iter_thread_bindings.return_value = []
            mock_wq.view_window.return_value = _window_view("manual_discovered")
            mock_tmux.kill_window = AsyncMock()
            await check_unbound_window_ttl([mock_window])
        assert ws.unbound_timer is None
        mock_tmux.kill_window.assert_not_called()

    async def test_ccgram_created_unbound_window_is_killed_after_ttl(self):
        ws = terminal_poll_state.get_state("@0")
        ws.unbound_timer = time.monotonic() - 100
        mock_window = MagicMock(window_id="@0", window_name="test")
        with (
            patch("ccgram.handlers.topics.topic_lifecycle.config") as mock_config,
            patch(
                "ccgram.handlers.topics.topic_lifecycle.thread_router"
            ) as mock_router,
            patch("ccgram.handlers.topics.topic_lifecycle.window_query") as mock_wq,
            patch("ccgram.handlers.topics.topic_lifecycle.tmux_manager") as mock_tmux,
        ):
            mock_config.autoclose_done_minutes = 1
            mock_router.iter_thread_bindings.return_value = []
            mock_wq.view_window.return_value = _window_view("ccgram_created")
            mock_tmux.kill_window = AsyncMock()
            await check_unbound_window_ttl([mock_window])
        mock_tmux.kill_window.assert_called_once_with("@0")


class TestPruneStaleState:
    async def test_syncs_display_names(self):
        mock_window = MagicMock(window_id="@0", window_name="test")
        with patch("ccgram.handlers.topics.topic_lifecycle.session_manager") as mock_sm:
            await prune_stale_state([mock_window])
            mock_sm.sync_display_names.assert_called_once_with([("@0", "test")])
            mock_sm.prune_stale_state.assert_called_once_with({"@0"})


class TestClassifyProbeError:
    @pytest.mark.parametrize(
        ("error_message", "expected"),
        [
            ("REACTION_EMPTY", "alive"),
            ("Bad Request: REACTION_EMPTY", "alive"),
            ("message to react not found", "deleted"),
            ("Bad Request: message to react not found", "deleted"),
            ("Topic_id_invalid", "deleted"),
            ("Message thread not found", "deleted"),
            ("Too Many Requests: retry after 5", "unknown"),
            ("Timed out", "unknown"),
            ("", "unknown"),
        ],
    )
    def test_classification(self, error_message: str, expected: str) -> None:
        assert classify_probe_error(error_message) == expected


def _probe_mocks():
    mock_router = patch("ccgram.handlers.topics.topic_lifecycle.thread_router")
    mock_tmux = patch("ccgram.handlers.topics.topic_lifecycle.tmux_manager")
    mock_wq = patch("ccgram.handlers.topics.topic_lifecycle.window_query")
    mock_cts = patch(
        "ccgram.handlers.topics.topic_lifecycle.clear_topic_state",
        new_callable=AsyncMock,
    )
    return mock_router, mock_tmux, mock_wq, mock_cts


class TestProbeTopicExistence:
    async def test_deleted_topic_unbinds_after_two_confirmations(self):
        bot = AsyncMock(spec=Bot)
        bot.set_message_reaction = AsyncMock(
            side_effect=BadRequest("message to react not found")
        )
        r, t, w, c = _probe_mocks()
        with r as mock_router, t as mock_tmux, w as mock_wq, c:
            mock_router.iter_thread_bindings.return_value = [(1, 100, "@0")]
            mock_router.resolve_chat_id.return_value = 42
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_id="@0")
            )
            mock_tmux.window_exists = AsyncMock(return_value=True)
            mock_wq.view_window.return_value = _window_view("manual_discovered")
            mock_tmux.kill_window = AsyncMock()

            await probe_topic_existence(bot)
            mock_router.unbind_thread.assert_not_called()

            await probe_topic_existence(bot)
            mock_router.unbind_thread.assert_called_once_with(1, 100)
            mock_tmux.kill_window.assert_not_called()

    async def test_deleted_topic_kills_ccgram_window(self):
        bot = AsyncMock(spec=Bot)
        bot.set_message_reaction = AsyncMock(
            side_effect=BadRequest("message to react not found")
        )
        r, t, w, c = _probe_mocks()
        with r as mock_router, t as mock_tmux, w as mock_wq, c:
            mock_router.iter_thread_bindings.return_value = [(1, 100, "@0")]
            mock_router.resolve_chat_id.return_value = 42
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_id="@0")
            )
            mock_tmux.window_exists = AsyncMock(return_value=True)
            mock_wq.view_window.return_value = _window_view("ccgram_created")
            mock_tmux.kill_window = AsyncMock()

            await probe_topic_existence(bot)
            await probe_topic_existence(bot)
            mock_tmux.kill_window.assert_called_once_with("@0")
            mock_router.unbind_thread.assert_called_once_with(1, 100)

    async def test_alive_reaction_empty_resets_confirmations(self):
        bot = AsyncMock(spec=Bot)
        bot.set_message_reaction = AsyncMock(
            side_effect=[
                BadRequest("message to react not found"),
                BadRequest("REACTION_EMPTY"),
                BadRequest("message to react not found"),
            ]
        )
        r, t, w, c = _probe_mocks()
        with r as mock_router, t as mock_tmux, w as mock_wq, c:
            mock_router.iter_thread_bindings.return_value = [(1, 100, "@0")]
            mock_router.resolve_chat_id.return_value = 42
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_id="@0")
            )
            mock_tmux.window_exists = AsyncMock(return_value=True)
            mock_wq.view_window.return_value = _window_view("ccgram_created")
            mock_tmux.kill_window = AsyncMock()

            await probe_topic_existence(bot)
            await probe_topic_existence(bot)
            await probe_topic_existence(bot)
            mock_router.unbind_thread.assert_not_called()
            mock_tmux.kill_window.assert_not_called()

    async def test_unknown_error_records_probe_failure_not_deletion(self):
        bot = AsyncMock(spec=Bot)
        bot.set_message_reaction = AsyncMock(
            side_effect=BadRequest("Too Many Requests: retry after 5")
        )
        r, t, w, c = _probe_mocks()
        with r as mock_router, t as mock_tmux, w, c:
            mock_router.iter_thread_bindings.return_value = [(1, 100, "@0")]
            mock_router.resolve_chat_id.return_value = 42
            mock_tmux.kill_window = AsyncMock()

            await probe_topic_existence(bot)
            await probe_topic_existence(bot)
            await probe_topic_existence(bot)
            mock_router.unbind_thread.assert_not_called()
            mock_tmux.kill_window.assert_not_called()
        assert terminal_poll_state.get_state("@0").probe_failures == 3

    async def test_suspended_probe_skipped(self):
        bot = AsyncMock(spec=Bot)
        ws = terminal_poll_state.get_state("@0")
        ws.probe_failures = 999
        with patch(
            "ccgram.handlers.topics.topic_lifecycle.thread_router"
        ) as mock_router:
            mock_router.iter_thread_bindings.return_value = [(1, 100, "@0")]
            await probe_topic_existence(bot)
        bot.set_message_reaction.assert_not_called()
