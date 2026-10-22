"""The Slack notifier — the real path behind `alerts.AlertFn`.

No network anywhere: every test injects a `PostFn` recorder, so nothing here opens
a socket or touches Slack. The properties worth pinning are the ones a caller
depends on without being able to see them: that `__call__` cannot raise, that it
does not block, and that the webhook URL never appears in full.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from argus.alerts import log_alert
from argus.notify import (
    MAX_DETAIL_CHARS,
    WEBHOOK_ENV_VAR,
    WINDOW_RETENTION_MULTIPLE,
    MissingWebhookError,
    SlackNotifier,
    alert_scope,
    build_alert_fn,
    combine,
    load_webhook_url,
    redact_webhook,
)

SECRET_PATH = "T00000000/B11111111/xxxxxxxxxxxxxxxxxxxxxxxx"
WEBHOOK = f"https://hooks.slack.com/services/{SECRET_PATH}"


class Recorder:
    """A `PostFn` that records instead of posting. Can be told to fail."""

    def __init__(self, *, fail_times: int = 0, raises: Exception | None = None) -> None:
        self.posts: list[tuple[str, str, float]] = []
        self.fail_times = fail_times
        self.raises = raises or RuntimeError("slack unreachable: [Errno 111] refused")

    def __call__(self, url: str, body: str, timeout_s: float) -> None:
        self.posts.append((url, body, timeout_s))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.raises

    @property
    def texts(self) -> list[str]:
        return [json.loads(body)["text"] for _, body, _ in self.posts]


async def drain(notifier: SlackNotifier) -> None:
    """Deliver everything queued, then stop the worker.

    `aclose()` joins the queue before cancelling, and the POST goes through
    `asyncio.to_thread`, so this is the only way to be sure a delivery has actually
    completed — spinning on `sleep(0)` never lets the worker thread finish.
    """
    await asyncio.wait_for(notifier.aclose(), timeout=2.0)


# -- the credential ------------------------------------------------------------


def test_redact_keeps_only_scheme_and_host():
    """The secret in an incoming-webhook URL is entirely in the PATH."""
    redacted = redact_webhook(WEBHOOK)
    assert redacted == "https://hooks.slack.com/<redacted>"
    assert SECRET_PATH not in redacted
    for fragment in ("T00000000", "B11111111", "xxxxxxxxxxxxxxxxxxxxxxxx"):
        assert fragment not in redacted


@pytest.mark.parametrize("bad", ["", "not a url", "://x"])
def test_redact_never_echoes_an_unparseable_url(bad: str):
    """The fallback must not fall back to printing the input."""
    assert bad not in redact_webhook(bad) or bad == ""


def test_missing_webhook_is_not_a_startup_failure():
    """Unlike the kill token, an absent webhook is a missing NOTIFICATION channel,
    not a broken safety rail — every alert still reaches the log."""
    assert load_webhook_url({}) is None
    assert load_webhook_url({WEBHOOK_ENV_VAR: "   "}) is None


def test_plaintext_webhook_is_refused():
    """An alert payload carries internal state; it does not go over http://."""
    with pytest.raises(MissingWebhookError) as exc:
        load_webhook_url({WEBHOOK_ENV_VAR: "http://hooks.slack.com/services/T/B/x"})
    assert "T/B/x" not in str(exc.value)  # not even in the rejection message


def test_build_alert_fn_falls_back_to_logging_on_a_malformed_webhook():
    alert, notifier = build_alert_fn(
        environment="shadow", env={WEBHOOK_ENV_VAR: "http://insecure/x"}
    )
    assert notifier is None
    assert alert is log_alert


def test_build_alert_fn_without_a_webhook_is_exactly_todays_behaviour():
    alert, notifier = build_alert_fn(environment="shadow", env={})
    assert notifier is None
    assert alert is log_alert


# -- never raises, never blocks ------------------------------------------------


