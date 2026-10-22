# Pinning the traded account (2026-09-10)

**Status: BUILT 2026-09-10, same day as sign-off.** The enforcement half (option
**D**) of the recommendation accepted in
[the 2026-08-31 record](2026-08-31-live-account-is-not-dedicated.md); closes **C9**
in [`docs/go-live-findings.md`](../go-live-findings.md). Design:
[2026-09-10 — pinning the traded account](../designs/2026-09-10-pinning-the-traded-account.md)
— approved with every §0 decision standing; D7 and D8 were the two judgement
calls flagged for overruling and neither was overruled.

---

## What shipped

- **`[broker] account_number`** — `str | None`, optional in the loader, present
  in all three shipped profiles. `argus.live.toml` pins `"5XX00000"` (the existing
  account being made dedicated, not a new one); dev and sandbox ship `""`,
  normalized to `None` = unpinned = `accounts[0]`. Cert stays unpinned **by
  decision, permanently**: cert accounts are re-provisioned by tastytrade, and a
  pinned number that stops existing is a `SessionAborted` — permanent — that
  parks the process.
- **The live guard is its own branch** in `Config.from_dict`, placed with the
  `[broker]` parse and therefore firing **before** the `account_stream_max_age_s`
  and `audit.synchronous` live guards. The three refuse live for three unrelated
  reasons and must never share a branch —
  `test_config.py::test_the_three_live_guards_are_independent` is what stops a
  tidy from folding them.
- **No format validation beyond non-empty, stripped, and no internal
  whitespace.** tastytrade account-number shapes are undocumented and vary; a
  loader that refuses a valid number is strictly worse than one that accepts an
  invalid one (the invalid one is caught by the account filter within seconds of
  start, loudly and permanently). A number with internal whitespace is refused at
  load because the account filter's no-match answer — `SessionAborted`, parked —
  is a worse failure than a failed start. No cert-value fence: dev and sandbox
  carry `""`, so there is nothing to inherit by copy-paste.
- **`__main__` passes `config.broker.account_number` explicitly.** `Application`
  does not read the config itself — the injection seam is what `tools/shadow.py`
  uses, and a supervisor that re-derived the number internally would give two
  callers two different answers. `tools/shadow.py` falls back to the config when
  `--account` is omitted (the CLI argument still wins); `tools/account.py`,
  `tools/loop_lag.py` and `tools/order_smoke.py` are untouched.
- **`/status` carries an Account row** immediately after Credentials, and the
  JSON's existing top-level `"account"` block carries `pinned` (bool) and
  `number`, ahead of its measurement fields. The post-deploy assertions: cert
  shows *"not pinned; this process takes the login's first account
  (accounts[0])"*; live shows `5XX00000 — pinned` and `account.pinned == true`.
- **The live reconcile alert names the likely cause.**
  `reconcile.shared_account_hint` — live-only, positions-only, firing only when
  EVERY position discrepancy is `unexpected_at_broker` — appends a `"hint"` key
  to the `reconcile.result` record and the `reconciliation_mismatch` alert. The
  key is **absent rather than `None`** when it does not fire. It is diagnostic
  only: it reaches `ok`, `halt_grade` and `mismatch_halts_process` through no
  path, and `test_the_hint_changes_no_verdict` is the test that bounds it.

**Ships coupled** (config + image together), bundled with the next release —
the same coupled move six previous releases already forced. The cert deploy is a
behavioural no-op by construction: `""` → `None` → `accounts[0]`, byte-identical
to today's path.

## D7 and D8, as decided

- **D7 — the unpinned `/status` row is muted, not amber.** Unpinned is the
  *intended, permanent* cert state and *unreachable* in live (the loader
  refuses). `_config_cell`'s own rule — colour is **actionable, not unusual** —
  makes an amber "not pinned" a badge lit on every cert page for the life of the
  profile, which is a badge that means nothing on the day it finally matters. The
  row says WHY instead of showing a dash.
- **D8 — `MAX_SANDBOX_LIVE_DIVERGENCE` stays 10.** This change takes
  sandbox↔live divergence from 9 to exactly 10 — the cap. Deliberately not
  raised: arriving at the threshold on a key that *is* structurally per-world is
  exactly the right moment to notice, and the **next** divergence should reopen
  the base+overlay question (design §5.1, 2026-09-04) rather than bump a number.

## What the build overturned

**One thing, in §3.5.** The design's `to_payload` snippet added a NEW top-level
`"account"` block "immediately after `"credentials"`" — but `to_json_dict`
already had a top-level `"account"` block (nlv, daily_pnl, open_position_count,
open_spread_equivalent, working_open_spread_count). A dict literal with the key
twice is legal Python in which the second silently clobbers the first, so the
design as written would have deleted the account-data payload every existing
consumer reads. The build **merged** `pinned` and `number` into the existing
block, placed first within it — identity before measurements — which preserves
both of the design's verifiable goals (`account.pinned == true`, §7; the pin
carried separately from the number, §4.4) without breaking the measurement half.
§4.4's exact-equality assertion on the block became field assertions for the
same reason.

**And §4.5's list was confirmed as a list of guesses, as it warned.** The first
failure pytest actually reported was
`tests/test_session.py::test_is_test_derived_from_environment[live]` — a live
`Config` reached through the module's `_config` **helper**, a shape no
line-number list can see. The fix went in the helper and covers every caller.
Conversely `test_dev_may_never_be_live` (listed at ~75) did not break —
`_check_profile` fires before the broker guard — but the pin was added there
anyway, so the test no longer depends on guard ordering to raise the right
refusal.

## Normative — do not relitigate without a new record

1. The key is `[broker] account_number`, type `str | None`. `""` and
   whitespace-only normalize to `None`. **Live refuses `None`; nothing else
   does.**
2. The live guard stays beside the `[broker]` parse, its own branch, independent
   of the other two live guards.
3. `Application` never derives the account from config itself; callers inject
   it. `tests/test_main_wiring.py` holds the supervisor's wiring structurally.
4. The `/status` Account row has **no colour state** (D7) and renders the
   CONFIGURED number, never a broker read-back — under a pin those cannot differ
   (the filter is exact-match or permanent `SessionAborted`).
5. The hint is live-only, positions-only, all-`unexpected_at_broker`-only,
   absent-not-`None`, and changes no verdict, grade or halt.
6. Tests pin the fake `5WX00000`, never the real number. `tests/support.py`
   ships `""` so the canonical config stays unpinned by default.
7. Nothing here is a step toward strategy-scoped accounting (option B, rejected
   in the 2026-08-31 record). The rails still read whole-account numbers, and
   that is correct once the account is dedicated.

## What this does not solve

Carried forward from the 2026-08-31 record, unchanged, all of it on the
live-flip checklist: the account must **stay** dedicated (a hand-placed trade
halts the next restart — operator discipline, not a rail); cash movements
contaminate the breaker and are not restart-clearable; the NLV floor
(`max_loss_per_position / position_size_pct` = **$31,667** at 950 / 0.03) leaves
the account funded-but-not-trading below it; the breaker is still unexercised;
and a foreign **working** order with no foreign position gets the ordinary alert
and no hint — a known gap, deliberately not closed by duplicating `halt_grade`'s
identifier test.
