# Pinning the traded account (2026-09-10)

**Status: I think this was built - verification needed.**

Closes **C9** in [`docs/go-live-findings.md`](../go-live-findings.md): the
*enforcement* half (option **D**) of the recommendation accepted on 2026-09-10 in
[the live account is not dedicated to Argus](../decisions/2026-08-31-live-account-is-not-dedicated.md).
Option **A** — the account becomes fully dedicated to Argus — was accepted
operationally that day and is in progress as **C8**; this is the code half that
turns "the account is dedicated" from an intention into something the loader
refuses to start without.

**Scope in one sentence:** a new `[broker] account_number`, a live-only refusal in
`Config.load`, one argument in `__main__`, a `/status` row that makes the pin
confirmable, and a diagnostic sentence on the reconcile alert that names the
likely cause. **No rail changes behaviour. Cert behaviour is a no-op by
construction.**

---

## 0. Decisions taken (answered 2026-09-10, in session)

These are settled. They are here at the top rather than buried in the prose
because the implementer should not have to reconstruct them.

| # | Question | Decision |
|---|---|---|
| **D1** | `tests/test_config_profiles.py` requires **identical key sets** across all three profiles, so the key must exist in dev and sandbox too. What value do they carry? | **`account_number = ""` in dev and sandbox**, normalized to `None` by the loader → today's `accounts[0]` behaviour, unchanged. Only live refuses empty. Rationale: cert accounts get re-provisioned (a pinned cert config would then raise `SessionAborted`, which is **permanent** and parks the process); and the cert login has one account, so there is no lottery there to remove. |
| **D2** | `argus.live.toml` is tracked in git and pushed to `github.com/<private-repository>`. Where does the number live? | **Committed in `argus.live.toml`.** It is not a credential — useless without the `TASTYTRADE_TRADING_LIVE_*` slot — and the deployed, bind-mounted artifact should state which account it trades. Required anyway: with the live guard in place, `test_every_profile_file_loads` fails unless the file carries a real value. |
| **D3** | The live account number. | **`5XX00000`.** This is the existing tastytrade account being made dedicated (C8), not a new one. |
| **D4** | `/status` **Account** row — in scope? | **Yes.** It is the only place an operator can confirm the pin took on the cutover. |
| **D5** | Reconcile diagnostic line (§4 of the 2026-08-31 record) — in scope? | **Yes.** Diagnostic only: it must not change the verdict, the grade, or the halt. |
| **D6** | Release packaging. | **Bundle with the next release.** It adds a REQUIRED-under-live key, so **config and image deploy together** — the same coupled move `[reconcile]`, `exit_unattempted_max_s`, `book_confirm_ttl_s`, `flatten_before_close_s`, `unmanaged_order_max_s` and `exit_walk_min_steps` already force. |

Two judgement calls made while writing this, both flagged so they can be
overruled rather than discovered:

| # | Call | Why |
|---|---|---|
| **D7** | The unpinned `/status` state renders **muted, not amber** — reversing what was sketched when D4 was asked. | Unpinned is the *intended, permanent* cert state and is *unreachable* in live (the loader refuses). `_config_cell`'s own docstring states the house rule: colour is **actionable, not unusual**, and "a badge that is always lit is a badge that means nothing". Amber here would light on every cert page forever. |
| **D8** | `MAX_SANDBOX_LIVE_DIVERGENCE` stays at **10**. | This change takes sandbox↔live divergence from **9 to exactly 10** — the cap. Measured, not counted from the allowlist (§4.2). Leave the constant alone: arriving at the threshold on a key that *is* structurally per-world is exactly the right moment to notice, and the next divergence should reopen the base+overlay question (design §5.1, 2026-09-04) rather than bump a number. |

---

## 1. Why — compressed

The full argument is [the 2026-08-31 record](../decisions/2026-08-31-live-account-is-not-dedicated.md);
it is not repeated here. The three sentences that decide the shape of this change:

- **Four rails read a WHOLE-ACCOUNT number and treat it as an Argus-only number**
  — reconciliation's position map (`set(expected) | set(broker)` against a table
  only Argus writes), the concurrency cap's leg count, the daily-loss breaker's
  NLV anchor, and the relative size rail. One foreign position makes all four
  wrong at once.
- **The symbol collision has no fix** (record §7): two strategies sharing an SPXW
  strike net to zero in `positions`, so a symbol-keyed predicate answers "not
  held" for the strike an Argus spread is standing on. It can only be excluded by
  policy — which is what a dedicated account is.