async def test_call_is_non_blocking_and_posts_from_the_background_worker():
    post = Recorder()
    notifier = SlackNotifier(WEBHOOK, environment="sandbox", post=post)
    notifier.start()

    notifier("watchdog_triggered", {"external_identifier": "argus-open-x"})
    # Nothing has been posted yet: __call__ only enqueues. This is the property that
    # keeps a synchronous AlertFn off the event loop's critical path (spec §4).
    assert post.posts == []

    await drain(notifier)
    assert notifier.delivered == 1
    assert "[argus:sandbox] watchdog_triggered" in post.texts[0]
    assert "argus-open-x" in post.texts[0]


async def test_a_full_queue_drops_rather_than_blocking_a_caller_mid_failure():
    post = Recorder()
    # throttle_s=0 so the QUEUE BOUND is what this measures. With the default
    # window a repeated kind never reaches the queue at all, which would make this
    # pass for the wrong reason.
    notifier = SlackNotifier(
        WEBHOOK, environment="shadow", post=post, queue_maxsize=2, throttle_s=0.0
    )
    # Worker deliberately not started, so nothing drains.
    for i in range(5):
        notifier("audit_write_failed", {"i": i})
    assert notifier.dropped == 3
    assert notifier.queue_depth == 2


async def test_a_failed_post_never_raises_and_is_counted():
    post = Recorder(fail_times=1)
    # throttle_s=0: this is about the DELIVERY counters, and both messages have to
    # reach the poster for "one failed, one succeeded" to mean anything.
    notifier = SlackNotifier(WEBHOOK, environment="shadow", post=post, throttle_s=0.0)
    notifier.start()
    notifier("heartbeat_stall", {"lag_s": 0.4})
    notifier("heartbeat_stall", {"lag_s": 0.5})
    await drain(notifier)
    assert (notifier.failed, notifier.delivered) == (1, 1)


async def test_a_failed_post_logs_the_redacted_host_and_never_the_secret(caplog):
    """The failure path is where a credential leaks — it is the one place the URL is
    naturally at hand. The poster here behaves like the real one (URL-free
    exception), so this pins what the notifier itself contributes to the line."""
    post = Recorder(fail_times=1)
    notifier = SlackNotifier(WEBHOOK, environment="shadow", post=post)
    notifier.start()
    with caplog.at_level(logging.WARNING, logger="argus.notify"):
        notifier("order_foreign", {"broker_order_id": 1})
        await drain(notifier)
    lines = [r.getMessage() for r in caplog.records if "not delivered" in r.getMessage()]
    assert lines, "a failed delivery must be logged"
    assert "https://hooks.slack.com/<redacted>" in lines[0]
    assert SECRET_PATH not in lines[0]


