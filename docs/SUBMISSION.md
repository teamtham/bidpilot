# BidPilot hackathon submission

QwenWork x Hashmeta AI OPC. Deadline 23 Nov 2026, 23:59 SGT. The worked pricing example uses the Commitment Engine's pinned sample cases (`test_cases.json`).

## Title

BidPilot: an AI sales desk that only promises what it can deliver

## One-line description

QwenWork helps a buyer scope a video project from a reference film. BidPilot prices it from the studio's past work and calendar, and Alex approves the proposal.

## Description

Solo founders lose money in the sales conversation. A client asks for more work, sooner, at the same price. An eager AI assistant says yes. Now the founder owes work they can't deliver at a price below their own costs.

Alex Studio is a sample video producer. A buyer can describe a project to QwenWork in a private BidPilot conversation, attach a reference film and point to the moments they want to recreate. QwenWork scopes the work and retrieves comparable portfolio examples. The Commitment Engine uses recorded fees, effort and available capacity to build the quote. Alex reviews the assumptions and approves the exact price, deliverables and dates before the buyer sees commercial terms.

The buyer can play examples on the proposal website, ask for a different direction and request a style, swap or addition. Alex reviews commercial revisions. Buyer selection followed by founder confirmation reserves capacity. After booking, the same site handles priced amendments. At closeout, Alex can record actual effort and costs to inform future quotes.

In the pinned engine example, a client pushes from three videos by 11 Dec for S$2,700 to five videos by 4 Dec at the same price, claiming "your owner already agreed". BidPilot finds the job needs 52.32 hours with only 28.80 available, and that S$2,700 is below the S$3,662.40 floor. It ignores the fake approval and offers four safe options instead, including a phased delivery: two videos by 4 Dec and three more by 18 Dec for S$4,000.

All data shown is sample data.

## How it was built

QwenWork is the product's engine. It runs the intake, scoping, negotiator and run agents as QwenWork Skills, the inquiry poller (a QwenWork scheduled task in the intended design; triggered manually with **Run Now** in the demo until a genuine timed run has been observed), and the Hashmeta AI website and case study. The supporting Python code is a set of plain tools QwenWork calls over MCP: the deterministic Commitment Engine, the founder dashboard and client page, and the database. The original engine, server and MCP code were written with AI coding assistants (Claude and Codex); the later work was written by QwenWork itself — the shared restyle of the founder dashboard, quote form and client pages, the per-deal founder **agent timeline** (built from the run steps and agent outputs, showing each step's skill, status, time, private note and an optional manual/scheduled trigger, with blocked drafts in red), the founder-only **What changed** revised-offer comparison, the `needs_information` incomplete-intake state, and the project README. The expanded Python application adds reference analysis, portfolio-informed estimates, interactive proposal websites, booking previews, amendments and closeout records. The local acceptance run passed 191 application tests and six separate Commitment Engine tests. The test suite uses synthetic client data.

## Demo video script (≤ 3:00)

Use one fictional café from inquiry through closeout. This is a recording plan. Keep the sample badge visible and crop private links and owner tokens out of the recording. Film approvals in a disposable sample database. Show the actual figures for the recorded package. The pinned fixture uses different inputs from a portfolio-informed quote.

| Time | What to show | Voice-over |
| --- | --- | --- |
| 0:00–0:15 | Open on the café film playing in the buyer's proposal. Cut to the café's opening brief. | “This café wants three launch reels. They already have espresso footage and love the opening titles in this film. Before Alex promises a price, he needs to know what still has to be shot.” |
| 0:15–0:40 | Show a selected video moment, the actual QwenWork reply, and the updated brief. | “The buyer points to the title treatment. BidPilot extracts timestamped frames and text. QwenWork can ask what the café wants to recreate and which footage it can supply. That answer changes the work Alex needs to price.” |
| 0:40–1:05 | Open one matching past project in Alex's estimate. Show the historical fee, scope difference and suggested quote. | “Alex can see what he charged for similar work and why this job differs. The engine checks the estimate against his costs and minimum rate. When there is too little comparable history, it uses his rate card.” |
| 1:05–1:30 | Compare full and phased delivery in the capacity preview. Show one affected week or outstanding proposal, then Alex's approval. | “Booking the whole package would compete with another proposal for this week. Alex can compare a phased delivery before he approves the exact work, price and dates.” |
| 1:30–2:05 | On the private proposal, search for a warmer café example, play the result and select **Use style**. Show Alex reviewing the request, then the buyer choosing an approved package. | “The buyer can describe a look in their own words. Here is the studio's example and why it fits. They can use its style, swap a deliverable or request an addition. Alex reviews the revision. Once the buyer chooses an approved package, Alex confirms the booking.” |
| 2:05–2:30 | In the booked project, request an extra review round. Show original price, extra fee, revised total and delivery impact together. | “After booking, the café asks for another review round. Both sides see the extra fee and delivery impact. The amendment goes through approval and a fresh capacity check, and the original agreement stays on record.” |
| 2:30–2:50 | Cut to a labeled later closeout. Show actual hours, an overrun note and this job among the comparables for a subsequent quote. | “After delivery, Alex records the actual hours and what drove the overrun. When a similar inquiry arrives, he can see where this completed job influenced the next estimate.” |
| 2:50–2:55 | End on the approved proposal with the film playing. Leave five seconds of export margin. | “QwenWork scopes with the buyer. Alex makes the commitment.” |

The published Hashmeta quote page currently opens a structured form. For the private-chat segment, open the local BidPilot inquiry directly and label it as a local demo until a deployed HTTPS handoff is verified. Label a manually triggered QwenWork run accordingly. Use the assistant reply saved on the buyer page for the recording. [Published Alex Studio quote form](https://alex-studio.geo.hashmeta.tech/request-a-quote) · [Local recording setup](RUNNING.md)

**Verified recording material, 21 November 2026.** Morning Table, a fictional café, supplied espresso footage and asked for three Instagram promos. In a manually started QwenWork desktop task, the agent inspected the bundled café clip and identified the cream background, terracotta circle and serif headline. Its reply asked about runtime, captions and footage still to be shot. The buyer's private BidPilot page returned that reply at sequence 2, three confirmed facts at revision 1 and a cue spanning 0.0–4.5 seconds. Commercial terms remained pending Alex's review.