- **Today the process takes `accounts[0]`**, whichever account the login happens
  to list first. There is no key, no environment variable, and no assertion.

D makes the precondition **stated and enforced** instead of implicit. It fixes
nothing on its own; it converts a confusing failure ("reconciliation failed with
7 discrepancies") into a refusal that names its cause, and it removes the
`accounts[0]` lottery.

---

## 2. What already exists — verified against source, 2026-09-10

Read, not assumed. This is the reason the change is small.

| Piece | Where | State |
|---|---|---|
| `Application(account_number=...)` | [app.py:370](../../src/argus/app.py), stored at [app.py:402](../../src/argus/app.py) | **Exists.** |
| Threading to process-start reconcile | [app.py:1138](../../src/argus/app.py) → `session.startup_reconcile(account_number=...)` | **Exists.** |
| Threading to the per-day session | [app.py:1385](../../src/argus/app.py) → `session.run_session(account_number=...)` | **Exists.** |
| The filter, process start | [session.py:628-629](../../src/argus/session.py) | **Exists**, and raises `SessionAborted` on no match — **permanent, never retried on the reconcile budget**. Covered by `test_reconcile_unreachable.py::test_a_wrong_account_number_stays_a_permanent_session_aborted`. |
| The filter, per day | [session.py:1081-1082](../../src/argus/session.py) | **Exists**, same shape, raises `SessionAborted`. |
| The only caller that passes one | [tools/shadow.py:41](../../src/argus/tools/shadow.py) | `--account`, CLI only. |
| `__main__` | [\_\_main\_\_.py:41](../../src/argus/__main__.py) — `Application(config)` | **The gap.** No account number, so both filters are skipped and `accounts[0]` wins. |
| `[broker] account_number` | nowhere | **The other gap.** `BrokerConfig` has exactly one field, `http_timeout_s`. |

So the build is: **a key, a guard, an argument, a row, a sentence, and tests.**

---

## 3. The change

### 3.1 `config.py` — the key

**`BrokerConfig`** ([config.py:838](../../src/argus/config.py)) gains one field.
Add it *after* `http_timeout_s` and extend the class docstring with a second
paragraph — the existing docstring is entirely about the timeout and would
otherwise silently become a description of only half the class:

```python
@dataclass(frozen=True)
class BrokerConfig:
    """Broker HTTP client settings, and WHICH ACCOUNT they address.

    ... existing http_timeout_s prose, unchanged ...

    `account_number` names the tastytrade account this process trades. `None`
    means "take the login's first account", which is what every environment did
    unconditionally until 2026-09-10 and what dev and sandbox still do. It is
    REQUIRED under `environment = "live"` and the loader refuses without it,
    because every account-derived rail Argus has reads a WHOLE-ACCOUNT number and
    treats it as an Argus-only number — so the live account must be dedicated to
    Argus, and a dedicated account that is merely *assumed* rather than *named* is
    one re-ordered `GET /customers/me/accounts` away from not being the one.
    → docs/decisions/2026-08-31-live-account-is-not-dedicated.md
    """

    http_timeout_s: float
    account_number: str | None = None
```

**Parsing and the guard**, in `Config.from_dict` at
[config.py:1097-1100](../../src/argus/config.py), replacing the current
three-line broker block:

```python
        broker_raw = _require_section(raw, "broker")
        # OPTIONAL in the loader, deliberately, even though all three shipped
        # profiles carry the key (test_config_profiles pins that). A config built
        # by `from_dict` — every test, every tool — must not have to name an
        # account to construct a broker section, and "" is how dev and sandbox
        # SAY unpinned in a file whose key set has to match live's.
        account_raw = broker_raw.get("account_number", "")
        if not isinstance(account_raw, str):
            raise ConfigError(
                "config key broker.account_number must be a string "
                f"(got {type(account_raw).__name__}); tastytrade account numbers are "
                "not integers — a bare 5XX00000 is not valid TOML and a quoted one is"
            )
        account_number = account_raw.strip() or None
        if account_number is not None and any(c.isspace() for c in account_number):
            # A typo'd number does not fail here today — it fails at the account
            # filter as SessionAborted, which is PERMANENT and parks the process.
            # Refusing at load turns a parked droplet into a failed start.
            raise ConfigError(
                f"config key broker.account_number must not contain whitespace, got "
                f"{account_number!r}: a mistyped account number is not rejected by the "
                "broker, it simply matches nothing, and the account filter answers that "
                "with a PERMANENT SessionAborted"
            )
        if environment == "live" and account_number is None:
            raise ConfigError(
                "environment='live' requires broker.account_number: Argus's "
                "account-derived rails (reconciliation's whole-account position map, "
                "the concurrency cap's leg count, the daily-loss breaker's NLV anchor "
                "and the relative size rail) all read WHOLE-ACCOUNT numbers and treat "
                "them as Argus-only, so the traded account must be DEDICATED to Argus "
                "and must be NAMED rather than inherited from whichever account the "
                "login lists first. See "
                "docs/decisions/2026-08-31-live-account-is-not-dedicated.md"
            )
        broker = BrokerConfig(
            http_timeout_s=_positive_float(broker_raw, "http_timeout_s", "broker"),
            account_number=account_number,
        )
```

Four things about that block, each of which is a decision:

- **It is its own branch.** Do **not** fold it in with the
  `account_stream_max_age_s` guard ([config.py:1162](../../src/argus/config.py)) or
  the `audit.synchronous` one ([config.py:1228](../../src/argus/config.py)). The
  three refuse live for three unrelated reasons; sharing a branch is precisely the
  mistake `account_stream_max_age_s`'s own comment warns against — *"never let the
  two staleness bounds share a guard; only one of them is a cert artifact."*
- **It fires EARLIEST of the three**, because `[broker]` is parsed before
  `[staleness]` and `[audit]`. That is correct — a guard belongs beside its key —
  and it is **a build hazard**: existing tests that assert a *different* live
  refusal now hit this one first. §4.5 lists every site.
- **No format validation beyond non-empty and no-whitespace.** No regex, no length
  check, no prefix check. Considered and rejected: tastytrade account-number shapes
  are undocumented and vary by account type, and a loader that refuses a valid
  number is strictly worse than one that accepts an invalid one (the invalid one is
  caught by the account filter within seconds of start, loudly).
- **No cert-value fence**, unlike `_refuse_cert_values_in_the_live_slot` for
  credentials. There is nothing to compare against: dev and sandbox carry `""`, so
  the live file cannot inherit a cert number by copy-paste. Considered and rejected
  as inventing a constant `config.py` has no business knowing.

### 3.2 The three profile files

**`argus.live.toml`**, in `[broker]` after `http_timeout_s`:

```toml
# WHICH tastytrade account this process trades. REQUIRED here and refused empty:
# every account-derived rail Argus has reads a WHOLE-ACCOUNT number and treats it
# as an Argus-only number — reconciliation compares the broker's ENTIRE position
# map against a table only Argus writes, the concurrency cap counts the account's
# LEGS, the daily-loss breaker measures whole-account NLV delta, and the relative
# size rail is a percentage of whole-account NLV. One foreign position makes all
# four wrong at once and hard-halts the process at the first reconcile.
#
# So this account is DEDICATED to Argus. Nothing else trades it, including by
# hand, and no cash moves into or out of it on a trading day (DailyPnl cannot tell
# a transfer from P&L, and a same-day restart deliberately re-uses the day's
# ORIGINAL anchor, so the contamination is not restart-clearable).
#
# Named, never inherited: without this key the process takes accounts[0] —
# whichever account `GET /customers/me/accounts` happens to list first.
# -> docs/decisions/2026-08-31-live-account-is-not-dedicated.md
account_number = "5XX00000"
```

**`argus.sandbox.toml`** and **`argus.dev.toml`**, same position:

```toml
# WHICH tastytrade account this process trades. EMPTY here = unpinned: take the
# login's first account, which is what every environment did before 2026-09-10 and
# is harmless on a cert login holding one account. Deliberately not pinned: cert
# accounts are re-provisioned by tastytrade, and a pinned number that stops
# existing is a PERMANENT SessionAborted that parks the process.
#
# `environment = "live"` REFUSES an empty value (config.py) — that is the whole
# point of the key, and the reason it exists in this file at all is that
# tests/test_config_profiles.py requires identical key sets across the profiles.
# -> docs/decisions/2026-08-31-live-account-is-not-dedicated.md
account_number = ""
```

### 3.3 `__main__.py` — the one line

[\_\_main\_\_.py:41](../../src/argus/__main__.py):

```python
    app = Application(config, account_number=config.broker.account_number)
```

Nothing downstream changes. Both filters already exist and already raise
`SessionAborted`.

Add a sentence to the module docstring, because this line is the entire reason
the key is not decoration:

```
The account number is passed EXPLICITLY rather than read from `config` inside
`Application`: the injection seam is what `tools/shadow.py` uses, and a supervisor
that quietly re-derived it would give two callers two different answers. Dropping
this argument restores the `accounts[0]` lottery silently — `tests/test_main_wiring.py`
is what stops a tidy from doing that.
```

### 3.4 `tools/shadow.py` — fall back to the config

[tools/shadow.py:41](../../src/argus/tools/shadow.py):

```python
    app = Application(config, account_number=account_number or config.broker.account_number)
```

The CLI argument still wins. Today the tool passes `None` when `--account` is
omitted, which would ignore a pinned config — harmless right now (shadow runs
under dev/sandbox, both unpinned by D1) and wrong the moment anyone pins one.
Leave `tools/account.py`, `tools/loop_lag.py` and `tools/order_smoke.py` alone:
they take an explicit `--account`, they build no `Application`, and
`tools/account.py` refuses live configs outright.

### 3.5 `/status` — the Account row

This is the confirmable half, and the cutover is the one moment the question
"which account is this process trading?" is the whole question.

**`status.py`, `StatusSnapshot`** — one field, immediately after
`credential_slot_marketdata` ([status.py:1029](../../src/argus/status.py)):

```python
    #: WHICH ACCOUNT, one layer further out again. `version` answers "which code?",
    #: `config_*` "which bytes?", `credential_slot_*` "which tastytrade
    #: REGISTRATION?" — and a registration can hold several accounts, so none of
    #: them answers "which of them is this process trading?".
    #:
    #: `None` is a real reading and the NORMAL one in cert: unpinned means
    #: accounts[0]. It is UNREACHABLE under live, where the loader refuses. A
    #: scalar for the same reason every other field here is one — this module
    #: imports no config loader.
    broker_account_number: str | None = None
```

**`StatusService.__init__`** — one keyword beside the credential slots
([status.py:1598](../../src/argus/status.py)), stored and passed through in
`snapshot()` ([status.py:1726](../../src/argus/status.py)).

**`to_payload`** — a top-level `"account"` block immediately after
`"credentials"` ([status.py:1114](../../src/argus/status.py)):

```python
            # Beside `credentials` because it closes the same sequence: which code,
            # which bytes, which registration, which ACCOUNT. `pinned` is separate
            # from `number` so a script can assert the RAIL without knowing the
            # value — "is this process pinned?" is the question a live deploy asks.
            "account": {
                "pinned": self.broker_account_number is not None,
                "number": self.broker_account_number,
            },
```

**The cell**, beside `_credentials_cell` ([status.py:2316](../../src/argus/status.py)):

```python
def _account_cell(account_number: str | None) -> str:
    """WHICH tastytrade account this process trades.

    No colour state, for the same reason `_credentials_cell` has none and by the
    rule `_config_cell` states outright — colour is ACTIONABLE, not unusual. There
    is no bad state reachable here: live cannot start unpinned (`config.py` refuses),
    and cert is unpinned by DECISION, permanently. An amber "not pinned" would be
    lit on every cert page for the life of the profile, which is a badge that means
    nothing on the day it finally matters.

    So MUTED for the unpinned case, and it SAYS WHY rather than showing a dash: an
    operator reading this on the droplet needs to know the state is deliberate and
    that live refuses it, not wonder whether something failed to load.

    The number rendered is the CONFIGURED one, not one read back from the broker —
    and under a pin those cannot differ, because `session.py`'s filter admits only
    an exact match and answers anything else with a PERMANENT `SessionAborted`.
    Unpinned, the account actually taken is in the session's own startup line
    (`account: <n>`), which is the honest place for it: the process does not know
    which one it got until it logs in, and this row is built before that.
    """
    if account_number is None:
        return (
            "<span class='muted'>—</span> <span class='muted'>&mdash; not pinned; "
            "this process takes the login's first account (accounts[0]). "
            "environment='live' refuses to start unpinned</span>"
        )
    return f"<code>{_esc(account_number)}</code> <span class='muted'>&mdash; pinned</span>"
```

**The row**, immediately after `Credentials`
([status.py:1367-1372](../../src/argus/status.py)):

```python
            _row("Account", _account_cell(self.broker_account_number)),
```

**`app.py`** — lift it beside the credential slots
([app.py:613](../../src/argus/app.py)):

```python
            credential_slot_marketdata=cred_slot_marketdata,
            # The account those credentials ADDRESS. Config-derived scalar, lifted
            # here with the other four for the same reason: `status.py` imports no
            # config loader.
            broker_account_number=cfg.broker.account_number,
```

### 3.6 `reconcile.py` — the diagnostic sentence

§4 of the 2026-08-31 record: *"a live halt that says 'reconciliation failed with 7
discrepancies' when the cause is 'this account is not dedicated to Argus' costs an
operator an hour at exactly the wrong time."*

**A pure function**, beside the other module-level predicates:

```python
#: The one shape that means "somebody else is trading this account". Formatted
#: with the count, never with the symbols — the symbols are already in the payload
#: and repeating them in prose makes a long alert longer.
_SHARED_ACCOUNT_HINT = (
    "{n} broker position(s) Argus has no record of. If this account is shared, it "
    "is NOT SUPPORTED — Argus's account-derived rails all read whole-account "
    "numbers: see docs/decisions/2026-08-31-live-account-is-not-dedicated.md"
)


def shared_account_hint(
    result: "ReconciliationResult", *, environment: str
) -> str | None:
    """A sentence naming the LIKELY cause of a live position mismatch. Never a verdict.

    DIAGNOSTIC ONLY. It is read by no rail, it reaches `ok`, `halt_grade` and
    `mismatch_halts_process` through no path, and a wrong hint costs an operator a
    second's confusion where a wrong verdict costs a trading day. That asymmetry is
    why this is prose on a payload and not a discrepancy kind.

    **Live only**, and that is not timidity: cert produces `unexpected_at_broker`
    rows legitimately, inside the sandbox reset's 24-hour order-reporting window,
    and a hint that fires on the ordinary cert failure is a hint nobody reads by
    the time live needs it.

    **Positions only.** A foreign WORKING order is the same diagnosis, but the test
    for "this order is not Argus's" already exists inside `halt_grade`, and a second
    copy here is two places that must agree about the same question forever. Named
    as a known gap rather than duplicated: a shared account holding only a manual
    working order gets the ordinary alert with no hint.
    """
    if environment != "live":
        return None
    if not result.discrepancies:
        return None
    if any(d.kind != UNEXPECTED_AT_BROKER for d in result.discrepancies):
        # A mixed set is a DIFFERENT problem — a missing_at_broker or a quantity
        # mismatch is Argus's own book disagreeing, and pointing at the shared
        # account there sends the operator to the wrong document.
        return None
    return _SHARED_ACCOUNT_HINT.format(n=len(result.discrepancies))
```

**The call site**, in `reconcile_and_record`
([reconcile.py:806-812](../../src/argus/reconcile.py)) — replacing the record and
alert pair:

```python
    hint = shared_account_hint(result, environment=environment)
    payload = result.as_payload() if hint is None else {**result.as_payload(), "hint": hint}
    await audit.record(
        "reconcile.result", {**payload, "trading_date": today.isoformat()}
    )
    if not result.ok:
        alert("reconciliation_mismatch", payload)
```

`environment` is already a parameter of `reconcile_and_record`, so nothing new is
threaded. The key is **absent** rather than `None` when there is no hint — a
`"hint": null` on every clean `reconcile.result` row is noise on the durable
chain, and a reader testing `"hint" in payload` is clearer than one testing for
non-null.

---

## 4. Tests owed

### 4.1 `tests/test_config.py` — the guard

Place these in a new `# -- broker.account_number: live must NAME its account ---`
block after the existing `broker.http_timeout_s` block.

| Test | Asserts |
|---|---|
| `test_live_requires_a_pinned_account_number` | `_cfg(environment="live", ...)` with the key **absent** → `ConfigError`, `match="broker.account_number"`. Must satisfy the other two live guards so it is *this* one firing. |
| `test_live_refuses_an_empty_account_number` | Parametrized over `""`, `"   "`, `"\t"` → same refusal. This is the state dev and sandbox ship, so it is the one that must not ride into live by being copied. |
| `test_live_accepts_a_pinned_account_number` | Loads, and `cfg.broker.account_number == "5WX00000"`. |
| `test_an_account_number_is_stripped` | `"  5WX00000  "` → `"5WX00000"`. Trailing whitespace in a hand-edited TOML string is invisible and would otherwise match nothing at the broker. |
| `test_an_account_number_with_internal_whitespace_is_refused` | `"5WX 00000"` → `ConfigError`. A `SessionAborted` park is a worse failure than a failed start. |
| `test_an_account_number_must_be_a_string` | `account_number = 5` → `ConfigError` naming the type. |
| `@pytest.mark.parametrize("env", ["shadow", "sandbox"])` `test_non_live_loads_unpinned` | Both load with `""` and with the key absent; `cfg.broker.account_number is None` in each. |
| `test_the_three_live_guards_are_independent` | Three cases, each with the other two satisfied, each raising with its own `match`: `broker.account_number`, `account_stream_max_age_s`, `requires audit.synchronous`. This is the test that fails if someone ever folds them into one branch. |

### 4.2 `tests/test_config_profiles.py`

Two `ALLOWED_DIVERGENCE` entries — dev↔sandbox do **not** diverge (both `""`), so
only the live pairs are listed:

```python
    ("sandbox", "live", "broker.account_number"): (
        "live NAMES its account (2026-09-10); cert stays unpinned on accounts[0]. "
        "Not a value that could ever be shared: pinning cert would turn a "
        "tastytrade re-provision into a PERMANENT SessionAborted, and live refuses "
        "empty because every account-derived rail reads a whole-account number"
    ),
    ("dev", "live", "broker.account_number"): (
        "dev carries the sandbox value; see the sandbox pair above"
    ),
```

One new assertion:

```python
def test_live_pins_an_account(flattened):
    """The live guard's file-side half. `test_every_profile_file_loads` already
    fails if this is empty — this says WHY in one line instead of raising a
    ConfigError the reader has to go and look up."""
    assert flattened["live"]["broker.account_number"].strip()
    assert flattened["sandbox"]["broker.account_number"] == ""
    assert flattened["dev"]["broker.account_number"] == ""
```

**`MAX_SANDBOX_LIVE_DIVERGENCE` stays at 10** (D8). Measured 2026-09-10: sandbox↔live
diverges on **9** keys today (`audit.db_path`, `environment`, `execution.walk_source`,
`risk.daily_max_loss`, `risk.max_loss_per_position`, `risk.position_size_pct`,
`staleness.account_stream_max_age_s`, `watchdog.trigger_source`,
`watchdog.unmanaged_order_cancel`). This makes it **10 — exactly the cap.**
`test_sandbox_live_divergence_stays_small` passes at `<= 10` and the **next**
divergence fails it, which is the intended prompt to reopen §5.1 rather than raise
the number. Say so in the implementation's commit message; the next author will
meet this test before they meet this design.

### 4.3 `tests/test_main_wiring.py` — new, structural

Modelled on [`tests/test_credential_slots.py`](../../tests/test_credential_slots.py):
the property is that a plausible-looking tidy cannot silently restore the
`accounts[0]` lottery. A behavioural test cannot reach it — `__main__.main()` runs
the supervisor.

```python
def test_main_passes_the_configured_account_number():
    """`Application(config)` in `__main__` is the whole of C9's defect.

    The key, the guard and both filters all existed before 2026-09-10; the
    supervisor simply never passed one, so a pinned live config would have loaded
    cleanly, rendered its number on /status, and traded accounts[0] anyway. The
    failure is SILENT in every environment and INVISIBLE in cert, where accounts[0]
    is the right answer — so it is pinned structurally rather than left to review.
    """
```

Parse `src/argus/__main__.py`, walk for an `ast.Call` whose `func` is
`Name("Application")`, and assert it carries a keyword `account_number` whose
value is `config.broker.account_number` as an attribute chain (`ast.unparse` the
value and compare the string — brittle to renames, which is the point: a rename
should surface here). Fail with a message naming the lottery.

### 4.4 The rest

| File | Test | Asserts |
|---|---|---|
| `tests/test_status.py` | `test_the_account_row_names_a_pinned_account` | The rendered HTML contains `5WX00000` and `pinned`. |
| | `test_the_account_row_says_why_it_is_unpinned` | `None` → the row contains `accounts[0]` and **no** `warn` class (D7 — this is the assertion that stops it being re-coloured). |
| | `test_the_payload_carries_the_pin_separately_from_the_number` | `payload["account"] == {"pinned": True, "number": "5WX00000"}`, and `{"pinned": False, "number": None}` unpinned. |
| `tests/test_order_reconcile.py` | `test_a_live_all_unexpected_mismatch_carries_the_shared_account_hint` | The alert payload has `"hint"` naming the count and the decision file. |
| | `test_the_hint_changes_no_verdict` | With the hint present: `result.ok` is unchanged, `halt_grade(result)` is unchanged, `mismatch_halts_process("live")` is unchanged. **The test the diagnostic exists to be bounded by.** |
| | `test_a_mixed_discrepancy_set_gets_no_hint` | One `missing_at_broker` alongside two `unexpected_at_broker` → no `"hint"` key at all. |
| | `test_sandbox_gets_no_hint` | Same input, `environment="sandbox"` → absent. Pins the live-only scoping against the cert reset window. |
| | `test_a_clean_result_records_no_hint_key` | `"hint" not in payload` — absent, not `None`. |

### 4.5 Existing tests this breaks — the list the compiler cannot give you

The new guard is parsed **before** `[staleness]` and `[audit]`, so every existing
test that builds a live `Config` and expects a *different* failure (or expects
success) now hits this one first. Add `broker={"account_number": "5WX00000"}` — a
deliberately fake number, distinct from the real `5XX00000`, so no test can be
mistaken for production:

- [`tests/support.py:22`](../../tests/support.py) — add `"account_number": ""` to
  `CONFIG["broker"]` so the canonical dict mirrors the shipped cert file. Do **not**
  put a number there: the default must stay the unpinned one, or every test starts
  asserting against a pin nothing set.
- `tests/test_config.py` — lines ~75, ~379, ~402, ~422, ~463, ~468, ~482.
- `tests/test_entry.py:285`
- `tests/test_execution_pipeline.py:87`
- `tests/test_session.py:351`
- `tests/test_session_reconcile.py:61` — the `_config` helper's `if environment ==
  "live":` branch already exists for `audit.synchronous`; add the account there
  beside it.

`tests/test_notify.py:607` is **not** affected — `build_alert_fn(environment="live")`
takes a bare string, not a `Config`.

The definitive list is whatever `./.venv/bin/python -m pytest` reports. Expect
these; if others appear, they are the same shape.

---

## 5. What is deliberately NOT changed

- **No rail changes behaviour.** Reconciliation, the concurrency cap, the breaker
  and the size rails all still read whole-account numbers, and that is *correct*
  once the account is dedicated. This change makes the premise enforced, not the
  rails scoped. Option **B** — a strategy-scoped accounting layer — was evaluated
  and rejected in the 2026-08-31 record, and nothing here is a step toward it.
- **`tools/account.py` still refuses a live config.** Read the live account's
  number and book off `my.tastytrade.com`. Relaxing a blanket sandbox-first posture
  on a tool is cheap to keep and was explicitly not recommended.
- **No environment variable.** `ARGUS_ACCOUNT_NUMBER` was considered (D2) and
  rejected: it splits the number away from the reviewable bind-mounted file and
  adds a second per-host manual step to a cutover whose entire design is *one file
  changing* (`/etc/argus/host.env`).
- **`sandbox` is not guarded.** A cert profile with `dry_run_submissions = false`
  routes real orders against cert and still takes `accounts[0]`. That is fine: the
  cert login holds one account, and pinning it trades a non-existent lottery for a
  real re-provisioning hazard (D1).
- **`Application` does not read the config itself.** The injection seam is what
  `tools/shadow.py` uses; a supervisor that re-derived the number internally would
  give two callers two different answers.

---

## 6. What this does not solve

Carried forward from the 2026-08-31 record, unchanged — none of it is in scope,
and all of it is on the live-flip checklist:

- **The account must STAY dedicated.** A hand-placed trade in it hard-halts the
  next restart. Loud and correct; still an operator discipline, not a rail.
- **Cash movements contaminate the breaker.** `DailyPnl` cannot distinguish a
  deposit, withdrawal or internal transfer from P&L, and a same-day restart
  deliberately re-uses the day's ORIGINAL anchor — so the contamination is not
  restart-clearable. Move capital on a non-trading day.
- **The NLV floor.** At `position_size_pct = 0.03` and
  `max_loss_per_position = 950`, the relative rail denies a single-lot position
  below an NLV of **$31,667**. Below that the account is funded and nothing trades.
- **The breaker is still unexercised.** A dedicated account makes the NLV basis
  *correct*; it does not make it *fired*. Separate live-flip gate.
- **A foreign working order with no foreign position gets the ordinary alert and
  no hint** (§3.6). Known gap, deliberately not closed by duplicating `halt_grade`'s
  identifier test.

---

## 7. Release and deploy

**Class: coupled config + image** (D6). Bundled with the next release, not its own
tag.

- The new key is REQUIRED under live and must exist in all three files, so
  **`argus.<profile>.toml` and the image deploy together** — the same coupled move
  six previous releases already forced. `scripts/argus-deploy.sh` classifies a
  config bind-mount change as restart-worthy already; no classifier change is
  needed (the two bind mounts it knows about are this host's
  `argus.<profile>.toml` and `blackout_dates.txt`).
- **The cert deploy is a behavioural no-op by construction**: `""` → `None` →
  `accounts[0]`, byte-identical to today's path. That is the point — it proves the
  plumbing on the environment where being wrong is free, which is the last
  rehearsal the pin gets before it matters.
- **Post-deploy assertion, cert:** `/status` shows the **Account** row reading
  *"not pinned; this process takes the login's first account (accounts[0])"*.
- **Post-deploy assertion, live (at the cutover):** `/status` shows
  `5XX00000 — pinned`, and `GET /status` JSON has
  `account.pinned == true`. A clean `reconcile.clean` at process start is the
  confirmation that the account is also *empty* of everything Argus did not place —
  that is **C8**, and it is a separate gate this change does not close.

---

## 8. Documentation this change owes

| File | Change |
|---|---|
| `CLAUDE.md` | Under **Broker is the only source of truth**, add the dedicated-account premise as a *preceding* paragraph — the existing text is only sound given it. Wording drafted in [the 2026-08-31 record §5](../decisions/2026-08-31-live-account-is-not-dedicated.md); carry it verbatim, updating "the account must hold nothing Argus did not place" to reference `[broker] account_number` as **built** rather than proposed. Everything already in that section stays verbatim. Also add `account_number` to the `argus.live.toml` line in the file-structure block. |
| `docs/argus-deployment-runbook.md` | Cutover section: the number is already in the tracked `argus.live.toml`, so the step is **verify, not set** — confirm `[broker] account_number = "5XX00000"`, confirm in the tastytrade UI that the account holds nothing and has no working orders, confirm NLV clears `max_loss_per_position / position_size_pct` ($31,667 at 950 / 0.03), then start and read the Account row back off `/status`. |
| `README.md` | Two standing rules in the daily rhythm: nothing else trades this account **including by hand**; no deposits, withdrawals or transfers on a trading day. |
| `docs/go-live-findings.md` | C9 → done, pointing at the decision record. |
| `docs/decisions/2026-08-31-live-account-is-not-dedicated.md` | Status banner: **"The D half — the enforcement — is NOT built yet"** becomes built, dated, with the pointer. |
| `docs/decisions/README.md` | New row for the decision record. |
| `docs/designs/README.md` | New row for this design, marked with its outcome. |
| `docs/decisions/2026-09-10-pinning-the-traded-account.md` | The decision record itself, on ship — normative, past tense, carrying D7 and D8 and whatever the build overturns. |

---

## 9. Decision summary

Everything an implementer might otherwise stop and ask, in one place.

| # | Decision |
|---|---|
| 1 | Key is `[broker] account_number`, type `str \| None`, **optional in the loader** and present in all three shipped files. |
| 2 | `""` and whitespace-only normalize to `None` = unpinned = `accounts[0]`. |
| 3 | dev and sandbox ship `""`; live ships `"5XX00000"`. |
| 4 | The live guard is **its own branch**, placed with the `[broker]` parse, and therefore fires **before** the other two live guards. |
| 5 | No format validation beyond non-empty and no-internal-whitespace. No cert-value fence. |
| 6 | `__main__` passes `config.broker.account_number` explicitly; `Application` does not read the config. |
| 7 | `tools/shadow.py` falls back to the config; the other three tools are untouched. |
| 8 | `/status` gains an **Account** row after **Credentials**, and an `account` block after `credentials` in the payload. |
| 9 | The unpinned row is **muted and explains itself**, never amber (D7). |
| 10 | The reconcile hint is **live-only**, **positions-only**, fires only when *every* position discrepancy is `unexpected_at_broker`, and is **absent** rather than `None` when it does not fire. |
| 11 | The hint reaches no verdict, grade or halt — pinned by `test_the_hint_changes_no_verdict`. |
| 12 | `MAX_SANDBOX_LIVE_DIVERGENCE` stays 10, at the cap (D8). |
| 13 | Tests use the fake `5WX00000`, never the real number. |
| 14 | Ships coupled (config + image), bundled with the next release. |
