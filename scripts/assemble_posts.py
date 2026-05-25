#!/usr/bin/env python3
"""Assemble the 365 daily moodful posts from the per-pillar drafts.

Reads moodful_responder/content/raw/<pillar>.json (one JSON array of strings
each), then:
  - drops anything over Bluesky's 300-char limit or containing emoji,
  - dedupes case/whitespace-insensitively (within and across pillars),
  - interleaves the pillars round-robin so consecutive days rotate themes,
  - trims to exactly 365,
and writes moodful_responder/content/posts.json.

Deterministic: same inputs -> same output. Re-run any time the raw files change.
"""

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RAW = REPO / "moodful_responder" / "content" / "raw"
OUT = REPO / "moodful_responder" / "content" / "posts.json"

# Order here is the round-robin rotation order across the year.
PILLARS = ["philosophy", "features", "prompts", "ethos", "benefits", "habit"]

TARGET = 365
MAX_LEN = 300

# Rough emoji / pictograph ranges. Em dash (U+2014) and curly quotes are NOT
# in these ranges, so they're preserved.
EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000026FF\U00002700-\U000027BF"
    "\U0001F1E6-\U0001F1FF\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F]"
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def main() -> int:
    groups, seen, dropped = [], set(), {"long": 0, "emoji": 0, "dup": 0}
    for name in PILLARS:
        path = RAW / f"{name}.json"
        items = json.loads(path.read_text(encoding="utf-8"))
        clean = []
        for s in items:
            s = (s or "").strip()
            if not s:
                continue
            if len(s) > MAX_LEN:
                dropped["long"] += 1
                continue
            if EMOJI.search(s):
                dropped["emoji"] += 1
                continue
            key = _norm(s)
            if key in seen:
                dropped["dup"] += 1
                continue
            seen.add(key)
            clean.append(s)
        groups.append(clean)
        print(f"  {name:<11} {len(items):>3} drafted -> {len(clean):>3} kept")

    # Round-robin interleave so days rotate through pillars.
    posts, i = [], 0
    total = sum(len(g) for g in groups)
    while len(posts) < total:
        for g in groups:
            if i < len(g):
                posts.append(g[i])
        i += 1

    if len(posts) < TARGET:
        print(f"\nERROR: only {len(posts)} unique posts after cleaning; "
              f"need {TARGET}. Add more drafts.", file=sys.stderr)
        return 1

    posts = posts[:TARGET]
    OUT.write_text(json.dumps(posts, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")

    print(f"\nwrote {len(posts)} posts -> {OUT.relative_to(REPO)}")
    print(f"dropped: {dropped['dup']} dupes, {dropped['long']} too-long, "
          f"{dropped['emoji']} emoji")
    print(f"length: max {max(len(p) for p in posts)}, "
          f"min {min(len(p) for p in posts)} chars")
    print(f"mention moodful.ca: {sum('moodful.ca' in p for p in posts)}/{len(posts)}")
    print(f"mention moodful (any): "
          f"{sum('moodful' in p.lower() for p in posts)}/{len(posts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
