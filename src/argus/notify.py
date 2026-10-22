"""The real alerting path behind `alerts.AlertFn` — a Slack incoming webhook.

`alerts.py` has declared this seam since the first week ("the phone-reachable
alerting path plugs in here later") and everything that needs to reach a human
has been calling into it ever since: the audit log's queue-depth and
write-failure alerts, the event-loop-lag heartbeat, reconciliation mismatches,
session errors, chunk 2's foreign-fill detection, chunk 3's walk failures. Until
now the only implementation was `log_alert`, which writes a WARNING line to a
journal nobody is reading at 10:42 on a Tuesday. This is the implementation that
actually leaves the box.

**It lives here, not under `execution/`, because it is not an execution concern.**
Half its callers (`app.py`, `heartbeat.py`, `audit/log.py`, `session.py`) sit
above the execution package; importing `argus.execution.notify` from `app.py`
would invert the layering for what is a process-wide facility. It sits next to
`alerts.py`, whose `AlertFn` type it implements.

Four properties, each of which is load-bearing rather than defensive habit:

**1. It never raises.** An alert is attached to a failure path — a dead audit
log, an unmappable fill, a watchdog exception. If delivering the alert can raise,
then Slack being down converts a handled failure into an unhandled one, at the
exact moment the system is already degraded. `__call__` cannot raise: every step
from enqueue to POST is guarded, and a failure is counted and logged instead.

**2. It never blocks the event loop.** `AlertFn` is a SYNCHRONOUS callable — it
is invoked from `AuditLog.telemetry`, from `LoopHeartbeat`, from the price-walking
loop — so it cannot await. A blocking `urlopen` inside it would stall the single
event loop the watchdog and the kill switch share (spec §4), which is precisely
the failure the heartbeat exists to detect. So `__call__` only enqueues onto a
BOUNDED queue, and a background task does the POST through `asyncio.to_thread`.
Same shape as the Tier 2 audit writer, for the same reason.

**3. The webhook URL is a credential and is never printed in full.** A Slack
incoming-webhook URL *is* the authentication — anyone holding it can post to the
channel — so it comes from the environment (`ARGUS_SLACK_WEBHOOK_URL`), never
from `argus.toml` or code, exactly like `ARGUS_KILL_TOKEN`. It is never logged,
and error messages carry `redact_webhook()` output (scheme + host only), because
the natural thing for a failing HTTP client to do is include the URL it was
calling.

**4. Delivery is best-effort and unordered relative to the audit log.** This is
NOT a second black-box recorder. The audit log is the durable record; Slack is a
notification. Nothing may infer from a delivered alert that anything was written,
and a dropped alert is not a safety event — which is why a full queue drops the
oldest concern rather than applying backpressure to a caller that is mid-failure.

**5. A repeating alert is COALESCED, per kind AND per subject.** Added
2026-08-05, after the first deployment of this module turned a benign,
long-standing event-loop-lag pattern into ~700 Slack messages an hour. The lesson
generalizes past that one caller: `AlertFn` has a dozen call sites, several of
them inside loops or per-tick paths, and this class had no rate limit of any kind
— so the first chatty one to appear was guaranteed to flood the channel, and a
channel that floods is a channel the operator mutes. That is a safety regression,
not a cosmetic one: `audit_write_failed` and `reconciliation_mismatch` arrive on
the same webhook.

The rule is deliberately the weakest one that fixes it, because dropping a
notification is not free:

  - The FIRST occurrence of a `kind` is ALWAYS delivered, immediately. An
    incident is never delayed by this.
  - Repeats of that same `kind` inside `throttle_s` are counted, not sent.
  - The count rides on the NEXT delivered message of that kind
    (`... (+58 suppressed)`), so a suppressed alert is deferred and summarized,
    never silently discarded.
  - The window is PER KIND, so a storm of one cannot delay a rare, important
    other — the failure mode a single global rate limit would have.

**The window is also per SUBJECT (2026-08-21, B1).** `kind` alone was the wrong
grain for half the callers. It was calibrated against `loop_lag_breach` — ONE
condition flapping every ten seconds, where a repeat inside the window genuinely
is the same condition continuing — and then applied to kinds where a repeat is a
DIFFERENT position: a stop-out pages as `watchdog_triggered` /
`watchdog_exit_filled`, with nothing per-position in the kind. With
`max_concurrent_positions = 2`, both spreads same-underlying and same-day, the
market move that triggers one stop-out is routinely the same move that triggers
the other — two stop-outs inside fifteen minutes is the expected correlated case,
not a tail. The operator got a full page for the first and a bare
`(+1 suppressed)` for the second, up to 15 minutes late. Both were still Tier 1
in the audit trail, so this was a notification-latency defect and never a record
defect — but real-time visibility on the second open position is the whole point
of the page.

So the throttle key is `(kind, scope)`, where `scope` is read from the detail
payload by `alert_scope` and is `""` for a kind that is not about a particular
position or order. Nothing opts in: an alert that names its subject gets its own
window automatically, and one that does not behaves exactly as it did before.
Two guards keep that from re-opening the 2026-08-05 hole — `MAX_SCOPES_PER_KIND`
(a kind that sprays distinct subjects loses scoping and coalesces as one kind
again) and `SCOPE_FIELDS` (a short, closed list of STABLE identifiers; see its
note on why `broker_order_id` is not among them).

Coalescing happens at ENQUEUE rather than at delivery, so suppressed alerts do
not occupy queue slots and therefore cannot push a different kind out of a full
queue. `suppressed` is reported separately from `dropped` for that reason: one
is a deliberate summary, the other is a real loss.

Stdlib `urllib.request`, not httpx, for the same reason `control.py` is a plain
`asyncio.start_server` rather than a web framework (see CLAUDE.md Build/test): one
fully-controlled request to one fixed URL does not justify a direct dependency in
a `--require-hashes` image whose goal is a minimal auditable supply chain.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from argus.alerts import AlertFn, log_alert

logger = logging.getLogger("argus.notify")

#: Where the webhook URL comes from. Env only — never config, never code.
WEBHOOK_ENV_VAR = "ARGUS_SLACK_WEBHOOK_URL"

#: Bound on one delivery attempt. Deliberately a module constant rather than a
#: config key: unlike the COA window or the broker timeout, nothing about trading
#: behaviour depends on it, and it exists only so a hung Slack cannot hold the
#: drain open at shutdown. Constructor-overridable for tests.
DEFAULT_TIMEOUT_S = 5.0

#: Bound on the pending-alert queue. An alert storm must not grow without limit
#: (spec §6's rule for Tier 2, applied for the same reason: the burst happens
#: during the volatility spike, and an OOM there relocates the problem).
DEFAULT_QUEUE_MAXSIZE = 256

#: How much of a detail payload reaches Slack. A reconciliation mismatch can
#: carry every discrepancy in the account; the full record is in the audit log,
#: and a phone notification only has to say enough to act on.
MAX_DETAIL_CHARS = 2000

#: Minimum gap between two Slack deliveries OF THE SAME `kind`. Repeats inside the
#: window are counted and summarized onto the next delivery (property 5 above).
#:
#: A module constant rather than a config key, for the reason `DEFAULT_TIMEOUT_S`
#: is: it shapes notification cadence and nothing about trading behaviour, so it
#: does not belong in the fail-closed loader — and adding a REQUIRED key to
#: `argus.toml` couples this fix to a config deploy, which is exactly the trap
#: CLAUDE.md documents for `[watchdog]` and `[risk]`. Constructor-overridable.
#:
#: 15 minutes is chosen against the phone, not the log: a persistent condition
#: reminds four times an hour, which is enough to stay visible and few enough that
#: the channel does not get muted. The first occurrence is never delayed by it.
DEFAULT_THROTTLE_S = 900.0

#: Detail fields naming WHICH position an alert is about. The throttle scopes on
#: the first one present (2026-08-21, B1).
#:
#: **The order matters and is not "most specific first".** `candidate_id` is the
#: TRADE IDEA — the day and the two strikes — and it is the one identifier that
#: stays the same across everything that ever happens to one spread.
#: `external_identifier` is per ORDER (`ORDER_ID_PREFIX + direction + candidate`),
#: so a single position has at least TWO over its life: the opening order's, which
#: `OpenSpread` carries, and the closing order's, which `ExitReport` carries. Both
#: appear in watchdog payloads about the same spread. Keying on the order would
#: split one position's episode across two windows — harmless in direction (more
#: messages, never fewer) but incoherent, and it would stop matching
#: `_Escalation`, which is per position.
#:
#: `external_identifier` stays as the fallback because the price-walking loop's
#: alerts carry it and no candidate at all.
#:
#: **`broker_order_id` is deliberately NOT here**, though several payloads carry
#: it. A price walk cancels and replaces, so one exit produces a NEW broker order
#: id per rung (CLAUDE.md's no-broker-side-stops rule turns on exactly that) —
#: scoping on it would give one un-throttled message per reprice, which is the
#: 2026-08-05 storm rebuilt out of the fix for it.
SCOPE_FIELDS = ("candidate_id", "external_identifier")

#: How many distinct scopes ONE kind may hold separate windows for. Past this, new
#: scopes of that kind fall back to the kind's single unscoped window.
#:
#: The flood guard for the generic rule above. Scoping is a concession to a small,
#: bounded, per-position population: `risk.max_concurrent_positions` is 2 spreads,
#: each of which can name an entry and an exit, so 4 is the real working set and 8
#: is double it. A kind that exceeds that is not reporting on positions — it is
#: spraying identifiers — and coalescing it as one kind is the older, safer
#: behaviour, not a new failure mode.
MAX_SCOPES_PER_KIND = 8

#: Longest scope kept. A scope is a throttle key AND goes in the message header;
#: an identifier is ~40 characters, so this only ever bites something unexpected.
MAX_SCOPE_CHARS = 80

#: How long a window outlives its own `throttle_s`, as a multiple of it. Bounds the
#: throttle state in an always-on process: an entry older than this can neither
#: suppress anything nor still be describing "the last {throttle_s}s", which is what
#: the summary it carries literally claims. 2x, not 1x, because a condition that
#: flaps just outside the window is the common case and its count must still ride on
#: the next message.
WINDOW_RETENTION_MULTIPLE = 2.0

#: post(url, body, timeout_s) -> None, raising on failure. The network seam: tests
#: inject a recorder, so no test ever opens a socket or calls Slack.
PostFn = Callable[[str, str, float], None]


class MissingWebhookError(RuntimeError):
    """`ARGUS_SLACK_WEBHOOK_URL` is absent or empty."""


def redact_webhook(url: str) -> str:
    """A form of the webhook URL that is safe to log: scheme + host, nothing else.

    The secret in an incoming-webhook URL is entirely in the path
    (`/services/T…/B…/…`), so the path is what must never appear. Returned instead
    of the URL anywhere a message might be logged — including exception text,
    which is where a URL leaks by accident rather than by decision.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<unparseable-webhook-url>"
    if not parts.scheme or not parts.netloc:
        return "<malformed-webhook-url>"
    return f"{parts.scheme}://{parts.netloc}/<redacted>"


