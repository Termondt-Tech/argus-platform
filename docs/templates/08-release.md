# Release preparation

**Use when:** one or more builds are on `main` (or a branch), reviewed, and you want
them packaged as a version: release notes, a deploy plan, a rollback, and the
post-deploy checks.

**Produces:** `docs/releases/vX.Y.Z.md` in the shape of `docs/releases/v0.5.2.md`, the
name row in `docs/releases/README.md`, and the CLAUDE.md "Current phase" paragraph for
the release (PREPARED, NOT YET TAGGED OR DEPLOYED).

**Mode:** documentation only. **The session never tags, never runs
`argus-deploy.sh`, never touches the droplet.** Those are Richard's, by hand, from the
notes this produces. It may run the test suite and compute the config sha.

**After deploy:** re-run the tail of this template (the "after deploy" prompt below)
to flip the notes and CLAUDE.md to DEPLOYED with the read-back sha.

| Placeholder | Fill with |
|---|---|
| `{{VERSION}}` | e.g. `v0.5.3` |
| `{{PREV_TAG}}` | The tag currently deployed on the droplet (read `/status`, not memory) |
| `{{CONTENTS}}` | The decision records / builds this release carries |
| `{{NAME}}` | A star name if you have one, or "propose" |

---

## The prompt

````text
Before beginning, read docs/templates/00-standing-preamble.md in full and follow it.

Then read, in full: docs/releases/README.md (version numbers and naming rules — both
binding), docs/releases/{{PREV_TAG}}.md and the previous two release notes for shape,
the decision records and designs for {{CONTENTS}}, and runbook §17 in
docs/argus-deployment-runbook.md (packaging and the deploy script's classifier).

Task: prepare release {{VERSION}} over {{PREV_TAG}}, carrying {{CONTENTS}}. Mode:
documentation only — do not tag, deploy, or touch the droplet.

Establish the facts from the tree, not from prose:
1. git log / git diff {{PREV_TAG}}..HEAD. List everything in the range, and flag
   anything in it that is NOT in {{CONTENTS}} — nothing ships by accident.
2. Release class: image-only, config-only, or COUPLED image + config. List every new
   REQUIRED key and confirm it is in all three profiles; list every changed value per
   profile. Migration or not. Any change to docker-compose.yml, Dockerfile,
   argus.service or a bind mount (the classifier must know it). State what the deploy
   script's plan should print (config / build / restart) and why.
3. Compute the live profile's config sha the way the loader does (the first 12 hex
   of sha256 over argus.live.toml's bytes, config.py CONFIG_SHA_LEN) and name it as the expected /status read-back, with the caveat
   that it changes if any config byte does before the tag.
4. Run the full suite; record passed / skipped counts, and the count at {{PREV_TAG}}.
5. The version number per docs/releases/README.md: MINOR only for a rollout-phase
   change (or a phase of work Richard has called); otherwise PATCH. If {{VERSION}}
   disagrees with the rule, say so.
6. Name: {{NAME}}. If "propose", propose one or two per the naming rules (a real star
   whose name reads as "the eye", fitted to the SHAPE of this release, etymology from
   two independent sources, and note if it spends the pool or a reservation). The
   final choice is Richard's.

Write docs/releases/{{VERSION}}.md with:
- The status callout: PREPARED, NOT YET TAGGED OR DEPLOYED; built on; release class in
  bold with the new REQUIRED keys; expected classifier output; expected config sha;
  migration / compose / unit changes (or "none"); suite count.
- "Deploying this changes trading behaviour in N ways" — every behaviour change, one
  line each, most consequential first. If it changes none, say that plainly.
- Rollback, per behaviour where possible (one-line config edits), and full rollback to
  {{PREV_TAG}}. "Never with a position open" wherever it applies, and any window where
  a restart has side effects (e.g. 15:00–16:00).
- An italic summary paragraph with links to every decision, design and research note.
- Name — and why it fits.
- What changed — one section per item, what the operator will see.
- New records and /status — a table: record, tier, meaning.
- Rules to carry — the rules a future session must not break, one line each.
- Why it's safe.
- What this release does NOT do.
- Deploying it — timing; 0. confirm (suite); 1. tag (the exact commands); 2. droplet
  (argus-deploy.sh --dry-run first, and exactly what the config diff must contain —
  "anything else in the diff is unexpected"); rollback.
- After deploy: what to check — numbered, each naming the /status field or audit
  record and what "right" looks like, including the first-occurrence checks for any
  new behaviour and the review/reversal triggers from the decision records.
- Status checklist — [x] for done, [ ] for tag, deploy, and each first-occurrence read.

Then:
- Add the {{VERSION}} row to docs/releases/README.md's naming table (marked proposed if
  Richard has not confirmed the name).
- CLAUDE.md "Current phase": one paragraph for {{VERSION}} — PREPARED, NOT YET TAGGED
  OR DEPLOYED, release class, what it changes, pointer to the notes. Shrink, don't
  grow: fold superseded "NOT YET DEPLOYED" sentences for the items it carries into it.

Commit and push to the branch you were given. Finish with the exact commands Richard
will run, in order, and the three things to check first after the restart.
````

### After deploy

````text
Before beginning, read docs/templates/00-standing-preamble.md in full and follow it.
{{VERSION}} was tagged and deployed on <DATE> at <TIME> ET. The /status read-back was:
version <...>, config sha <...>, profile <...>. <Anything that went differently.>

Update docs/releases/{{VERSION}}.md's status callout and checklist to DEPLOYED with
those facts; replace any "expected sha" with the read-back one, and flag it loudly if
they differ. Update CLAUDE.md "Current phase" (TAGGED and DEPLOYED, date, sha) and the
status blocks of the decision records the release carries (BUILT → DEPLOYED in
{{VERSION}}). Change nothing else. Commit and push.
````

---

## Notes for the operator (not part of the prompt)

- **Several builds, one release**: list them all in `{{CONTENTS}}`; step 1 catches
  anything on `main` that you did not mean to ship.
- **A config-only release** (a retune, a promotion from cert) is usually preceded by a
  `09-config-decision` record; the release notes then mostly *are* the config diff.
