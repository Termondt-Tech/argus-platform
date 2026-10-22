# Retrospective: the dedicated-account requirement surfaced at go-live, not at design (2026-09-01)

**Type: internal retrospective / lessons-learned. Not normative.** Nothing here
changes code or config. The design response to the finding itself lives in
[`docs/decisions/2026-08-31-live-account-is-not-dedicated.md`](../decisions/2026-08-31-live-account-is-not-dedicated.md);
this document is about *how the gap got to go-live planning undetected* and what
we change so the next one does not.

---

## 1. What happened

On 2026-08-31, while planning the live cutover, Richard identified that Argus
requires a tastytrade account **dedicated to Argus** — nothing else may open
positions in it — and that the live account he intended to use is a shared
trading account (manual trades, other strategies, Treasuries).

A same-day design pass confirmed, against source at `main` 3be57b5, that this is
a **hard live-flip blocker**: four account-derived rails
(reconciliation, the concurrency cap, the daily max-loss breaker's P&L basis,
the relative size rail) each read a **whole-account** number and treat it as an
**Argus-only** number. Pointing `environment = "live"` at a shared account does
not degrade — it hard-halts at the first reconcile, before any session is built,
and would block every entry forever if it somehow got past that.

This was found **~7 weeks and 150 commits into the build** (first commit
2026-07-14), after Phase 0 (shadow) and most of Phase 1 (sandbox live-fire) were
complete and the pre-flip checklist was being worked. It was found by planning,
not by an incident, and before any real money moved.

## 2. Timeline of the assumption

| When | Event | What it did to the assumption |
|---|---|---|
| 2026-07-14 | Spec written (`0dte_spx_desk_spec.md`), spun out of the TOMIC strategy roadmap | Spec §8 / §11 size Argus as "2% of account, graduating toward TOMIC's 5% norm" — language that describes **a fraction of a shared portfolio**. Spec §5 says "direct reuse" of TOMIC's `AccountStreamCache`. The spec's mental model is *Argus is TOMIC's 0DTE desk*. It never states "dedicated account" and never lists it as an open question — §11 raises only "separate **app** credentials". |
| 2026-07-17 | Reconciliation designed: broker positions `==` Argus's expected `positions` table, refuse to resume on any mismatch | The assumption becomes **load-bearing** — and invisible. `reconcile_positions` walks `set(expected) | set(broker)`; a whole-account set union is only a correct comparison when nothing else trades the account. Nobody reconciled this with the spec's shared-portfolio sizing language. |
| 2026-07 → 2026-08 | `open_position_count`, `DailyPnl` anchor, the relative size rail, `held_but_unbooked` all built | The assumption is now relied on in **five places at once**, in each case as the *absence of a scope*. No decision record captures it, because nothing was decided — a default was inherited. |
| 2026-07-29 → 2026-08-31 | Continuous cert / sandbox testing, including two live-fire passes and ~15 sessions | Every reconciliation test passes. The cert account is dedicated to Argus **by nature** — nothing else can trade it — so the one property that would expose the gap (a foreign position in the account) is not reachable in the test environment. |
| 2026-08-13 | Live-fire pass names the daily max-loss breaker "structurally inert in sandbox … proving it is a LIVE-FLIP GATE" | We had a working notion of "things cert cannot exercise". The dedicated-account assumption belonged on that list and was not on it. |
| 2026-08-31 | Go-live planning | Richard supplies the environmental fact the build never had: the live account is shared. Blocker identified. |

## 3. Root causes

Five, and they compound. None is "someone made a mistake"; each is a class of
blind spot.

### 3a. The assumption was never written down, because nothing was decided

Decision records are very good at capturing choices that were **made**
deliberately, usually with a measurement or an incident behind them. They are
structurally blind to defaults that were **assumed**. "The account is the
strategy" was never a decision — it was the shape the first design naturally
took, and every subsequent rail inherited it. An assumption that lives as the
*absence of a scope* in five functions is invisible to code review: there is no
line to point at.

