"""Provenance Guard — Flask app skeleton.

Backend that classifies submitted creative writing on a scale of
likely-human to likely-AI (see planning.md).

M3 scope:
  * Flask app skeleton
  * Signal 1 — Grammar/Punctuation heuristic
  * POST /submit wired to signal 1 + structured audit log
  * GET /log to retrieve recent audit entries

M4 scope:
  * Signal 2 — Buzzword Overuse heuristic
  * Signal 3 — LLM classification via Groq (graceful fallback on failure)
  * Weighted confidence scoring combining all three signals

M5 scope:
  * Transparency label display text
  * POST /appeal — updates a content's audit entry in place ("under review")
"""

import os
import re
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

load_dotenv()

app = Flask(__name__)

# Rate limiting (planning.md → Rate Limiting): keyed per client IP. The
# submission limit is applied per-route below; excess requests get HTTP 429.
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
)


@app.errorhandler(429)
def rate_limit_exceeded(error):
    """Return rate-limit rejections as JSON (HTTP 429)."""
    return jsonify({
        "error": "Rate limit exceeded. Try again later.",
        "detail": str(error.description),
    }), 429

# --- Signal weights (planning.md → Uncertainty Representation) -------------
# Confidence = 0.25·grammar + 0.25·buzzword + 0.50·llm. The LLM signal is
# weighted heavier because it assesses the text holistically; the two
# stylometric heuristics act as supporting evidence.
WEIGHT_GRAMMAR = 0.25
WEIGHT_BUZZWORD = 0.25
WEIGHT_LLM = 0.50

# Groq config for signal 3.
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_TIMEOUT_SECONDS = 10.0
_groq_client = None

# In-memory audit log. Each submission/appeal appends one structured entry
# (schema documented in planning.md). Replace with a datastore later if needed.
_AUDIT_LOG = []


def get_log():
    """Return audit log entries, most recent first."""
    return list(reversed(_AUDIT_LOG))


def label_for(confidence: float) -> str:
    """Map a confidence score to a transparency label (thirds, per planning.md)."""
    if confidence <= 0.33:
        return "likely human"
    if confidence <= 0.66:
        return "uncertain"
    return "likely AI"


# User-facing transparency text for each label (planning.md → Transparency
# Label Design). Shown in the UI alongside the attribution.
_DISPLAY_TEXT = {
    "likely human": "This has passed as likely human generated.",
    "uncertain": "The system is uncertain if this is human or AI work.",
    "likely AI": "The system has deemed this work likely written by AI.",
}

# Labels whose creators are permitted to appeal (planning.md → Appeal workflow).
_APPEALABLE = {"uncertain", "likely AI"}


def display_text_for(label: str) -> str:
    """Return the user-facing transparency text for a label."""
    return _DISPLAY_TEXT.get(label, "")


def find_entry(content_id: str):
    """Return the audit entry with the given content_id, or None."""
    for entry in _AUDIT_LOG:
        if entry["content_id"] == content_id:
            return entry
    return None


# ---------------------------------------------------------------------------
# Signal 1 — Grammar/Punctuation
# ---------------------------------------------------------------------------
# Premise (planning.md): polished, mechanically "perfect" writing that leans
# on punctuation rarely mastered in casual writing — semicolons, em-dashes,
# consistent sentence capitalization — reads as more AI-like. Casual markers
# (lowercase "i", dropped apostrophes, run-ons) pull the score back toward
# human.
#
# Returns a float in [0, 1]: higher == more AI-like on this signal alone.

