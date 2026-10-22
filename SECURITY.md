# Security

This repository is a reference implementation, published to be read. It is not
maintained on a roadmap and carries no service level. It can still contain a
defect that matters to someone who runs it, so reports are welcome.

## Reporting

Use GitHub's **private vulnerability reporting** on this repository (the
"Security" tab, then "Report a vulnerability"). Do not open a public issue for a
finding that could be exploited against a running instance.

A report is most useful with: the file and function, the input or sequence that
reaches the defect, and what a running instance would do as a result (send an
order, skip a rail, lose an audit record, expose a credential).

## Scope

In scope: anything under `src/` that could cause an instance to send an order it
should not, skip or weaken a rail, lose or corrupt the audit chain, or expose a
credential or the kill-switch token.

Out of scope: the example strategy's trading performance (it is documented as
losing; see `RESULTS.md`), the broker's own API behaviour, and anything that
needs an attacker already holding the host's credentials.

## What this repository never contains

No credentials, tokens or account numbers: every credential comes from the
environment at runtime (`.env.example` names them), and the live profile ships
with a placeholder account. The private repository it was exported from has a
redaction check that must report zero hits before an export; see `PROVENANCE.md`.
