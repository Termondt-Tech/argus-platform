# Architecture documentation update

**Use when:** a release (or a run of releases) has changed a container, a component, an
execution flow, a state machine, a route, a volume, a bind mount or a timer — or when
`docs/architecture/00-index.md`'s "Verified against `main` at …" line is more than a
release or two behind.

**Produces:** edits to `docs/architecture/00-index.md` through `06-whiteboard.md`.
The rules are the ones `00-index.md` sets for itself: **derived from source**, real
names, anchors by NAME not line number, `> **Unverified:**` / `> **Assumption:**` where
source cannot confirm, `useMaxWidth: false` on every Mermaid diagram, rationale linked
not repeated.

**Mode:** documentation only. No `src/`, `tests/` or config change. A drift between
code and diagram that turns out to be a *code* defect is reported, not fixed.

| Placeholder | Fill with |
|---|---|
| `{{SINCE}}` | The sha or tag the docs were last verified against (read it from `00-index.md`) |
| `{{UNTIL}}` | The sha or tag to verify against now (usually `HEAD` of `main`, or a release tag) |
| `{{RELEASES}}` | Release notes in range, e.g. `docs/releases/v0.4.5.md … v0.5.2.md` |
| `{{SCOPE}}` | `full` (every file) or a list of files/areas |

---

## The prompt

````text
Before beginning, read docs/templates/00-standing-preamble.md in full and follow it.

Then read, in full: docs/architecture/00-index.md (its "Living document" and "Viewing
and printing" sections are the rules for this task), every file in docs/architecture/
in scope ({{SCOPE}}), and the release notes {{RELEASES}}.

Task: bring docs/architecture/ up to date with the code at {{UNTIL}}, from its last
verification at {{SINCE}}.

Method:
1. Build the change list from the code, not the prose: git log and git diff
   {{SINCE}}..{{UNTIL}} -- src/ docker-compose.yml Dockerfile argus.service scripts/
   plus any systemd timers. List every new or removed module, class, public function,
   HTTP route, audit category family, config section, volume, bind mount, timer and
   state/transition. Use the release notes and decision records only to understand
   intent.
2. Map each change to the file(s) that must reflect it, using 00-index.md's rules
   (new module → 03; route/volume/mount/timer/unit → 02; walk state machine, exception
   boundaries, halt grade, reconcile path → 04/05; any of these → check 06).
3. Update each diagram and its prose. Every node and arrow must correspond to a name
   and a call or dependency that exists in src/ at {{UNTIL}}: verify each one you add
   or keep by reading the source (grep is not enough for an arrow — read the call
   site). Anchors by NAME (module.py::Class.method), never line numbers. Every
   diagram keeps a useMaxWidth: false init directive on its first line; its key
   depends on the diagram type (e.g. %%{init: {"c4": {"useMaxWidth": false}}}%% for
   C4), so copy it from an existing diagram of the same type. Where source cannot
   confirm something (a broker-side transition
   Argus only observes, a value recorded as unmeasured), mark it
   "> **Unverified:**" or "> **Assumption:**" inline.
4. Do not repeat rationale. Link the decision record instead.
5. Update 00-index.md:
   - "Verified against main at <sha> (<date>), the tree <tag> (<name>) was built
     from", and the one-paragraph system status (phase, profile, size) checked against
     argus.live.toml, not CLAUDE.md;
   - "What changed since …": the structural changes in this range, one bullet each,
     behaviour changes only;
   - the open-items list: add what is newly open, move what closed to "Resolved since
     the previous revision", and drop what was already in that list last revision
     (it is kept for exactly one revision).
6. Render check: if the environment allows, run ./scripts/render-architecture.sh and
   confirm every diagram renders; otherwise, check each Mermaid block's syntax by
   reading it carefully and say that the render was not run.

Report, at the end of 00-index.md's change list or in your final message:
- every drift you found where the CODE looks wrong rather than the doc (report only;
  do not fix src/);
- anything you could not verify.

Commit with a message naming the verified sha, and push to the branch you were given.
````

---

## Notes for the operator (not part of the prompt)

- **Cadence**: after every MINOR, and after any PATCH that the build notes say owes an
  architecture update. A doc verified many releases ago is worse than none, because it
  is read as current.
- **Scope it** when time is short: `{{SCOPE}} = 04-uml-sequences.md, 05-uml-state.md`
  after an exit-path release is usually the valuable subset.
- The sequence diagrams are wide (~3,800px). If one grows unreadable, ask for it to be
  split by scenario rather than compressed.
