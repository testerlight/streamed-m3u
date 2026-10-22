# Automatic source switching under safeguards

Status: ready-for-agent
Blocked by: 03

Spec: ../spec.md. ADR 0001. Gotcha #32 (picker, `_check_source`, pins).

Implement the agreed trigger and safeguards, reusing `_check_source` and the pin/primed-payload mechanism.

## Acceptance

- Off switch, default off until the user enables it.
- Rate limit, no straight-back switch, stays put if nothing passes; every decision logged and in stream history.
- Tests with scripted flapping sources show no hopping.
