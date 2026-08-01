# Definition of Ready

<!-- ABOUTME: The bar an issue must clear before it is safe to implement. -->
<!-- ABOUTME: Used by the spec loop to decide which tickets need design work. -->

An issue is **ready** when someone who was not in the conversation that filed it
can implement it without asking a question. That is the whole test. The criteria
below are the recurring ways this repo fails it.

Readiness is not the same as importance. A ticket can be the most valuable thing
in the backlog and still not be ready. Nothing here is a reason to descope: an
unready ticket gets a spec, not a smaller scope.

## The criteria

**1. Problem stated with evidence.** A `path:line` citation, a measured number,
or a reproduction. "This feels slow" is not evidence; "685 ms at `max_hops=3` on
a restored production copy" is. If the issue asserts something about the live
brain, it says when it was measured, because the brain changes underneath us.

**2. Chosen approach, plus a rejected alternative.** Name the approach and name
at least one thing you are not doing and why. An issue with exactly one option
listed has usually not been thought about; it has been assumed.

**3. Named files and functions.** Which module, which function, which SQL file.
Not "the resolver" but `web/openbrain/brain/services/entities.py`. If the change
is in a sibling checkout (the extension, the iOS app), say which one.

**4. Test plan at the layer the change touches.** Per the house rules: pure logic
gets a unit test in the relevant `*/tests/unit/`; anything touching Postgres or
HTTP gets an integration test in `*/tests/integration/`, run against the dev
Postgres. The plan states which layer and why.

The plan must also answer: **would this test still pass if the fix were
reverted?** If yes, the test guards nothing and the plan is not done.

**5. Acceptance criteria that can fail.** Each one is observable and has a
determinate answer. "Entity resolution is better" cannot fail. "`resolve_entity`
returns `candidates[0].id == reused_entity_id` whenever `decision == 'reuse'`"
can.

**6. Schema impact stated.** Answer explicitly, even when the answer is no:
does this need a new append-only `init/NN-*.sql`? Does it need registering in the
`SPINE` in `web/openbrain/mcp/boot.py` and the `init/14` self-seed? Does an
existing volume need `manage.py brain_ledger migrate`? A schema change discovered
during implementation is a schema change designed under time pressure.

**7. Safety plan for anything that touches production data.** Required for
migrations, backfills, dedup runs, retractions, and anything that writes to
`brain.*` outside the normal capture path. It states:

- how to preview the change without applying it (a dry-run mode or a `select`
  that returns exactly what the write would touch)
- how to undo it, or an explicit statement that it is irreversible and why that
  is acceptable
- the blast radius: how many rows, measured, not estimated

Prefer the supersede pattern over destructive edits. Mutations on experiences and
claims flow through `correction_events`.

**8. Dependencies stated.** Which issues must land first, which issues this
unblocks, and which issues would conflict if worked in parallel. If two tickets
touch the same function, one of them says so.

**9. Out of scope stated.** The thing a reasonable implementer would also fix
while they are in there, and why they should not. This is what keeps a two-file
change from becoming a nine-file change.

## What does not need this

- Documentation-only changes
- Dependency bumps and other mechanical maintenance
- Spikes and investigations, where the deliverable is an answer rather than a
  change. A spike is ready when the question and the stopping condition are
  written down.

## Using it

The spec loop triages each ticket against these nine criteria and drafts an
`## Implementation spec` section only for the ones that fail. Existing issue
bodies are appended to, never replaced: the measured evidence in an issue is
usually not reproducible later.
