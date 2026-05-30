import contextlib
import io
import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUBTITLE_AGENT = ROOT / "tools" / "video_agent" / "subtitle_agent.py"


def load_agent():
    spec = importlib.util.spec_from_file_location("subtitle_agent_for_tests", SUBTITLE_AGENT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VideoNamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent = load_agent()
        cls.agent.STATUS_FILE = None
        cls.agent.LEGACY_STATUS_FILE = None

    def write_srt(self, path, lines):
        blocks = []
        for idx, text in enumerate(lines, 1):
            blocks.append(f"{idx}\n00:00:{idx:02d},000 --> 00:00:{idx + 1:02d},000\n{text}\n")
        path.write_text("\n".join(blocks), encoding="utf-8")

    def test_transcript_naming_uses_topic_title_not_full_sentence(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            srt = folder / "source.srt"
            video = folder / "source.mxf"
            self.write_srt(srt, [
                "네 오늘은 성장역량이 아이들에게 어떻게 나타나는지에 대해서 말씀드리겠습니다.",
                "성장역량은 문제해결방식검사에서 중요한 설명 축입니다.",
                "질문 답변으로 성장역량 사례를 하나씩 보겠습니다.",
            ])

            naming = self.agent.transcript_naming_from_srt(srt, video)

        self.assertIn("성장역량", naming["title"])
        self.assertNotIn("말씀드리겠습니다", naming["title"])
        self.assertLessEqual(len(naming["title"]), 24)

    def test_transcript_naming_combines_specific_terms(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            srt = folder / "source.srt"
            video = folder / "source.mxf"
            self.write_srt(srt, [
                "문제해결방식검사의 중심역량과 핵심역량 차이를 설명합니다.",
                "중심역량은 사고의 중심을 잡고 핵심역량은 실행의 방향을 만듭니다.",
                "문제해결방식검사 결과지에서 두 역량을 비교해 보겠습니다.",
            ])

            naming = self.agent.transcript_naming_from_srt(srt, video)

        self.assertIn("문제해결방식검사", naming["title"])
        self.assertTrue("중심역량" in naming["title"] or "핵심역량" in naming["title"])
        self.assertNotRegex(naming["title"], r"설명합니다|비교해")

    def test_finalize_workspace_renames_video_and_transcript_to_topic_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "_staging_20260531_source"
            staging.mkdir()
            video = staging / "source.MXF"
            srt = staging / "source.srt"
            video.write_bytes(b"fake")
            self.write_srt(srt, [
                "문제해결방식검사의 중심역량과 핵심역량 차이를 설명합니다.",
                "중심역량은 사고의 중심을 잡고 핵심역량은 실행의 방향을 만듭니다.",
            ])

            with contextlib.redirect_stdout(io.StringIO()):
                new_video, new_srt = self.agent.finalize_workspace_folder(video, srt)

            self.assertTrue(new_video.exists())
            self.assertTrue(new_srt.exists())
            self.assertEqual(new_video.suffix, ".MXF")
            self.assertIn("문제해결방식검사", new_video.stem)
            self.assertIn("문제해결방식검사", new_srt.stem)
            self.assertNotIn("설명합니다", new_video.name)
            self.assertNotIn("_staging", str(new_video.parent))


if __name__ == "__main__":
    unittest.main()
