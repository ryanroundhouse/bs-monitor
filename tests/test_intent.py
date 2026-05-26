"""Tests for the intent + crisis classifier — the safety-critical piece.

Run: python -m unittest discover -s tests
"""

import unittest

from moodful_responder import intent, replies


class CrisisGate(unittest.TestCase):
    """Anything with distress markers must never be eligible to reply."""

    CRISIS_POSTS = [
        "i feel suicidal and don't know what to do",
        "looking for a mood tracker because i keep wanting to die",  # ask + crisis
        "any mood journal recommendations? been so hopeless lately",  # ask + crisis
        "i can't go on like this",
        "recovering from self harm, what app helps track moods",      # ask + crisis
    ]

    def test_crisis_posts_are_excluded(self):
        for text in self.CRISIS_POSTS:
            res = intent.evaluate(text)
            self.assertTrue(res.is_crisis, f"should flag crisis: {text!r}")
            self.assertFalse(res.should_reply, f"must not reply: {text!r}")

    def test_contextual_crisis_terms_require_negative_personal_sentiment(self):
        neutral_or_third_party = [
            "Here's the full breakdown:",
            "the bluejays offense is worthless",
            "Evans went for a Suicide Dive, but Penta moved",
        ]
        for text in neutral_or_third_party:
            res = intent.evaluate(text)
            self.assertFalse(res.is_crisis, f"neutral/third-party usage should not flag: {text!r}")

    def test_contextual_crisis_terms_still_flag_personal_distress(self):
        distress_posts = [
            "i feel completely worthless today",
            "i'm having a breakdown and need help",
            "had a panic attack while writing this today",
        ]
        for text in distress_posts:
            res = intent.evaluate(text)
            self.assertTrue(res.is_crisis, f"personal distress should flag: {text!r}")
            self.assertFalse(res.should_reply, f"must not reply: {text!r}")


class GenuineAsks(unittest.TestCase):
    """Clear recommendation requests should be detected."""

    ASKS = [
        "anyone know a good mood tracker? looking for something simple",
        "can someone recommend a mood journal app?",
        "looking for a journaling app with no streak pressure, suggestions?",
        "what mood tracker do you all use?",
        "trying to find an app to track my feelings — any recommendations?",
        "best mood tracking app for daily journaling?",
    ]

    def test_asks_are_detected(self):
        for text in self.ASKS:
            res = intent.evaluate(text)
            self.assertTrue(res.should_reply, f"should reply: {text!r}")
            self.assertGreaterEqual(res.confidence, 0.6, f"low conf: {text!r}")


class NotAsks(unittest.TestCase):
    """Mood-adjacent posts that are NOT requests must be ignored."""

    NON_ASKS = [
        "i love my mood tracker, been using it for years",
        "ugh my mood today is all over the place",
        "just journaled for an hour, feeling better",
        "the weather really affects my mood",
        "check out my new mood tracking app i built!",  # someone else promoting
        "anyone else watching the game tonight?",
    ]

    def test_non_asks_are_ignored(self):
        for text in self.NON_ASKS:
            res = intent.evaluate(text)
            self.assertFalse(res.should_reply, f"should NOT reply: {text!r}")


class ReplyDrafting(unittest.TestCase):
    def test_every_reply_discloses_and_links(self):
        for tmpl in replies.GENERAL_TEMPLATES + replies.BURNOUT_TEMPLATES:
            self.assertIn("moodful.ca", tmpl)
            self.assertLessEqual(len(tmpl), replies.MAX_GRAPHEMES)

    def test_burnout_context_uses_forgiving_template(self):
        text = "deleted my last mood tracker, the streak guilt burned me out. recommendations?"
        draft = replies.draft_reply(text)
        self.assertIn(draft, replies.BURNOUT_TEMPLATES)

    def test_link_facet_byte_range_matches_link(self):
        text = "one option is moodful.ca, full disclosure it's mine"
        facets = replies.link_facets(text)
        self.assertEqual(len(facets), 1)
        idx = facets[0]["index"]
        sliced = text.encode("utf-8")[idx["byteStart"]:idx["byteEnd"]]
        self.assertEqual(sliced.decode("utf-8"), "moodful.ca")


if __name__ == "__main__":
    unittest.main()
