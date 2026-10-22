# Prompt templates

Repeatable prompts for building out and tuning Argus with Claude Code. Each template is
a copy-paste prompt with `{{PLACEHOLDERS}}`, plus notes on when to use it, what it
produces and where the output goes. They encode how this repo already works — the shape
of a design, a decision record, a release note, a research note — so a fresh session
produces the same kind of document every time, to the same standard.

## The lifecycle

```
                  ┌─────────────────────┐
  something       │ 07 investigation    │  what happened? (report-only)
  happened ──────►│  → docs/decisions/  │──┐
                  └─────────────────────┘  │
                  ┌─────────────────────┐  │     ┌──────────────────┐
  something       │ 01 business spec    │  ├────►│ 02 design        │  how? (no src/)
  is wanted ─────►│  → docs/specs/      │──┤     │ → docs/designs/  │
                  └─────────────────────┘  │     └────────┬─────────┘
                  ┌─────────────────────┐  │              │ Richard signs off
  a question      │ 06 research         │──┘              ▼
  needs data ────►│  → docs/research/   │         ┌──────────────────┐
                  └─────────────────────┘         │ 03 build         │  code + tests
                  ┌─────────────────────┐         └────────┬─────────┘
  a value         │ 09 config decision  │                  ▼
  should move ───►│  → docs/decisions/  │──┐      ┌──────────────────┐
                  └─────────────────────┘  │      │ 04 code review   │  fresh session
                                           │      └────────┬─────────┘
                                           │               ▼
                                           └─────►┌──────────────────┐
                                                  │ 08 release       │  notes + plan
                                                  └────────┬─────────┘
                                                           │ Richard tags & deploys
                                                           ▼
                                    05 architecture update · 10 docs hygiene
```

## The templates

| # | Template | Mode | Output |
|---|---|---|---|
| 00 | [Standing preamble](00-standing-preamble.md) | — | Read by every template. The rules that apply regardless of task. |
| 01 | [Business spec](01-business-spec.md) | document | `docs/specs/YYYY-MM-DD-<slug>.md` — what and why, acceptance criteria, pre-registered success metrics, risk envelope |
| 02 | [Design](02-design.md) | design (no `src/`) | `docs/designs/YYYY-MM-DD-<slug>.md` — ends in numbered decisions for sign-off |
| 03 | [Build](03-build.md) | build | Code, tests, all three profiles, mutation check; decision record BUILT; design build notes; one CLAUDE.md sentence |
| 04 | [Code review](04-code-review.md) | report or fix | Findings table in the decision record (`Review (date)` / `Review fixes (date)`) |
| 05 | [Architecture update](05-architecture-update.md) | document | `docs/architecture/*`, verified against a named sha |
| 06 | [Research](06-research.md) | research (read-only) | `docs/research/YYYY-MM-DD-<slug>.md` + a queries directory with its pre-registration |
| 07 | [Investigation](07-investigation.md) | report-only | `docs/decisions/YYYY-MM-DD-<slug>.md`, status OPEN |
| 08 | [Release](08-release.md) | document | `docs/releases/vX.Y.Z.md` + CLAUDE.md phase paragraph; an "after deploy" follow-up prompt |
| 09 | [Config decision](09-config-decision.md) | propose or apply | `docs/decisions/YYYY-MM-DD-<slug>.md` with a reversal rule; profile edits only when approved |
| 10 | [Docs hygiene](10-docs-hygiene.md) | document | A trimmed CLAUDE.md (relocate, never delete) and cross-document consistency fixes |
| 11 | [Profit objective review](11-profit-objective-review.md) | report-only | |

The six asked for are 01–06. 07–10 are the other steps this repo already repeats: an
investigation starts most decision records, a release note follows every build, a
config retune is a decision in its own right, and CLAUDE.md is ~1,800 lines against its
own ~300-line re-audit threshold.

Related, kept where it is: the
[proactive defect-class review](../tasks/argus_proactive_defect_class_review_prompt_template.md)
— a report-only hunt for new instances of defect shapes already paid for. Run it after a
release has held quiet, and before any environment-gating flip. Template 04's checklist
C is the per-change version of the same list; when a new class is paid for, add it to
both.

## How to use one

1. Open the template, fill the placeholder table, and copy the block under **The
   prompt** into a new Claude Code session.
2. **One template per session.** A design session that also builds loses the review
   gap between them, and that gap is what catches the defects.
3. Start **code review in a fresh session** — a reviewer that did not write the code
   finds different things.
4. Read the output before the next step. The templates stop at the points that are
   yours: sign-off on a design's decisions, approval of config values, the tag and the
   deploy.

**Placeholders** are `{{UPPER_SNAKE}}`. Anything optional says so; write "none" rather
than deleting the line, so the session knows it was considered.

**Modes** are stated in every prompt, because the most common way for a session to go
wrong is to do the next step early:

| Mode | May change |
|---|---|
| report-only / research | Only the document it writes |
| document / design | Documents only — no `src/`, `tests/`, `argus.*.toml` |
| build / fix / apply | Code, tests and config, for exactly what was approved |
| — | **No template ever tags, deploys, restarts, or touches `/etc/argus/host.env`** |

## Tuning the templates

These are meant to evolve. When a session goes wrong in a way a sentence in a prompt
would have prevented, add the sentence — to `00-standing-preamble.md` if it applies to
every task, to the one template otherwise. When a new defect class is paid for, add it
to `04-code-review.md` checklist C. Keep each prompt short enough to be read; a rule
that lives in CLAUDE.md or a decision record should be pointed at, not copied.

`docs/dev-code-prompt-template.md` is the original that `02-design` and `03-build`
grew from. It predates `argus-live.db` and names only `argus.db` as off-limits; prefer
the templates here.
