<!-- Architecture diagram → put it in planning.md. Include it under an ## Architecture section.  -->
<!-- detection pipeline must use at least 2 distinct signals to classify content. -->
<!-- Your system must return a confidence score, not just a binary label. The score should reflect genuine uncertainty — a 0.51 confidence should produce a meaningfully different transparency label than a 0.95. -->
 <!-- Implement a mechanism for creators to contest a classification. At minimum, an appeal must: capture the creator's reasoning, log the appeal alongside the original decision, and update the content's status to "under review." Automated re-classification is not required. -->
 <!-- Implement rate limiting on your submission endpoint.  -->
  <!-- Audit log - including confidence score, signals used, and any appeals — must be captured in a structured audit log. Document the log in your README (or via the GET /log output) with at least 3 entries visible. -->

## Description
This project is Provenance Guard, a backend system that any platform where people share original creative work — writing, music, art, etc. - could plug into to classify submitted content on a scale of likely human to likely AI. The system scores confidence in that classification and produces a user-level transparency label on the UI. This system also handle appeals from creators who believe they've been misclassified.

## Detection signals
| # | Signal | Description | Score Range |
|---|--------|-------------|-------------|
| 1 | Grammar/Punctuation | A text with perfect grammar, especially frequently using punctuation such as hyphens and semi-colons, which are not commonly mastered or used in casual writing. | 0 – 1 |
| 2 | Buzzword Overuse | The usage of upper echelon vocabulary which leans almost corporate or unnecessarily formal for the context. | 0 – 1 |
| 3 | LLM Classification (Groq) | An LLM-based signal that asks a model to assess whether the text reads as human- or AI-generated. Captures semantic and stylistic coherence holistically, beyond the surface-level heuristics of signals 1 and 2. | 0 – 1 |

## Uncertainty Representation
Raw signal outputs will be mapped to a confidence score where low combined signal represents likely human writing and higher confidence represents likely AI writing. Confidence scores will be judged in threshold of thirds so, 0 - 0.33 / 0.34 - 0.66 / 0.67 - 1.

The three signals are combined into a single confidence score using a weighted average where the LLM-based signal is weighted heavier because it assesses the text holistically, whereas other the two stylometric heuristic signals act as supporting evidence:

| Signal | Weight |
|--------|--------|
| Grammar/Punctuation | 0.25 |
| Buzzword Overuse | 0.25 |
| LLM Classification | 0.50 |

```
confidence = (0.25 × grammar) + (0.25 × buzzword) + (0.50 × llm)
```

In terms of interpreting uncertainty, a confidence of 0.6 would, for example, mean the weighted sum of evidence falls in a middle ground of uncertainty of either AI or human work, but leans toward AI indecisively. If the Groq signal is unavailable (API failure/timeout), the score falls back to a standard average of the two heuristic signals (each weighted 0.5).

## Transparency Label Design
On the UI the readable labels will be as follows:

| Label | Display Text | Confidence Score Range |
|-------|--------------|------------------------|
| likely human | This has passed as likely human generated. | 0 – 0.33 |
| uncertain | The system is uncertain if this is human or AI work.| 0.34 – 0.66 |
| likely AI | The system has deemed this work likely written by AI.| 0.67 – 1 |

## Architecture
### Workflows
**Submission workflow:** POST /submit → signal 1 → signal 2 → signal 3 (Groq LLM) → confidence scoring → transparency label → audit log → response
* The audit log will be updated with any new submission by including the confidence score and signals used.

**Appeal workflow:** POST /appeal → status update → audit log → response
* Only users who receive writing classification of "likely AI" or "uncertain" can appeal. An appeal is provided by the user in the form of reasoning text justifying their appealed classification.
* When appealed, the content's existing audit entry is updated in place: the appeal reasoning is recorded in `appeal_reason` and its status is updated to "under review". No new entry is created and automatic reclassification is not conducted.

### Rate Limiting
The `POST /submit` endpoint is rate limited to prevent abuse and to control cost on LLM call. The limit is enforced per client IP.

| Endpoint | Limit | Reasoning |
|----------|-------|-----------|
| POST /submit | 5 requests / minute per IP | Generous enough for legitimate single-user submission flows, but low enough combat intentional abuse. Also, to reduce speed at which Groq requests quota is reached.|

Requests exceeding the limit receive an HTTP `429 Too Many Requests` response.

### Audit Log
Every submission produces one structured audit entry. An appeal does not create a new entry — it updates the content's existing entry in place. Each entry follows the schema below:

```json
{
  "content_id": "uuid",
  "creator_id": "id of the submitting creator",
  "timestamp": "ISO-8601 datetime",
  "signals": {
    "grammar": 0.0,
    "buzzword": 0.0,
    "llm": 0.0
  },
  "confidence": 0.0,
  "attribution": "likely Human | uncertain | likely AI",
  "status": "classified | under review",
  "appeal_reason": "creator's reasoning text (null unless appealed)"
}
```

On an **appeal**, no new entry is created and no re-analysis is run (no automatic reclassification). Instead the content's existing audit entry is updated in place: `status` is set to `"under review"` and `appeal_reason` records the creator's reasoning. The original `signals`, `confidence`, and `attribution` are preserved for reviewer context.

The log is retrievable via `GET /log`.

## Anticipated Edge Cases
The system may struggle on formal writing, or even poetry, especially when it comes to the signal of "Buzzword Overuse". Text where this less colloquial tone is acceptable, i.e. professional emails, etc., may experience false positives as AI-generated. Similarly, poetry can at times use large/ rare words for stylistic effect and this could as read as AI-generated to our system.

## AI Tool Plan
**M3** - I will call Claude's attention to my detection signals section and architecture diagrams. I will ask it to generate a Flask app skeleton and the first signal function. I will verify this output by testing with a few inputs directly before wiring into the endpoint.

**M4** - I will call Claude's attention to detection signals section, the uncertainty representation section, and again the architecture diagram within my `planning.md`. I will ask for the generation of the second signal function (Buzzword Overuse), the third signal function (the Groq LLM classification call), and the scoring logic that combines all three signals into a single confidence score. To check LLM output, I will run some tests and validate that scores vary meaningfully between clearly AI and clearly human text, and that the system degrades gracefully if the Groq API call fails or times out.

**M5** - I will call Claude's attention to my label variants, appeals workflow, and architecture diagram from `planning.md`. I'll ask it to generate my label generation logic and the appeal endpoint. This will be a POST /appeal endpoint that accepts a `content_id` and `creator_reasoning`. To verify generation, I will test all three label variants are reachable and that an appeal updates status correctly to "under review".