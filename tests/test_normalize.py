"""Golden-set test: normalizer must reproduce tests/numbers.jsonl exactly."""
import json
import unittest
from pathlib import Path

from delivery_layer.normalize import normalize, normalize_with_map, cardinal, ordinal, year_words

GOLDEN = Path(__file__).with_name("numbers.jsonl")


class TestGolden(unittest.TestCase):
    def test_golden_set(self):
        rows = [json.loads(l) for l in GOLDEN.read_text().splitlines() if l.strip()]
        self.assertEqual(len(rows), 40)
        for r in rows:
            with self.subTest(r["display"]):
                self.assertEqual(normalize(r["display"]), r["spoken"])

    def test_span_map_covers_every_display_token(self):
        text = "Pay $1,842.00 by 12/31/2026 per Section 4(b)(ii); policy HO-2026-048113."
        spoken, segs = normalize_with_map(text)
        # every non-space char of the display text lies inside some segment
        covered = set()
        for s in segs:
            covered.update(range(s.display_start, s.display_end))
        for i, ch in enumerate(text):
            if not ch.isspace():
                self.assertIn(i, covered, f"char {i} {ch!r} not covered")
        replaced = [s for s in segs if s.replaced]
        self.assertEqual([s.display_text(text) for s in replaced],
                         ["$1,842.00", "12/31/2026", "Section 4(b)(ii)", "HO-2026-048113"])
        self.assertEqual(replaced[2].spoken, "Section four b two")

    def test_number_words(self):
        self.assertEqual(cardinal(1_250_003), "one million two hundred fifty thousand three")
        self.assertEqual(ordinal(12), "twelfth")
        self.assertEqual(ordinal(40), "fortieth")
        self.assertEqual(year_words(2007), "two thousand seven")
        self.assertEqual(year_words(2026), "twenty twenty-six")
        self.assertEqual(year_words(1900), "nineteen hundred")

    def test_idempotent_on_spoken_text(self):
        """Feeding spoken text back through the normalizer must not change it
        (the round-trip script relies on this)."""
        for t in ["one thousand dollars", "Section four b two", "twenty twenty-six", "H O, two zero"]:
            self.assertEqual(normalize(t), t)


if __name__ == "__main__":
    unittest.main()