def grammar_punctuation_signal(text: str) -> float:
    """Score the grammar/punctuation polish of `text` on a 0–1 scale."""
    if not text or not text.strip():
        return 0.0

    words = re.findall(r"\b\w+\b", text)
    word_count = max(len(words), 1)

    # Split into rough sentences for capitalization checks.
    sentences = [s.strip() for s in re.split(r"[.!?]+", text) if s.strip()]

    score = 0.0

    # --- AI-leaning cues -----------------------------------------------------

    # Semicolons: rarely used in casual writing. Reward presence, scaled by use.
    semicolons = text.count(";")
    if semicolons:
        score += min(0.30, 0.15 + 0.05 * semicolons)

    # Em-dashes / spaced hyphens used as sentence-level punctuation.
    em_dashes = text.count("—")  # —
    spaced_hyphens = len(re.findall(r"\s-\s", text))
    dashes = em_dashes + spaced_hyphens
    if dashes:
        score += min(0.25, 0.12 + 0.04 * dashes)

    # Consistent sentence-initial capitalization across multiple sentences.
    if len(sentences) >= 2:
        capped = sum(1 for s in sentences if s[:1].isupper())
        cap_ratio = capped / len(sentences)
        score += 0.20 * cap_ratio

    # Proper terminal punctuation on the whole passage.
    if re.search(r"[.!?]\s*$", text.strip()):
        score += 0.10

    # Apostrophe use in contractions handled correctly (don't, it's) is a mild
    # polish cue versus dropped apostrophes (dont, its-as-contraction).
    proper_contractions = len(re.findall(r"\b\w+'\w+\b", text))
    if proper_contractions:
        score += min(0.10, 0.05 * proper_contractions)

    # --- Human-leaning cues (subtract) --------------------------------------

    # Standalone lowercase "i" — a classic casual marker.
    if re.search(r"(^|\s)i(\s|$)", text):
        score -= 0.20

    # Dropped apostrophes in common contractions (dont, cant, wont, im).
    if re.search(r"\b(dont|cant|wont|im|youre|isnt|didnt|wasnt)\b", text, re.I):
        score -= 0.15

    # Multiple consecutive punctuation / casual emphasis (!!, ?!, ...).
    if re.search(r"([!?])\1|\.{3,}", text):
        score -= 0.10

    return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------
# Signal 2 — Buzzword Overuse
# ---------------------------------------------------------------------------
# Premise (planning.md): "upper echelon" vocabulary that leans corporate or
# unnecessarily formal for the context. We measure the density of such terms
# relative to total word count. Higher density == more AI-like.
#
# Returns a float in [0, 1].

# Curated set of corporate/AI-favored words and connective filler frequently
# overused by LLMs. Multi-word phrases are matched as substrings.
_BUZZWORDS = {
    "leverage", "utilize", "facilitate", "robust", "holistic", "synergy",
    "paradigm", "streamline", "optimize", "seamless", "innovative",
    "comprehensive", "furthermore", "moreover", "thus", "hence", "delve",
    "intricate", "realm", "tapestry", "testament", "navigate", "landscape",
    "foster", "underscore", "pivotal", "crucial", "myriad", "plethora",
    "nuanced", "multifaceted", "endeavor", "commence", "ascertain",
    "paramount", "elevate", "empower", "transformative", "dynamic",
    "strategic", "framework", "ecosystem", "actionable", "scalable",
    "cutting-edge", "game-changing", "groundbreaking", "unprecedented",
    "meticulous", "vibrant", "bustling", "embark", "harness", "showcase",
}
_BUZZ_PHRASES = (
    "in today's world", "in the realm of", "it is important to note",
    "plays a crucial role", "a testament to", "navigate the complexities",
    "rich tapestry", "ever-evolving", "in conclusion",
)


def buzzword_overuse_signal(text: str) -> float:
    """Score corporate/formal buzzword density of `text` on a 0–1 scale."""
    if not text or not text.strip():
        return 0.0

    words = re.findall(r"\b[\w-]+\b", text.lower())
    word_count = max(len(words), 1)

    hits = sum(1 for w in words if w in _BUZZWORDS)
    lowered = text.lower()
    hits += sum(lowered.count(p) for p in _BUZZ_PHRASES)

    # Density relative to length; ~5% buzzword density saturates to 1.0.
    density = hits / word_count
    return max(0.0, min(1.0, density / 0.05))


# ---------------------------------------------------------------------------
# Signal 3 — LLM Classification (Groq)
# ---------------------------------------------------------------------------
# Asks a Groq-hosted model to holistically judge whether text reads as human-
# or AI-written, returning a 0–1 likelihood of being AI-generated.
#
# Returns a float in [0, 1], or None if Groq is unavailable (missing key,
# network error, timeout, unparseable response) so the caller can degrade
# gracefully to the heuristic-only average.

_LLM_SYSTEM_PROMPT = (
    "You are a forensic text analyst. Judge whether the text was written by a "
    "human or generated by an AI language model. Consider semantic and "
    "stylistic coherence holistically. Respond with ONLY a single decimal "
    "number between 0 and 1: 0 means definitely human-written, 1 means "
    "definitely AI-generated. Output nothing but the number."
)