def load_webhook_url(env: dict[str, str] | None = None) -> str | None:
    """The configured webhook URL, or None if alerting to Slack is not set up.

    Deliberately returns None rather than raising, and that is the difference
    between this and `control.load_kill_token`. An unauthenticated kill switch is
    a broken safety rail, so a missing token refuses process start. A missing
    Slack webhook is a missing NOTIFICATION channel: every alert still reaches the
    log and every safety rail still functions, so refusing to boot over it would
    trade a real halt for a missing convenience.
    """
    raw = (env if env is not None else os.environ).get(WEBHOOK_ENV_VAR, "")
    value = raw.strip()
    if not value:
        return None
    if not value.startswith("https://"):
        # Fail loud on a URL that would send an alert — and with it whatever
        # internal state the detail payload carries — over plaintext.
        raise MissingWebhookError(
            f"{WEBHOOK_ENV_VAR} must be an https:// URL; got {redact_webhook(value)}"
        )
    return value


def scrub(message: str, url: str) -> str:
    """Replace any occurrence of the webhook URL in `message` with its redacted form.

    Belt and braces on top of not putting the URL into messages in the first place.
    `URLError.reason` and any custom `PostFn`'s exception text are strings this
    module did not write, and a credential in a log line is not the kind of thing to
    leave to the good manners of another library.
    """
    return message.replace(url, redact_webhook(url)) if url else message


