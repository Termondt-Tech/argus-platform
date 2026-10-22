# A `/status` seam drops what its source gained

**2026-09-02. BUILT. `src/` only (one file), no config key, no migration.**
Found while shipping the credit-stop fixes; out of scope there, raised and fixed in
the `/status` pass. Task prompt:
[`docs/tasks/argus_status_version_and_blackout_expiry_implementation_prompt.md`](../tasks/argus_status_version_and_blackout_expiry_implementation_prompt.md)
(Task 0).

---

## What was wrong

`/status` rendered **`mark: unpriced` for every spent exit, always**, from
2026-08-28 to 2026-09-02.

`_fmt_spent_mark` (`status.py`) took its `p.cost_to_close is None` branch on every
render. Not because the mark was missing — the watchdog had it, and the Slack page
carried it — but because the session seam between them never copied it across:

```python
# session.py, run_session, as written 2026-08-21
def spent_exits(wd: PositionWatchdog = trading.watchdog):
    return tuple(
        UnclosablePosition(
            external_identifier=p.external_identifier,
            candidate_id=p.candidate_id,
            contracts=p.contracts,
            outcome=p.outcome,
            spent_for_s=p.spent_for_s,          # <- five of eight
        )
        for p in wd.spent_exit_attempts()
    )
```

`SpentExitAttempt` carries **eight** fields. The three missing ones are the mark —
`cost_to_close`, `credit_stop`, `still_triggered` — and `UnclosablePosition` declares
all three with `None` defaults.

## Why it is worth a record

**Nobody wrote a bug.** The seam was correct on the day it was written (2026-08-21,
A2b): `SpentExitAttempt` had exactly those five fields. It went wrong on 2026-08-28,
when the exit-taxonomy pass added the mark to the SOURCE — the pass whose entire
stated point was that a held, triggered spread had gone 76 minutes with no surface
able to say what it was worth. The Slack page got the mark. `/status` did not, and
`/status` is the surface reachable from a phone.

**Every layer was structurally incapable of noticing.**

| Layer | Why it stayed quiet |
|---|---|
| `session.py` | a keyword omitted from a constructor call is legal Python |
| `UnclosablePosition` | `None` defaults, so construction succeeded |
| `status.py`'s renderer | `None` is a REAL state (`unpriced`) it must render, correctly |
| `tests/test_status.py` | hand-built the `UnclosablePosition` **with the fields populated** |
| `tests/test_watchdog.py` | asserted on `SpentExitAttempt`, upstream of the seam |
| `tests/test_session_trading_side.py` | AST-asserted the seam is PUBLISHED, not what it copies |

The last two rows are the shape of it: both halves were covered, and the join was
not. The suite was green throughout, at 1,860 tests.

**So the rule is:** *a copy between two dataclasses where the target has `None`
defaults cannot fail loudly, so it must be pinned structurally.* There is no runtime
signal available — that is what a default means — and a behavioural test written the
obvious way (build the target, assert the rendering) reproduces the defect rather than
catching it.

## What was built

`session.py` only. The three inline closures in `run_session` became one-line calls
to module-level converters — `_to_unclosable`, `_to_evaluability`,
`_to_entries_ended` — under a block comment naming the failure. The closures keep
their names, keep their `wd.spent_exit_attempts()` / `wd.impaired_positions()` calls
and keep sitting inside the `trading is not None` guard, so the three existing AST
tests in `tests/test_session_trading_side.py` bind unchanged.

Two kinds of test, and **neither alone is enough**:

- **Behavioural** (`tests/test_session_status_seams.py`): a real `PositionWatchdog`
  is driven until its one exit attempt is spent and a further tick produces a mark,
  then through the converter, `SessionStatus`, `StatusService.assemble` and into
  rendered HTML. **No status-layer row is constructed by hand anywhere in the file**,
  because hand-building one is exactly what missed this. It catches a MIS-wired copy
  (`credit_stop=p.cost_to_close` names every field and is wrong), which the sweep
  cannot.
- **Structural**: for each converter, the target construction's keyword set must equal
  the source dataclass's field set, and the source's and target's field sets must
  match each other. It catches a field added to a source that nobody has thought to
  assert on yet — the actual mechanism here — on the day it is added. Same family as
  the `credit_payload()` and `reject_reason` sweeps.

Plus a third test that is the point of doing it at all:
`test_the_sweep_covers_every_converter_in_the_seam_block` fails if a `_to_*` converter
is added to `session.py` without joining `_SEAMS`. The
[pending `unmanaged` design](../designs/2026-09-02-status-unmanaged-positions.md) adds
two more seams of exactly this shape, and they now cannot be added without the
protection.

Both were mutation-checked: deleting the three field copies fails the behavioural test
AND the sweep; the sweep alone passes when the copy is present but wrong.

## What was checked and found correct

- `_to_evaluability` copies all five of `ImpairedPosition`'s fields — complete.
- `_to_entries_ended` builds a different shape (`EntriesEnded` → `EntriesEndedToday`,
  None-or-one rather than a sequence) and copies all four — complete.
- `spent_exits` was the only wrong one.

## What was deliberately NOT changed

- **Nothing in `execution/`.** `SpentExitAttempt` already carried the mark and
  `spent_exit_attempts()` already populated it correctly.
- **The three-state rendering.** `None` still means "that tick could not price it" and
  still renders `unpriced` — never a placeholder number, on a row whose entire purpose
  is telling a human what to do by hand. This made the other two states *reachable*;
  it did not change what `None` means. A test pins the `None` path so the fix cannot
  regress it in the opposite direction.
- **What a spent exit is, when it clears, or the one-attempt rule.** Untouched.

## Deploy

`src/` only, one file. No config key, no migration, no coupling — the image deploys
alone. Cert cannot prove this one either way; it is a rendering defect, and the tests
are its coverage.