### 3b. The spec was internally inconsistent and nobody ran a consistency pass

§8 and §11 size Argus against a shared portfolio ("2% of account … toward
TOMIC's 5% norm"). §5's state model and the reconciliation design assume a
dedicated one. Those two halves of the same document describe **different
accounts**. A spec-consistency check — *do the sizing section, the state model,
and the reconciliation section all describe the same account?* — would have
caught it on day one.

### 3c. The test environment could not exercise the failing property

Cert and sandbox accounts hold only what Argus puts in them. Every reconciliation
pass, every `open_position_count` assertion, every `held_but_unbooked` test ran
against an account that was *genuinely dedicated*. The environment silently
supplied the assumption, so the assumption never had to be stated to make the
tests pass. We already knew one property cert cannot exercise (the breaker's NLV
basis) and tracked it explicitly; we did not generalize "what else does cert make
true for free?"

### 3d. Phase gates checked mechanism, not the mechanism's preconditions

Shadow → sandbox → live-min-size → scale validates *execution mechanics*: does
the walk reprice, do fills book, does the watchdog adopt a position. No phase
asked *what will the live account actually contain, and what else touches it?*
The rollout plan (spec §10) has no "target-environment reality check" step.

### 3e. "Spun out of TOMIC" was read as full isolation when it was partial

Argus got its own repo, its own process, its own deploy, its own credentials
guidance — and the mental model "Argus is a separate system" quietly extended to
"…therefore a separate account." Repo separation was decided and documented.
Account separation was **neither decided nor ruled out** — it was assumed by one
side and not by the other. When a component spins out of a parent system, the
isolation boundary needs to be stated in full: what *is* separated, and equally
what is *not*.

### 3f. (Contributing) Solo developer + AI, with no mitigation for architectural blind spots

The risk register (spec §9) lists "Solo-developer blind spot" — mitigated only by
"kill switch + conservative sizing", both of which are *runtime* safety, not
*design-time* review. The AI build partner built cleanly and correctly against
the assumption it was given, and never challenged it — a dedicated-account design
is genuinely simpler and looks *more* correct, so there was no friction to
prompt the question. The human held the environmental context (what's in the real
account) that would have flagged it; the model held the architectural view (this
assumption is load-bearing in five places) that would have made it worth raising.
Neither surfaced it to the other.

## 4. What went right — do not over-correct

- **The failure mode is loud and safe.** A shared account hard-halts at the first
  reconcile, then *parks and serves* `/status` and `/kill` (2026-08-17
  halted-but-alive). It never trades wrongly. Fail-closed worked exactly as
  designed.
- **Caught in planning, before the flip.** No money moved. The cost was a design
  pass, not an incident.
- **The blast radius is confined to four rails.** Every *order*- and
  *ledger*-derived path — the exit executor, the watch list, `DuplicateOrderGuard`,
  the residual-order recheck, the working-order term of the concurrency cap — is
  correctly scoped by Argus's own `external_identifier`. Argus **cannot close,
  flatten, or cancel a foreign position or order**. The "scope by our own minted
  identifier" discipline paid off everywhere it was applied; the gap is precisely
  where a rail reads the broker's *account-level* aggregates instead.
- **The design response was cheap.** Because the rails are *correct for a
  dedicated account*, the recommendation is one config key
  (`[broker] account_number`) plus a live-guard — not a rewrite. An assumption
  made true is better than an assumption worked around.

The lesson is not "the design was wrong". The rails are well-built. The lesson is
that a load-bearing environmental assumption rode to go-live unwritten and
unverified.

## 5. Lessons

1. **An unwritten assumption relied on in N places is an N-way single point of
   failure.** The absence of a scope is not reviewable. If a rail's correctness
   depends on a fact about the deployment environment, that fact is an artifact,
   not a vibe.
2. **The test environment supplies assumptions for free, and hides them by doing
   so.** A green suite against cert proves the mechanism works *given cert's
   properties*. Enumerate those properties.
3. **Phase gates that only test mechanism will pass a system whose preconditions
   are false.** A gate needs a step that looks at the actual target.
4. **"Separate system" is a set of specific separations, not a single boolean.**
   State each one.
5. **Spec-internal contradictions survive if nobody is assigned to look for
   them.** Especially across sections written at different altitudes (sizing vs.
   state model).
6. **In an AI-assisted build, environmental/organizational context is the
   human's to supply and the model's to interrogate.** The model should
   enumerate what it is assuming about the deployment; the human confirms or
   corrects.

## 6. Concrete process improvements

Ordered by leverage. These are proposals for Richard to adopt, adjust, or reject;
none is a code change.

### I1 — Assumptions register, reviewed at every phase gate

Create `docs/assumptions.md` (analogous to the spec's risk register). One row per
load-bearing assumption about the world outside the code:

| Assumption | Where it is relied on | What breaks if false | How it is verified, and by whom | Last verified |

Seed it now with at least: the dedicated account; market-data entitlement
separation from TOMIC (see the concurrent-operation assessment); "SPXW is the
only index-option product in the account"; "no cash moves into/out of the account
on a trading day"; the COA timing values; blackout-date file currency. Review the
whole register at each phase gate and record the date.

### I2 — A "target-environment reality check" step in the rollout plan

Before live-min-size, a written enumeration, confirmed by Richard:

- What positions/instruments does the live account hold right now?
- What else opens or closes positions in it (people, other strategies, standing
  orders)?
- What else connects to the same tastytrade **customer login** (not just the
  same app registration)? What are the market-data entitlements and streamer
  limits on that login, and who else draws on them?
- What moves cash in or out of it, and on what schedule?

The cutover runbook's new checklist items are a start; I2 is the general form.

### I3 — Isolation-boundary doc when a component spins out

A short `docs/decisions/*-isolation-boundary.md` stating, for Argus vs TOMIC:
what is separated (repo, process, deploy, DB, kill switch, app registration) and
— in the same list — what is **not, and why that is acceptable or what makes it
so** (the brokerage account, until now; the tastytrade customer login and its
market-data entitlements; the host, if co-located). Absence of a statement is
not a decision.

### I4 — Track "what the test environment makes true for free" alongside
"what it cannot exercise"

We already maintain the latter (the breaker's cert-inertness, `open_questions` in
`docs/decisions/README.md`). Add the mirror list: properties cert/sandbox
**satisfies by construction** that live will not — the dedicated account was one;
realistic fill behaviour is another already known. Each entry is a candidate
live-flip gate.

### I5 — Spec-consistency pass as a named review

Once per significant spec revision: do the sizing section, the state/reconciliation
model, and the risk register all describe the same account, the same capital, and
the same operating context? Record that the check was run.

### I6 — Standing design-checkpoint prompt for the AI build partner

At each design document or decision record, the model explicitly answers: *what
am I assuming about the deployment environment, the account, the operator, and
the other systems that share infrastructure with this one — and which of those
has a human confirmed?* Cheap, repeatable, and it converts 3f from a hope into a
step.

## 7. Follow-up items owned elsewhere

- The **design fix** (dedicated account, `[broker] account_number`, config guard,
  runbook, `CLAUDE.md` wording) — [decision record](../decisions/2026-08-31-live-account-is-not-dedicated.md),
  awaiting Richard's sign-off.
- **Running TOMIC and Argus live at the same time** — its own assessment and
  action list:
  `docs/tomic-argus-concurrent-operation-assessment.md` (private record, not published).
- The **live-flip checklist** gains the three items named in the decision record
  (account is named and dedicated; account is empty of non-Argus positions/orders;
  NLV clears `max_loss_per_position / position_size_pct`).
