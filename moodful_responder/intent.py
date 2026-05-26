"""Decide whether a post is a genuine request for a mood/journaling tool —
and, crucially, whether it shows any sign of crisis (in which case we never act).

Precision over recall, by design. We only treat a post as an "ask" when it
contains BOTH a topic signal (it's about mood/journaling tracking) AND a seeking
signal (the author is looking for / asking for a recommendation) — or one of a
few unambiguous high-confidence phrases. A human still reviews every match; this
classifier exists to keep that queue small, relevant, and safe.

The crisis gate is intentionally broad. The cost of a false crisis flag is only
"we don't reply"; the cost of a miss is replying to someone in distress with
marketing. So we bias hard toward exclusion.
"""

import re
from dataclasses import dataclass, field
from typing import List


# --- Crisis / acute-distress markers ---------------------------------------
# Direct self-harm / acute-risk phrases remain a hard stop. Broader distress
# words are contextual: they only trip the crisis gate when the post appears to
# be about the author's negative personal state. This keeps neutral uses like
# "full breakdown" or "the offense is worthless" out of the audit stream while
# preserving the safety rail for first-person distress.
HARD_CRISIS_TERMS = [
    "suicidal", "kill myself", "killing myself", "end my life",
    "end it all", "want to die", "wanna die", "wanting to die", "want to be dead",
    "better off dead",
    "self harm", "self-harm", "selfharm", "cut myself", "cutting myself",
    "hurt myself", "harm myself", "no reason to live", "don't want to be here",
    "dont want to be here", "can't go on", "cant go on", "give up on life",
    "crisis line", "hotline", "in crisis",
]

CONTEXTUAL_CRISIS_TERMS = [
    "suicide", "hopeless", "worthless", "panic attack", "breakdown",
    "relapse", "relapsing",
]

# Backwards-compatible aggregate for callers/tests that inspect the term list.
CRISIS_TERMS = HARD_CRISIS_TERMS + CONTEXTUAL_CRISIS_TERMS

PERSONAL_STATE_PATTERNS = [
    r"\b(i|i'm|im|me|my|myself|mine)\b",
    r"\b(had|having|feel|feeling|felt|been|being|am|was)\b",
    r"\bneed help\b",
]

NEGATIVE_SENTIMENT_TERMS = [
    "awful", "bad", "can't", "cant", "crying", "depressed", "done", "exhausted",
    "hate", "help", "hurting", "miserable", "overwhelmed", "sad", "scared",
    "spiraling", "struggling", "terrible", "tired", "ugh", "worse", "worst",
]

# --- Topic: the post is about mood / journaling / feeling tracking. ----------
TOPIC_PHRASES = [
    "mood tracker", "mood trackers", "mood tracking", "mood-tracking",
    "mood journal", "mood journaling", "mood diary", "mood log", "mood app",
    "track my mood", "track my moods", "tracking my mood", "track my feelings",
    "track my emotions", "track how i feel", "track how i'm feeling",
    "journaling app", "journalling app", "journal app", "feelings journal",
    "feelings tracker", "emotion tracker", "emotion journal",
    "mental health journal", "wellness journal", "gratitude journal",
    "habit tracker for mood", "daily journaling",
]

# --- Seeking: the author wants a recommendation / is looking for something. --
SEEKING_PHRASES = [
    "recommend", "recommendation", "recommendations", "suggest", "suggestion",
    "suggestions", "looking for", "in search of", "trying to find",
    "anyone know a", "anyone know of", "anyone know any", "does anyone know",
    "anyone use a", "anyone using a", "any good", "what do you use",
    "what app do you", "which app", "what's a good", "whats a good",
    "need a good", "need an app", "any apps", "app suggestions",
    "help me find", "can someone recommend", "any recommendations",
    "what should i use", "looking to start",
]

# --- High-confidence: these alone are unambiguous recommendation requests. ---
HIGH_CONFIDENCE_PHRASES = [
    "mood tracker recommendation", "mood tracking app recommendation",
    "recommend a mood", "best mood tracker", "best mood tracking",
    "looking for a mood", "looking for a journal", "looking for a journaling",
    "journaling app recommendation", "what mood tracker", "which mood tracker",
    "any mood tracker", "good mood tracking app", "good mood tracker",
]


def _contains_any(haystack: str, needles: List[str]) -> List[str]:
    return [n for n in needles if n in haystack]


def _has_personal_negative_sentiment(norm: str) -> bool:
    """Heuristic sentiment/context check for broad crisis words.

    We intentionally avoid adding an ML dependency to the always-on relay. A
    contextual crisis hit must look personal (first-person or personal-state
    language) and negative. The matched crisis word itself counts as the negative
    signal; this helper mainly filters out neutral/third-party uses.
    """
    return any(re.search(pat, norm) for pat in PERSONAL_STATE_PATTERNS) or any(
        term in norm for term in NEGATIVE_SENTIMENT_TERMS
    )


def _crisis_hits(norm: str) -> List[str]:
    hits = _contains_any(norm, HARD_CRISIS_TERMS)
    contextual_hits = _contains_any(norm, CONTEXTUAL_CRISIS_TERMS)
    if contextual_hits and _has_personal_negative_sentiment(norm):
        hits.extend(contextual_hits)
    return hits


@dataclass
class IntentResult:
    is_ask: bool = False
    is_crisis: bool = False
    confidence: float = 0.0
    topic_hits: List[str] = field(default_factory=list)
    seeking_hits: List[str] = field(default_factory=list)
    crisis_hits: List[str] = field(default_factory=list)

    @property
    def should_reply(self) -> bool:
        """Eligible to reply iff it's an ask AND shows no crisis markers."""
        return self.is_ask and not self.is_crisis


def evaluate(text: str) -> IntentResult:
    """Classify a single post's text."""
    norm = re.sub(r"\s+", " ", text.lower()).strip()

    crisis_hits = _crisis_hits(norm)
    topic_hits = _contains_any(norm, TOPIC_PHRASES)
    seeking_hits = _contains_any(norm, SEEKING_PHRASES)
    high_conf = _contains_any(norm, HIGH_CONFIDENCE_PHRASES)

    is_ask = bool(high_conf) or (bool(topic_hits) and bool(seeking_hits))

    # Confidence is only used for the optional hybrid auto-send threshold and
    # for ranking the human's review queue.
    confidence = 0.0
    if topic_hits:
        confidence += 0.45
    if seeking_hits:
        confidence += 0.35
    if high_conf:
        confidence = max(confidence, 0.8)
    if "?" in text:
        confidence += 0.1
    if len(topic_hits) > 1 or len(seeking_hits) > 1:
        confidence += 0.05
    confidence = round(min(confidence, 1.0), 2)

    return IntentResult(
        is_ask=is_ask,
        is_crisis=bool(crisis_hits),
        confidence=confidence if is_ask else 0.0,
        topic_hits=topic_hits,
        seeking_hits=seeking_hits,
        crisis_hits=crisis_hits,
    )
