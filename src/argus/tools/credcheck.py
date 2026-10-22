"""Credential shape diagnostic — NEVER prints secret values.

`invalid_grant / Client secret mismatch` from /oauth/token means the client_secret
sent does not belong to the app registration that issued the refresh_token. The
usual causes are mechanical: values from two different registrations, a client ID
pasted where the secret goes, a cert value in a live slot, or copy/paste damage.

This reports only shape — set/empty, length, stray whitespace, and whether two
variables are identical. That is enough to catch every common mistake without
exposing anything.

Credentials live on a (role, world) grid: trading/cert, trading/live, and
market-data/live (market data in cert falls back to the cert TRADING slot — the
same registration). This walks that grid, so "which of these do I actually need
right now" is answered per slot rather than left to be inferred.

    ./.venv/bin/python -m argus.tools.credcheck
"""

from __future__ import annotations

from argus.bootstrap import load_env
from argus.broker.session import (
    MARKETDATA_LIVE_REFRESH_VAR,
    MARKETDATA_LIVE_SECRET_VAR,
    TRADING_CERT_REFRESH_VAR,
    TRADING_CERT_SECRET_VAR,
    TRADING_LIVE_REFRESH_VAR,
    TRADING_LIVE_SECRET_VAR,
)
import os

#: The (role, world) grid, in the order an operator provisions it. Each entry is
#: (label, secret var, refresh var, required-when).
_SLOTS = [
    (
        "trading / cert",
        TRADING_CERT_SECRET_VAR,
        TRADING_CERT_REFRESH_VAR,
        "always — every non-live profile trades on it, and market data falls back to it",
    ),
    (
        "trading / live",
        TRADING_LIVE_SECRET_VAR,
        TRADING_LIVE_REFRESH_VAR,
        "only when environment = 'live' (the cutover)",
    ),
    (
        "market data / live",
        MARKETDATA_LIVE_SECRET_VAR,
        MARKETDATA_LIVE_REFRESH_VAR,
        "whenever [marketdata] source = 'live' — true in both shipped profiles",
    ),
]

_ALL = [var for _, secret, refresh, _req in _SLOTS for var in (secret, refresh)]


