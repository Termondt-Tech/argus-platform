# Argus: An Exit-Scoped Staleness Key, Shipped at Parity With Entry's

## Priority

Not urgent, and deliberately structured so it can never become urgent by itself: this
task adds a config surface with **no behavior change**. It exists to make a future,
separate, not-yet-designed decision cheaper to make later. Safe to schedule whenever.

## Context

`staleness.market_data_max_age_s` (currently 2.0) is a single shared bound consumed by
several different things: entry pricing, the pre-trade gate's staleness rail, and the
whole exit path (both trigger-evaluation's quote-freshness check and
`build_order`/`_limit_price`'s check of whether the current quote is usable at all).
CLAUDE.md states, more than once, that an exit-path staleness problem must never be fixed
by raising this key — doing so would also loosen entry pricing and the gate.

On 2026-09-02 this bound produced 545 blind episodes in one session (`tools.evaluability`
measurement), max duration 21.9s, and contributed to `put-7640-7630` closing roughly four
minutes after its `FLATTEN_BEFORE_CLOSE` clock fired — both market triggers were dark
(`credit_multiple` and `underlying_near_short_strike`), leaving only the clock-based
flatten evaluable. See
[the third live-fire day](../decisions/2026-09-02-third-live-fire-day.md), finding 4, for
the full measurement.

**This task does not decide whether or how to loosen the exit-path bound.** That's a
separate, harder question — how much staleness is safe to act on for an *already-open*
position, which is a different risk profile than refusing a *new* entry — and it hasn't
been designed yet. This task only builds the knob, defaulted so nothing changes today.

## Proposed approach

1. **Enumerate first, before touching anything.** Grep every consumer of
   `staleness.market_data_max_age_s` (config.py's `StalenessConfig`, and every read site —
   likely `signal/live_chain.py`'s freshness check, `execution/watchdog.py`'s
   `LegQuotes.resolve`/trigger-evaluation path, and `execution/build.py`'s
   `_limit_price`/quote-usability check). Classify each as ENTRY-ONLY, GATE, or
   EXIT-PATH. Report this enumeration before writing the rewire — the boundary matters
   more than the plumbing, and getting it wrong here is the whole risk of this task.
2. Add a new config key, e.g. `staleness.exit_market_data_max_age_s`, as an OPTIONAL
   field. When unset, it must resolve to whatever `staleness.market_data_max_age_s`
   currently is — decide and document whether that's a load-time derived default or an
   explicit equal default, following whichever pattern `config.py` already uses for
   similar "falls back to another key" fields.
3. Rewire **only** the exit-path consumers identified in step 1 to read the new key.
   Entry pricing and the gate's staleness rail must keep reading
   `staleness.market_data_max_age_s`, untouched.
4. **This key must be OPTIONAL, never REQUIRED.** The entire point is that an unmodified
   `argus.toml` produces identical behavior. Do not force a coupled config+image deploy
   for a change designed to be a no-op.
5. Add a comment next to the new key in `argus.toml` (and `argus.dev.toml`,
   `argus.live.toml` if present) stating clearly: defaults to parity with
   `market_data_max_age_s` on purpose; retuning it away from that value is a separate,
   not-yet-designed decision (see the finding cited above) and is explicitly **not** part
   of this task.

## Non-goals — must hold

- No retuning. This task ships the knob at parity and stops there.
- No change to entry pricing or gate staleness behavior, under any config.
- No new REQUIRED key, no coupled deploy.

## Before implementing

Confirm the actual consumer list against current source rather than guessing from this
brief — `main` moves quickly and the exact file/function names above may have shifted. If
the enumeration in step 1 turns out ambiguous (a consumer that's arguably both
entry-adjacent and exit-adjacent), stop and report it rather than guessing which side it
belongs on.

## Testing requirements

- With the new key unset: exit-path staleness checks produce results identical to
  before this change, for the same inputs and the existing 2.0s threshold.
- With the new key set to a different value: only exit-path checks respect it; entry
  pricing and gate checks still use the original key, unaffected.
- A config round-trip test confirming the key is genuinely optional (a config file
  without it loads without error and behaves as case 1 above).

## Deliverables

- Code change, scoped to the enumerated exit-path consumers only.
- CLAUDE.md updated (required every session).
- A short decision-record note documenting what's now separately tunable and explicitly
  that no value has changed — link from
  [the third live-fire day](../decisions/2026-09-02-third-live-fire-day.md)'s finding 4.
- Test suite green, new/total count reported.

Confirm with Richard whether to work on a branch or on `main`, and whether to commit. Do
not push unless explicitly told to.
