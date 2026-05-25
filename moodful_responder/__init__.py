"""moodful_responder — find people *asking* for a mood/journaling tool on
Bluesky and (with human approval) reply to them about moodful.

Design stance: this responds ONLY to people who are actively soliciting a
recommendation. It never sends unsolicited outreach, never targets people by
mood/distress signals, applies a hard crisis-exclusion gate, discloses that it
is the maker, dedupes so no one is contacted twice, and queues every draft for
human approval before anything is posted.
"""

__all__ = ["intent", "replies", "store", "bsky", "jetstream"]
