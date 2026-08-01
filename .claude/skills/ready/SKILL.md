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

Full prompt template: `references/agent-prompts.md`.

## Step 4 — Refute

A second agent, **different model, fresh context**, whose job is to refute the spec rather
than improve it. Same-context review rubber-stamps reasoning it just produced.

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

## Step 6 — Terminate

**Two revision rounds, then escalate to the human.** Not "loop until clean" — a spec that
has failed twice is telling you the ticket has a problem the loop cannot solve.

## Step 7 — Post

Append to the issue body under a `---` divider, as an `## Implementation spec` section.

**Never replace the body.** Issue reports carry measured evidence — counts, timings, entity
ids from a live system — that is usually not reproducible later. Overwriting it destroys
the only record.

```bash
gh issue view <N> --json body --jq '.body' > /tmp/body.md
printf '\n\n---\n\n' >> /tmp/body.md
cat spec.md >> /tmp/body.md
gh issue edit <N> --body-file /tmp/body.md
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

## Cost

The first full run cost roughly 570k subagent tokens on one design-heavy ticket across two
revision rounds. Budget accordingly, and prefer the narrow path whenever triage allows it.
Most of the second round existed only because the revision skipped re-grounding, so step 5
is where the savings are.
