# Trading credentials are environment-keyed, and every slot says which world it is for

**2026-09-09. OPTION (b) SIGNED OFF AND BUILT the same day** — `src/` + tests +
`.env.example` + docs; **no config KEY, no migration, no audit category, no rail
changed**, so the image deploys alone. Guarantees (i), (ii) and (iii) all shipped,
and the `/status` credential row (question 4) with them. Raised by the go-live
cutover work (`docs/go-live-findings.md`, the OPEN row *"Live trading OAuth app
registration + credentials"*).

**THE DEPLOY IS NOT SELF-CONTAINED — `.env` must be edited FIRST**, on every host
and dev machine, or the process refuses to start. That is §5.2's step 1, and it is
the one part of this no image can carry. Questions 2, 3, 5 and the new 7 in §7 are
still open; §6 records a claim this design got WRONG and the build corrected.

The runbook says the live cutover is one file changing on the droplet
(`/etc/argus/host.env`, [config profiles](2026-09-04-config-profiles.md) step 2).
**That claim is currently false.** The trading session's credentials are not
keyed on `environment` at all, so the cutover is also an unrecorded, unreversible
hand-edit of `/opt/argus/.env` — a second change, in a second file, that the
documented procedure does not mention and the deploy cannot see.

---

## 1. What is there now

### The two credential pairs

`src/argus/broker/session.py:79-84` declares four variable names, in two pairs:

```
DEFAULT_SECRET_VAR  = "TASTYTRADE_CLIENT_SECRET"        # :79
DEFAULT_REFRESH_VAR = "TASTYTRADE_REFRESH_TOKEN"        # :80
LIVE_SECRET_VAR     = "TASTYTRADE_LIVE_CLIENT_SECRET"   # :83
LIVE_REFRESH_VAR    = "TASTYTRADE_LIVE_REFRESH_TOKEN"   # :84
```

`TastytradeCredentials.from_env` (`session.py:96-110`) defaults to the first pair
and takes the var names as arguments, so it is world-agnostic by construction —
it reads whatever it is told to read and raises `MissingCredentialsError` naming
only the missing variables, never a value.

Exactly one function keys a pair on configuration
(`market_data_credentials`, `session.py:259-268`):

```python
if config.marketdata.source == "live":
    return TastytradeCredentials.from_env(LIVE_SECRET_VAR, LIVE_REFRESH_VAR)
return TastytradeCredentials.from_env()
```

**There is no trading equivalent.** Every trading call site invokes
`TastytradeCredentials.from_env()` bare, so the trading session is always built
from `TASTYTRADE_CLIENT_SECRET` / `TASTYTRADE_REFRESH_TOKEN`, in every
environment, including `live`.

### Which world a session talks to

`TastytradeSession.is_test` (`session.py:136-146`) is the sandbox-first boundary:

```python
if self._role is SessionRole.MARKET_DATA:
    return self._config.marketdata.source != "live"
return self._config.environment != "live"
```

So the endpoint is chosen by config and the credential is not. On the day
`environment` becomes `"live"`, the *same two variables* are sent to the
production API.

### Every call site

| Site | Role | Credential source today | Can run against |
|---|---|---|---|
| `src/argus/session.py:606` (`startup_reconcile` — short-lived, process start) | TRADING | `from_env()` bare | any profile |
| `src/argus/session.py:1037` (`run_session` — per trading day) | TRADING | `from_env()` bare | any profile |
| `src/argus/session.py:1038` (`run_session`) | MARKET_DATA | `market_data_credentials(config)` | any profile |
| `src/argus/tools/account.py:34` | TRADING | `from_env()` bare | any profile (**read-only**) |
| `src/argus/tools/order_smoke.py:72` | TRADING | `from_env()` bare | refuses `live` at `:69-70` |
| `src/argus/tools/order_smoke.py:74` | MARKET_DATA | `market_data_credentials(config)` | as above |
| `src/argus/tools/loop_lag.py:430` (`--live-cert`) | TRADING | `from_env()` bare | any profile (read-only REST) |
| `src/argus/tools/chain.py:33` | MARKET_DATA | `market_data_credentials(config)` | any profile |
| `src/argus/tools/quotes.py:36` | MARKET_DATA | `market_data_credentials(config)` | any profile |
| `src/argus/tools/mark.py:77` | MARKET_DATA | `market_data_credentials(config)` | any profile |
| `src/argus/tools/credcheck.py:19-26, 58-71` | — | reads all four constants directly | n/a (shape diagnostic) |

Seven runtime/tool sites build a trading session; **all seven take the same two
variables regardless of environment.** `src/argus/broker/__init__.py:5-17`
re-exports `TastytradeCredentials`, `MissingCredentialsError`, `SessionRole` and
`market_data_credentials` — there is no trading-credentials symbol to export.

**Already correct and not a finding:** `order_smoke` refuses `environment =
"live"` outright (`:69-70`), so the one tool that can route an order cannot be
pointed at production by a `--config` typo. `account.py` and `loop_lag.py` are
read-only against the trading session and are safe against a live profile — that
matters in §6.

### What `TASTYTRADE_LIVE_*` actually means

It is **live market data only**, consumed at exactly one place
(`session.py:267`), and only when `[marketdata] source = "live"` — which is
already true in both `argus.sandbox.toml:71` and `argus.live.toml:71`. It is a
production, read-only registration for quotes, greeks and instruments.

So the name says *live trading credentials* and the wiring says *live
market-data credentials*, while the actual live trading credentials have no name
of their own and ride in the unprefixed pair.

---

## 2. The hazard, stated precisely

**The cutover is a two-part change documented as a one-part change.** To go live
today an operator must:

1. flip `ARGUS_ENV=sandbox` → `ARGUS_ENV=live` in `/etc/argus/host.env` — the
   documented step; and
2. **replace the values of `TASTYTRADE_CLIENT_SECRET` and
   `TASTYTRADE_REFRESH_TOKEN` in `/opt/argus/.env`** with the live registration's
   — undocumented, and invisible to every assertion the deploy makes.

Four consequences, in ascending order of how much they matter.

**(a) The failure is loud, but expensive.** Forget step 2 and the process comes
up under `environment = "live"` and presents a cert refresh token to the
production API: `invalid_grant`. That is caught at startup — `tt.open()` forces
an OAuth refresh precisely so a bad credential fails fast (`session.py:197-215`).
But `_login_with_retry` spends **6 attempts a flat 10 minutes apart**, deliberately,
because each failed refresh accrues toward tastytrade's undocumented IP-block
threshold (`session.py:598-604`). A missed step on cutover morning therefore
costs an hour of retries and a real chance of an IP block, not a fast error.

**(b) Rollback is asymmetric.** The profile flip is reversible; the credential
swap is not undone by it. After step 2, flipping `host.env` back to `sandbox`
leaves *live* trading credentials in the variables a cert session reads, and the
cert API rejects them the same way — the rollback path lands in the same failure
as the forward path. **A rollback that requires remembering a second manual edit
under time pressure is not a rollback.** This is the sharpest cost.

**(c) It cannot be rehearsed.** Proving the live registration authenticates and
the live account exists requires putting live values into the variables cert
operation depends on. There is no way to test half of it. The one class of
cutover failure you would most want to retire the night before is the one this
wiring makes untestable.

**(d) The runbook's load-bearing sentence is wrong.** `docs/argus-deployment-runbook.md:255-258`
and `docker-compose.yml`'s own comment both assert that the live cutover is
`host.env` changing. An operator who rehearses that procedure rehearses an
incomplete one.

**What is NOT a problem, stated so it is not re-litigated:** none of this can
route a live order from a cert profile. `is_test` derives from `environment`
alone (`session.py:146`), so a cert profile always reaches the cert endpoint no
matter which values sit in the variables — live credentials against cert simply
fail to authenticate. **The sandbox-first guarantee is intact.** The defect is
operational: an unreversible, unrehearsable, undocumented second step. Do not
argue for this change on safety grounds it does not have.

**Second-order:** the unprefixed pair is byte-identical in name to TOMIC's
(`docs/tomic-argus-concurrent-operation-assessment.md:228-231`, Area F). The two
are isolated only by living in separate `.env` files in separate deployments;
run a TOMIC tool and an Argus tool in one shell and whichever `.env` loaded first
wins. Area F asks for a note in both `.env.example`s. A rename retires it instead.

---

## 3. Is the stated goal the right one?

The brief's desired end state — *cert and live trading credentials coexist in
`.env` simultaneously under clearly-named vars, so the cutover is only the
profile flip and rollback is symmetric* — is **correct, and is the only one of
the reachable end states that makes the runbook's sentence true.** Validated
against three properties the repo already holds:

- **It matches the profile model exactly.** `argus.sandbox.toml` and
  `argus.live.toml` are two full files that coexist, and the profile selects one.
  Credentials should coexist the same way and be selected by the same authority.
  The 2026-09-04 record's rejection of base+overlay was a rejection of a *merge
  step*, not of coexistence — coexistence is the shape it chose.
- **It preserves "credentials never in config or code."** Selection is keyed on
  `config.environment`; the values stay in the environment. No credential enters
  a git-tracked, sha-fingerprinted, bind-mounted file.
- **It makes the `.env` static across the cutover**, which is what the deploy
  needs: `.env` is hand-edited, never touched by `argus-deploy.sh`, and already
  the documented trap for `ARGUS_IMAGE_TAG` (runbook §8.0). A cutover that
  requires editing it is a cutover with an unverifiable step.

One refinement to the goal: the end state should also make *which slot a session
used* observable. The config fingerprint exists because config is bind-mounted
and does not roll back with the image; credentials are one layer further out —
nothing in the process can currently say which registration it authenticated
with. Names only, never values (§4.3).

---

## 4. Options

Two axes matter: **role** (trading, which can move money / market data, which
cannot) and **world** (cert / production). That is four slots. Today three are
named and one is not:

| | cert world | production world |
|---|---|---|
| **trading** | `TASTYTRADE_*` | *(unnamed — borrows the cert pair)* |
| **market data** | *(falls back to the cert trading pair)* | `TASTYTRADE_LIVE_*` |

Every option below is a different answer to "how are those four slots named".

### (a) Environment-keyed trading credentials reusing `TASTYTRADE_LIVE_*`

A `trading_credentials(config)` mirroring `market_data_credentials`, returning
`from_env(LIVE_SECRET_VAR, LIVE_REFRESH_VAR)` when `config.environment == "live"`.

Smallest possible diff: one new function, seven call sites, no `.env` change on
any host that already has live market data working.

**Rejected on the collision.** `TASTYTRADE_LIVE_*` is a read-only market-data
registration. Reusing it for trading means one credential pair serves both the
read-only session and the money-moving one in live. The `SessionRole` split stays
structural in code (`trading_session` raises for a market-data session,
`session.py:229-241`), but the *credential* would no longer be scoped — a leaked
or over-shared market-data token would be a trading token. It also silently
changes what that pair authorizes in cert, where it is read-only today. This
trades a naming problem for a privilege-separation problem, and the naming
problem is the one that was actually reported.

**Variant (a′)** — key on environment but read a *distinct* new pair for live
trading — is not this option; it is option (d1).

### (b) Name every slot by role and world

Six variables, `TASTYTRADE_<ROLE>_<WORLD>_<FIELD>`:

```
TASTYTRADE_TRADING_CERT_CLIENT_SECRET      TASTYTRADE_TRADING_CERT_REFRESH_TOKEN
TASTYTRADE_TRADING_LIVE_CLIENT_SECRET      TASTYTRADE_TRADING_LIVE_REFRESH_TOKEN
TASTYTRADE_MARKETDATA_LIVE_CLIENT_SECRET   TASTYTRADE_MARKETDATA_LIVE_REFRESH_TOKEN
```

Market data in cert keeps its existing fallback to the cert *trading* pair —
unchanged behaviour, now under a name that says what it is falling back to.

**Role first, then world, deliberately.** Role is the safety-relevant axis, so
`grep TASTYTRADE_TRADING_` is a complete audit of every credential that can move
money. World-first (`TASTYTRADE_LIVE_TRADING_*`) groups by blast radius instead,
which is the less useful grouping and keeps `TASTYTRADE_LIVE_` as a prefix that
still means two different things depending on what follows it.

Costs a rewrite of `.env` on the droplet and on every dev machine. §5 shows that
this is orderable so that no host is ever in a broken state.

Closes the naming confusion (the reported complaint), the cutover hazard, and the
TOMIC var-name collision — the unprefixed names disappear entirely, so no shell
can load one repo's values into the other's slots.

### (c) One `TASTYTRADE_*` pair, values swapped at cutover — status quo

**Rejected.** All four consequences of §2 are exactly this option's properties.
It is not merely unfixed; it makes the documented cutover procedure false and the
rollback conditional on an operator remembering an undocumented step at the worst
possible moment. Kept in this list only so nobody proposes it as the "no change
needed" reading.

### (d) Hybrids

**(d1) Minimal addition.** Keep the unprefixed pair meaning cert trading; add
only `TASTYTRADE_LIVE_TRADING_CLIENT_SECRET` / `_REFRESH_TOKEN`, selected by
`environment == "live"`. Two new names, no droplet churn for the cert pair, and
the cutover hazard is fully closed.

This is the cheapest *correct* option and it is a legitimate choice. What it does
not do: the unprefixed pair still means "cert trading" only implicitly, the
prefix `TASTYTRADE_LIVE_` still means market data in one variable and trading in
another, and Area F's TOMIC collision survives. It fixes the mechanism and leaves
the confusion the report opened with.

**(d2) Credentials in the profile config file.** Rejected outright — CLAUDE.md
non-negotiable, and these files are git-tracked, bind-mounted, and sha-printed on
`/status`. Named only to close it.

**(d3) Per-profile env files** (`env_file: .env.${ARGUS_ENV}`). Compose would
interpolate it and the cutover would stay one variable. Rejected: it either
duplicates or splits the non-tastytrade secrets (`ARGUS_KILL_TOKEN`,
`ARGUS_SLACK_WEBHOOK_URL`), it makes the flip depend on a second file existing
with no assertion that it does, and it defeats the stated goal — the two worlds'
credentials would no longer coexist in one place, so nothing could be verified
without switching profiles. The failure mode is a `live` flip that finds an
`.env.live` someone never wrote, discovered at 09:29 ET.

### The market-data fallback, addressed explicitly

`market_data_credentials` falls back to the cert *trading* pair when
`source != "live"` (`session.py:268`). Under (b) that fallback must be repointed
at `TASTYTRADE_TRADING_CERT_*` by name. This is behaviour-preserving: it is the
same registration, and the fallback is currently unreachable in practice because
both shipped profiles set `source = "live"`. It stays rather than being deleted —
it is the path `argus.dev.toml` and any future offline profile take, and deleting
a reachable-in-principle fallback to save a line is not worth the coupling.

Under (d1) the fallback is untouched.

---

## 5. Decision — option (b)

**SIGNED OFF 2026-09-09.** Option (b), delivered by an additive three-step
migration, plus the structural guarantees in §5.1.

Rationale:

- **The marginal cost over (d1) is close to zero at exactly this moment.** Going
  live requires opening `/opt/argus/.env` by hand to paste the live trading
  secret and refresh token no matter which option is chosen. Renaming four
  existing lines in the same edit is free; doing it later is a second scheduled
  touch of the highest-value secret on the box.
- **It matches how this repo resolves naming.** `_end_entries_today` over
  `_end_day`, `minted_direction` over `.direction`, `unmanaged` over
  `degraded_positions` — each time the ambiguous name was replaced rather than
  documented around, because the next reader gets the name and not the note.
  `TASTYTRADE_LIVE_*` meaning market-data is that same defect.
- **It closes Area F mechanically.** No shared variable name with TOMIC means no
  shell in which the wrong `.env` can win.

### 5.1 The two structural guarantees

Both fall out of the change and neither costs a config key.

**(i) The live refusal is free, and needs no new check.** With
`trading_credentials(config)` keyed on `environment`, a `live` profile whose live
trading pair is absent raises `MissingCredentialsError` from `from_env` naming
exactly the two missing variables, at startup, before any network call. This is
the analogue of `config.py:1162` (`account_stream_max_age_s` under live) and
`config.py:1228` (`audit.synchronous` under live) — but it lives in
`broker/session.py`, **not `config.py`, deliberately**: the config loader has no
business reading credentials, and putting the check there would give a
fail-closed config loader a reason to touch the environment. A cert profile still
requires only the cert pair, so a dev machine with no live registration is
unaffected.

**(ii) A byte-identical live pair must be refused.** In `trading_credentials`,
under `environment == "live"`, refuse when the live trading pair is identical to
the cert trading pair — reporting variable names only, never values.

This is not hypothetical: **step 1 of the migration below deliberately instructs
the operator to copy existing values into newly-named slots.** The failure it
creates — new names present, still carrying cert values — is the one this refusal
catches, and without it that state produces a live process that authenticates
against production with a cert token and burns the 6×10-minute login budget.
`tools.credcheck:58-67` already performs exactly this comparison for the
market-data pair; this promotes the same reasoning from a diagnostic an operator
must remember to run into a rail that runs itself.

**(iii) An AST sweep: no module in `src/` calls `TastytradeCredentials.from_env()`
with no arguments.** Today a bare `from_env()` *is* the trading path; after this
change a bare `from_env()` is precisely the bug — a trading session built from
cert credentials under a live profile, silently, at whichever call site was
missed or added later. The repo already holds invariants of this exact shape this
way (`residual_orders` reading no `.direction`; the halt-grade eligibility test
comparing no symbols; the single-legging and `place_order` sweeps). Without it,
"trading credentials are environment-keyed" is a convention that a new tool
breaks by writing the obvious line.

### 5.2 Migration, ordered so no host is ever broken

`.env` is hand-edited and untouched by the deploy, so the ordering is the whole
safety argument. Three steps, each independently reversible:

1. **Add, do not move.** On each host and dev machine, add the six new names
   alongside the existing four. `TASTYTRADE_TRADING_CERT_*` takes a *copy* of the
   current `TASTYTRADE_*` values; `TASTYTRADE_MARKETDATA_LIVE_*` takes a copy of
   `TASTYTRADE_LIVE_*`; `TASTYTRADE_TRADING_LIVE_*` takes the **live trading
   registration's own values** — not a copy of anything (guarantee (ii) enforces
   this). Nothing reads the new names yet. Verify with the extended
   `tools.credcheck`.
