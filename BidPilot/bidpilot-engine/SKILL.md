---
name: bidpilot-engine
description: Runs the deterministic BidPilot Python evaluator and outbound checker. Use to price or evaluate a structured offer, validate counteroffers, or check a draft against approved options.
---

# BidPilot Commitment Engine

## Inputs and output

Evaluation consumes `{"as_of":"YYYY-MM-DD","offer":{...},"comparable_ids":[]}` and the five seed tables. Outbound checking consumes `draft`, `approved_options`, `private_values` and `other_client_names`. It returns `passed` and `violations`. Send only the allowed options to the negotiator and retain the full result for Alex.

1. Locate this skill's `engine.py` and `data/`. In the QwenWork sandbox run `python3 -B -m unittest -v test_engine.py` from this skill folder once after installing. Use the installed `python` executable if `python3` is absent.
2. Save the evaluated request as UTF-8 JSON and run `python3 -B engine.py offer.json`. Use stdin if the runner supports it.
3. On exit code zero, validate the output shape against `evaluation-output.schema.json`. Record the result verbatim. On nonzero exit, missing script, or malformed output, write a blocked step and stop this offer.
4. Run `python3 -B engine.py --outbound draft-check.json` for the exact text Alex will approve. Any edit invalidates the check and approval for the prior text. Keep checker output private.

## Hard rules

Never estimate via model arithmetic, modify fixtures to make a test pass, forward internal metrics to the negotiator, or tell the client a verdict directly. If Python cannot run in this session, follow the tested deterministic fallback in the build guide.

## Example

With `as_of` 23 Nov 2026 and the supplied fixture `inquiry_1`, return `accept`, `planning_hours: 35.07`, and option `as_offered` at S$2,700. Only the option can be offered externally.