def _describe(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        return f"  {name:34s} NOT SET"
    if not value:
        return f"  {name:34s} set but EMPTY"
    flags = []
    if value != value.strip():
        flags.append("leading/trailing whitespace")
    if "\n" in value or "\r" in value:
        flags.append("contains a NEWLINE (wrapped paste?)")
    if " " in value.strip():
        flags.append("contains an internal space")
    if value.strip() != value.strip().replace('"', "").replace("'", ""):
        flags.append("contains quote characters")
    suffix = f"   << {'; '.join(flags)}" if flags else ""
    return f"  {name:34s} set, length={len(value):4d}{suffix}"


def main() -> None:
    loaded = load_env()
    print(f".env loaded: {loaded}\n")
    print("shape (no values shown):")
    for label, secret, refresh, required in _SLOTS:
        print(f"\n  [{label}]  required: {required}")
        print(_describe(secret))
        print(_describe(refresh))

    print("\ncross-checks:")
    env = {n: os.environ.get(n) for n in _ALL}
    problems = []

    # A slot is half-provisioned. Both halves or neither — the refresh token is
    # issued BY a specific client, so one without the other authenticates nothing.
    for label, secret, refresh, _required in _SLOTS:
        if bool(env[secret]) != bool(env[refresh]):
            missing = secret if not env[secret] else refresh
            problems.append(f"[{label}] is HALF set — {missing} is missing")

    # A secret pasted where a refresh token goes, within one slot.
    for label, secret, refresh, _required in _SLOTS:
        if env[secret] and env[secret] == env[refresh]:
            problems.append(f"[{label}] {secret} and {refresh} hold the SAME value")

    # CERT VALUES IN A LIVE SLOT. The migration to the (role, world) names copies
    # existing values into the CERT slots, so copying into a LIVE slot as well is
    # the mechanical mistake this exists to catch. `trading_credentials` refuses
    # the trading half of this at startup under environment='live'; the
    # market-data half has no such rail, because a wrong quote credential costs a
    # failed subscription and not a trading day.
    for live_var, cert_var, why in (
        (
            TRADING_LIVE_SECRET_VAR,
            TRADING_CERT_SECRET_VAR,
            "a cert secret cannot authenticate against production",
        ),
        (
            TRADING_LIVE_REFRESH_VAR,
            TRADING_CERT_REFRESH_VAR,
            "a cert refresh token is not valid on production",
        ),
        (
            MARKETDATA_LIVE_SECRET_VAR,
            TRADING_CERT_SECRET_VAR,
            "a cert secret cannot read live quotes",
        ),
        (
            MARKETDATA_LIVE_REFRESH_VAR,
            TRADING_CERT_REFRESH_VAR,
            "a cert refresh token cannot read live quotes",
        ),
    ):
        if env[live_var] and env[live_var] == env[cert_var]:
            problems.append(f"{live_var} is IDENTICAL to {cert_var} — {why}")

    # How the two LIVE slots relate. Stated, never flagged — every combination
    # below is a legitimate configuration.
    #
    # TWO FIELDS, TWO DIFFERENT QUESTIONS, and conflating them is what this tool
    # got wrong until 2026-09-09:
    #
    #   CLIENT_SECRET  identifies the APP.       Same secret  -> one registration.
    #   REFRESH_TOKEN  is the CREDENTIAL, and    Same token   -> literally one
    #                  SCOPE rides on it.                        credential doing
    #                                                            both jobs.
    #
    # One app can mint several refresh tokens with DIFFERENT scopes — a `read`-only
    # one for market data beside a `read`+`trade` one for orders — which is real,
    # broker-enforced privilege separation from a single registration. Reading
    # "same secret" as "no separation" reports that setup as the insecure one it is
    # the fix for. Scope is not visible from the environment at all, so where the
    # tokens differ this says what it can see and names what it cannot.
    both_live = env[TRADING_LIVE_SECRET_VAR] and env[MARKETDATA_LIVE_SECRET_VAR]
    same_app = both_live and env[TRADING_LIVE_SECRET_VAR] == env[MARKETDATA_LIVE_SECRET_VAR]
    same_token = (
        env[TRADING_LIVE_REFRESH_VAR]
        and env[MARKETDATA_LIVE_REFRESH_VAR]
        and env[TRADING_LIVE_REFRESH_VAR] == env[MARKETDATA_LIVE_REFRESH_VAR]
    )

    if not env[MARKETDATA_LIVE_SECRET_VAR] or not env[MARKETDATA_LIVE_REFRESH_VAR]:
        problems.append(
            "live market data needs BOTH market-data live variables set "
            "(both shipped profiles set [marketdata] source = 'live')"
        )

    if problems:
        for p in problems:
            print(f"  PROBLEM: {p}")
    else:
        print("  no shape problems found — every slot present is complete, and no live")
        print("  slot is carrying a cert value.")

    print("\nnotes:")
    if same_token:
        # The only genuinely un-separated case: one credential doing both jobs, so
        # whatever scope it carries, market data holds it too.
        print("  the two LIVE slots hold the SAME REFRESH TOKEN — one credential")
        print("  serving both trading and market data. Revoking or rotating it takes")
        print("  BOTH down, and if that token carries `trade` scope then the")
        print("  read-only/money-moving split holds ONLY in code (SessionRole), not")
        print("  in the credential. Mint a second, `read`-only token for market data")
        print("  — the same app registration can issue it.")
    elif same_app and env[MARKETDATA_LIVE_REFRESH_VAR]:
        print("  the two LIVE slots share a client secret (ONE app registration) but")
        print("  hold DIFFERENT refresh tokens. Scope rides on the token, not the")
        print("  app, so this CAN be full privilege separation — a `read`-only token")
        print("  for market data beside a `read`+`trade` one for orders.")
        print("  SCOPE IS NOT VISIBLE FROM THE ENVIRONMENT: this tool cannot confirm")
        print("  it. Verify in the tastytrade portal that the market-data token has")
        print("  `trade` UNCHECKED. Note both tokens die together if the app itself")
        print("  is revoked.")
    elif both_live:
        print("  the two LIVE slots are distinct registrations — trading and market")
        print("  data are independently revocable.")
    if not env[TRADING_LIVE_SECRET_VAR] and not env[TRADING_LIVE_REFRESH_VAR]:
        print("  the live TRADING slot is unset. Expected before the cutover; a")
        print("  'live' profile refuses to start without it.")
    print("\n  If /oauth/token says 'Client secret mismatch', the values in that slot")
    print("  are well-formed but come from DIFFERENT app registrations. The refresh")
    print("  token is issued BY a specific client, so the secret must be that same")
    print("  client's secret — re-copy both from one registration.")


if __name__ == "__main__":
    main()
