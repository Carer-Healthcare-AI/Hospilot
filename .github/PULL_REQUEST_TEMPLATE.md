## What does this change?

<!-- One or two sentences. What's different after this PR, and why. -->

## Which service(s)?

- [ ] `agentic-framework`
- [ ] `fabric`
- [ ] docs / README only

## Testing

<!-- What did you run to confirm this works? `python -m pytest` output, a manual repro
     steps, etc. A PR with no testing notes is harder to review, not faster. -->

## Live flow run

<!-- REQUIRED if you touched agentic-framework/agents/**, workflows/**, or the flow
     catalog. Run the end-to-end flows and paste the receipt they print:

         cd agentic-framework && pytest tests/flows -m live

     CI recomputes the fingerprint against your branch, so a receipt from before
     your latest change will be rejected as stale. If you cannot run the stack,
     say so here and ask a maintainer to run it for you. -->

_Not applicable — this PR does not touch agents, workflows or the flow catalog._

## Checklist

- [ ] Tests pass locally (`python -m pytest` in the service(s) you touched)
- [ ] New behavior has test coverage
- [ ] Scoped to one change — no unrelated reformatting/renaming mixed in
