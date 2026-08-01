---
name: ready
description: "Bring existing backlog tickets up to the definition of ready. Invoke with `/ready #<issue>` or `/ready` for a whole milestone. Also triggers on 'spec this ticket', 'is this implementable', 'get these issues ready', 'run the spec loop', 'sprint prep'. Triages each ticket against docs/definition-of-ready.md, drafts a spec only for what fails, has an independent agent try to refute it, and appends the result to the issue body. NOT for new feature ideas — those are /spec."
---

<!-- ABOUTME: The spec loop for existing backlog tickets: triage, draft, refute, revise, append. -->
<!-- ABOUTME: Encodes what held up on the first real run; see "What this learned the hard way". -->

# Ready

Take a ticket that already exists and make it safe to implement.

## When NOT to use this

- **A new feature idea with no issue yet.** That is `/spec`, which creates the issue.
  Do not use `/ready` for it — `/ready` never creates issues, it only amends them.
- **A ticket that already passes the bar.** Triage first (step 1). Adding a spec section
  to a ticket that is already implementable is pure ceremony, and it buries the parts
  someone actually needs to read.
- **Docs, dependency bumps, one-off scripts.** Exempt by the definition of ready itself.

## Step 0 — Load the bar

Read `docs/definition-of-ready.md` in the repo. If it does not exist, stop and say so:
this skill has no opinion of its own about what "ready" means, and inventing one silently
is how the loop stops having an exit condition.

## Step 1 — Triage before spending anything

For each ticket, read the full issue body and judge it against every criterion. Produce
one of three verdicts:

- **Ready** — no agent spend. Say which criteria carried it.
- **Done already** — for a bug ticket, check the defect still reproduces before you spend
  anything. Triage judges the issue's text; the text can be stale. One run triaged a bug
  NARROW GAP that had shipped a fix weeks earlier, and would have specced work that was
  already merged. The confirmation is usually one command. Run it.
- **Narrow gap** — fails one or two criteria, usually because a single question is open.
  These do not need a full spec. Answer the question, append a short section.
- **Design gap** — fails on an unmade decision, typically visible as the issue's own text
  saying "decide X" or "options: A or B". These get the full loop.

Expect roughly a third of any backlog to come back Ready. Report the triage before
drafting anything, so the spend is a decision rather than a default.

## Step 2 — Order the work

Read the related tickets before drafting, not during. Two orderings matter:

- **Dependency chains run serially.** If ticket B consumes a decision ticket A makes,
  A's spec must land before B's is drafted, or B gets written against assumptions A has
  not settled.
- **Conflict pairs get flagged.** Two tickets that modify the same function will collide.
  Neither issue usually mentions the other. Say so in both specs.

Everything else fans out in parallel.

## Step 3 — Draft

One agent per ticket. **Hand it its reading list** — triage already told you which files
matter, and letting the drafter rediscover them costs real tokens.

The drafter must **make the decision the ticket defers**, not restate the options. A spec
that says "we should decide between A and B" has failed; that is the thing the ticket
already said.

**Specs go to files, not through you.** Give each drafter a path — one spec per file under
the session scratchpad, `spec-<N>.md` — and have it return a short decision report instead
of the spec text. The refuter reads the file directly. Specs run tens of KB; returning them
means paying for every one twice, inbound and then written back out, for no gain.

Also tell the drafter to check the ticket's own citations before building on them. The
refuter is instructed to hunt false citations in the spec; nothing otherwise stops a stale
one in the *issue* from being inherited into it.

Full prompt template: `references/agent-prompts.md`.

## Step 4 — Refute

A second agent whose job is to refute the spec rather than improve it.

**Fresh context is the load-bearing part** — a reviewer that can see the drafter's
reasoning ratifies it. A different model helps on top of that, but it is the secondary
property, and it is the one to trade away. Where a spec prescribes destructive actions,
production writes, or anything whose undo path is in doubt, pick the strongest reviewer
available and take same-model-fresh-context over weaker-model-fresh-context. On one run
that trade is what caught a spec citing a prior issue as having answered a question that
issue could not have asked — the authorisation for a merge on the busiest node in the batch.

The contract that makes this work, in full in `references/agent-prompts.md`:

