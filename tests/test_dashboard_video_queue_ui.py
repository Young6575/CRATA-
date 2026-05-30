import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard.html"


class DashboardVideoQueueUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = DASHBOARD.read_text(encoding="utf-8")

    def function_body(self, name):
        match = re.search(rf"function {re.escape(name)}\([^)]*\) \{{(?P<body>.*?)\n\}}", self.html, re.S)
        self.assertIsNotNone(match, f"{name} function not found")
        return match.group("body")

    def test_selecting_video_job_renders_main_process_panels(self):
        body = self.function_body("selectVideoJob")
        self.assertIn("renderSelectedVideoJobMainPanels", body)

    def test_polling_keeps_selected_video_job_visible_in_main_panels(self):
        body = self.function_body("updateVideoUI")
        self.assertIn("activeVideoProcessData", body)
        self.assertIn("ensureFinalTranscriptArtifact(activeData)", body)


if __name__ == "__main__":
    unittest.main()