def alert_scope(detail: dict[str, Any]) -> str:
    """WHICH position or order this alert is about, or `""` if it is not about one.

    The whole of the per-subject rule (2026-08-21, B1), and deliberately GENERIC
    rather than an allowlist of per-position kinds. An allowlist would have to be
    maintained alongside every new alert, and the cost of forgetting an entry is
    silent: the kind quietly folds two positions together again, which is the
    defect this fixes and is invisible until the day it matters. Reading the
    subject out of the payload means an alert that names its position gets its own
    window because it named it.

    Only a non-empty `str` counts. `ExitReport.external_identifier` and
    `Order.candidate_id` are both `str | None`, several payloads carry
    `broker_order_id: int | None`, and a `None` or an `int` that stringified into a
    key would be a key nobody intended.
    """
    for field in SCOPE_FIELDS:
        value = detail.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()[:MAX_SCOPE_CHARS]
    return ""


@dataclass
class _Window:
    """One throttle window: when its kind+scope last went out, and what it owes."""

    #: Monotonic seconds. Set ONLY on a successful enqueue (see `__call__`).
    last_enqueued_at: float
    #: Repeats counted since that delivery, to ride on the next one.
    suppressed: int = 0


def _urllib_post(url: str, body: str, timeout_s: float) -> None:
    """POST `body` as JSON. Runs in a worker thread, never on the event loop.

    Raises on any non-2xx or transport failure — the caller counts and logs it.

    **The failure is raised OUTSIDE the `except` block, on purpose.** `raise X from
    None` clears `__cause__` but leaves `__context__` pointing at the original
    `HTTPError`, which holds `.url` — so anything that ever formats a traceback for
    this exception (a `logger.exception`, a crash dump) would print the full webhook
    URL. Building the message inside the handler and raising after it leaves means
    there is no chained exception to leak from at all.
    """
    request = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    failure: str | None = None
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            status = getattr(response, "status", None)
            if status is not None and not 200 <= int(status) < 300:
                failure = f"slack returned HTTP {status}"
    except urllib.error.HTTPError as exc:
        # Only the status code. `HTTPError` carries the request URL on `.url` and in
        # some formatted representations.
        failure = f"slack returned HTTP {exc.code}"
    except urllib.error.URLError as exc:
        failure = scrub(f"slack unreachable: {exc.reason}", url)
    except Exception as exc:  # noqa: BLE001 - normalized below, then re-raised
        failure = scrub(f"{type(exc).__name__}: {exc}", url)
    if failure is not None:
        raise RuntimeError(failure)


