# One platform, a strategy per process (2026-10-22)

*The platform contract, stated on its own: how a second strategy runs on the same
Argus as the SPX book without sharing a process, a writer or a submission path.*

**Status: DECIDED.** This record states the shape the platform has had since 2026-10-07,
when it was produced by extracting shared code out of the SPX book for a second
strategy. It says nothing about what any other strategy does; a strategy's own design
and rules live with that strategy. The decision records in this directory remain
normative for the SPX book and for the platform rails.

## The shape

1. **One image, one entry point.** `python -m argus` dispatches on `ARGUS_STRATEGY`.
   Unset is the SPX book, exactly the path that existed before the registry:
   `Config.load()` then `Application`. A set value is looked up in a literal table in
   `src/argus/__main__.py` whose entries are dispatching functions; each imports its
   strategy's package only when it runs, so the SPX process never imports another
   strategy. An unknown value is refused, naming the strategies this build runs.
   "Unset is the SPX book" is not the kind of default this repository forbids: the
   forbidden default picked a *file* (and once the live database). This one picks
   nothing: unset still needs `ARGUS_ENV` and a matching `argus.<profile>.toml`.

2. **A strategy's own config file and loader.** A strategy reads
   `argus.<strategy>.<profile>.toml`: two dots, so the SPX book's filename rule
   (`CONFIG_FILENAME_RE`) never claims it and the SPX loader never reads it.
   `config.resolve_config_path(env, strategy=...)` derives that name with the same
   precedence as the SPX call (`ARGUS_CONFIG`, then `ARGUS_ENV` in `ARGUS_CONFIG_DIR`,
   else refuse) and makes an explicit path name the same strategy. A strategy's loader
   is strict (unknown keys refused) and checks the filename against its own `strategy`
   and `environment` keys, as the SPX loader checks its own.

3. **One scheduler, a window per strategy.** `SessionScheduler` takes an optional
   `window(day) -> (start, end) | None`. The default is `calendar.session_window`, the
   SPX book's, unchanged. Another strategy passes its own; one attempt per day per
   process is inherited.

4. **A process, a unit, a volume and a record per strategy.** Each strategy is its own
   always-on process under its own systemd unit, its own compose project with its own
   named volume, and its own per-host profile file under `/etc/argus/` naming its
   `ARGUS_ENV`. The strategy literal is in its compose file, never in the image or the
   profile. Nothing shares a writer: the SPX book's audit log is the SPX book's, and
   another strategy keeps its own record. The platform rails that are properties of a
   process (the kill-switch surface, the audit chain, reconciliation) belong to the
   process that trades; a strategy that sends nothing needs none of them.

5. **One deploy, which restarts what runs and never starts a unit.** The release builds
   one image per tag. `scripts/argus-deploy.sh` holds a STRATEGY UNITS table, one row
   per strategy process: its unit file, its per-host profile file, its config filename
   prefix, and the repo files its container bind-mounts. For each unit installed on the
   host it reads the profile, checks the tag carries that unit's config, syncs the unit
   file, `systemctl try-restart`s it (a stopped unit stays stopped) and reads the
   startup line back from that unit's journal. Change classification is per unit: a
   file only another strategy reads does not restart this one.

6. **One submission boundary per strategy, pinned by AST.** A strategy's shadow modules
   import nothing that can reach an order or an account. A strategy that sends has
   exactly one submission point of its own, and the SPX book's is
   `pipeline.trades_for_real`. The SPX book imports no other strategy, and the entry
   point imports a strategy's package only inside its dispatcher
   (`tests/test_strategy_registry.py` derives each package from the dispatcher's own
   import and checks the SPX process never loads it).

7. **Shared code moves only when a second strategy uses it** ("platform by
   extraction"). Code is not generalised in advance; it is extracted when a second user
   exists, unchanged for the first. The request governor (`broker/ratelimit.py`,
   extracted from `execution/ratelimit.py` on 2026-10-08) is the pattern: the SPX book's
   class and defaults, re-exported where its wiring and tests expect them.

## Shapes rejected

- **A second process on the SPX book's volume or database.** The audit log has one
  writer, enforced by a lock; a second strategy on the same database is the collision
  the lock exists to refuse.
- **A second strategy inside the SPX session loop.** It would share the session's stop
  event, the watchdog's clock and the entry gate; a halt in one would be a halt in both.
- **A timer-driven one-shot process.** The scheduler owns the window, the restart
  semantics and the single attempt per day; a timer would re-create them beside it.

## How to add a strategy

A new row in the registry (a dispatching function that imports the strategy's package
inside its body), a new row in the deploy script's STRATEGY UNITS table, the strategy's
own compose file, unit, profile file and `argus.<strategy>.<profile>.toml`, and its own
test modules (`tests/test_<strategy>_*.py`) pinning its row and its isolation. Nothing in
the SPX book changes.
