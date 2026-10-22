# Config decision (a retune, a promotion, a policy change)

**Use when:** the change is a *value*, not code — a threshold retuned on data, a flag
promoted from cert to live, a cap moved, a policy constant set by the owner. In this
repo a config value in `argus.live.toml` is a trading decision with real money behind
it, so it gets a decision record like any build does, with its evidence, its limits,
and a **reversal rule fixed before the first live session**.

Examples already in the repo: `2026-09-19-live-cancel-recheck-promotion.md` (a
promotion), `2026-09-17-exit-ladder-second-pass.md` (a data-backed retune),
`2026-09-26-strike-touch-stop.md` (an owner's policy change with review triggers).

**Produces:** `docs/decisions/YYYY-MM-DD-<slug>.md` and — only once Richard has
approved the values — the edit to the profile(s). Never both in one step unless the
prompt says the values are already approved.

**Mode:** `PROPOSE` (record only, values marked awaiting sign-off) or `APPLY` (values
approved: edit the profiles, run the tests, and mark the record DECIDED).

| Placeholder | Fill with |
|---|---|
| `{{KEYS}}` | The key(s) and the proposed value(s), or "recommend" |
| `{{WHY}}` | What prompted it: research note, investigation, P&L week, cert track record |
| `{{PROFILES}}` | Which profiles move (`live`, `sandbox`, `dev`) — and why any does not |
| `{{MODE}}` | `PROPOSE` or `APPLY` |
| `{{DATE}}` / `{{SLUG}}` | Date and file slug |

---

## The prompt

````text
Before beginning, read docs/templates/00-standing-preamble.md in full and follow it.

Then read: every decision record that set or constrains {{KEYS}} (search
docs/decisions/ for the key names), the research or investigation cited in {{WHY}},
tests/test_config_profiles.py (the key-set pin and the declared-divergence allowlist),
and the section of config.py that loads and validates {{KEYS}}.

Change: {{KEYS}}
Why: {{WHY}}
Profiles: {{PROFILES}}
Mode: {{MODE}}

Task: write the decision record for this config change and, in APPLY mode only, make it.

Establish:
1. What the key does TODAY, from the code: every reader of it (module::name), what it
   gates, and whether anything else reads the same key for a different purpose (a key
   shared between entry and exit, or between a gate and a staleness rail, is the
   classic trap — e.g. staleness.market_data_max_age_s must never be raised to fix an
   exit-path problem).
2. Its current value in each profile, and its history (the records that moved it).
3. The evidence for the new value, and its admissibility: which snapshot and window;
   whether it is realized-basis live data or cert (a number measured in cert cannot
   size a change on its own); whether it sits inside a standing prohibition (e.g. the
   credit_multiple prohibition) — if it does, say so at the top, and say who is
   setting it aside and on what evidence.
4. Interactions: the welded invariant
   (max_concurrent_positions × max_loss_per_position ≤ daily_max_loss) with before and
   after numbers; any loader coupling that refuses the new value; any other key whose
   meaning changes with this one.
5. The release class: config-only (no image change) unless a key is new. Whether a
   restart has a side effect for this key (a window to avoid, a position to not hold).

Write docs/decisions/{{DATE}}-{{SLUG}}.md:
- Status: PROPOSED (awaiting Richard's sign-off) or DECIDED {{DATE}} (Richard).
- The decision — a table: key, profile, was, now, effect.
- Why — the evidence, with its limits stated beside it.
- Reversal rule, fixed in advance: numbered review triggers (each a measurable
  quantity with a threshold and a sample size), and the rollback values. "Never with a
  position open."
- Interactions — what else this moves.
- What this does NOT change.
Add it to docs/decisions/README.md.

If Mode is APPLY: edit exactly the keys and profiles above, keep each key's comment in
the profile truthful (a stale comment beside a live value is a defect), update the
declared-divergence allowlist if profiles now differ on purpose, run
tests/test_config_profiles.py and the full suite, compute the live config sha (first 12
hex of sha256 over argus.live.toml's bytes) and put it in the record, and add one
sentence to CLAUDE.md "Current phase" (DECIDED, NOT YET DEPLOYED, config-only).

If Mode is PROPOSE: do not touch any argus.*.toml.

Commit and push to the branch you were given.
````

---

## Notes for the operator (not part of the prompt)

- **Raising a safety margin vs. lowering one.** Many keys have a safe direction (the
  flatten deferral's `k`: raising needs no analysis; lowering needs a recalibration and
  a record). State the direction in `{{WHY}}` — the prompt will hold the record to it.
- **Cert → live promotions**: put the cert track record *and* live's own records for
  the path in `{{WHY}}`. The 2026-09-19 promotion found cert could not supply the
  record the gate asked for, and said so.
- Follow with `08-release` for the deploy notes.