class SlackNotifier:
    """An `AlertFn` that posts to a Slack incoming webhook. Never raises.

    Call `start()` once a loop is running and `aclose()` at shutdown. Alerts
    raised before `start()` (or after `aclose()`) are queued and logged rather
    than lost silently — but only up to the queue bound.
    """

    def __init__(
        self,
        webhook_url: str,
        *,
        environment: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        throttle_s: float = DEFAULT_THROTTLE_S,
        max_scopes_per_kind: int = MAX_SCOPES_PER_KIND,
        post: PostFn | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not webhook_url:
            raise MissingWebhookError("SlackNotifier needs a webhook URL")
        self._url = webhook_url
        self._environment = environment
        self._timeout_s = timeout_s
        self._throttle_s = throttle_s
        self._max_scopes_per_kind = max_scopes_per_kind
        self._post = post or _urllib_post
        # MONOTONIC, and injected only so the throttle is testable without waiting.
        # Never a wall clock: an NTP step backwards would silence a whole kind for
        # the length of the correction.
        self._clock = clock
        # (kind, detail, scope, suppressed_since_last_delivery_of_that_kind+scope)
        self._queue: asyncio.Queue[tuple[str, dict[str, Any], str, int]] = asyncio.Queue(
            maxsize=queue_maxsize
        )
        self._worker: asyncio.Task[None] | None = None
        # Throttle state, keyed by (kind, scope). `kind` is a closed set of ~25
        # string literals; `scope` is what makes this a per-detail key, so it is
        # bounded on BOTH axes rather than trusted to stay small: at most
        # `max_scopes_per_kind` scopes per kind (`_throttle_key`), and only for as
        # long as they can still say anything (`_expire_windows`).
        self._windows: dict[tuple[str, str], _Window] = {}
        self._delivered = 0
        self._failed = 0
        self._dropped = 0
        self._suppressed = 0

    # -- the AlertFn interface ---------------------------------------------

    def __call__(self, kind: str, detail: dict[str, Any]) -> None:
        """Queue an alert. Synchronous, non-blocking, and cannot raise.

        Every failure mode here — a full queue, no running loop, a detail payload
        that will not serialize — is swallowed into a log line. A raise would
        propagate into whatever failure path called the alert.

        Repeats of a `kind` ABOUT THE SAME SUBJECT inside `throttle_s` are COUNTED
        rather than queued (property 5). The count is attached to the next delivered
        message of that kind and subject, so this defers and summarizes; it never
        discards. The log sink is unaffected either way — `combine()` calls it
        independently, so the full unthrottled sequence is always in the journal.
        """
        now = self._clock()
        self._expire_windows(now)
        kind_and_scope = self._throttle_key(kind, detail)
        scope = kind_and_scope[1]
        window = self._windows.get(kind_and_scope)
        if window is not None and (now - window.last_enqueued_at) < self._throttle_s:
            window.suppressed += 1
            self._suppressed += 1
            logger.debug(
                "slack alert %s%s suppressed (%d since last delivery; window %.0fs)",
                kind, f" [{scope}]" if scope else "",
                window.suppressed, self._throttle_s,
            )
            return
        pending = window.suppressed if window is not None else 0
        try:
            self._queue.put_nowait((kind, detail, scope, pending))
        except asyncio.QueueFull:
            self._dropped += 1
            logger.warning(
                "slack alert queue full; dropped %s (dropped_total=%d)", kind, self._dropped
            )
            return
        except Exception:  # noqa: BLE001 - an alert must never raise into its caller
            self._dropped += 1
            logger.exception("could not queue slack alert %s", kind)
            return
        # ONLY after a successful enqueue. A dropped alert must not start the
        # throttle window (that would suppress the follow-ups to something that was
        # never sent) and must not consume the pending suppression count — the
        # replacement here resets `suppressed` to 0 precisely because the count it
        # held has just gone out on the message above.
        self._windows[kind_and_scope] = _Window(last_enqueued_at=now)

    # -- the throttle ------------------------------------------------------

    def _throttle_key(self, kind: str, detail: dict[str, Any]) -> tuple[str, str]:
        """The window this alert belongs to: its kind, and its subject if it has one.

        Falls back to the kind's unscoped window once a kind is already tracking
        `max_scopes_per_kind` distinct subjects. That is the flood guard on an
        otherwise open-ended key: N distinct scopes inside one window means N
        delivered messages, and the whole reason this class throttles is that a
        caller nobody predicted turned out to be chatty. Degrading to per-kind
        coalescing is the pre-2026-08-21 behaviour — noisier is not one of the
        options.

        An already-tracked scope keeps its own window regardless of the cap, so a
        position that has been paging cannot be demoted mid-episode by unrelated
        traffic on the same kind.
        """
        scope = alert_scope(detail)
        if not scope:
            return (kind, "")
        scoped = (kind, scope)
        if scoped in self._windows:
            return scoped
        tracked = sum(1 for k, s in self._windows if k == kind and s)
        if tracked >= self._max_scopes_per_kind:
            if (kind, "") not in self._windows:
                # Once per overflow episode, not once per alert — a kind that has
                # earned this is by definition not a quiet one, and the journal
                # already has every alert through `log_alert`.
                logger.warning(
                    "slack alert %s already tracks %d distinct scopes; coalescing "
                    "%s and further subjects into one window for this kind",
                    kind, tracked, scope,
                )
            return (kind, "")
        return scoped

    def _expire_windows(self, now: float) -> None:
        """Forget windows too old to suppress anything or to describe anything.

        The bound on throttle state in a process that runs all day, every day. A
        window stops suppressing at `throttle_s`; it is kept for
        `WINDOW_RETENTION_MULTIPLE` times that so a condition flapping just outside
        the window still gets its count. Past that, a pending count is not a
        deferred summary any more — the message it would ride on says "in the last
        {throttle_s}s", which would be false — and it is not silently lost either:
        every one of those repeats went to the journal through `log_alert`, and the
        discard is logged here with its count.
        """
        if not self._windows:
            return
        retain_s = self._throttle_s * WINDOW_RETENTION_MULTIPLE
        expired = [
            key for key, window in self._windows.items()
            if (now - window.last_enqueued_at) >= retain_s
        ]
        for key in expired:
            window = self._windows.pop(key)
            if window.suppressed:
                logger.info(
                    "slack alert %s%s: %d suppressed repeat(s) went unsummarized — "
                    "the episode ended over %.0fs ago; the journal has all of them",
                    key[0], f" [{key[1]}]" if key[1] else "",
                    window.suppressed, retain_s,
                )

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="slack-notifier")

    async def aclose(self, *, drain_timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        """Deliver what is queued, then stop. Bounded — shutdown must not hang.

        A dropped alert at shutdown is acceptable; a process that will not exit
        because Slack is unreachable is not. The drain is time-boxed and the
        worker is cancelled either way.
        """
        if self._worker is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=drain_timeout_s)
        except Exception:  # noqa: BLE001 - teardown must never raise, incl. on timeout
            logger.warning(
                "slack notifier did not drain within %.1fs; %d alert(s) undelivered",
                drain_timeout_s, self._queue.qsize(),
            )
        self._worker.cancel()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - teardown must not raise
            logger.exception("slack notifier worker failed during shutdown")
        self._worker = None

    async def _run(self) -> None:
        while True:
            kind, detail, scope, suppressed = await self._queue.get()
            try:
                body = self._body(kind, detail, scope, suppressed)
                await asyncio.to_thread(self._post, self._url, body, self._timeout_s)
                self._delivered += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a failed alert is logged, never raised
                self._failed += 1
                # The redacted host is logged explicitly so a misconfigured endpoint
                # is diagnosable, and the exception text is SCRUBBED: the default
                # poster is written not to carry the URL, but an injected `PostFn`
                # is not this module's code and must not be trusted with it.
                logger.warning(
                    "slack alert %s not delivered to %s: %s (failed_total=%d)",
                    kind, redact_webhook(self._url), scrub(str(exc), self._url),
                    self._failed,
                )
            finally:
                # Unconditional, including on cancellation: the item was taken off
                # the queue, so `join()` in `aclose` must not wait on it forever.
                self._queue.task_done()

    # -- message construction ---------------------------------------------

    def _body(
        self, kind: str, detail: dict[str, Any], scope: str = "", suppressed: int = 0
    ) -> str:
        text = f"[argus:{self._environment}] {kind}"
        if scope:
            # On the first line, next to the kind, for the same reason the
            # suppressed count is: a phone's notification preview is one line, and
            # two correlated stop-outs are now two full messages whose kinds are
            # IDENTICAL. Without the subject there, the operator cannot tell the
            # second position's page from a redelivery of the first's.
            text = f"{text} [{scope}]"
        if suppressed:
            # On the FIRST line, so it survives a phone's notification preview: the
            # count is what distinguishes "it happened again" from "it has been
            # happening continuously", and that difference is the whole diagnosis.
            text = f"{text} (+{suppressed} suppressed in the last {self._throttle_s:.0f}s)"
        rendered = _render_detail(detail)
        if rendered:
            text = f"{text}\n```{rendered}```"
        return json.dumps({"text": text})

    # -- introspection (tests, and a future /status field) -----------------

    @property
    def delivered(self) -> int:
        return self._delivered

    @property
    def failed(self) -> int:
        return self._failed

    @property
    def dropped(self) -> int:
        """Alerts LOST — the queue was full. Distinct from `suppressed`."""
        return self._dropped

    @property
    def suppressed(self) -> int:
        """Alerts coalesced by the kind+scope throttle. Reported, not lost."""
        return self._suppressed

    @property
    def tracked_windows(self) -> int:
        """Live throttle windows. The bound `_expire_windows` exists to hold."""
        return len(self._windows)

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()


