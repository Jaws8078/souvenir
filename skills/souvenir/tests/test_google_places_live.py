"""Opt-in live smoke test for Google Places (never runs in normal CI)."""
import os
import unittest

from pathlib import Path
import importlib.util


SCRIPT = Path(__file__).parents[1] / "scripts" / "trip_engine.py"
SPEC = importlib.util.spec_from_file_location("souvenir_google_live_engine", SCRIPT)
engine = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(engine)


LIVE = os.environ.get("SOUVENIR_LIVE_GOOGLE_TEST", "").strip().lower() in {"1", "true", "yes", "on"}
API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "").strip()


@unittest.skipUnless(LIVE and API_KEY, "opt-in: set SOUVENIR_LIVE_GOOGLE_TEST=1 and GOOGLE_PLACES_API_KEY")
class GooglePlacesLiveTests(unittest.TestCase):
    def test_text_search_returns_safe_candidate_packet(self):
        packet = engine.prepare_google_place_candidate_page(
            "후쿠오카 관광지", API_KEY, "후쿠오카", summary_only=True
        )
        self.assertEqual(packet["검색 제공자"], "Google Places API (New)")
        self.assertLessEqual(len(packet["후보"]), 10)
        self.assertNotIn(API_KEY, str(packet))


if __name__ == "__main__":
    unittest.main()