2. **Deploy the image that reads only the new names.** It refuses to start if
   they are absent — and they are not, from step 1. Rollback to the previous
   image tag works, because the old names are still present.
3. **Delete the four old names,** at leisure, once step 2 has run a session.

**No aliasing in code.** An alias is a second name for one credential, which is
the disease this change treats; and the repo's own precedent is to refuse rather
than fall back (`resolve_config_path` has no default; a missing bind-mount source
is a hard `ConfigError`). The ordering above buys everything aliasing would, with
no lingering second name.

---

## 6. Rehearsal — what this does and does NOT unlock

**Correction, found during implementation.** The design's first draft claimed
this change makes the live registration provable before the cutover, via
`python -m argus.tools.account --config argus.live.toml`. **That is wrong.**
All three tools that build a trading session refuse `environment = "live"`
outright, and did so before this change:

- `tools/account.py:31-32` — *"refusing to run the account smoke tool against a
  live session"*
- `tools/loop_lag.py:427-428` — same shape
- `tools/order_smoke.py:69-70` — same shape

So the credential keying makes those commands *coherent* (they would read the
live slot rather than the cert one), but they still stop at their own guard. **No
relaxation was made here** — those refusals are deliberate, `account.py` is
documented as a *"Sandbox account + reconciliation smoke tool"*, and quietly
widening a live refusal is not something a credentials change should carry.