def test_urllib_post_strips_the_url_out_of_an_http_error(monkeypatch):
    """`urllib`'s own `HTTPError.__str__` does not carry the URL, but `HTTPError`
    exposes `.url`, and any future formatting of the exception would pick it up. The
    adapter therefore raises its OWN exception carrying only the status code — this
    pins that, because it is the difference between a status line and a credential
    in the journal."""
    import urllib.error

    from argus.notify import _urllib_post

    def boom(request, timeout):
        raise urllib.error.HTTPError(WEBHOOK, 403, "Forbidden", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(RuntimeError) as exc:
        _urllib_post(WEBHOOK, "{}", 1.0)
    assert "403" in str(exc.value)
    assert SECRET_PATH not in str(exc.value)
    assert "hooks.slack.com" not in str(exc.value)
    # And nothing chained behind it can carry the URL either.
    assert exc.value.__cause__ is None and exc.value.__context__ is None


def test_urllib_post_strips_the_url_out_of_a_transport_error(monkeypatch):
    import urllib.error

    from argus.notify import _urllib_post

    def boom(request, timeout):
        raise urllib.error.URLError(f"connection to {WEBHOOK} refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(RuntimeError) as exc:
        _urllib_post(WEBHOOK, "{}", 1.0)
    # `URLError.reason` is a string this module did not write, so it is SCRUBBED
    # rather than trusted: a library that puts the URL in its own error message must
    # not thereby put a credential in the journal.
    assert SECRET_PATH not in str(exc.value)
    assert "<redacted>" in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__context__ is None


async def test_an_unserializable_detail_still_produces_an_alert():
    """The caller is already in a failure path; "could not format the alert about
    the problem" is the least useful possible outcome."""
    post = Recorder()
    notifier = SlackNotifier(WEBHOOK, environment="shadow", post=post)
    notifier.start()
    notifier("session_error", {"exc": object()})
    await drain(notifier)
    assert notifier.delivered == 1
    assert "session_error" in post.texts[0]


async def test_a_huge_detail_is_truncated():
    post = Recorder()
    notifier = SlackNotifier(WEBHOOK, environment="shadow", post=post)
    notifier.start()
    notifier("reconciliation_mismatch", {"d": ["x" * 100] * 100})
    await drain(notifier)
    assert "truncated" in post.texts[0]
    assert len(post.texts[0]) < MAX_DETAIL_CHARS + 300


async def test_aclose_is_bounded_when_slack_hangs():
    """A process must not fail to exit because a notification channel is down."""

    def hangs(url: str, body: str, timeout_s: float) -> None:
        raise RuntimeError("timed out")

    notifier = SlackNotifier(WEBHOOK, environment="shadow", post=hangs)
    notifier.start()
    notifier("x", {})
    await asyncio.wait_for(notifier.aclose(drain_timeout_s=0.05), timeout=1.0)


async def test_aclose_before_start_is_a_no_op():
    await SlackNotifier(WEBHOOK, environment="shadow", post=Recorder()).aclose()


# -- the per-kind throttle -----------------------------------------------------
#
# The regression these pin: on 2026-08-05 the first deployment of this module put
# ~700 messages an hour into Slack, because `LoopHeartbeat` flaps against a
# long-standing ~10s event-loop stall and nothing here had any rate limit at all.
# The fix must quiet a chatty kind WITHOUT delaying a first occurrence or letting
# one kind starve another — those two are what make it safe to apply to a channel
# that also carries `audit_write_failed`.


class FakeClock:
    """A monotonic clock that only moves when a test says so."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _throttled(post, clock, *, throttle_s: float = 900.0) -> SlackNotifier:
    return SlackNotifier(
        WEBHOOK, environment="shadow", post=post, throttle_s=throttle_s, clock=clock
    )


async def test_the_first_alert_of_a_kind_is_never_delayed():
    """An incident must not wait on a rate limiter."""
    post, clock = Recorder(), FakeClock()
    notifier = _throttled(post, clock)
    notifier.start()
    notifier("audit_write_failed", {"seq": 4128})
    await drain(notifier)
    assert notifier.delivered == 1
    assert notifier.suppressed == 0
    assert "audit_write_failed" in post.texts[0]


async def test_repeats_inside_the_window_are_summarized_not_discarded():
    post, clock = Recorder(), FakeClock()
    notifier = _throttled(post, clock, throttle_s=900.0)
    notifier.start()

    notifier("loop_lag_breach", {"lag_s": 0.10})   # delivered
    for i in range(58):                            # 58 repeats, ~10s apart
        clock.advance(10.0)
        notifier("loop_lag_breach", {"lag_s": 0.10 + i / 1000})
    await drain(notifier)

    # One message for the whole 580s, not 59.
    assert notifier.delivered == 1
    assert notifier.suppressed == 58
    assert notifier.dropped == 0  # suppressed is NOT lost

    # ...and the next one past the window carries the count, so the operator can
    # tell "it happened again" from "it never stopped".
    clock.advance(900.0)
    notifier.start()
    notifier("loop_lag_breach", {"lag_s": 0.17})
    await drain(notifier)
    assert notifier.delivered == 2
    assert "(+58 suppressed" in post.texts[1]


async def test_the_window_is_per_kind_so_a_storm_cannot_starve_a_rare_alert():
    """The failure mode a single global rate limit would have: the chatty kind
    holds the token and the one that matters waits behind it."""
    post, clock = Recorder(), FakeClock()
    notifier = _throttled(post, clock)
    notifier.start()

    notifier("loop_lag_breach", {"n": 0})
    for _ in range(50):
        clock.advance(1.0)
        notifier("loop_lag_breach", {"n": 1})
    notifier("reconciliation_mismatch", {"discrepancies": 1})  # different kind
    await drain(notifier)

    kinds = [t.split()[1] for t in post.texts]
    assert kinds == ["loop_lag_breach", "reconciliation_mismatch"]


async def test_a_dropped_alert_does_not_start_the_throttle_window():
    """A full queue is a real LOSS, so it must not also suppress the follow-ups to
    a message that was never sent — that would turn one dropped alert into a
    silent 15 minutes."""
    post, clock = Recorder(), FakeClock()
    notifier = SlackNotifier(
        WEBHOOK, environment="shadow", post=post, queue_maxsize=1,
        throttle_s=900.0, clock=clock,
    )
    # Worker not started, so nothing drains: the first fills the queue, the second
    # is dropped.
    notifier("order_foreign", {"i": 0})
    notifier("watchdog_fatal", {"i": 1})
    assert (notifier.dropped, notifier.suppressed) == (1, 0)

    # The dropped kind is still eligible immediately, no window advanced.
    notifier.start()
    await asyncio.sleep(0)
    notifier("watchdog_fatal", {"i": 2})
    await drain(notifier)
    assert any("watchdog_fatal" in t for t in post.texts)


async def test_throttling_is_off_when_the_window_is_zero():
    """Escape hatch, and the shape every pre-throttle test implicitly assumed."""
    post, clock = Recorder(), FakeClock()
    notifier = _throttled(post, clock, throttle_s=0.0)
    notifier.start()
    for i in range(5):
        notifier("session_error", {"i": i})
    await drain(notifier)
    assert notifier.delivered == 5
    assert notifier.suppressed == 0


# -- the throttle is per (kind, subject), not per kind (2026-08-21, B1) --------
#
# The 2026-08-05 window was calibrated against `loop_lag_breach`: ONE condition
# flapping every ten seconds, where a repeat inside the window really is the same
# condition continuing. It was then applied to every kind — including the ones
# where a repeat is a DIFFERENT POSITION. A stop-out's kind is `watchdog_triggered`
# / `watchdog_exit_filled`, with nothing per-position in it, and with
# `max_concurrent_positions = 2` both spreads are same-underlying and same-day, so
# the move that stops one out is routinely the move that stops the other out.
# The operator got a full page for the first and a bare `(+1 suppressed)` for the
# second, up to fifteen minutes later.
#
# These pin BOTH halves: distinct subjects must not suppress each other, and the
# same subject repeating must still throttle exactly as it did on 08-05.

PUT_SPREAD = "argus-STO-20260821-put-6300.0-6295.0"
CALL_SPREAD = "argus-STO-20260821-call-6400.0-6405.0"


async def test_two_positions_stopping_out_together_both_page_in_full():
    """The documented expected case, not a tail: one market move, two stop-outs,
    two full Tier 1 pages — the second must not arrive as `(+1 suppressed)`."""
    post, clock = Recorder(), FakeClock()
    notifier = _throttled(post, clock)
    notifier.start()

    notifier("watchdog_triggered", {"external_identifier": PUT_SPREAD, "spot": 6299.0})
    clock.advance(4.0)                          # same move, seconds apart
    notifier("watchdog_triggered", {"external_identifier": CALL_SPREAD, "spot": 6299.0})
    await drain(notifier)

    assert notifier.delivered == 2
    assert notifier.suppressed == 0
    # ...and the two are TELLABLE APART on a phone, where the preview is one line
    # and the kind is identical.
    assert PUT_SPREAD in post.texts[0] and CALL_SPREAD not in post.texts[0]
    assert CALL_SPREAD in post.texts[1]
    assert "suppressed" not in post.texts[1]


async def test_the_same_position_repeating_still_throttles():
    """The 08-05 fix, unregressed at the new grain: one position's condition
    continuing is still one message per window, with the count on the next."""
    post, clock = Recorder(), FakeClock()
    notifier = _throttled(post, clock)
    notifier.start()

    notifier("watchdog_exit_still_unplaced", {"external_identifier": PUT_SPREAD})
    for _ in range(30):                         # the same spread, 20s apart
        clock.advance(20.0)
        notifier("watchdog_exit_still_unplaced", {"external_identifier": PUT_SPREAD})
    await drain(notifier)
    assert notifier.delivered == 1
    assert notifier.suppressed == 30

    clock.advance(900.0)
    notifier.start()
    notifier("watchdog_exit_still_unplaced", {"external_identifier": PUT_SPREAD})
    await drain(notifier)
    assert notifier.delivered == 2
    assert "(+30 suppressed" in post.texts[1]


async def test_one_position_storming_cannot_delay_the_other_positions_first_page():
    """The per-kind failure mode, one level down: the spread that cannot be closed
    pages every cadence tick, and the OTHER spread's first page waits behind it."""
    post, clock = Recorder(), FakeClock()
    notifier = _throttled(post, clock)
    notifier.start()

    notifier("watchdog_exit_still_unplaced", {"external_identifier": PUT_SPREAD})
    for _ in range(50):
        clock.advance(1.0)
        notifier("watchdog_exit_still_unplaced", {"external_identifier": PUT_SPREAD})
    notifier("watchdog_exit_still_unplaced", {"external_identifier": CALL_SPREAD})
    await drain(notifier)

    assert notifier.delivered == 2
    assert [PUT_SPREAD in t for t in post.texts] == [True, False]
    assert CALL_SPREAD in post.texts[1]


async def test_a_kind_with_no_subject_keeps_exactly_the_old_behaviour():
    """`loop_lag_breach` is the kind the window was built for and it names no
    position — varying detail must NOT split it into separate windows, or the
    08-05 storm comes back through the fix for it."""
    post, clock = Recorder(), FakeClock()
    notifier = _throttled(post, clock)
    notifier.start()

    notifier("loop_lag_breach", {"lag_s": 0.10})
    for i in range(58):
        clock.advance(10.0)
        notifier("loop_lag_breach", {"lag_s": 0.10 + i / 1000, "n": i})
    await drain(notifier)

    assert (notifier.delivered, notifier.suppressed) == (1, 58)
    # ...and no subject tag on the header, since there is no subject to name.
    assert post.texts[0].splitlines()[0] == "[argus:shadow] loop_lag_breach"


async def test_candidate_id_scopes_an_alert_that_has_no_order_identifier_yet():
    """The entry path alerts before an order exists, so `candidate_id` is the only
    subject it can name — two candidates in one window are two events."""
    post, clock = Recorder(), FakeClock()
    notifier = _throttled(post, clock)
    notifier.start()

    notifier("entry_rejected", {"candidate_id": "20260821-put-6300.0-6295.0"})
    notifier("entry_rejected", {"candidate_id": "20260821-call-6400.0-6405.0"})
    notifier("entry_rejected", {"candidate_id": "20260821-put-6300.0-6295.0"})
    await drain(notifier)

    assert (notifier.delivered, notifier.suppressed) == (2, 1)


def test_alert_scope_prefers_the_trade_idea_over_either_order_identifier():
    """One position has TWO `external_identifier`s over its life — the opening
    order's, which `OpenSpread` carries, and the closing order's, which
    `ExitReport` carries — and watchdog payloads about the same spread contain
    both. Keying on the order would split one episode across two windows.

    `broker_order_id` is an int and both id fields are `str | None`: neither a
    `None` nor a stringified int may become a key nobody intended."""
    assert alert_scope({"external_identifier": PUT_SPREAD, "candidate_id": "x"}) == "x"
    assert alert_scope({"candidate_id": None, "external_identifier": PUT_SPREAD}) == (
        PUT_SPREAD
    )
    assert alert_scope({"candidate_id": "  ", "external_identifier": PUT_SPREAD}) == (
        PUT_SPREAD
    )
    assert alert_scope({"broker_order_id": 1234}) == ""
    assert alert_scope({"lag_s": 0.1}) == ""


async def test_a_kind_spraying_subjects_falls_back_to_coalescing_the_whole_kind():
    """The flood guard. Scoping is a concession to a small, bounded population; a
    kind that exceeds it is not reporting on positions, and the older per-kind
    behaviour is the safe degradation — noisier is not one of the options."""
    post, clock = Recorder(), FakeClock()
    notifier = SlackNotifier(
        WEBHOOK, environment="shadow", post=post, throttle_s=900.0,
        max_scopes_per_kind=3, clock=clock,
    )
    notifier.start()
    for i in range(20):
        clock.advance(1.0)
        notifier("unmappable_broker_order", {"external_identifier": f"foreign-{i}"})
    await drain(notifier)

    # 3 scoped windows + 1 unscoped for the overflow, and everything after that is
    # coalesced into the unscoped one.
    assert notifier.delivered == 4
    assert notifier.suppressed == 16
    assert notifier.tracked_windows == 4


async def test_a_position_already_paging_keeps_its_window_when_the_cap_is_hit():
    """An open episode must not be demoted mid-flight by unrelated traffic on the
    same kind — that would silence the position for the rest of the window."""
    post, clock = Recorder(), FakeClock()
    notifier = SlackNotifier(
        WEBHOOK, environment="shadow", post=post, throttle_s=900.0,
        max_scopes_per_kind=2, clock=clock,
    )
    notifier.start()
    notifier("watchdog_exit_still_unplaced", {"external_identifier": PUT_SPREAD})
    notifier("watchdog_exit_still_unplaced", {"external_identifier": CALL_SPREAD})
    for i in range(5):                          # cap reached; these coalesce
        clock.advance(1.0)
        notifier("watchdog_exit_still_unplaced", {"external_identifier": f"other-{i}"})
    clock.advance(900.0)                        # the put's window reopens
    notifier("watchdog_exit_still_unplaced", {"external_identifier": PUT_SPREAD})
    await drain(notifier)

    # Two full pages for the put, 900s apart — not one page and then silence
    # because five unrelated subjects arrived on the same kind in between.
    assert len([t for t in post.texts if PUT_SPREAD in t]) == 2


# -- bounding the throttle state ----------------------------------------------


async def test_a_pending_count_still_rides_on_a_message_one_window_later():
    """A condition that flaps just outside the window is the common case, so the
    retention has to outlast the window itself."""
    post, clock = Recorder(), FakeClock()
    notifier = _throttled(post, clock)
    notifier.start()
    notifier("watchdog_triggered", {"external_identifier": PUT_SPREAD})
    clock.advance(10.0)
    notifier("watchdog_triggered", {"external_identifier": PUT_SPREAD})   # suppressed
    clock.advance(1000.0)                       # past 900s, inside 2 x 900s
    notifier("watchdog_triggered", {"external_identifier": PUT_SPREAD})
    await drain(notifier)
    assert "(+1 suppressed" in post.texts[1]


async def test_a_window_nothing_has_touched_for_two_windows_is_forgotten():
    """The bound on throttle state in an always-on process. A count older than
    `2 x throttle_s` cannot ride on a message that says "in the last 900s" — and it
    is not lost either: `combine()` put every one of those repeats in the journal."""
    post, clock = Recorder(), FakeClock()
    notifier = _throttled(post, clock)
    notifier.start()
    for i in range(4):                          # four spreads, four windows
        notifier("watchdog_exit_filled", {"external_identifier": f"argus-{i}"})
    await drain(notifier)
    assert notifier.tracked_windows == 4

    clock.advance(900.0 * WINDOW_RETENTION_MULTIPLE)
    notifier.start()
    notifier("watchdog_exit_filled", {"external_identifier": "argus-tomorrow"})
    await drain(notifier)
    assert notifier.tracked_windows == 1        # the four are gone, not accumulating


# -- composition ---------------------------------------------------------------


async def test_combine_keeps_every_sink_alive_when_one_fails():
    """The journal line must not disappear because a notification channel was
    added on top of it."""
    seen: list[str] = []

    def broken(kind, detail):
        raise RuntimeError("sink is down")

    combine(broken, lambda k, d: seen.append(k), broken)("audit_log_down", {})
    assert seen == ["audit_log_down"]


async def test_build_alert_fn_sends_to_both_log_and_slack(caplog):
    post = Recorder()
    alert, notifier = build_alert_fn(
        environment="live", env={WEBHOOK_ENV_VAR: WEBHOOK}, post=post
    )
    assert notifier is not None
    notifier.start()
    with caplog.at_level(logging.WARNING, logger="argus.alert"):
        alert("daily_loss_breach", {"pnl": -1000.0})
    await drain(notifier)
    assert any("daily_loss_breach" in r.getMessage() for r in caplog.records)
    assert "[argus:live] daily_loss_breach" in post.texts[0]
