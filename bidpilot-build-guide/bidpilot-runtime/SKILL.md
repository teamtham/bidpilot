---
name: bidpilot-runtime
description: Maps the BidPilot skills' "Page" actions to the bidpilot MCP tools. Apply together with bidpilot-run whenever a BidPilot inquiry or client change is processed.
---

# BidPilot runtime mapping

The "Page" in the BidPilot skills is the local BidPilot service. Use only these bidpilot MCP tools:

| Skill action | MCP tool |
| --- | --- |
| Find new inquiries and client changes | list_pending_work (returns inquiries[], change_requests[] and unevaluated_offers[]) |
| Claim or resume by (source, source_id) | claim_intake: source is "site", "hashmeta" or "manual"; requires client, raw_inquiry, offer_request {as_of, offer, comparable_ids}; optional evidence and request_id |
| Read a deal, its current offer and versions | get_deal |
| Founder-approved history for scoping (private) | get_scoping_history |
| Save intake / scoping / negotiator / clarification output | save_agent_output with kind "intake" {offer, evidence, missing_fields, clarification}, "scoping" {scope_profile[{field,value,evidence}], comparable_ids[], differences[{project_id,fields,why,evidence}]}, "negotiator" {draft, option_ids[]}, "clarification" {questions[]} |
| Append a run step | record_step: step_key is one of claimed, intake, scoping, engine, draft, outbound, review, needs_information, stale_version, blocked, error; status is one of running, ok, blocked, error, skipped; optional trigger is "manual" or "scheduled" (omit when unknown — it is backward compatible and the founder timeline then shows "trigger not recorded") |
| Run the Commitment Engine | evaluate_offer(deal_id, offer_id). It returns evaluation_id, verdict, reason_codes, estimate (PRIVATE), options and draft_version |
| Outward-safe material for the negotiator | negotiation_payload(evaluation_id). Give the negotiator only this and the client's message, never the estimate or history |
| Save a draft (each save makes a new version) | save_draft(version_id, draft_body, request_id) |
| Outbound gate | check_draft(version_id) |
| Private run log | run_trace(deal_id) |
| Client change that is queued for clarification | submit_change_offer(deal_id, request_id, version_id, offer_request, evidence {raw_message}) |

Order for a new inquiry:
1. Run bidpilot-intake on the form fields and raw message. Use as_of 2026-11-23. The demo configuration sets direct_costs_sgd to 0 explicitly. If a required field is missing, there is no deal yet (claim_intake rejects incomplete offers). Report the clarification questions to Alex in this task and stop that item.
2. Call get_scoping_history and run bidpilot-scoping to choose comparable_ids.
3. claim_intake with offer_request = {as_of, offer, comparable_ids}. A duplicate (source, source_id) resumes the same deal. Then record_step claimed, intake and scoping as ok, and save_agent_output for intake and scoping.
4. evaluate_offer, then record_step engine. For a clarify or decline verdict, save the reason for Alex and stop at review.
5. negotiation_payload, then run bidpilot-negotiator, save_agent_output negotiator, save_draft on the returned draft_version.version_id, then check_draft. If blocked, record_step outbound blocked, remove the unsafe text, save_draft again and check once more. If it is still blocked, leave it for Alex.
6. record_step draft, outbound and review. Stop. Tell Alex the deal is waiting for review in the founder dashboard.

Client changes:
- From change_requests[] in list_pending_work: run bidpilot-intake against the previous offer (from get_deal), then submit_change_offer with the full new offer_request (same as_of and comparable_ids) and the version_id given. If it returns stale_version, record it and stop. Then continue from step 4.
- From unevaluated_offers[] in list_pending_work (changes the client page already turned into a structured offer): check the offer's terms against the client's message (body) with bidpilot-intake. If they don't match, record_step blocked with a private note for Alex and stop. Otherwise continue from step 4 with that deal_id and offer_id.
