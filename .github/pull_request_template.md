## What

<!-- The change, in a sentence or two. -->

## Why

<!-- The problem it solves. If you rejected an alternative, say which and why — that is
     often the most useful part of a PR. -->

## How it was verified

<!-- `uv run pytest` output, plus anything you checked by hand. If the change touches parsing
     or extraction, say what real data you ran it against — counts, not content. -->

## Checklist

- [ ] `uv run pytest` passes
- [ ] Tests cover the failure case, not only the success case
- [ ] No real transcript content in fixtures, tests, or this description
- [ ] No new network calls
- [ ] Docs updated if behaviour or the data model changed
