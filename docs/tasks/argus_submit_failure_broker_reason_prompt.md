# Argus: A Submit Failure's Own Reason Never Reaches the Page

## Priority

Not urgent. The correct outcome already happens in every observed case — no order, no
exposure, the one-attempt rule retires the candidate cleanly. Only the *explanation* on
`/status` and Slack is wrong. Safe to schedule whenever.

## Context

Observed 2026-09-02: 7 of 8 failed entry attempts were `instrument_validation_failed:
Trading of SPXW <symbol> is not supported` — cert refusing to trade a strike that exists
in production's chain (the market-data session is `source = "live"`) but not in cert's
tradeable universe. One was a genuine transient: `timeout_error: Request Timed Out`. Both
kinds surfaced identically: `NO_ORDER_EXISTS`, `reject_reason: null`, the same fixed
template reason string ("submit failed and no order exists under this identifier"), the
same reconnect-and-back-off treatment — even though the instrument-validation case can
never succeed on retry and the timeout might. See
[the third live-fire day](../decisions/2026-09-02-third-live-fire-day.md), finding 6, for
the full observed case and why it initially got misread as a cert outage.

The broker's own prose — which names the exact offending symbol — is captured in the
audit log (`order.submit_failed`'s `error`/`error_type` fields) and never reaches the
`NO_ORDER_EXISTS` alert or `WalkResult`.

## Where this lives (confirm against current source — `main` moves quickly)

In `src/argus/execution/loop.py`, `_recover(state, what, exc)` is called from two sites
with the causing exception already in hand:

- the initial submit path — `except Exception as exc: ... return await
  self._recover(state, what="submit", exc=exc)`
- the replace path — `except Exception as exc: ... return await self._recover(state,
  what="replace", exc=exc)`

When `_recover` concludes nothing is working and nothing filled, it calls `_finish` with a
**fixed template string** as the reason — `exc`, sitting right there in scope, is never
read into it. `_recover` is shared machinery: both `entry.py` and the exit path
(`watchdog.py`) walk through it, so fixing it once fixes both.

## Proposed approach

Thread `str(exc)` (truncated to the existing `_MAX_REJECT_REASON_CHARS`-style bound
already used elsewhere in this codebase, e.g. `reconcile.py`'s `_text_or_none`) into the
`NO_ORDER_EXISTS` outcome — either folded into the `reason` string, or as a dedicated
`WalkResult` field (e.g. `submit_error: str | None`) that flows through `to_payload()` the
same way `reject_reason` already does for actually-rejected orders.

**Prefer the dedicated-field approach** if this codebase already has an AST sweep
asserting "every alert naming an exit/entry outcome carries the broker's reason for it"
(added around 2026-09-02 per
[a terminal rung is not a verdict](../decisions/2026-09-02-a-terminal-rung-is-not-a-verdict.md)
— confirm it exists and find it before deciding). If it does, extend that sweep to cover
the new field rather than adding an ungoverned one.

## Non-goals — must hold

- Do not change `NO_ORDER_EXISTS`'s retry/outcome classification. `entry.py`'s
  `_ALERTING_OUTCOMES` still doesn't retry it; the exit path's `_RETRYABLE_EXITS` retry
  behavior is untouched. This is purely a reporting change — what text is shown, never
  what action is taken.
- Do not add any logic that distinguishes "permanent" vs "transient" failures by
  inspecting `error_type` or matching text — that's a different, larger decision
  (`entry.py`'s own reject-reason-text-matching prohibition applies by the same
  reasoning). Just surface what the broker said; don't act on it differently.

## Before implementing

Confirm the exact call sites and line numbers against current source — do not assume the
line numbers above are still accurate. If `_recover`'s signature or the `_finish` call it
makes has changed shape since this was written, report the discrepancy before building
around a guess.

## Testing requirements

- A `NO_ORDER_EXISTS` outcome from a submit exception carries that exception's message in
  the reported payload, for both the entry-path and exit-path callers of the shared loop.
- A `NO_ORDER_EXISTS` outcome from a replace exception carries its message too.
- Regression: outcome classification and retry counts are unchanged before/after — this
  test should fail if the fix accidentally touches the retryable-outcome set.

## Deliverables

- Code change scoped to `loop.py`'s shared `_recover`/`_finish` path.
- CLAUDE.md updated (required every session).
- A resolution line added to
  [the third live-fire day](../decisions/2026-09-02-third-live-fire-day.md)'s finding 6,
  and to the follow-up it filed for this exact gap.
- Test suite green, new/total count reported.

Confirm with Richard whether to work on a branch or on `main`, and whether to commit. Do
not push unless explicitly told to.