What IS provable before the cutover, and is enough for most of the risk:

- **Shape**, via `python -m argus.tools.credcheck` — every slot present and
  complete, no live slot carrying a cert value, and whether the two live slots
  are one registration or two. It reads no config and touches no network.
- **The refusals themselves**, because they are unit-tested: a live profile with
  no live slot, and a live slot holding cert values, both fail closed at startup
  before any network call.

**Still owed, and now the sharper open question (§7.7): nothing authenticates the
live trading registration until the cutover itself.** That is a real remaining
gap — the same class the daily max-loss breaker is in, where day one live is the
first real exercise. The cheapest closure is a read-only login probe (a
`credcheck --verify-login <slot>` that opens a session and does nothing else), but
that is a new capability with its own blast radius and is deliberately NOT bundled
here.

`order_smoke`'s refusal is load-bearing after this change in a way it was not
before: previously a live `order_smoke` would have failed to authenticate anyway,
and now it would not.

---

## 7. Open questions — sign-off needed before implementation

1. ~~**(b) or (d1)?**~~ **ANSWERED 2026-09-09 — (b).** The full six-name rename,
   with the additive three-step `.env` migration of §5.2. This was the only
   question that changes the shape of the work; everything below is detail that
   can be settled without redesigning anything.
2. **Is the live trading registration the same tastytrade app as
   `TASTYTRADE_LIVE_*` (live market data)?** If it is, the recommendation is
   still two separate variable pairs holding identical values — so that
   re-registering one later does not silently re-scope the other, and so the
   role split survives in the environment as well as in code. Confirm that
   duplication is acceptable rather than a liability. This also feeds AI-1 in
   `docs/tomic-argus-concurrent-operation-assessment.md` (same customer as TOMIC
   or not), which is a separate question and is **not** answered here.
