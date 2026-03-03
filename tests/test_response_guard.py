import unittest

from response_guard import RankedSource, ensure_body_text


class EnsureBodyTextTests(unittest.TestCase):
    def test_keeps_existing_body(self):
        out = ensure_body_text(
            body_text="Hotovo",
            citations=["[1]"],
            ranked_sources=[RankedSource("T", "S")],
            synthesizer=lambda _: "ignored",
        )
        self.assertEqual(out, "Hotovo")

    def test_regenerates_when_body_missing_and_citations_exist(self):
        out = ensure_body_text(
            body_text="   ",
            citations=["[1]"],
            ranked_sources=[RankedSource("RX 9060 XT", "Ceny jsou kolem 10 000 Kč")],
            synthesizer=lambda src: f"Přibližně {src[0].snippet.lower()}.",
        )
        self.assertIn("přibližně", out.lower())

    def test_falls_back_to_snippet_when_synthesizer_fails(self):
        def broken(_):
            raise RuntimeError("LLM unavailable")

        out = ensure_body_text(
            body_text="",
            citations=["[1]", "[2]"],
            ranked_sources=[
                RankedSource("A", "První snippet"),
                RankedSource("B", "Druhý snippet"),
            ],
            synthesizer=broken,
            top_n=2,
        )
        self.assertIn("První snippet", out)

    def test_returns_empty_without_citations(self):
        out = ensure_body_text(
            body_text="",
            citations=[],
            ranked_sources=[RankedSource("A", "Snippet")],
            synthesizer=lambda _: "X",
        )
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
