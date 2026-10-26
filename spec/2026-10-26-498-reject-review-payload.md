---
status: draft
issue: 498
intent: intent/2026-10-26-498-reject-review-payload.md
---

# Spec: reject review sends a valid apply-correction body

## Design

In `propose_reject_review`, the AIFactory branch sends the same shape TFactory's sender does (TFactory `apps/backend/agents/handback/send.py`):

```json
{
  "fix_request_md": "# Review rejected from the cockpit\n\n<reason>\n",
  "source": "cockpit",
  "confirm": true,
  "correlation_key": "<key>",
  "reason": "<reason>",
  "triage": {
    "source": "triage",
    "failing_tests": [{"test_id": "cockpit-review", "reason": "<reason>"}],
    "correlation_key": "<key>"
  }
}
```

- `triage.source` is `"triage"`: the published schema (`handback_triage.schema.json`) enumerates `triage | visual_inspection`. A `"cockpit"` value would pass AIFactory's runtime validator (unknown sources are soft) but fail the published schema.
- A human reject has no failing test, and the validator needs "something to act on". So the reviewer's note goes in as one failing item with `test_id: "cockpit-review"`.
- Top-level `source: "cockpit"` is a free string and is kept for AIFactory's audit log.
- `reason` is kept: Pydantic ignores extra keys, and the cockpit's propose response shows it.
- `confirm: true`: the cockpit's own confirm dialog is the human confirmation. Without it AIFactory only previews.

The plan-stage PFactory branch is unchanged.

## Alternatives rejected

- `triage.source: "cockpit"`: AIFactory's runtime validator accepts it, but the published schema does not.
- `has_visual_plan: true` with no failing tests: this would claim a visual plan that does not exist.
- Turning off `APP_REQUIRE_CORRECTION_TRIAGE`: this weakens the AIFactory gate for every sender.

## Risks

If AIFactory narrows the triage contract, the pinned schema copy goes stale. The test names the pinned AIFactory SHA so the copy can be re-vendored.

## Verification

A contract test validates the payload against a byte-identical pinned copy of AIFactory's `handback_triage.schema.json` and against the required fields of `ApplyCorrectionRequest`. The existing tests in `tests/test_actions.py` are updated.