3. **Guarantee (ii) — the byte-identical refusal. BUILT as a startup rail.**
   Confirm the comparison is live-trading-vs-cert-trading only, which is how it
   shipped: step 1 of the migration duplicates cert values into the cert slot and
   duplicates the market-data pair, and neither is compared, so neither trips it.
   `credcheck` additionally flags the market-data half as a diagnostic, with no
   rail behind it — a wrong quote credential costs a failed subscription, not a
   trading day.
4. ~~**Should `/status` and the startup line name the credential SLOT?**~~
   **ANSWERED 2026-09-09 — yes, BUILT.** A "Credentials" row under "Config",
   plain text with no colour state (nothing here is actionable-when-wrong,
   because wrong cannot start), a `credentials` object in the JSON payload, and
   `creds=` on both startup lines. Fed by `credential_slot_labels(config)`, which
   is pinned by an AST test to read no environment — it feeds a page served over
   the control surface, so it must never be one refactor from holding a value.
5. **Sequencing against the live-cutover runbook (config-profiles step 4).** This
   change is a prerequisite for that runbook to be *true*, so the recommendation
   is that it lands first and the runbook is written against the result. Confirm
   that ordering.

7. **NEW, raised by the build (§6): nothing authenticates the live trading
   registration before the cutover.** All three trading tools refuse
   `environment = "live"` by their own guard, so the rehearsal this design
   claimed is not available and was not built. Options: relax `account.py`'s
   refusal (it is read-only, but the refusal is deliberate and predates this
   work), add a read-only `credcheck --verify-login` probe, or accept it as
   first-day-live risk alongside the max-loss breaker. **Recommend deciding this
   before the cutover date is set, not at it.**
