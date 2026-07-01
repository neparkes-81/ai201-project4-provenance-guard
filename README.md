# Provenance Guard

A backend system that any platform where people share original creative work could plug into to classify submitted content on a scale of likely human to likely AI. It scores confidence in that classification, produces a user-level transparency label, and handles appeals from creators who believe they were misclassified.

Endpoints:
- `POST /submit` takes `text` and `creator_id`, returns the attribution, confidence, label text, and signals.
- `POST /appeal` takes `content_id` and `creator_reasoning`, moves the content to "under review".
- `GET /log` returns the audit log.

## Architecture overview

A submission follows the workflow from `planning.md`:

```
POST /submit -> signal 1 -> signal 2 -> signal 3 (Groq LLM) -> confidence scoring -> transparency label -> audit log -> response
```

The text runs through all three detection signals. Their outputs get combined into one confidence score, the score maps to a transparency label, and the whole decision is written to the audit log before the response goes back to the caller. Every submission produces one audit entry. An appeal does not create a new entry, it updates the existing one in place.

## Detection signals

The pipeline uses three signals, each scored 0 to 1 where higher means more likely AI-generated.

| # | Signal | What it measures | Why I chose it | What it misses |
|---|--------|------------------|----------------|----------------|
| 1 | Grammar/Punctuation | Idealistic grammar that is uncommon in casual writing, like semicolons, em-dashes, and consistent capitalization | Casual human writing rarely masters this punctuation, so heavy use is a frequent AI tell | Formal human writing where such writing is anticipated scores higher than it should |
| 2 | Buzzword Overuse | Density of corporate or unnecessarily formal vocabulary relative to length | LLMs lean on this vocabulary far more than most people do | Corporate or academic register may be flagged as AI|
| 3 | LLM Classification (Groq) | A holistic read of whether the text sounds human or AI, done by a model | It captures semantic and stylistic coherence that the surface heuristics cannot | It is the slowest signal and depends on an external API that can fail |

Signals 1 and 2 are stylometric heuristics that act as supporting evidence. Signal 3 does the holistic judgement.

## Confidence scoring

The three signals combine into a single score using a weighted average. The LLM signal is weighted heavier because it assesses the text holistically, while the two heuristics act as supporting evidence.

```
confidence = (0.25 x grammar) + (0.25 x buzzword) + (0.50 x llm)
```

If the Groq signal is unavailable because of an API failure or timeout, the score falls back to a standard average of the two heuristics, each weighted 0.5. This is how the system degrades gracefully instead of erroring.

I validated that the scores are meaningful by running clearly AI and clearly human text through the pipeline and checking that they land far apart. See the following examples as a demonstration:

| Submission | grammar | buzzword | llm | confidence | label |
|------------|---------|----------|-----|------------|-------|
| Corporate LLM text ("In today's ever-evolving landscape, leveraging robust scalable frameworks...") | 0.35 | 1.0 | 0.9 | **0.7875** | likely AI |
| Casual note ("idk man i just threw it together last night lol...") | 0.0 | 0.0 | 0.2 | **0.1** | likely human |

The gap between 0.7875 and 0.1 is the point. A 0.79 lands in the likely AI band and a 0.1 lands in likely human, so the score drives a genuinely different label instead of just a binary flag.

## Transparency label

Confidence maps to a label in thresholds of thirds, per `planning.md`. Each label shows this exact text to the reader:

| Label | Confidence range | Display text |
|-------|------------------|--------------|
| likely human | 0 to 0.33 | `This has passed as likely human generated.` |
| uncertain | 0.34 to 0.66 | `The system is uncertain if this is human or AI work.` |
| likely AI | 0.67 to 1 | `The system has deemed this work likely written by AI.` |

## Rate limiting

`POST /submit` is limited to 5 requests per minute per client IP and requests over the limit get an HTTP 429. I picked 5 per minute because it is generous enough for a legitimate single-user submission flow but low enough to combat intentional abuse. It also keeps the Groq request quota from being burned through too quickly, since every submission makes an LLM call.

## Audit log

Every attribution decision and every appeal is captured in a structured audit log, retrievable at `GET /log`. Sample output with two entries, one of which has been appealed and moved to "under review":

```json
{
  "entries": [
    {
      "content_id": "9aca404c-19f7-4830-9fe3-08f533021e7f",
      "creator_id": "creator-cleo",
      "timestamp": "2026-07-01T03:28:42.345434+00:00",
      "signals": { "grammar": 0.3, "buzzword": 0.0, "llm": 0.2 },
      "confidence": 0.175,
      "attribution": "likely human",
      "status": "classified",
      "appeal_reason": null
    },
    {
      "content_id": "20265e09-484c-4b91-b166-afcabe397f69",
      "creator_id": "creator-anna",
      "timestamp": "2026-07-01T03:28:41.862000+00:00",
      "signals": { "grammar": 0.35, "buzzword": 1.0, "llm": 0.9 },
      "confidence": 0.7875,
      "attribution": "likely AI",
      "status": "under review",
      "appeal_reason": "This is my own formal writing style for work emails."
    }
  ]
}
```

Only creators classified as "uncertain" or "likely AI" can appeal. An appeal records the reasoning in `appeal_reason` and sets `status` to "under review". No reclassification is run automatically.

## Known limitations

The system will likely misclassify formal human writing. A clean professional email or a piece of academic writing uses the exact punctuation and vocabulary that signals 1 and 2 are built to catch, so it scores as AI-generated even when a person wrote it. Similarly, poetry can at times use large/ rare words for stylistic effect and this could as read as AI-generated to our system.

## Spec reflection

The spec provided a lot of help guiding me through better understanding how to build out my project, despite not having a starter repository/ codebase to work with. I decided to diverge from the spec in terms of my signal. I used three instead of two and for this reason I also included signals as a dictionary instead of standalone items in the JSON.

## AI usage

1. I directed Claude to generate the Groq LLM signal and the scoring logic that combines all three signals. I kept the weighted formula and the graceful fallback but overrode how failures were handled so that any Groq error returns `None` and drops cleanly to the heuristic-only average, rather than letting an exception reach the caller.

2. I directed Claude to build the `POST /appeal` endpoint. It first wrote appeals as brand new audit entries. I overrode that to update the existing entry in place, and had it add the guard that only "uncertain" or "likely AI" content can be appealed, which the original version did not enforce.
