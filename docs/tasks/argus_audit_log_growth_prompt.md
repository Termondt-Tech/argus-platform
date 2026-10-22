# Argus: the audit log grows without bound, and one category is 82% of it

## Priority

**DESIGN FIRST — do not implement without Richard's sign-off.** This changes what
the black-box recorder records. Nothing here touches a safety rail, but the
thing being edited is the evidence trail every post-mortem in
`docs/decisions/` was written from, and **audit data cannot be regenerated**. A
category written thinner today is a question that cannot be asked of that day
ever again.

**This is NOT a live-flip gate and must not block one.** It is not urgent as a
disk problem — 700 MB/month is fine on the droplet for well over a year, and R2
charges cents. It earns a task because **five separate costs are now linear in
table size**, and the Litestream work moved two of them onto paths where they
are felt.

**Sequencing: AFTER the Litestream deploy, not before, and not coupled to it.**
It was surfaced by that design
([`docs/designs/2026-09-03-litestream-r2-replication.md`](../designs/2026-09-03-litestream-r2-replication.md)
§19.7) and the two share numbers in both directions — see
[Interaction with Litestream](#interaction-with-litestream).

**The framing matters more than usual here, so it is stated up front: this is
not "prune the audit log."** Pruning is close to unfixable (below) and every
minute spent there is wasted. The tractable question is *why one Tier 2
telemetry category is 82% of a hash-chained ledger*, and there is a precedent in
this repo for exactly that move.

## Current state and the gap

Nothing prunes, rotates, archives or vacuums `audit_log`. There is no retention
policy, no `DELETE FROM audit_log` anywhere in `src/` or `scripts/`, and no
design that anticipates one. The table has grown monotonically since the first
shadow session.

**Measured growth** (from the 2026-08-31 session, the shape `argus.toml`'s
`[audit]` comment says to size against — two positions watched all session):

| | |
|---|---|
| Records/session | **46,138 Tier 2 + 5,308 Tier 1 ≈ 51,400** |
| Rate | ~2.3 records/sec total, ~0.2/sec Tier 1 |
| Average row, logical bytes | **~618** (measured over a real chain) |
| Growth | **≈ 33 MB/day, ≈ 700 MB/month** over ~21 trading days |

**And the distribution is not flat. `watchdog.check` alone was 37,965 of 46,138
records — 82% of everything the process wrote that day, from one category.**

It is Tier 2, written once per position per tick from the *not-fired* branch of
`WatchdogLoop.check_once` (`src/argus/execution/watchdog.py:2639`), and its own
comment already names what it is:

> `# The routine case by volume, and Tier 2 for exactly that reason: a`
> `# no-action check carries no crash-survival requirement, and at this`
> `# cadence these would otherwise drown the Tier 1 trail.`

That reasoning was right about the tier and silent about the volume. The payload
is also wide — `external_identifier`, `candidate_id`, `contracts`, then
`**spread.credit_payload()`, `**evaluation.to_payload()`, and a `sustaining`
dict built per tick — which is most of the measured 618 bytes.

**So the growth problem is mostly one category, and the category is a per-tick
heartbeat of "nothing happened."**

### Why "just prune it" is the wrong first move

`AuditLog.verify_chain()` asserts `seq` contiguity from 1 and each row's
`prev_hash` link. **Deleting any row breaks verification permanently.** There is
no soft-delete, no tombstone, and no partial-verification mode — by design,
because a chain you can delete from is not evidence.

Any archival scheme therefore has to *roll to a new database and keep the old
one*, which drags in `_load_head()`, the single-writer flock, migrations,
`positions`/`orders` (which live in the same file and are read back as expected
state by reconciliation), the Litestream replica path, and every read-only tool
that opens `argus.db` by name. **That is its own large design and it is
explicitly OUT OF SCOPE here.** It is the follow-on if writing less turns out not
to be enough.

### The precedent

**Fix C (2026-08-18)** did the analogous move one tier up: it re-levelled
`watchdog.unevaluable` from Tier 1 to Tier 2 `watchdog.degraded`, cutting 1,517
awaited fsync'd writes a session. Total record volume *rose* afterwards and the
cost still fell, because the fix was aimed at the right axis. Read that arc in
[`docs/decisions/2026-08-13-live-fire-sandbox-pass.md`](../decisions/2026-08-13-live-fire-sandbox-pass.md)
and the `argus.toml` `[audit]` comment before proposing anything — including its
warning that **the rate scales with POSITIONS WATCHED, not with time**, so any
before/after must compare like-for-like days.

## Why this is worth doing at all: the five linear costs

Disk is the least of them. In rough order of sharpness:

1. **`AuditLog._load_counts()` — a `GROUP BY category` FULL SCAN at every
   process start.** Read its docstring: moving it off the 15-second status poll
   "turns an unbounded recurring cost into a bounded one-off." *Bounded per
   process* — it is still O(rows), now paid once per boot, **including the
   mid-session recovery restart where seconds matter most** (2026-09-03's manual
   15:37 restart is the live example). **This is the one to measure first**, and
   the design must state the number at the current table size rather than assert
   it is fine.
2. **`verify_chain()`** — O(rows) with a JSON parse and a `sha256` per row. The
   Litestream design puts it on the restore path (its §6.4) and in a nightly
   drill (its §11.2). Both grow forever.
3. **Litestream restore RTO** — driven by the WAL *object* count and snapshot
   size (Litestream design §12.3).
4. **R2 storage** — retention keeps ~8 LZ4-compressed copies of the whole DB, so
   every byte written is multiplied (Litestream design §8.1).
5. **Droplet disk** — ~7 GB/year at the current rate, and a full disk takes
   SQLite down with it, which is the failure the compose log-rotation comment
   already names.

None of these is on fire. Together they are a slope with no top, and the cheapest
intervention is at the source.

## Interaction with Litestream

**Read this before sizing anything, in either document.**

- **A change here moves the Litestream numbers.** If `watchdog.check` volume
  drops materially, that design's §8.1 storage table and §12.3 RTO object count
  are both stale. Whichever ships second re-reads the other's numbers, and says
  so in its decision record.
- **Moving rows to a different TABLE in the same file buys nothing for costs 3,
  4 and 5.** Litestream replicates the *file*. Only two moves help: **write
  fewer bytes**, or **write to a different file** — and a different file
  collides with the one-DB, one-writer, one-flock design and with
  reconciliation reading `positions` out of the same database. Weigh that
  honestly; do not assume a side table is free.
- **Do not couple the deploys.** The Litestream work has its own acceptance bar
  and its own confounder discipline; landing a write-volume change inside its
  measurement window destroys both readings.

## The consumers, and what they actually need

This is the constraint set, and it is sharper than it looks. **Both were read
from source; confirm them rather than trusting this list.**

**`tools.desk` (`read_marks`, `desk.py:137`)** — scans every `watchdog.check` in
the date range and keeps the **LAST one per `candidate_id` that has a non-null
`cost_to_close`**. It reads five payload fields: `candidate_id`, `contracts`,
`cost_to_close`, `credit_stop`, `unknown_reasons`, plus `ts`.

> **The subtlety that breaks the naive fix:** it is the last *priceable* tick,
> not the last tick. An unpriceable check is deliberately skipped so it cannot
> overwrite a good mark — "reporting a stale price is better than reporting
> none, and far better than reporting None as a number." Any "write only the
> final tick" scheme has to reproduce that, and it cannot, because it does not
> know at write time which tick will be the last priceable one.

**`tools.evaluability`** — uses `watchdog.check` **only as a count**
(`ticks = counts.get(CHECK)[1]`, `evaluability.py:242`). It reads **no payload
field at all**. That count is the denominator for "% of ticks blind / degraded",
which is the instrument the settle-allowance decision is sized from. Downsampling
breaks the denominator unless the tick count is preserved some other way.

So the two consumers want opposite things — **one row per position per day with
a payload, and a total tick count with no payload** — and neither wants the
37,965 rows currently written to serve them.

## Design questions to resolve

### Volume

1. **What is the real category distribution on the droplet, today?** Not the
   2026-08-31 figures reproduced above — a fresh read, over several days,
   because the rate scales with positions watched. `tools.evaluability` already
   prints the tier split; a category histogram may want a small addition or a
   one-off read-only query. **Everything below is sized from this.**
2. **Is `watchdog.check` the only category worth touching?** Name the top five
   by bytes (rows × mean payload), not by row count — a wide category at low
   cadence can beat a narrow one at high cadence. Say explicitly which ones are
   left alone and why.
3. **Row count versus payload width are two independent levers.** `desk` uses
   five fields of a payload that carries `credit_payload()`, `to_payload()` and
   a `sustaining` dict. Quantify what a narrower `watchdog.check` saves on its
   own, before any change to cadence — it may be most of the win at a fraction
   of the risk.

### Shape

4. **Edge-triggered, downsampled, slimmed, or moved?** Evaluate at least:
   (a) narrow the payload only; (b) write on *change* plus a periodic keepalive;
   (c) write every Nth tick plus always the last priceable one per position per
   day; (d) move it out of the chain entirely. For each: what `desk` loses, what
   `evaluability` loses, and what a future investigation loses.
5. **If the tick count must survive, where does it live?** A counter on a
   lower-cadence record is the obvious answer; say what it is attached to and
   what happens to it across a restart, a day rollover, and a position that
   closes mid-session.
6. **Does a per-tick mark belong in a hash chain at all?** The chain is a
   tamper-evident record of decisions and actions. A "nothing happened" tick is
   a time series. If the answer is no, `Tier` may need a third level or the
   record may need to leave — and both of those are bigger than they look. State
   the case; do not assume it.

### Consequences

7. **Historical continuity.** After the change, `desk` and `evaluability` read
   two eras of data. Do they need to be version-aware, and if so keyed on what —
   a date, a schema marker, the presence of a field? **Never on `environment`**
   (`tools.mark`'s rule: keyed on CATEGORY PRESENCE, never on environment).
8. **What can no longer be asked?** List the questions that become
   unanswerable — concretely, against real past investigations. At least one
   `docs/decisions/` record was written from per-tick data; find it and say
   whether it could still have been written.
9. **`/status` and the live surfaces.** Confirm nothing on the monitoring path
   reads `watchdog.check`, and that `_load_counts`'s per-category tally (which
   `/status` renders) still means what the page says it means.
10. **The mid-session restart cost.** Does the change measurably move
    `_load_counts()` at startup? If the answer is "not enough to matter", say so
    with the number — and then say whether `_load_counts` needs its own separate
    fix regardless, because that is a defect this task can *reveal* without
    being obliged to solve.

## Deliverables

1. A **design proposal** in `docs/designs/`, following the lifecycle in
   [`docs/designs/README.md`](../designs/README.md): measured category
   distribution, the shapes considered with what each costs the two consumers,
   one recommendation, the historical-continuity answer, and the numbered
   decisions requiring sign-off.
2. **The measurement**, first and separately — the category histogram by bytes,
   the `_load_counts()` timing at the current table size, and the projected
   growth with and without the change. **No shape is proposed before this
   exists.**
3. **The consumer-impact statement** — `desk` and `evaluability`, field by
   field, including the last-priceable-tick subtlety.
4. **The Litestream cross-check** — which numbers in
   `docs/designs/2026-09-03-litestream-r2-replication.md` this invalidates, and
   the ordering rule for whichever ships second.
5. On sign-off: the implementation, a **decision record** in `docs/decisions/`
   indexed in its README, and the CLAUDE.md line. `docs/argus-deployment-runbook.md`
   §15 gains the archival follow-on as a named deferred item.

## Non-negotiables this must respect

- **The hash chain is not editable.** No deletes, no rewrites, no
  soft-delete, no partial verification. `verify_chain()`'s contiguity assertion
  is the property, not an obstacle to route around.
- **A failed audit write still halts trading, and the Tier 1/Tier 2 split is
  untouched.** This task changes what is *written*, never what a write *means*.
  Tier 1 volume is 0.2/sec and is not the target.
- **Audit data cannot be regenerated.** Every byte not written is a question
  that cannot be asked of that day, ever. When the call is close, write it.
- **`NEVER TOUCH THE LIVE DATABASE`.** All measurement is read-only
  (`file:argus.db?mode=ro`); all dev/testing uses `argus.dev.toml`.
- **Do not confound the Litestream measurement.** No write-volume change lands
  inside its baseline or acceptance window.
- **The rate scales with positions watched, not with time.** Any before/after
  compares like-for-like days, or it compares nothing.

## Out of scope

- **Archival, rotation, or rolling to a second database.** Named as the
  follow-on; not this task. If the recommendation is "writing less is not
  enough", that is a legitimate finding — say it and stop.
- Any change to `audit.synchronous`, the Tier 1/Tier 2 model, the hash itself,
  or `db/lock.py`.
- The Litestream design's own open items (§19), including the LZ4 ratio and the
  `generations` lag threshold.
- `.env` having no off-box copy (Litestream design §16) — separate, and not a
  database problem.