6. **Does this ship coupled or alone?** `src/` + `.env.example` + docs is an
   image-only release. But if the two profile files' comments are updated in the
   same pass (§8), they are bind mounts — `argus-deploy.sh`'s classifier restarts
   on any change to `argus.<profile>.toml`, so a comment-only edit becomes a
   restart. Either bundle them knowingly or leave those two comments for the
   cutover release.

---

## 8. Blast radius — a checklist, no code

Option (b), as signed off. Questions 3, 4 and 6 are still open and are marked
inline where they move a row.

### Source

- [ ] `src/argus/broker/session.py:79-84` — replace the four constants with six.
      Suggested names: `TRADING_CERT_SECRET_VAR` / `TRADING_CERT_REFRESH_VAR`,
      `TRADING_LIVE_SECRET_VAR` / `TRADING_LIVE_REFRESH_VAR`,
      `MARKETDATA_LIVE_SECRET_VAR` / `MARKETDATA_LIVE_REFRESH_VAR`.
- [ ] `src/argus/broker/session.py:96-110` — `from_env` keeps taking var names,
      but **loses its defaults**. A defaulted `from_env()` is the thing guarantee
      (iii) forbids; leaving the default in place leaves the footgun loaded.
- [ ] `src/argus/broker/session.py` — new `trading_credentials(config)` beside
      `market_data_credentials`, keyed on `config.environment`, carrying
      guarantee (ii)'s identical-pair refusal.
