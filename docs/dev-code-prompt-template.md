Before beginning:

1. Read CLAUDE.md in full. This repo has non-negotiables and standing rules
   (kill switch, audit chain, no live-DB writes, size rails, etc.) that apply
   regardless of what the task file says — they are the wider contract this
   task lives inside.

2. Read docs/decisions/2026-09-24-a-flatten-that-crossed-below-its-own-ladder.md in full

3. Standing repo rule, restated because it applies to every session
   regardless of task: NEVER write to argus.db. Any exploration or ad hoc
   check is read-only (sqlite3.connect("file:argus.db?mode=ro", uri=True));
   any dev/test run uses argus.dev.toml, never the bare command.

Now: read docs/decisions/2026-09-24-a-flatten-that-crossed-below-its-own-ladder.md
and design the solution. The solution must be complete and completely resolve all findings, be of high quality, and performant. The solutions must not introduce ANY DEFECTS.

Follow best practices for trading systems as first priority and general architecture best practices as second priority (docs/architecture/general-best-practices.md)

Save the output as an .md formated file in docs/designs
