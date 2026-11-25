"""Archival versus efficient classification and the efficient size band."""
import unittest

from kura import policy


class Classify(unittest.TestCase):
    def test_default_is_efficient(self):
        self.assertEqual(policy.classify(999000001), ("efficient", ["default"]))
        self.assertEqual(policy.classify(None, favourites={1}, score=10), ("efficient", ["default"]))

    def test_favourite_is_archival(self):
        self.assertEqual(policy.classify(999000001, favourites={999000001}), ("archival", ["favourite"]))

    def test_score_threshold(self):
        self.assertEqual(policy.classify(999000001, score=9.0), ("archival", ["score_9_plus"]))
        self.assertEqual(policy.classify(999000001, score=9.5)[0], "archival")
        self.assertEqual(policy.classify(999000001, score=8.9), ("efficient", ["default"]))
        self.assertEqual(policy.classify(999000001, score=None)[0], "efficient")

    def test_both_reasons_are_listed(self):
        self.assertEqual(policy.classify(1, favourites={1}, score=9.2),
                         ("archival", ["favourite", "score_9_plus"]))

    def test_override_beats_the_automatic_rules_both_ways(self):
        self.assertEqual(policy.classify(1, score=3.0, overrides={1: "archival"}), ("archival", ["override_archival"]))
        self.assertEqual(policy.classify(1, favourites={1}, score=10.0, overrides={1: "efficient"}),
                         ("efficient", ["override_efficient"]))
        self.assertEqual(policy.classify(2, favourites={1}, overrides={1: "efficient"}), ("efficient", ["default"]))

    def test_band(self):
        self.assertEqual(policy.EFFICIENT, {"target_mb": 250, "min_mb": 120, "max_mb": 450})
        self.assertEqual([policy.band_position(x) for x in (119.9, 120, 250, 450, 450.1, None, True)],
                         ["below", "within", "within", "within", "above", None, None])


if __name__ == "__main__":
    unittest.main()