- [ ] `src/argus/broker/session.py:259-268` — `market_data_credentials`: repoint
      the `source != "live"` fallback at the cert *trading* pair by its new name;
      docstring says which registration it is borrowing and why.
- [ ] `src/argus/broker/session.py:1-19` — module docstring §1 still says
      "`TT_SECRET` / `TT_REFRESH` by default", which names variables that do not
      exist anywhere in the repo. **Already wrong today**; fix in this pass.
- [ ] `src/argus/broker/__init__.py:5-17` — export `trading_credentials`.
- [ ] `src/argus/session.py:606` (`startup_reconcile`) — `trading_credentials(config)`.
- [ ] `src/argus/session.py:1037` (`run_session`) — `trading_credentials(config)`.
      Note `:1039` catches `MissingCredentialsError` and routes it to
      `session.login_gave_up` + a `login_missing_credentials` alert; the new
      refusals surface through that same seam and need no new handling.
- [ ] `src/argus/session.py:1052-1055` — startup print: add which credential slot
      each session used (variable names, never values), beside the existing
      `env=` / `is_test=` line. Question 4.
- [ ] `src/argus/tools/account.py:34` — `trading_credentials(config)`.
- [ ] `src/argus/tools/order_smoke.py:72` — `trading_credentials(config)`.
      `:69-70`'s live refusal is unchanged.
