# Documentation hygiene (CLAUDE.md re-audit and cross-document consistency)

**Use when:** after a MINOR, after a run of releases, or when a session has been misled
by stale prose. `CLAUDE.md` asks to be re-audited when it crosses ~300 lines; it is
~1,800. Every line of it is loaded into every session regardless of task, so its length
is a cost paid on every prompt, and its staleness is a defect ("drift between these docs
and the code is a bug").

This also covers the retrospective's **I5, a spec-consistency pass**: do the spec, the
profiles, CLAUDE.md, the runbooks and the decision index describe the same system, the
same account and the same numbers?

**Produces:** a trimmed `CLAUDE.md`, narrative relocated into the `docs/decisions/`
records it belongs to, corrections to stale values across the docs, and a short report
of what moved and what was found.

**Mode:** documentation only. **Relocate, never delete** — every sentence removed from
CLAUDE.md either already exists in a linked record or is moved into one. No `src/`,
`tests/` or config change; a stale doc that turns out to reveal a *code* problem is
reported, not fixed.

| Placeholder | Fill with |
|---|---|
| `{{TARGET_LINES}}` | Target CLAUDE.md length, e.g. `~400` |
| `{{SCOPE}}` | `claude-md`, `consistency`, or `both` |

---

## The prompt

````text
Before beginning, read docs/templates/00-standing-preamble.md in full and follow it.

Then read CLAUDE.md in full, especially its "Convention" section (the rules for what
earns a place there), docs/decisions/README.md, docs/0dte_spx_desk_spec.md, README.md,
docs/live-cutover-runbook.md, docs/go-live-findings.md, and the three argus.*.toml
profiles.

Scope: {{SCOPE}}. Mode: documentation only — relocate, never delete; change no src/,
tests/ or config.

Part 1 — CLAUDE.md (if scope includes it). Target about {{TARGET_LINES}} lines.
What earns a place: current phase and state; the non-negotiables; the standing
landmines; build/test commands; the file map; credentials rules; the "never" rules a
session must know before touching code. What does not: how a past problem was solved,
measurements, incident narratives, per-release histories.
1. Inventory every paragraph: keep / shorten to one current-state sentence + pointer /
   relocate. For each relocation, name the destination record, and confirm the content
   is already there or move it there first (append under a dated "Relocated from
   CLAUDE.md" heading).
2. Collapse the "Current phase" section to: the phase, the deployed version and config
   sha, what is BUILT-not-deployed, and pointers. One sentence per release beyond the
   current one, at most.
3. Keep every "never" rule. A rule may be shortened; it may not be weakened or lost.
   List every never-rule before and after and show the lists match.
4. Keep the file readable top to bottom by a session that knows nothing.

Part 2 — consistency (if scope includes it). For each fact below, read it from the
code/config and compare every document that states it; fix the documents, and list
each fix:
- the phase, the deployed version, the live config sha, the account;
- the risk caps and the welded invariant numbers, per profile;
- every config value quoted in prose (sample the watchdog, risk, staleness and signal
  sections at least);
- the blackout list's end date;
- the open questions and open decisions in docs/decisions/README.md against the
  records that closed them;
- orphans: any file in docs/decisions/, docs/designs/ or docs/research/ that no index
  lists, and any index row pointing at a missing file;
- the spec (§6 rails, §8 sizing, §9 risk register, §10 rollout) against what is built:
  say where the spec is now historical and should say so.

Report (in your final message, and as a dated entry at the end of
docs/decisions/README.md if anything normative moved): lines before → after; what was
relocated where; every stale value corrected (document, was, now, source of truth);
anything that looks like a code problem rather than a doc problem.

Commit in two commits (relocations; consistency fixes) and push to the branch you were
given.
````

---

## Notes for the operator (not part of the prompt)

- **Review the never-rule list before merging.** It is the one thing this pass must
  not lose, and the prompt asks for it to be shown precisely so you can check it.
- Consider running `consistency` alone before every MINOR, and `both` after it.