def _render_detail(detail: dict[str, Any]) -> str:
    """Detail as bounded, readable JSON. Falls back rather than failing.

    A detail payload that will not serialize must still produce an alert — the
    caller is usually already in a failure path, and "the alert about the problem
    could not be formatted" is the least useful possible outcome.
    """
    if not detail:
        return ""
    try:
        rendered = json.dumps(detail, sort_keys=True, default=str, allow_nan=False)
    except Exception:  # noqa: BLE001 - fall back to a repr rather than dropping the alert
        rendered = repr(detail)
    if len(rendered) > MAX_DETAIL_CHARS:
        rendered = f"{rendered[:MAX_DETAIL_CHARS]}… (truncated; full detail in the audit log)"
    return rendered


def combine(*alerts: AlertFn) -> AlertFn:
    """One `AlertFn` that fans out to several, where one failing cannot stop the rest.

    Used to keep `log_alert` in the chain when Slack is configured: the journal
    line is the local record and must not disappear because a notification channel
    was added on top of it.
    """

    def fan_out(kind: str, detail: dict[str, Any]) -> None:
        for alert in alerts:
            try:
                alert(kind, detail)
            except Exception:  # noqa: BLE001 - one sink must not take out the others
                logger.exception("alert sink failed for %s", kind)

    return fan_out


def build_alert_fn(
    *,
    environment: str,
    env: dict[str, str] | None = None,
    post: PostFn | None = None,
) -> tuple[AlertFn, SlackNotifier | None]:
    """The process's `AlertFn`, plus the notifier to start/stop (None if unconfigured).

    Returns `log_alert` alone when `ARGUS_SLACK_WEBHOOK_URL` is unset, so a
    deployment without Slack behaves exactly as it does today. With it set, alerts
    go to BOTH the log and Slack — never Slack alone.
    """
    try:
        url = load_webhook_url(env)
    except MissingWebhookError as exc:
        # A malformed webhook is a configuration error worth shouting about, but
        # not worth refusing to boot over: the log sink still works.
        logger.error("slack alerting disabled: %s", exc)
        return log_alert, None
    if url is None:
        logger.info(
            "slack alerting not configured (%s unset); alerts go to the log only",
            WEBHOOK_ENV_VAR,
        )
        return log_alert, None
    notifier = SlackNotifier(url, environment=environment, post=post)
    logger.info("slack alerting enabled -> %s", redact_webhook(url))
    return combine(log_alert, notifier), notifier
