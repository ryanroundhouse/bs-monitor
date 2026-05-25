"""Draft a reply in moodful's voice.

Voice rules pulled from moodful's design system: calm, "curious, not clinical,"
sentence-case, no emoji, no medical claims, easy to ignore. Every template
discloses that the sender makes moodful (transparency is both the honest move
and what keeps this on the right side of Bluesky's rules) and includes the
moodful.ca link, which we turn into a clickable richtext facet.
"""

import random
from typing import Dict, List, Optional

LINK = "moodful.ca"
LINK_URI = "https://moodful.ca"

# Used when the asker is clearly burned out by streak-y / nagging apps.
BURNOUT_MARKERS = [
    "streak", "guilt", "burned out", "burnt out", "burnout", "pressure",
    "nag", "nags", "nagging", "gave up", "fell off", "too much", "overwhelming",
    "deleted", "quit", "stopped using",
]

GENERAL_TEMPLATES = [
    "if you want something low-key for this, i build a little mood journal "
    "called moodful (moodful.ca). no streak guilt, no good/bad days — just a "
    "quiet place to note how you felt. happy to answer anything.",

    "i'm biased — i make moodful (moodful.ca) — but it's built to feel like a "
    "notebook by your bed, not a clinical dashboard. might be the unhurried "
    "kind of thing you're after.",

    "fwiw the thing i work on, moodful (moodful.ca), is deliberately simple: "
    "pick a mood, jot a line, done. no scores, no 'you're doing badly today.' "
    "might fit.",

    "if you'd rather not have an app that nags you, moodful (moodful.ca) is the "
    "one i make — gentle by design. no pressure either way, just leaving it "
    "here since you asked.",

    "one option (full disclosure, it's mine): moodful at moodful.ca. it treats "
    "a heavy day as just a day, not a failure. that framing was the whole "
    "reason i built it.",

    "i'd genuinely just second whatever feels calm to you — but the one i make, "
    "moodful (moodful.ca), leans into 'curious, not clinical.' worth a look if "
    "that's your vibe.",
]

BURNOUT_TEMPLATES = [
    "if the streak pressure is what burned you out, that's exactly what i tried "
    "to avoid with moodful (moodful.ca) — there's a streak, but a missed day "
    "isn't framed as failing. no hard sell, just sharing.",

    "totally hear the burnout. the app i build, moodful (moodful.ca), is built "
    "to be forgiving about gaps on purpose. happy to point you at it if you "
    "want something quieter.",
]

MAX_GRAPHEMES = 300  # Bluesky post limit


def _has_burnout_context(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in BURNOUT_MARKERS)


def link_facets(text: str, link: str = LINK, uri: str = LINK_URI) -> List[Dict]:
    """Build AT Protocol richtext facets so the link is clickable.

    Facets index into the post's UTF-8 *bytes*, not characters.
    """
    idx = text.find(link)
    if idx < 0:
        return []
    byte_start = len(text[:idx].encode("utf-8"))
    byte_end = byte_start + len(link.encode("utf-8"))
    return [
        {
            "index": {"byteStart": byte_start, "byteEnd": byte_end},
            "features": [
                {"$type": "app.bsky.richtext.facet#link", "uri": uri}
            ],
        }
    ]


def draft_reply(post_text: str, rng: Optional[random.Random] = None) -> str:
    """Choose a contextual reply template for a given ask."""
    r = rng or random
    pool = BURNOUT_TEMPLATES if _has_burnout_context(post_text) else GENERAL_TEMPLATES
    reply = r.choice(pool)
    # Safety net — every template should already satisfy these.
    assert LINK in reply, "reply must contain the moodful.ca link"
    assert len(reply) <= MAX_GRAPHEMES, "reply exceeds Bluesky's 300-char limit"
    return reply
