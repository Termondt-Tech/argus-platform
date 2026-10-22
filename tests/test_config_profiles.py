"""The three shipped config files are siblings, not forks.

`argus.dev.toml`, the sandbox config and `argus.live.toml` are FULL standalone
files, deliberately — a base+overlay merge layer was rejected (see
`docs/designs/2026-09-04-config-profiles-and-config-only-releases.md` §5.1)
because it puts a merge step in front of a fail-closed loader, splits 83 keys of
load-bearing prose away from the values it justifies, and buys that at a
divergence of three keys.

The price of full files is silent drift, and this module is what is paid instead.
The failure being prevented is specific and has nearly happened four times: **four
of the last five releases forced a coupled config move** (`exit_unattempted_max_s`,
`[reconcile]`, `book_confirm_ttl_s`, `unmanaged_order_max_s`, `exit_walk_min_steps`).
Adding a REQUIRED key to two of three files is a `ConfigError` on the third — and
for the live file that is a process that will not boot, discovered at 09:29 ET on
cutover day.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from argus.config import (
    DURABLE_SYNCHRONOUS,
    Config,
    ConfigError,
    profile_from_filename,
)

REPO = Path(__file__).resolve().parent.parent

#: Profile -> file. Every file is now named by its profile, so this map is a
#: statement of WHICH PROFILES ARE EXPECTED TO EXIST rather than a translation
#: table — `test_no_profile_file_is_unlisted` fails if a fourth file appears and
#: is not added here, which is how a new profile joins the drift assertions
#: instead of quietly sitting outside them.
PROFILE_FILES = {
    "dev": REPO / "argus.dev.toml",
    "sandbox": REPO / "argus.sandbox.toml",
    "live": REPO / "argus.live.toml",
}

#: Every intended value divergence, keyed `(profile_a, profile_b, dotted.key)` with
#: the reason it is intended. **Anything not listed here fails the test**, so adding
#: a divergence is a deliberate reviewed line rather than a silent edit.
#:
#: Kept as prose rather than a bare set on purpose: the reason is the part that
#: rots, and a reader six months from now needs it more than the key name.
ALLOWED_DIVERGENCE: dict[tuple[str, str, str], str] = {
    # -- sandbox <-> live: EIGHT keys, each one a rail or its recorded rationale --
    ("sandbox", "live", "environment"): (
        "the profile/environment assertion (config._check_profile) requires it"
    ),
    ("sandbox", "live", "broker.account_number"): (
        "live NAMES its account (2026-09-10); cert stays unpinned on accounts[0]. "
        "Not a value that could ever be shared: pinning cert would turn a "
        "tastytrade re-provision into a PERMANENT SessionAborted, and live refuses "
        "empty because every account-derived rail reads a whole-account number"
    ),
    ("dev", "live", "broker.account_number"): (
        "dev carries the sandbox value; see the sandbox pair above"
    ),
    ("sandbox", "live", "audit.db_path"): (
        "live must not open the cert database — a shared name continues the cert "
        "hash chain and feeds cert `positions` to live reconciliation"
    ),
    ("sandbox", "live", "staleness.account_stream_max_age_s"): (
        "120.0 is a cert artifact (sandbox /positions latency); config.py refuses "
        "anything above LIVE_MAX_ACCOUNT_STREAM_AGE_S under environment='live'"
    ),
    ("dev", "live", "staleness.account_stream_max_age_s"): (
        "dev carries the sandbox value; the live ceiling applies only to live"
    ),
    # The ("sandbox", "live") entry was retired in v0.4.5 (2026-09-19), as it asked
    # to be — by flipping live, never cert back. Its cert-first condition could no
    # longer be met. → docs/decisions/2026-09-19-live-cancel-recheck-promotion.md
    ("dev", "sandbox", "watchdog.unmanaged_order_cancel"): (
        "dev registers no residuals at all (dry_run_submissions routes nothing, so "
        "the registry is empty by construction) — the flag is moot there and stays "
        "on the side that sends nothing"
    ),
    ("dev", "live", "watchdog.unmanaged_order_cancel"): (
        "dev registers no residuals at all (dry_run_submissions routes nothing, so "
        "the registry is empty by construction) — the flag is moot there and stays "
        "on the side that sends nothing"
    ),
    # `max_loss_per_position` and `daily_max_loss` were sandbox-vs-live divergences
    # here until 2026-09-11, when cert was synced to live's min-size numbers
    # (max_concurrent_positions 2 -> 1, max_loss_per_position 1250 -> 950,
    # daily_max_loss 2500 -> 950, the welded invariant re-derived at 1 x 950).
    # Retired rather than left stale, per `test_the_allowlist_has_no_stale_entries`.
    # `position_size_pct` is the one rail left that is genuinely account-sized
    # rather than min-size-sized, and stays a deliberate divergence —
    # `test_the_welded_size_invariant_holds` enforces the daily-loss inequality,
    # not the individual values.
    ("sandbox", "live", "risk.position_size_pct"): (
        "3% is chosen so the ABSOLUTE rails govern and this one stays the sanity "
        "bound it is meant to be: at the live account's size 3% is non-binding "
        "above ~$28-30k NLV and becomes the automatic de-risk trigger below it. "
        "Cert's 2% on a small account would bind well below that and cut the strategy to "
        "ONE contract from day one — the relative rail silently becoming the "
        "binding constraint is exactly what the three-rail design exists to avoid"
    ),
    ("dev", "live", "risk.position_size_pct"): (
        "dev carries the sandbox value; see the sandbox pair above"
    ),
    # -- dev <-> the rest: dev submits nothing and writes to dev/ --------------
    ("dev", "sandbox", "environment"): "dev runs shadow; it routes nothing",
    ("dev", "live", "environment"): "dev runs shadow; it routes nothing",
    ("dev", "sandbox", "audit.db_path"): "NEVER TOUCH THE LIVE DATABASE (CLAUDE.md)",
    ("dev", "live", "audit.db_path"): "NEVER TOUCH THE LIVE DATABASE (CLAUDE.md)",
    ("dev", "sandbox", "audit.synchronous"): "dev takes NORMAL; it holds no trading record",
    ("dev", "live", "audit.synchronous"): "dev takes NORMAL; it holds no trading record",
    ("dev", "sandbox", "events.blackout_dates_path"): "dev reads the repo copy, not /etc/argus",
    ("dev", "live", "events.blackout_dates_path"): "dev reads the repo copy, not /etc/argus",
    ("dev", "sandbox", "execution.dry_run_submissions"): "dev must never route",
    ("dev", "live", "execution.dry_run_submissions"): "dev must never route",
    ("dev", "sandbox", "server.bind_host"): "dev binds loopback, not the tailnet",
    ("dev", "live", "server.bind_host"): "dev binds loopback, not the tailnet",
    ("dev", "sandbox", "marketdata.source"): "dev reads the cert feed; no live registration",
    ("dev", "live", "marketdata.source"): "dev reads the cert feed; no live registration",
    ("dev", "sandbox", "market.login_attempts"): "dev fails fast; no cert warmup to absorb",
    ("dev", "live", "market.login_attempts"): "dev fails fast; no cert warmup to absorb",
    ("dev", "sandbox", "market.login_retry_interval_s"): "dev fails fast",
    ("dev", "live", "market.login_retry_interval_s"): "dev fails fast",
    ("dev", "sandbox", "reconcile.retry_interval_s"): "dev fails fast",
    ("dev", "live", "reconcile.retry_interval_s"): "dev fails fast",
    ("dev", "sandbox", "reconcile.retry_budget_s"): "dev fails fast",
    ("dev", "live", "reconcile.retry_budget_s"): "dev fails fast",
    ("dev", "sandbox", "risk.daily_max_loss"): "dev sizing; nothing is at risk",
    ("dev", "live", "risk.daily_max_loss"): "dev sizing; nothing is at risk",
    ("dev", "sandbox", "signal.max_width"): (
        "F5 (2026-09-04): contradicts dev's own 'keep the VALUES in step' note. "
        "Harmless — dev submits nothing — but allowlisted rather than silent, and "
        "worth narrowing to 10.0 the next time the dev config is touched"
    ),
    ("dev", "live", "signal.max_width"): "see the sandbox pair above",
    # `walk_source` / `trigger_source` are PROSE fields — the recorded reasoning for
    # the walk and trigger numbers. dev's copies point back at the sandbox file
    # rather than duplicating several hundred words that would then drift.
    #
    # sandbox <-> live: the seven NUMBERS are byte-identical and deliberately so —
    # none of them is denominated in account size, so the live rail change does not
    # reach them (2026-09-09). Only the PROSE diverges, because the field is read in
    # the file that is deployed and "SANDBOX-ENTRY values" is a false description of
    # a file routing real money. Retire these two entries by running the owed
    # data-backed pass at the first size increase, never by re-copying cert's text.
    ("sandbox", "live", "execution.walk_source"): (
        "live records the min-size acceptance, why cert cannot tune these (the "
        "undocumented fill mechanism; the entry path stopped by the lost 5-point "
        "ladder), and the one parameter whose data IS admissible today "
        "(entry_drift_abort_points, off the live index path)"
    ),
    ("sandbox", "live", "watchdog.trigger_source"): (
        "as above, plus a WIDER scope than cert's: live's note covers "
        "exit_walk_away_pct and exit_walk_min_steps, which the owed pass names and "
        "which had no recorded justification before it"
    ),
    ("dev", "sandbox", "execution.walk_source"): "dev's copy points at the real one",
    ("dev", "live", "execution.walk_source"): "dev's copy points at the real one",
    ("dev", "sandbox", "watchdog.trigger_source"): "dev's copy points at the real one",
    ("dev", "live", "watchdog.trigger_source"): "dev's copy points at the real one",
}

#: Revisit threshold from the design: at ~10 sandbox<->live entries the DRY argument
#: starts to win and base+overlay should be reopened. Asserted rather than written
#: down, because a threshold nobody measures is a threshold nobody honours.
MAX_SANDBOX_LIVE_DIVERGENCE = 10


def _flatten(path: Path) -> dict[str, object]:
    raw = tomllib.loads(path.read_text())
    out: dict[str, object] = {}

    def walk(d: dict, prefix: str = "") -> None:
        for k, v in d.items():
            dotted = f"{prefix}{k}"
            if isinstance(v, dict):
                walk(v, dotted + ".")
            else:
                out[dotted] = v

    walk(raw)
    return out


@pytest.fixture(scope="module")
def flattened() -> dict[str, dict[str, object]]:
    return {p: _flatten(f) for p, f in PROFILE_FILES.items()}


# -- (a) identical key sets ------------------------------------------------


def test_every_profile_file_exists():
    for profile, path in PROFILE_FILES.items():
        assert path.is_file(), f"{profile}: {path} is missing"


def test_profiles_carry_identical_key_sets(flattened):
    """THE assertion this module exists for.

    A REQUIRED key added to two files and not the third is a ConfigError on the
    third — silent until that profile is the one being started.
    """
    reference = set(flattened["sandbox"])
    for profile, keys in flattened.items():
        missing = reference - set(keys)
        extra = set(keys) - reference
        assert not missing, f"argus.{profile}: missing {sorted(missing)}"
        assert not extra, f"argus.{profile}: has keys sandbox does not: {sorted(extra)}"


# -- (b) every profile file actually loads ---------------------------------


@pytest.mark.parametrize("profile", sorted(PROFILE_FILES))
def test_every_profile_file_loads(profile):
    """(a)'s teeth. Catches a missing key, a broken cross-key constraint and a
    live-fence violation IN THE FILE THAT IS NOT BEING RUN TODAY — which is the
    only place any of those can hide."""
    cfg = Config.load(PROFILE_FILES[profile])
    assert cfg.source is not None
    assert cfg.source.sha


# -- (c) value divergence is on the allowlist -------------------------------


def test_value_divergence_is_declared(flattened):
    profiles = sorted(PROFILE_FILES)
    undeclared: list[str] = []
    for i, a in enumerate(profiles):
        for b in profiles[i + 1 :]:
            for key in sorted(flattened[a]):
                if flattened[a][key] == flattened[b][key]:
                    continue
                if (a, b, key) in ALLOWED_DIVERGENCE or (b, a, key) in ALLOWED_DIVERGENCE:
                    continue
                undeclared.append(
                    f"{a} vs {b}: {key} = {flattened[a][key]!r} vs {flattened[b][key]!r}"
                )
    assert not undeclared, (
        "undeclared config divergence — if it is intended, add it to "
        "ALLOWED_DIVERGENCE with the reason:\n  " + "\n  ".join(undeclared)
    )


def test_the_allowlist_has_no_stale_entries(flattened):
    """An entry that no longer describes a real difference is worse than none: it
    reads as a live exemption while exempting nothing, and it is how the next
    genuine divergence gets waved through."""
    stale = [
        f"{a} vs {b}: {key} ({reason})"
        for (a, b, key), reason in ALLOWED_DIVERGENCE.items()
        if key in flattened[a] and key in flattened[b] and flattened[a][key] == flattened[b][key]
    ]
    assert not stale, "ALLOWED_DIVERGENCE entries that no longer diverge:\n  " + "\n  ".join(stale)


def test_sandbox_live_divergence_stays_small(flattened):
    count = sum(
        1
        for key in flattened["sandbox"]
        if flattened["sandbox"][key] != flattened["live"][key]
    )
    assert count <= MAX_SANDBOX_LIVE_DIVERGENCE, (
        f"{count} keys diverge between sandbox and live. Past "
        f"{MAX_SANDBOX_LIVE_DIVERGENCE} the base+overlay shape rejected in the "
        f"2026-09-04 design starts to win — reopen §5.1 rather than raising this."
    )


# -- (d) live-specific invariants -------------------------------------------


def test_live_declares_live():
    assert _flatten(PROFILE_FILES["live"])["environment"] == "live"


def test_live_does_not_share_the_cert_database(flattened):
    """F1. Resolved against the Dockerfile's WORKDIR (/data, the one state volume),
    so a shared name means live opens the cert file: the hash chain continues from
    sandbox rows, and cert `positions` are read back as expected state against the
    LIVE account — missing_at_broker on day one, which in live hard-halts."""
    assert flattened["live"]["audit.db_path"] != flattened["sandbox"]["audit.db_path"]
    assert flattened["live"]["audit.db_path"] != flattened["dev"]["audit.db_path"]


def test_live_is_durable(flattened):
    """Also enforced by the loader; asserted here so the FILE is wrong loudly at
    test time rather than at 09:29 ET on cutover day."""
    assert flattened["live"]["audit.synchronous"] in DURABLE_SYNCHRONOUS


def test_live_pins_an_account(flattened):
    """The live guard's file-side half. `test_every_profile_file_loads` already
    fails if this is empty — this says WHY in one line instead of raising a
    ConfigError the reader has to go and look up."""
    assert flattened["live"]["broker.account_number"].strip()
    assert flattened["sandbox"]["broker.account_number"] == ""
    assert flattened["dev"]["broker.account_number"] == ""


@pytest.mark.parametrize("profile", sorted(PROFILE_FILES))
def test_the_welded_size_invariant_holds(profile, flattened):
    """`max_concurrent_positions x max_loss_per_position <= daily_max_loss`.

    CLAUDE.md names this a standing landmine and it is checked by PROSE today. The
    breaker only stops NEW entries and cannot close what is open, so breaking the
    inequality lets it trip having already authorized more than it caps.
    """
    f = flattened[profile]
    authorized = f["risk.max_concurrent_positions"] * f["risk.max_loss_per_position"]
    assert authorized <= f["risk.daily_max_loss"], (
        f"argus.{profile}: {f['risk.max_concurrent_positions']} x "
        f"{f['risk.max_loss_per_position']} = {authorized} > "
        f"{f['risk.daily_max_loss']}"
    )


# -- the profile mechanism itself -------------------------------------------


def test_filenames_resolve_to_their_profile():
    assert profile_from_filename("argus.live.toml") == "live"
    assert profile_from_filename("argus.dev.toml") == "dev"
    assert profile_from_filename("argus.sandbox.toml") == "sandbox"
    # Two dots required, so the PRE-RENAME name carries no profile and is asserted
    # against nothing. That is what let the loader ship a release ahead of the
    # rename; it stays true afterwards for an ad-hoc `--config` path, and it is why
    # the /status row goes amber on a profile-less file.
    assert profile_from_filename("argus.toml") is None
    assert profile_from_filename("scratch.toml") is None


def test_no_profile_file_is_unlisted():
    """A fourth `argus.<profile>.toml` appearing in the repo and NOT in
    PROFILE_FILES would sit outside every assertion in this module — no key-set
    check, no load check, no divergence check. Which is the state the whole module
    exists to prevent, one file over."""
    on_disk = {
        p.name for p in REPO.glob("argus.*.toml") if profile_from_filename(p.name)
    }
    listed = {p.name for p in PROFILE_FILES.values()}
    assert on_disk == listed, (
        f"profile config files not covered by this module: {sorted(on_disk - listed)}"
    )


def test_the_pre_rename_name_is_gone():
    """`argus.toml` ceases to exist, and `.gitignore` carries it so a stray copy —
    a hand-made backup, a half-finished cutover — cannot be committed back and
    quietly become the file somebody points `--config` at. It would carry no
    profile, so the filename<->environment assertion would not be armed on it."""
    assert not (REPO / "argus.toml").exists(), (
        "argus.toml is back. Config is selected by profile; this name asserts nothing"
    )
    assert "\nargus.toml\n" in (REPO / ".gitignore").read_text()


def test_a_mislabelled_live_file_is_refused(tmp_path):
    """The end-to-end version of `_check_profile`: it is the FILENAME that arms it,
    so it has to be exercised through `load`, not only through `from_dict`."""
    raw = PROFILE_FILES["live"].read_text().replace(
        'environment = "live"', 'environment = "sandbox"', 1
    )
    bad = tmp_path / "argus.live.toml"
    bad.write_text(raw)
    with pytest.raises(ConfigError, match="names profile"):
        Config.load(bad)