- [ ] `src/argus/tools/loop_lag.py:430` — `trading_credentials(config)`.
- [ ] `src/argus/tools/credcheck.py:19-26` — six names; `:58-71` cross-checks
      restructured per (role, world): cert-trading vs live-trading identical,
      live-trading vs marketdata-live identical (informational only if question 2
      says they are one registration), each pair's two halves identical, and the
      "live market data needs BOTH" rule extended to "each configured world needs
      both halves". Its `_describe` shape checks are unchanged.
- [x] `src/argus/status.py` — the credential-slot row (question 4). **Correction
      to this checklist's first draft: it does NOT join `_SEAMS`.** That sweep
      covers dataclass→dataclass CONVERTERS in `session.py`, and this is a scalar
      lifted off the config in `Application.startup`, exactly like
      `config_profile` / `environment`. The under-copy failure mode the sweep
      exists to catch (a target with `None` defaults silently gaining a field)
      does not apply to a two-scalar constructor argument.

### Tests

- [ ] `tests/test_session.py:83-95` — `from_env` tests: it no longer has
      defaults, so the bare-call test becomes a call with explicit names.
- [ ] `tests/test_session.py:166-187` — `market_data_credentials`: update to the
      new names; keep the "raises naming the missing var" assertion.
- [ ] **New** — `trading_credentials` parametrized over
      `environment ∈ {shadow, sandbox, live}`: cert pair for the first two, live
      trading pair for the third; missing live pair under `live` raises naming
      exactly those two variables; a cert profile with no live pair present
      succeeds.
- [ ] **New** — guarantee (ii): identical live/cert trading pairs under `live`
      refuse, and the message contains neither value.
- [ ] **New (AST)** — guarantee (iii): no module under `src/` calls
      `TastytradeCredentials.from_env` with zero arguments.
- [ ] `tests/test_reconcile_unreachable.py:661, 687, 708` — **the sharpest
      breakage, and it is silent.** These monkeypatch
      `session_mod.TastytradeCredentials.from_env` as a zero-arg staticmethod. If
      `startup_reconcile` calls `trading_credentials(config)` instead, the patch
      stops intercepting and the test reads the developer's **real environment**.
      It will not fail loudly — it will pick up a real `.env`. Repoint all three
      at `session_mod.trading_credentials`.
- [ ] `tests/test_session_login.py:111-113` — same patch shape; same repoint.
- [ ] `tests/test_app.py:95` — clears only `TASTYTRADE_CLIENT_SECRET` /
      `TASTYTRADE_REFRESH_TOKEN` so "a developer's real `.env` cannot turn this
      into a live login". Its comment is the right instinct and its list is now
      incomplete: extend to every trading variable, both worlds. Sandbox configs
      are unaffected in practice, but the list must not silently under-cover a
      test that later builds a `live` config.
- [ ] `tests/test_config_profiles.py` — **no change expected.** Credentials are
      not config keys, so the key-set and allowlist assertions are untouched.
      Verify rather than assume.