- Assume every claim about the code is wrong until a file has been opened.
- **Verify every load-bearing citation.** This is the highest-value thing it does.
- Return a structured verdict separating blocking from non-blocking.
- An explicit out-of-bounds list, so it cannot relitigate scope, splitting, deferral, or style.
- A stated padding penalty: a padded blocking list is a failed review.
- List what it checked and found **true**, not only what it found wrong. That list is how
  you know it actually looked.

Let it execute code. On the first real run, the reviewer ran the planner in a throwaway
process and disproved a claim that reading alone would have accepted.

## Step 5 — Revise

**Never let the reviser work from a summary of the findings.** Hand it the reviewer's
`file:line` spans and require it to reopen them. The instruction "verify this yourself" is
not enough on its own — on the first run a drafter ignored exactly that instruction,
rewrote from the summary, and fabricated a citation to a file it never opened.

Symptom to watch for: a revision that used almost no tool calls. That is not efficiency,
it is a revision written from memory.

## Step 6 — Verify the revision, then terminate

**A revision is unreviewed work until something checks it.** Round one is not the end of
the loop; it is the end of the first half. Every revision introduces claims no independent
agent has seen, and the drafter is the party least able to catch its own fabrications.

So: **every revision gets a round-two pass**, scoped to the diff, using the second-round
refuter variant in `references/agent-prompts.md`. The only permitted substitute is
verifying the new claims yourself, by opening the spans the revision cites and re-running
anything it measured. Cheap for two or three new claims; not cheap for a rewritten spec.

Signals that make round two non-negotiable:

- the revision used almost no tool calls
- it introduced numbers, counts, or scores that were not in the original
- it changed a safety plan, a blast radius, or an undo path
- it corrected a citation, which is itself a new claim about the code

**Exit criteria.** Done when a round returns no blocking findings. **Two revision rounds
maximum, then escalate to the human** — a spec that has failed twice is telling you the
ticket has a problem the loop cannot solve.

Track what actually got verified. Shipping four specs whose revisions nobody re-checked,
and believing the loop ran, is worse than knowing you stopped early.

## Step 7 — Post

Append to the issue body under a `---` divider, as an `## Implementation spec` section.

**Never replace the body.** Issue reports carry measured evidence — counts, timings, entity
ids from a live system — that is usually not reproducible later. Overwriting it destroys
the only record.

Work in the session scratchpad, not `/tmp`, and keep the original body as a `.bak` until
the edit is confirmed — it is the only copy of evidence you cannot reproduce.

```bash
gh issue view <N> --json body --jq '.body' > "$WORK/body-<N>.md"
cp "$WORK/body-<N>.md" "$WORK/body-<N>.bak.md"
printf '\n\n---\n\n' >> "$WORK/body-<N>.md"
cat "$SPECS/spec-<N>.md" >> "$WORK/body-<N>.md"
gh issue edit <N> --body-file "$WORK/body-<N>.md"
```

## What this learned the hard way

**A defect in the code is not a gap in the spec.** When review turns up a real bug that
exists independently of the ticket, file it as its own issue and make the spec depend on
it. Folding it in silently widens a ticket that was already sized. The first run found a
batch command that silently re-merged entity pairs a human had explicitly marked
keep-separate; that became its own issue rather than scope creep.

**Scope the second review to the diff.** Round two should attack what the revision
introduced, not re-verify what round one already checked and confirmed.

**Batch the narrow-gap tickets.** They are single questions over shared repo context.
Four separate agents means paying to load that context four times.

**Decode HTML entities before writing agent output to a file.** Agent results arrive with
`&gt;`, `&lt;`, and `&amp;` escaped. Written straight to a file they land in the issue as
literal escapes, which mangles every SQL comparison and generic type in the spec. Check
with `grep -c '&gt;\|&lt;\|&amp;'` before posting.

**Verify the fix before believing the revision.** Grep the final spec for the specific
defects the review named. A revision claiming to have fixed something is not evidence that
it did.

**Agents die mid-run. Assume nothing landed until you have looked.** Two died on connection
errors in one run, one of them a revision. Before resuming or re-sending anything, list the
spec directory and read the mtimes: a file untouched since the original draft did not get
revised, whatever the agent's last message said. This is why the drafter prompt says to
write once at the end — a partial spec is worse than no spec, because it looks finished.

**Say what a measured number was measured against.** A count from a restored snapshot is
not a count from the live system, and the gap is exactly where a stale blast radius hides.
Name the database, say whether it is live or a restore, and date it. In one run two specs
cited counts from a restored copy as if they were current, while a third measured the live
system the same day and found the sets had grown.
