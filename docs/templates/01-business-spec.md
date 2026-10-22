# Business spec

**Use when:** there is a trading or operating *want* that is not yet a design — a new
strategy behaviour, a change in how Argus trades, a new report or operator surface, a
rollout-phase step (e.g. scaling size). It answers **what and why, for whom, and how we
will know it worked** — never how it is built.

**Produces:** `docs/specs/YYYY-MM-DD-<slug>.md` (create `docs/specs/` on first use, with a
one-table `README.md` index). The top-level system spec,
`docs/0dte_spx_desk_spec.md`, is the model for shape and tone; a business spec is a
scoped addendum to it, and says which of its sections it amends.

**Mode:** document only. No `src/`, `tests/` or config change.

**Next step:** a design (template `02`) that cites this spec.

| Placeholder | Fill with |
|---|---|
| `{{TITLE}}` | Short name of the capability, e.g. "Scaling to two contracts per spread" |
| `{{SLUG}}` | kebab-case file slug |
| `{{DATE}}` | Today, `YYYY-MM-DD` |
| `{{THE_ASK}}` | One to five sentences in your own words: what you want and why now |
| `{{INPUTS}}` | Docs, research notes, incidents, P&L days that prompted it (paths) |

---

## The prompt

````text
Before beginning, read docs/templates/00-standing-preamble.md in full and follow it.
Then read docs/0dte_spx_desk_spec.md in full (§2 Goals, §3 Non-goals, §6 safety rails,
§8 sizing, §9 risk register and §10 rollout plan are the sections a business spec
most often amends).

Inputs to read in full: {{INPUTS}}

The ask, in the owner's words:
{{THE_ASK}}

Task: write a BUSINESS SPEC for "{{TITLE}}". It defines the what and the why, the
acceptance criteria and the risk envelope. It does not design the solution — where a
"how" is tempting, write it as a constraint the design must satisfy instead.

Mode: document only. Do not change src/, tests/, or any argus.*.toml.

Save it as docs/specs/{{DATE}}-{{SLUG}}.md (create docs/specs/ and a README.md index
table if they do not exist) with these sections, in this order:

0. Status line — "DRAFT, awaiting Richard's sign-off", the date, and which sections of
   docs/0dte_spx_desk_spec.md this amends.
1. Problem and context — what is true today (cite the code, config values and
   decision records that make it true, checked against src/ and the profiles, not
   against CLAUDE.md prose), and what it costs or forgoes. Quantify from the record
   where the record allows; say "unmeasured" where it does not.
2. Goals — outcomes, each one testable.
3. Non-goals — at least three, including the tempting adjacent ones.
4. Users and surfaces — who acts on this (the trading process itself, the operator on
   a phone, the EOD reader, the ledger) and where they see it (/status, Slack, the
   desk view, the Sheet).
5. Acceptance criteria — observable, each tied to a record or a surface. Write them so
   a reviewer can say pass/fail without asking you.
6. Success metrics, PRE-REGISTERED — the statistic, the unit (a trading DAY, never a
   pooled tick), the predicted sign, the number of qualifying sessions before the one
   look, and what result would mean "revert". State plainly if the sample Argus can
   produce at minimum size can never reach significance, and what that means for the
   decision.
7. Risk envelope — which of the non-negotiables and the size rails this touches, and
   how. Re-state the welded invariant
   (max_concurrent_positions × max_loss_per_position ≤ daily_max_loss) with today's
   numbers and the numbers this implies. Anything that raises size or loss is a
   rollout-phase question (spec §10) and must say so.
8. Environment assumptions — the preamble §4 checkpoint, answered: what this assumes
   about the account, the broker, the operator and the host, which of those a human
   has confirmed, and what cert makes true for free that live will not.
9. Constraints on the design — the rules the design must satisfy (fail-closed
   direction, what may never gate what, what must stay config and never a literal,
   what must be visible on /status), each with its source.
10. Rollout and reversal — the phase it belongs to, what "minimum exposure first"
    means here, and the reversal rule fixed IN ADVANCE (the trigger and the rollback,
    in the shape of docs/decisions/2026-09-26-strike-touch-stop.md "Reversal rule").
11. Risk register delta — rows to add to spec §9 (risk, category, mitigation).
12. Open questions for Richard — numbered, each with your recommendation and why.
    Nothing that is the owner's call (a budget, a size, a policy constant) is decided
    in the body.

Keep it to what a reader needs to approve or reject it — a spec is not a design and
not a research note. If the evidence needed to state a goal or metric does not exist,
say what research (template 06) would produce it rather than inventing a number.

Finish with the bookkeeping in preamble §6 (docs/specs/README.md row; no CLAUDE.md
change until the spec is approved), commit, and push to the branch you were given.
````

---

## Notes for the operator (not part of the prompt)

- **Why a separate `docs/specs/`**: `docs/designs/` holds *how*, `docs/decisions/` holds
  what was *settled*. A spec is neither — it is the owner's intent before either exists,
  and it is the thing a design's §1 should be checked against.
- **When to skip it**: a defect fix or an incident follow-up already has its "what" in
  the investigation record — go straight to `02-design`. Use a spec when the change is
  *wanted*, not *found*.
- For a rollout-phase step (sandbox → live → scale), §7 and §10 are the whole document;
  expect the rest to be short.
