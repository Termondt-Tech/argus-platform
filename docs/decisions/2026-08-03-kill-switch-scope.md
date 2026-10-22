# Kill-switch scope — what a trip actually stops (resolved 2026-08-03)

*What a kill-switch trip actually stops, and what it deliberately does not.*

> Relocated from `CLAUDE.md` on 2026-08-12. Settled decisions - do not
> relitigate. See [`docs/decisions/README.md`](README.md) for the index.


Investigated from source because the architecture pass could not confirm it, then
fixed. The finding was real: `POST /kill` used to call `KillSwitch.engage` and
nothing else, whose only functional consumer was `PreTradeGate` — so a walk already
in flight kept walking and **could still fill** after the operator pressed the
button. The scope is now deliberate and asymmetric. Do not relitigate:

- **A trip stops NEW RISK at three points**, all on the entry side: the gate
  (`risk/gate.py`, unchanged); `EntryExecutor.submit`'s pre-build re-check, which
  closes the gate/submit race (the two are separated by an awaited Tier 1
  `gate.decision` write, so a trip landing in that window would otherwise send an
  order already halted — records `entry.halted`); and an **entry walk already at the
  exchange**, via an injected `halt_check` (`loop.HaltCheckFn`) polled at each cycle
  boundary → `ABORTED` + cancel, bounded by one `cycle_wait_s`.
- **It deliberately does NOT stop exits or the watchdog.** `ExitExecutor` arms no
  `halt_check` and an AST test pins that. Cancelling the order closing a position
  would leave the operator holding exactly the risk they pressed the button to shed
  — the inverse of what a kill switch is for. `stop` is still passed, so a session
  close or SIGTERM does end an exit walk; a trip does not.
- **It does NOT stop the process.** The switch owns no `asyncio.Event` (a test
  asserts this), so `/status` stays up and the trip survives day rollover.
- **Wiring `/kill` to `Application._halt_process` was CONSIDERED AND REJECTED.**
  `_halt_process` sets the process stop event, which `scheduler.py`'s `stop_bridge`
  bridges to the **session** stop event — and that is the same event the watchdog
  and every exit walk read. So the phone button would have cancelled in-flight
  closing orders and stopped watching the book. Right for a corrupted-state halt
  (audit failure, reconcile mismatch, entry/watchdog exception), inverted for a
  panic button. The two mechanisms stay separate on purpose, and `_halt_process`
  stays reachable only from internal conditions.
- **A trip survives day rollover, which makes any DAILY trip source a permanent
  halt.** The switch is process-lifetime on purpose (above), so a source that fires
  once per session start does not halt one day — it halts every day until someone
  restarts. `[heartbeat] trip_on_sustained_stall` is exactly that source today and
  is why it must stay `false`; see "The warmup stall" below. Weigh this against any
  future automatic trip source: "would this fire on an ordinary morning?" is a
  different and more important question than "is this condition bad?".
- **Every trip alerts Slack, from inside `KillSwitch.engage`** — the one funnel all
  sources pass through (HTTP endpoint, heartbeat stall, audit-failure hook,
  reconcile mismatch, entry/watchdog exception halts), so no future caller can
  forget it and `engage`'s idempotency makes it exactly once per trip. Fired BEFORE
  the audit write (which goes through `to_thread` and can be slow or fail) and
  wrapped so a raising sink can never undo a halt.
- **The abort is at CYCLE granularity, and that bound is deliberate.** Interrupting
  `_hold` would cancel inside the COA window and race a straggler auction response
  — the exact failure `cycle_wait_s` exists to prevent. The gate's refusal is
  instant; the in-flight cancel takes up to one cycle.
- `WalkResult` is still a REPORT, not a guarantee: a fill can cross a cancel, so
  `positions` and reconciliation stay authoritative. `_stop_reason()` returns the
  REASON (not a bool) so `walk.finished` distinguishes a kill from a session close —
  they are the same outcome and completely different post-mortems.

Tested end to end in `tests/test_kill_scope.py` — a real TCP request to a real
`ControlServer`, into the real `KillSwitch`, reaching a real `PriceWalkingLoop`,
with the exit-walk case as the negative control. That file exists because the gap
was a TEST gap first: `test_control.py` wires the server to a bare switch with no
session behind it, so nothing had ever exercised "press it while an order is
working". Audit categories added: `entry.halted` (Tier 1).