def _get_groq_client():
    """Lazily build a Groq client, or return None if no API key is set."""
    global _groq_client
    if _groq_client is None:
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key or not api_key.strip():
            return None
        from groq import Groq
        _groq_client = Groq(api_key=api_key, timeout=GROQ_TIMEOUT_SECONDS)
    return _groq_client


def llm_classification_signal(text: str):
    """Return 0–1 likelihood `text` is AI-generated, or None on failure."""
    client = _get_groq_client()
    if client is None:
        return None
    try:
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": _LLM_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0,
            max_tokens=10,
        )
        raw = (resp.choices[0].message.content or "").strip()
        match = re.search(r"\d*\.?\d+", raw)
        if not match:
            return None
        return max(0.0, min(1.0, float(match.group())))
    except Exception:
        # Any failure (network, timeout, auth, rate limit) → graceful fallback.
        return None


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

def compute_confidence(grammar: float, buzzword: float, llm):
    """Combine signals into a single confidence score (planning.md).

    With the LLM signal: weighted average (0.25 / 0.25 / 0.50).
    Without it (Groq unavailable): equal-weight average of the two heuristics.
    """
    if llm is None:
        return (grammar + buzzword) / 2
    return (
        WEIGHT_GRAMMAR * grammar
        + WEIGHT_BUZZWORD * buzzword
        + WEIGHT_LLM * llm
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/submit", methods=["POST"])
@limiter.limit("5 per minute")
def submit():
    """Accept a content submission.

    Expects a JSON body with at least `text` and `creator_id`.
    Returns a hardcoded response for now so the route can be verified
    before classification logic is wired in.
    """
    data = request.get_json(silent=True) or {}
    text = data.get("text")
    creator_id = data.get("creator_id")

    if not text or not creator_id:
        return jsonify({"error": "Both 'text' and 'creator_id' are required."}), 400

    # Run all three detection signals.
    grammar = round(grammar_punctuation_signal(text), 4)
    buzzword = round(buzzword_overuse_signal(text), 4)
    llm_raw = llm_classification_signal(text)
    llm = round(llm_raw, 4) if llm_raw is not None else None

    signals = {"grammar": grammar, "buzzword": buzzword, "llm": llm}
    confidence = round(compute_confidence(grammar, buzzword, llm), 4)
    attribution = label_for(confidence)

    entry = {
        "content_id": str(uuid.uuid4()),
        "creator_id": creator_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "signals": signals,
        "confidence": confidence,
        "attribution": attribution,
        "status": "classified",
        "appeal_reason": None,
    }
    _AUDIT_LOG.append(entry)

    return jsonify({
        "content_id": entry["content_id"],
        "creator_id": creator_id,
        "confidence": confidence,
        "attribution": attribution,
        "display_text": display_text_for(attribution),
        "status": "classified",
        "signals": signals,
    }), 201


@app.route("/appeal", methods=["POST"])
def appeal():
    """Let a creator contest a classification.

    Expects a JSON body with `content_id` and `creator_reasoning`.
    Updates the content's existing audit entry in place — no new entry and no
    re-analysis: `status` becomes "under review" and `appeal_reason` records
    the reasoning. Only "uncertain" or "likely AI" classifications may appeal.
    """
    data = request.get_json(silent=True) or {}
    content_id = data.get("content_id")
    creator_reasoning = data.get("creator_reasoning")

    if not content_id or not creator_reasoning:
        return jsonify({
            "error": "Both 'content_id' and 'creator_reasoning' are required."
        }), 400

    entry = find_entry(content_id)
    if entry is None:
        return jsonify({"error": f"No submission found for content_id {content_id}."}), 404

    if entry["attribution"] not in _APPEALABLE:
        return jsonify({
            "error": "Only 'uncertain' or 'likely AI' classifications can be appealed.",
            "attribution": entry["attribution"],
        }), 403

    # Update the existing entry in place (no new audit entry, no reclassification).
    entry["status"] = "under review"
    entry["appeal_reason"] = creator_reasoning

    return jsonify({
        "content_id": entry["content_id"],
        "attribution": entry["attribution"],
        "status": entry["status"],
        "appeal_reason": entry["appeal_reason"],
    }), 200


@app.route("/log", methods=["GET"])
def log():
    """Return the most recent audit log entries as JSON."""
    return jsonify({"entries": get_log()})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
