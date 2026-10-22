# Argus platform

**A reference implementation of a fully automated 0DTE SPX credit-spread trading platform,
built by guiding an AI coding agent under a written constitution.** It ran real money at
minimum size from 2026-09-15 to 2026-10-01, and its first strategy was shut down on pre-registered
evidence that it loses. It is published so the rails, the tests that pin them, and the
method can be read. It is not a product and is not maintained on a roadmap. Nothing in it is
investment advice. The example strategy lost money (`RESULTS.md`). The platform ships with
shadow as the default, refuses to trade live without explicit configuration and credentials,
and is designed to be monitored and stopped by a human.

Two articles accompany this repository; part one links here, and `RESULTS.md` points at
part two.

## What is here

| Read | For |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | **The constitution.** The file the coding agent loaded into every session: the objective, the non-negotiable rails, the standing landmines, the file map. It is the headline artifact; everything else is evidence for it. |
| [`docs/decisions/`](docs/decisions/README.md) | Every settled decision, one record each, normative. Read the relevant record before changing the component it covers. |
| [`docs/architecture/`](docs/architecture/00-index.md) | The shape of the system (C4 and UML), derived from source, and the best-practices standard the design was held to, with the three places it departs and why. |
| [`docs/0dte_spx_desk_spec.md`](docs/0dte_spx_desk_spec.md) | The original design spec. Its rollout sections are historical. |
| [`src/argus/`](src/argus/) | The platform: one `asyncio` process, stdlib apart from the broker SDK. Signal engine → pre-trade gate → execution engine → broker; the position watchdog as a separate task; a hash-chained audit log written before the next action proceeds. |
| [`src/argus/signal/`](src/argus/signal/) | The retired example strategy, kept as the worked example. Its verdict is `RESULTS.md`. |
| [`tests/`](tests/) | About 3,100 tests. The invariant tests pin the rails by structure, several by AST: for example [`test_kill_scope.py`](tests/test_kill_scope.py), [`test_kill_persistence.py`](tests/test_kill_persistence.py), [`test_halt_scope.py`](tests/test_halt_scope.py), [`test_credential_slots.py`](tests/test_credential_slots.py), [`test_execution_pipeline.py`](tests/test_execution_pipeline.py). |
| [`docs/go-live-findings.md`](docs/go-live-findings.md), [`docs/live-cutover-runbook.md`](docs/live-cutover-runbook.md) | The going-live record: what gated the flip, what closed it, what stayed unproven. |
| [`docs/operations.md`](docs/operations.md) | The operating guide as it was run, redacted. |
| [`docs/releases/`](docs/releases/README.md), [`docs/designs/`](docs/designs/README.md), [`docs/reviews/`](docs/reviews/), [`docs/tasks/`](docs/tasks/) | Release notes, design proposals, reviews, and the prompts the agent was given. |
| `PROVENANCE.md` (private record, not published) | How this tree was made from the private repository, and what was left out. |

## The method, in one paragraph

A human wrote and kept the constitution; the agent built under it, one task at a time, with
a decision record for every rule that was settled and a test that pins each rail by
structure rather than by example. The constitution is loaded into every session, so the
rails compete for the agent's attention on every change; the records keep the reasoning
out of it. The incidents that shaped the rails are in the records, dated.

## Running the tests

Python 3.12.

```
python3 -m venv .venv
./.venv/bin/python -m pip install -e ".[dev]"
./.venv/bin/python -m pytest
```

The suite boots credential-free and reaches no network; it runs in about a minute. The
same suite runs in CI on every push (`.github/workflows/ci.yml`), with the production
image built on the architecture the platform was deployed on.

## Running shadow against the broker's test environment

Shadow runs the whole pipeline and **sends nothing**: its terminal action is a record
(`shadow.would_submit`), asserted by an AST test. It needs a tastytrade **certification**
(sandbox) OAuth registration of your own.

1. `cp .env.example .env` and fill in the `TASTYTRADE_TRADING_CERT_*` pair. Set
   `ARGUS_KILL_TOKEN` to any secret string; the process refuses to start without one,
   because an unauthenticated halt control is a broken rail.
2. `mkdir -p dev`, then `ARGUS_ENV=dev ./.venv/bin/python -m argus`. The `dev` profile
   (`argus.dev.toml`) is `environment = "shadow"` with the sandbox's delayed market
   data, and writes its database and writer lock under `dev/`, which it expects to
   exist. There is deliberately no default profile.
3. Read the status surface, bound to loopback in `dev`:
   `curl -H "Authorization: Bearer $ARGUS_KILL_TOKEN" http://127.0.0.1:8443/status`.
   `POST /kill` on the same listener halts new entries; `POST /resume?confirm=yes` is the
   only thing that clears it.

Live trading is refused unless all of these hold at once: a profile file named
`argus.live.toml` whose own `environment` key says `live`; a pinned
`[broker] account_number`; a separate `TASTYTRADE_TRADING_LIVE_*` credential pair that
differs from the sandbox pair; and synchronous audit writes. The live profile here ships
with a placeholder account. Read the constitution's non-negotiables before considering it,
and then read `RESULTS.md` again.

## What this is not

- Not maintained. Issues are open for reading notes, questions and defects; there is no
  roadmap and no promise of a fix. Pull requests cannot be merged: this tree is a one-way
  export, and the next export would overwrite them.
- Not advice, and not a strategy to run. The example strategy's verdict is in `RESULTS.md`.
- Not the whole private repository. The owner's second strategy, the research programme,
  the host's runbooks and anything identifying the account, the host or the household are
  not here (`PROVENANCE.md`).

## Licence

Code under the Apache License 2.0 (`LICENSE`, `NOTICE`); documents under Creative Commons
Attribution 4.0 (`LICENSE-docs`).
