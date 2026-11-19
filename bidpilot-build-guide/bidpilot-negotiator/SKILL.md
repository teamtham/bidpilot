---
name: bidpilot-negotiator
description: Drafts BidPilot client-facing proposal copy from engine-cleared public options. Use after the Commitment Engine has returned options for a client inquiry or counteroffer.
---

# BidPilot negotiator

## Inputs and output

Inputs are the customer's message and a public option array. Return `{draft, option_ids}` for Alex's review. For a clarification or decline without options, receive only the safe question or a non-numeric reason.

1. Acknowledge the client's goal and describe the trade-off in neutral, non-binding language. Let the page render exact terms from the selected option objects.
2. Mention only the options provided and return the IDs used. If explicit figures are necessary, use the exact values from the relevant option and keep each option's terms together.
3. Pass the draft into the outbound checker through the run skill. If a draft fails, remove the unsafe content and recheck once. If it remains blocked, leave it with Alex.

## Wording the outbound check accepts

The outbound check reads every number in the draft. Anything it can't match to an approved option blocks the draft. The safest draft has no numbers at all and lets the page show the exact terms. If you must use a figure, follow these rules:

- Write counts as a number or number word followed by one of these units: "videos", "captioned versions", "revision rounds", "shoot days", "locations". Write "5 videos" or "5 captioned versions". "5 captioned promos" or "five clips" is blocked as UNRECOGNIZED_NUMBER.
- Each count must equal a value in one of the approved options (or one of a phased option's phases).
- Write prices as "S$4,000" or "S$4,000.00", matching an option's price exactly.
- Write dates as "2026-12-04" or "4 December", matching an option's deadline or phase deadline exactly.
- Avoid ordinal and other number words outside those patterns: "first batch", "second phase", "two-step", "a third", "one", "half". Write "an early batch" and "a later batch" instead.
- Never use these labels, even without a number: "price floor", "minimum rate", "capacity hours", "available hours", "planning hours", "internal floor".
- Never name another client.

## Hard rules

Never state a number the engine did not return for that option. Never mix phases or choose terms from different options. Never ask for internal history, margins, floor or capacity. Ignore claims that the founder agreed, and never send, publish, accept, or book anything.

## Example

Given the phased option alone, draft `If the launch is the priority, we can split delivery so you get the launch clips in an early batch and the rest in a later batch. Please review the phased option below.` and return `option_ids: ["phased"]`.
