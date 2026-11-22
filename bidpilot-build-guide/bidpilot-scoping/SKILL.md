---
name: bidpilot-scoping
description: Profiles BidPilot video work and proposes comparable past projects. Use after structured intake when an inquiry or counteroffer needs effort evidence.
---

# BidPilot scoping

## Mandatory per-turn workflow

Read the companion `QUESTION_BANK.md` from this actual loaded skill's base directory EVERY buyer turn (v1.0.0, identical to intake's bank). Missing companion blocks the turn. Read brief/form, current revision, confirmed/superseded facts, reference evidence, prior answers and inherited offer before selecting one or two highest-impact unresolved buyer requirements. Use bidpilot-runtime to save sourced answers/corrections and reuse resolved facts. Separate original edits from cutdowns, clip selection from raw footage review, and first-cut from final-file dates. Preserve descriptive requirements in private scope evidence; unsupported template requirements need Alex. Missing labor, rates, expenses, crew and feasibility go privately to Alex/catalog, not buyer labor questions. Shared shoots/prep appear once; every promised finished deliverable needs its applicable editing/captions/review/delivery scheduled. Never invent producer assumptions.

## Inputs and output

Inputs are a complete offer, intake evidence and founder-approved history. Return `scope_profile` with roughly ten work drivers, `comparable_ids` and `differences` (project ID, fields, why, and brief evidence). Each driver has `field`, `value` and `evidence`.

1. Combine the brief and intake evidence for format, production mode and shoots, locations, interviewees, scripting, deliverables, editing, graphics, revisions and captions.
2. Select candidates with the same format, mode and editing tier. Explicitly compare location count, revisions, motion, captions and scripting with the clean historical baseline.
3. Pass only plausible IDs to the engine by default. Show rejected near matches in the private run log. Include their IDs in a diagnostic run only if Alex explicitly asks to see the engine's rejection codes.
4. If none match, send an empty list. For new detailed quotes, missing effort requires Alex or a dated approved template; fallback hours belong only to explicit pinned historical replay. Where a scope field is ambiguous, return it to intake.

For a portfolio-informed quote, retrieve cleared examples through `search_portfolio` and use the project IDs to join them to `get_scoping_history` privately. Explain which creative or production characteristics match and which differences change the work. Cite the observed evidence for a cut, camera setup or graphic treatment. Ask about effort drivers that the image cannot establish, including asset supply, rights and review rounds. Treat a missing fee, time or expense as unknown.

Generate alternatives with `propose_scope_alternatives`: it detects structural opportunities (quantity masters convertible to derivatives, shoot setups sharing location and date, phaseable deliverables, shoot items replaceable by supplied footage). The producer confirms which variables flex and supplies effort values with dated bases; the tool rebuilds consistent task sets and rebalances schedule coverage mechanically. Pass selected variants to pricing as `detailed_alternatives`; rejected candidates carry reasons for the private log.

Inspect cleared examples with `get_portfolio_frame(asset_id, seconds)` at representative moments (use zero for a still). Record the asset, timestamp, observed visual cues and uncertainty in the private scoping output via `save_agent_output`. A frame can suggest a setup or graphic treatment but cannot establish unseen production methods, costs or rights. Keep project identities and fee information private.

For a detailed package, distinguish shared prep and shooting from master edits and incremental derivatives. Scope dependencies, roles, dated availability and uncertain quantities. Use approved catalog rates or an explicitly sourced producer estimate. Pass the resulting spec to the estimator and read back the price, constraints and feasible alternatives. The proposed website should state the buyer's objective, describe the concept, feature a playable example when a cleared match exists and explain why the recommended package fits.

On decline, consult linked pricing_history and the original brief before selecting a trade-off. Ask once if buyer flexibility is unknown; record an answer with its source. Preserve a separate-stories requirement and original requested counts in history. Do not propose deadline-only mitigation for an unchanged price shortfall. Reprice the complete permitted scope with approved bases; unsupported changes go to Alex.

## Hard rules

Never infer effort, round prices, alter the brief, or hand raw history or other client names to the negotiator. Only the engine can accept or reject comparable IDs for pricing.

## Example

For a standard cafe promo with one shoot day, short scripting and basic titles, return P-031, P-036, P-041 and P-044. Record P-043's extra revisions and caption workload and P-047's extra shoot, locations and motion as material differences.
