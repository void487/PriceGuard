import unittest

from query_guard import finalize_web_query


class FinalizeWebQueryTests(unittest.TestCase):
    def test_strips_user_message_wrapper_and_keeps_payload(self):
        result = finalize_web_query(
            user_text="Za kolik korun se priblizne prodava amd rx9060xt s 16GB VRAM???",
            rewritten_query='User message: "Za kolik korun se priblizne prodava amd rx9060xt s 16GB VRAM???"',
        )
        self.assertEqual(
            result.query,
            "Za kolik korun se priblizne prodava amd rx9060xt s 16GB VRAM???",
        )
        self.assertFalse(result.used_fallback)

    def test_falls_back_when_rewrite_drops_entities(self):
        result = finalize_web_query(
            user_text="amd rx9060xt 16GB VRAM cena",
            rewritten_query="price of gpu",
        )
        self.assertEqual(result.query, "amd rx9060xt 16GB VRAM cena")
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.reason, "low_entity_overlap")

    def test_falls_back_on_empty_rewrite(self):
        result = finalize_web_query(
            user_text="rx 9060 xt cena czk",
            rewritten_query="   ",
        )
        self.assertEqual(result.query, "rx 9060 xt cena czk")
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.reason, "empty_rewrite")


if __name__ == "__main__":
    unittest.main()
