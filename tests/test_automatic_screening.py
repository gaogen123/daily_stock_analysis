import importlib.util
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "automatic_screening.py"
spec = importlib.util.spec_from_file_location("automatic_screening", SCRIPT)
screening = importlib.util.module_from_spec(spec)
spec.loader.exec_module(screening)


class ScreeningReportTests(unittest.TestCase):
    def test_empty_is_valid_only_for_nonempty_market_snapshot(self):
        self.assertEqual(screening.validated_candidates({"snapshot_count": 100, "candidates": []}), [])
        with self.assertRaises(ValueError):
            screening.validated_candidates({"snapshot_count": 0, "candidates": []})

    def test_reject_stale_duplicate_and_invalid_codes(self):
        bad_results = [
            {"snapshot_count": 100, "degradation": ["Snapshot source fallback: last_good_cache stale"], "candidates": []},
            {"snapshot_count": 100, "candidates": [{"code": "600519"}, {"code": "600519"}]},
            {"snapshot_count": 100, "candidates": [{"code": "600519;echo"}]},
        ]
        for result in bad_results:
            with self.subTest(result=result), self.assertRaises(ValueError):
                screening.validated_candidates(result)

    def test_summary_discloses_filter_and_degradation(self):
        result = {"snapshot_count": 100, "snapshot_source": "sina", "degradation": ["partial data"]}
        text = screening.screening_summary(result, [{"code": "600000", "name": "示例", "score": 0}], "2026-11-13")
        self.assertIn("3–220", text)
        self.assertIn("部分数据源", text)
        self.assertIn("600000", text)
        self.assertIn("| 0 |", text)

    def test_report_requires_each_stock_heading_not_merely_a_body_mention(self):
        candidates = [{"code": "600000"}, {"code": "600001"}]
        partial = "## 示例甲 (600000)\n新闻中提及 600001。\n"
        self.assertFalse(screening.has_candidate_reports(partial, candidates))
        self.assertTrue(screening.has_candidate_reports(partial + "\n## 示例乙 (600001)\n分析内容\n", candidates))

    def test_valid_empty_result_sends_summary_without_running_stock_analysis(self):
        calls = []
        config = object()
        result = {"snapshot_count": 5000, "candidates": [], "llm_ranked": False}

        class FakeService:
            def __init__(self, received_config):
                self.assert_config = received_config

            def screen(self, **kwargs):
                calls.append(("screen", kwargs))
                return result

        class FakeSender:
            def __init__(self, received_config):
                self.assert_config = received_config

            def send_to_email(self, content, subject):
                calls.append(("email", content))
                return True

        modules = {
            "src.config": types.SimpleNamespace(get_config=lambda: config),
            "src.core.trading_calendar": types.SimpleNamespace(get_trading_dates=lambda *args: [1]),
            "src.services.screening_service": types.SimpleNamespace(ScreeningService=FakeService),
            "src.notification_sender.email_sender": types.SimpleNamespace(EmailSender=FakeSender),
        }
        with tempfile.TemporaryDirectory() as directory, patch.object(screening, "ROOT", Path(directory)), \
             patch.dict(sys.modules, modules), patch.object(screening.os, "chdir"), \
             patch.object(screening.subprocess, "run") as process, \
             patch.dict(screening.os.environ, {"SCREENING_FORCE_RUN": "false"}):
            self.assertEqual(screening.main(), 0)
            process.assert_not_called()
            self.assertIn("没有符合条件", calls[-1][1])
            self.assertEqual(calls[0][1]["strategy"], "balanced_alpha")

    def test_selected_candidates_are_analyzed_and_emailed_once(self):
        calls = []

        class FakeService:
            def __init__(self, config):
                pass

            def screen(self, **kwargs):
                return {"snapshot_count": 5000, "candidates": [{"code": "600000", "name": "示例"}]}

        class FakeSender:
            def __init__(self, config):
                pass

            def send_to_email(self, content, subject):
                calls.append(content)
                return True

        modules = {
            "src.config": types.SimpleNamespace(get_config=lambda: object()),
            "src.core.trading_calendar": types.SimpleNamespace(get_trading_dates=lambda *args: [1]),
            "src.services.screening_service": types.SimpleNamespace(ScreeningService=FakeService),
            "src.notification_sender.email_sender": types.SimpleNamespace(EmailSender=FakeSender),
        }
        with tempfile.TemporaryDirectory() as directory, patch.object(screening, "ROOT", Path(directory)), \
             patch.dict(sys.modules, modules), patch.object(screening.os, "chdir"), \
             patch.dict(screening.os.environ, {"SCREENING_FORCE_RUN": "false"}):
            def analyze(command, **kwargs):
                self.assertIn("--no-notify", command)
                self.assertEqual(command[command.index("--stocks") + 1], "600000")
                day = datetime.now(ZoneInfo("Asia/Shanghai"))
                (Path(directory) / "reports" / f"report_{day:%Y%m%d}.md").write_text("## 示例 (600000)\n个股分析 600000", encoding="utf-8")

            with patch.object(screening.subprocess, "run", side_effect=analyze) as process:
                self.assertEqual(screening.main(), 0)
                process.assert_called_once()
            self.assertEqual(len(calls), 1)
            self.assertIn("候选名单", calls[0])
            self.assertIn("个股分析 600000", calls[0])


if __name__ == "__main__":
    unittest.main()
