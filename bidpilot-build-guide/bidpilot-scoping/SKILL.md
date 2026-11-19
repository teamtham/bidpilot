---
name: bidpilot-scoping
description: Profiles BidPilot video work and proposes comparable past projects. Use after structured intake when an inquiry or counteroffer needs effort evidence.
---

# BidPilot scoping

## Inputs and output

Inputs are a complete offer, intake evidence and founder-approved history. Return `scope_profile` with roughly ten work drivers, `comparable_ids` and `differences` (project ID, fields, why, and brief evidence). Each driver has `field`, `value` and `evidence`.

1. Combine the brief and intake evidence for format, production mode and shoots, locations, interviewees, scripting, deliverables, editing, graphics, revisions and captions.
2. Select candidates with the same format, mode and editing tier. Explicitly compare location count, revisions, motion, captions and scripting with the clean historical baseline.
3. Pass only plausible IDs to the engine by default. Show rejected near matches in the private run log. Include their IDs in a diagnostic run only if Alex explicitly asks to see the engine's rejection codes.
4. If none match, send an empty list. The engine owns fallback hours and confidence. Where a scope field is ambiguous, return it to intake.

## Hard rules

Never infer effort, round prices, alter the brief, or hand raw history or other client names to the negotiator. Only the engine can accept or reject comparable IDs for pricing.

## Example

For a standard cafe promo with one shoot day, short scripting and basic titles, return P-031, P-036, P-041 and P-044. Record P-043's extra revisions and caption workload and P-047's extra shoot, locations and motion as material differences.
