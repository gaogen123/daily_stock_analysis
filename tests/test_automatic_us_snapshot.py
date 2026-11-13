"""Regression checks for the optional US close-date and coverage gate."""
import sys
import types
import unittest
from unittest.mock import patch

import pandas as pd

from src.services.screening import snapshot_us


def _market_data(stale_count=0, invalid_count=0):
    dates = pd.to_datetime(["2026-11-11", "2026-11-12"])
    frames = {}
    for index, ticker in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE"]):
        close = [10.0, 11.0]
        if index < stale_count:
            close[-1] = float("nan")
        elif index < stale_count + invalid_count:
            close[-1] = 0.0
        frames[ticker] = pd.DataFrame({"Close": close, "Volume": [1000, 1100]}, index=dates)
    return pd.concat(frames, axis=1)


class SnapshotTests(unittest.TestCase):
    def _run(self, data, target="2026-11-12"):
        class FakeTicker:
            def __init__(self, ticker):
                self.fast_info = types.SimpleNamespace(market_cap=10_000_000, shares=1_000_000)

        fake_yf = types.SimpleNamespace(download=lambda *args, **kwargs: data, Ticker=FakeTicker)
        with patch.dict(sys.modules, {"yfinance": fake_yf}), \
             patch.dict(snapshot_us.os.environ, {"SCREENING_US_TARGET_DATE": target}):
            return snapshot_us.fetch_us_snapshot(["AAA", "BBB", "CCC", "DDD", "EEE"])

    def test_targeted_snapshot_rejects_stale_and_discloses_coverage(self):
        frame = self._run(_market_data(stale_count=1))
        self.assertEqual(len(frame), 4)
        self.assertNotIn("AAA", frame["code"].tolist())
        self.assertEqual(frame.attrs["snapshot_source"], "yfinance/provided: 4/5")

    def test_stale_and_invalid_prices_do_not_count_toward_coverage(self):
        with self.assertRaisesRegex(RuntimeError, "below 80%"):
            self._run(_market_data(stale_count=1, invalid_count=1))


if __name__ == "__main__":
    unittest.main()