- [ ] `tests/test_deploy_wiring.py` — **no change expected** unless the runbook
      or compose comments it pins are reworded. Verify.

### Config / non-Python

- [ ] `.env.example` — restructure into the four (role, world) slots with the
      fallback stated. **It is separately stale and should be fixed in the same
      pass**: it documents `ARGUS_CONFIG` as having a baked-in image default
      (removed — the image now carries neither), refers to `argus.toml` (renamed
      to `argus.sandbox.toml`), and describes `ARGUS_ENV` as *overriding*
      `environment` (it now names the FILE, and the loader refuses on
      disagreement). See [config profiles](2026-09-04-config-profiles.md) step 2.
- [ ] `argus.sandbox.toml:66` and `argus.live.toml:70` — the `[marketdata]`
      comments name `TASTYTRADE_LIVE_CLIENT_SECRET` / `_REFRESH_TOKEN`. Comment
      only — but see question 6: these are bind mounts and the deploy classifier
      restarts on them.
- [ ] `docker-compose.yml` — **no change.** `env_file: - .env` carries whatever
      is in the file; six variables reach the container exactly as four do.

### Docs

- [ ] `README.md:70-77` — the two-row credential table becomes four rows.
- [ ] `README.md:1604` — the `[marketdata] source = "live"` row names
      `TASTYTRADE_LIVE_*`.
- [ ] `docs/argus-deployment-runbook.md:269-280` (§8.1 Credentials) — the six
      slots and the three-step migration.
- [ ] `docs/argus-deployment-runbook.md:255-258` (§8.0) — the "the live cutover is
      this one file changing" sentence becomes true; state that `.env` carries
      both worlds and is not touched by the flip.
- [ ] The live-cutover procedure (config-profiles **step 4**, unwritten) — write
      it against this. §9 below is the input.
- [ ] `docs/architecture/01-c4-context.md:57` — the market-data row's
      "Separate credentials (`TASTYTRADE_LIVE_*`)".
- [ ] `docs/architecture/02-c4-containers.md:48` — the `.env` container
      description.
- [ ] `docs/go-live-findings.md` — the OPEN row *"Live trading OAuth app
      registration + credentials"*: the repo-side half closes here; the
      provisioning half stays open and repo-external.
- [ ] `docs/tomic-argus-concurrent-operation-assessment.md:228-231` (Area F) —
      the shared-variable-name footgun is retired by the rename; record that
      instead of the note it currently asks for.
- [ ] `docs/decisions/README.md` — pointer row (added with this doc).
- [ ] **`CLAUDE.md` — NOT touched by this doc.** The credentials section
      (`### Credentials`, currently naming `TASTYTRADE_CLIENT_SECRET` /
      `TASTYTRADE_REFRESH_TOKEN` and describing `TASTYTRADE_LIVE_*` as the
      market-data registration) gets **one sentence** with the implementation,
      alongside the runbook cutover-step update. Both land with the code, not
      with the design.

**Size: ~14 source/test files, ~10 doc/config files.** No config key, no
migration, no audit category, no change to any rail. `src/` + `.env.example` +
docs is an image-only release unless the two profile comments ride along.

---

## 9. The cutover, after this change

**Forward.** On the droplet, with the live pair already present in `.env` and
verified (§6):

```bash
printf 'ARGUS_ENV=live\n' | sudo tee /etc/argus/host.env >/dev/null
sudo chmod 600 /etc/argus/host.env
sudo systemctl restart argus.service
```

Then the assertions that already exist: `/status` reports
`environment: live`, the config fingerprint `(live, argus.live.toml, <sha>)`, and
the build version — and, if question 4 lands, the credential slot in use.
`.env` is not opened, not edited, and not diffed.

**Rollback.** The same three lines with `sandbox`. Symmetric, because nothing
about the credentials changed in either direction — both worlds' values were
already in the file, and each profile reads only its own.

**What still has to be true independently**, and is not this change's to give:
a live registration that authenticates (§6 proves it in advance), a live account
that is flat and Argus-dedicated, and the live-min-size risk caps decided at the
flip. Those are separate OPEN rows in `docs/go-live-findings.md`.
