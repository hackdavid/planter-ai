# Pipeline Orchestration Design

## Why Target-Driven Instead of Bulk Processing

The Planter Prospecting Engine is architected to support **both** bulk and targeted workflows, but the current prototype and API are built around a **target-driven, single-request pipeline**. This document explains that decision so future developers, clients, or AI assistants understand the rationale before suggesting changes.

---

## What We Built

The orchestrator accepts three user-driven parameters:

| Parameter | Purpose | Example |
|---|---|---|
| `target` | Exact number of usable venues needed | `3` |
| `search` | Size of the candidate pool Phase 1 should discover | `20` |
| `retries` | How many times to re-run with expanded parameters if target is not met | `3` |

The pipeline then:

1. Discovers `search` candidates
2. Acquires images for the top half of that pool
3. Runs Vision QA **sequentially** and **stops immediately** once `target` venues pass
4. If short after the first attempt, it retries with a wider grid, broader categories, or relaxed thresholds
5. Returns exactly `target` venues (or as many as it could find, with an honest `status`)

---

## What We Did NOT Build (Yet)

### Bulk Processing

The system *could* be configured to process 5,000 venues per week in a single batch. That mode would look like:

```
POST /api/bulk/run
{
  "city": "London, UK",
  "quantity": 5000,
  "max_venues_for_images": 500
}
```

**Why we did not expose this in the prototype:**

| Concern | Target-Driven API | Bulk Batch |
|---|---|---|
| **HTTP timeout** | Completes in 30–90 seconds | Runs for 30–60 minutes |
| **API cost control** | Stops early, spends ~$0.20 | Burns through quota blindly, spends ~$50–$80 |
| **Failure handling** | One retry loop, clear status | Needs partial-failure reporting, checkpointing, resume |
| **Infrastructure** | Single FastAPI worker | Requires background workers, job queue, state persistence |
| **Client experience** | Request → immediate response | Request → job ID → poll for status → download results later |

A bulk workflow is fundamentally a **long-running background job**, not a synchronous REST API call. Building it properly requires:

- A task queue (Celery, RQ, or AWS SQS)
- A worker pool to process venues in parallel
- A database table tracking job state (`queued`, `running`, `partial`, `completed`, `failed`)
- A polling or webhook mechanism to notify the client when results are ready
- Idempotency keys so retrying the same bulk job does not double-bill API calls

That is a large engineering lift. It is the right architecture for production scale, but it is overkill for a prototype, a client demo, or a sales pitch where the goal is to show 3 composed images in under a minute.

---

## Design Principle: Cost-First, Scale-Second

Every API call in this pipeline costs money:

| Service | Cost per call | Weekly cost at 5,000 venues |
|---|---|---|
| Google Places Nearby Search | ~$0.017 | ~$85 |
| Street View Static API | ~$0.007 per image | ~$70 |
| Street View Metadata API | Free | $0 |
| Gemini Vision QA | ~$0.005 per image | ~$50 |
| Place Details (fallback) | ~$0.017 | ~$17 |

**Total: ~$222/week** for a naive bulk run that processes every candidate.

The target-driven pipeline **cuts this by 90%** for demo use:

- `target=3`, `search=20`, `retries=3`
- Best case: ~$0.12
- Worst case: ~$0.40
- Demo mode (reading dev cache): **$0.00**

This cost-first approach means:
- We can run the demo 100 times during development without worrying about billing
- The client can self-serve the demo without us monitoring their API spend
- When they are ready to scale, the bulk queue system is an additive layer on top of the same services

---

## How Bulk Would Be Added Later

When the business need justifies it, the bulk workflow would reuse the exact same services but wrap them in a queue:

```
FastAPI API layer (unchanged)
  ↓
Job Queue (new: Celery / RQ / SQS)
  ↓
Worker pool (new: 4–8 workers)
  ↓
Same Phase 1 service
Same Phase 2 service
Same Vision QA service
Same Fallback service
  ↓
Results stored in S3 / database
Webhook or polling endpoint notifies client
```

The target-driven API does not block this. It is the **foundational layer**. The queue system is the **scaling layer**.

---

## Demo Mode vs Production Mode

| | Demo Mode | Production Mode (target-driven) |
|---|---|---|
| **Venue discovery** | Reads from dev SQLite cache | Live Places API calls |
| **Image acquisition** | Reads from dev disk cache | Live Street View / Fallback API calls |
| **Vision QA** | Reads from dev QA cache | Live Gemini calls |
| **Plant compositing** | Runs at runtime on cached frontage images | Runs at runtime on fresh frontage images |
| **Latency** | < 2 seconds | 30–90 seconds |
| **Cost per run** | $0.00 | ~$0.12–$0.40 |

The demo mode exists because during development we ran the full pipeline dozens of times. Every run wrote real candidates, real images, and real QA verdicts to local disk. Demo mode simply reads that same cache. No API keys are touched. The only live work is the compositing algorithm.

---

## Retry Strategy

If `target` is not met on the first attempt, the orchestrator retries up to `retries` times:

| Attempt | Change | Rationale |
|---|---|---|
| 1 | `search = user_input` | Baseline |
| 2 | `search × 1.5`, wider grid radius | Wider net in same area |
| 3 | `search × 2`, broader categories, relaxed thresholds | Max effort before giving up |

If all retries fail, the orchestrator returns an honest `status: "failed"` or `"partial"` with whatever it found. It does not invent candidates or return low-quality images just to hit a number.

---

## For Future Developers / AI Assistants

If you are reading this because a client asked to "just process 10,000 venues at once," the answer is:

> The underlying services support it. The orchestrator does not expose it yet because that requires a job queue and worker infrastructure. The target-driven API is the correct first step. Bulk mode is a Phase 4 project.

If the client insists on bulk before the queue system is built, the **unsafe shortcut** is to run multiple target-driven requests in a loop and aggregate the results client-side. This is not recommended because:
- It wastes API calls on duplicate candidates across requests
- It has no deduplication logic
- It can hit rate limits or billing caps quickly

The **safe path** is to build the queue layer first.

---

## Summary

| Question | Answer |
|---|---|
| Can this process 5,000 venues/week? | **Yes, architecturally.** Not in the current API. |
| Why is the API target-driven? | **Cost control and immediate feedback.** |
| When do we build bulk? | **After the client validates the demo and commits to a subscription.** |
| What changes for bulk? | **Add a queue and workers. The services stay the same.** |
| Is demo mode real data? | **Yes.** It is cached from actual development runs, not synthetic placeholders. |
