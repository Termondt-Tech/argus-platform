# Provenance

This repository is a **one-way export** of a private repository, made on 2026-10-22 from
private commit `b9dfd26` (a clean working tree). The history was not carried: the public repository
starts at one commit, and each later export is one more squashed commit.

## What the export is

An allow-list copy of the private tree, made by a private script, which only deletes and
redacts:

- Files that would name the owner's other strategy, the host, its storage, or the research
  programme are left out (301 tracked files, below).
- Sections marked private in the files that stay are stripped (10 blocks), and
  43 further edits remove the remaining references.
- Identifiers are replaced by placeholders: the brokerage account number is `5XX00000`,
  addresses are RFC 5737 documentation addresses (`192.0.2.x`), host, user and storage
  names are generic (`droplet`, `deploy-user`, `/path/to/...`).
- 8 rows of the decision, design and release indexes that pointed at unpublished
  documents are dropped; 66 links to unpublished documents
  are rewritten to say so, and 40 links into the
  research folder point at `RESULTS.md`.
- A private check with every sensitive pattern ran against this tree and reported zero
  hits before it was published; a second net is GitHub's secret scanning.

The tree holds 454 files, 173,444 lines.

## What was left out, by category

- host runbooks and logs: 19 files
- storage and reporting plumbing, and this export's scripts: 25 files
- task prompts naming the owner's machine, the account or the article: 4 files
- tests of the host plumbing: 2 files
- the research programme: 195 files
- the second strategy: 55 files
- this export's own design: 1 files

## What stays true across exports

The private tree is the only source. Nothing is edited in this repository: a change here
would be overwritten by the next export, which is why pull requests cannot be merged
(see the README). The private commit above is the point of reference for any question
about what the platform looked like when this was published.
