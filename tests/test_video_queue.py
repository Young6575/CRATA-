import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server


class VideoQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.inbox = self.root / "처리관리" / "inbox"
        self.inbox.mkdir(parents=True)
        self.root_patch = mock.patch.object(server, "ROOT", self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.tmp.cleanup()

    def write_task(self, name, **data):
        path = self.inbox / name
        task = {
            "id": name.removeprefix("task_").removesuffix(".json"),
            "type": "video_encoding",
            "title": data.get("title", name),
            "status": data.get("status", "queued"),
            "statusLbl": data.get("statusLbl", "대기 중"),
            "created_at": data.get("created_at", "2026-05-31T00:00:00"),
            "updated_at": data.get("updated_at", data.get("created_at", "2026-05-31T00:00:00")),
            "runnerPreference": data.get("runnerPreference", "codex"),
        }
        task.update(data)
        path.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        return path

    def test_video_task_should_queue_when_another_video_task_is_running(self):
        self.write_task("task_active.json", status="active", pid=1234)
        with mock.patch.object(server, "task_process_alive", return_value=True):
            self.assertTrue(server.should_queue_video_task({"type": "video_encoding"}))

    def test_auto_start_next_video_task_starts_oldest_queued_video_task(self):
        older = self.write_task("task_old.json", status="queued", created_at="2026-05-31T00:00:00")
        self.write_task("task_new.json", status="queued", created_at="2026-05-31T00:01:00")

        started = []

        def fake_start(path, runner):
            started.append((Path(path), runner))

        with mock.patch.object(server, "task_process_alive", return_value=False), \
                mock.patch.object(server, "start_task_worker", side_effect=fake_start):
            selected = server.auto_start_next_video_task()

        self.assertEqual(Path(selected), older)
        self.assertEqual(started, [(older, "codex")])
        updated = json.loads(older.read_text(encoding="utf-8"))
        self.assertEqual(updated["status"], "pending")
        self.assertEqual(updated["statusLbl"], "대기 중")

    def test_auto_start_next_video_task_can_continue_while_preview_waits(self):
        queued = self.write_task("task_waiting.json", status="queued", created_at="2026-05-31T00:00:00")

        with mock.patch.object(server, "read_video_status", return_value={"status": "waiting_preview_review"}), \
                mock.patch.object(server, "task_process_alive", return_value=False), \
                mock.patch.object(server, "start_task_worker") as start_worker:
            selected = server.auto_start_next_video_task()

        self.assertEqual(Path(selected), queued)
        start_worker.assert_called_once()

    def test_continue_video_queue_after_video_task_finishes_starts_next(self):
        current = self.write_task("task_current.json", status="done", created_at="2026-05-31T00:00:00")
        next_task = self.write_task("task_next.json", status="queued", created_at="2026-05-31T00:01:00")

        with mock.patch.object(server, "read_video_status", return_value={"status": "done"}), \
                mock.patch.object(server, "task_process_alive", return_value=False), \
                mock.patch.object(server, "start_task_worker") as start_worker:
            selected = server.continue_video_queue_after_task(current)

        self.assertEqual(Path(selected), next_task)
        start_worker.assert_called_once()

    def test_video_queue_payload_exposes_history_for_dashboard(self):
        self.write_task("task_done.json", status="done", statusLbl="완료됨", progress=100, created_at="2026-05-31T00:00:00")
        self.write_task("task_waiting.json", status="queued", statusLbl="대기열", progress=0, created_at="2026-05-31T00:01:00")

        with mock.patch.object(server, "task_process_alive", return_value=False):
            payload = server.video_queue_payload()

        self.assertEqual([item["id"] for item in payload[:2]], ["waiting", "done"])
        self.assertEqual(payload[0]["status"], "queued")
        self.assertEqual(payload[0]["status_label"], "대기열")
        self.assertIn("created_at", payload[0])

    def test_video_queue_payload_includes_process_snapshot_for_dashboard_detail(self):
        self.write_task(
            "task_done.json",
            status="done",
            videoStatusSnapshot={
                "status": "done",
                "current_process": "final_encode",
                "current_process_label": "최종 인코딩 완료",
                "process_status": {
                    "raw_transcribe": {"status": "done", "progress": 100},
                    "final_encode": {"status": "done", "progress": 100},
                },
                "process_results": {
                    "final_encode": [{"title": "최종 영상", "path": "C:\\work\\result_final.mp4"}],
                },
                "message": "완료",
            },
        )

        payload = server.video_queue_payload()

        self.assertEqual(payload[0]["current_process"], "final_encode")
        self.assertEqual(payload[0]["process_status"]["raw_transcribe"]["status"], "done")
        self.assertEqual(payload[0]["process_results"]["final_encode"][0]["title"], "최종 영상")
        self.assertEqual(payload[0]["message"], "완료")

    def test_video_queue_payload_infers_review_and_term_artifacts_from_workspace(self):
        workspace = self.root / "work" / "성장역량_김규아"
        workspace.mkdir(parents=True)
        video = workspace / "성장역량_김규아.mxf"
        video.write_text("video", encoding="utf-8")
        review = workspace / "성장역량_김규아_quality_review.md"
        review.write_text("quality", encoding="utf-8")
        term = workspace / "성장역량_김규아_term_correction.md"
        term.write_text("term", encoding="utf-8")
        self.write_task(
            "task_done.json",
            status="done",
            videoSourcePath="H:\\Q&A 강의 영상\\김규아\\성장역량\\성장역량_김규아.MXF",
            videoWorkspaceRoot=str(workspace),
            videoStatusSnapshot={
                "status": "done",
                "workspace_path": str(workspace),
                "source_path": "H:\\Q&A 강의 영상\\김규아\\성장역량\\성장역량_김규아.MXF",
                "process_status": {
                    "transcript_quality_review": {"status": "done", "progress": 100},
                    "crata_term_correction": {"status": "done", "progress": 100},
                },
            },
        )

        payload = server.video_queue_payload()

        results = payload[0]["process_results"]
        self.assertEqual(results["transcript_quality_review"][0]["path"], str(review))
        self.assertEqual(results["crata_term_correction"][0]["path"], str(term))

    def test_video_queue_payload_finds_legacy_workspace_by_source_name(self):
        workspace_root = self.root / "처리관리" / "video_workspaces"
        workspace = workspace_root / "_staging_20260531_020726_성장역량_김규아"
        workspace.mkdir(parents=True)
        video = workspace / "성장역량_김규아.mxf"
        video.write_text("video", encoding="utf-8")
        review = workspace / "성장역량_김규아_review.md"
        review.write_text("quality", encoding="utf-8")
        term = workspace / "성장역량_김규아_term_correction.md"
        term.write_text("term", encoding="utf-8")
        self.write_task(
            "task_legacy.json",
            status="done",
            videoSourcePath="H:\\Q&A 강의 영상\\김규아\\성장역량\\성장역량_김규아.MXF",
            videoWorkspaceRoot=str(workspace_root),
        )

        payload = server.video_queue_payload()

        results = payload[0]["process_results"]
        self.assertEqual(results["transcript_quality_review"][0]["path"], str(review))
        self.assertEqual(results["crata_term_correction"][0]["path"], str(term))

    def test_continue_video_queue_after_task_saves_process_snapshot(self):
        current = self.write_task("task_current.json", status="done")
        self.write_task("task_next.json", status="queued", created_at="2026-05-31T00:01:00")

        with mock.patch.object(server, "read_video_status", return_value={
                "status": "done",
                "current_process": "final_encode",
                "process_status": {"final_encode": {"status": "done", "progress": 100}},
                "message": "최종 인코딩 완료",
            }), \
                mock.patch.object(server, "task_process_alive", return_value=False), \
                mock.patch.object(server, "start_task_worker"):
            server.continue_video_queue_after_task(current)

        updated = json.loads(current.read_text(encoding="utf-8"))
        self.assertEqual(updated["videoStatusSnapshot"]["current_process"], "final_encode")
        self.assertEqual(updated["videoStatusSnapshot"]["process_status"]["final_encode"]["progress"], 100)

    def test_continue_video_queue_after_task_marks_preview_waiting_and_starts_next(self):
        current = self.write_task("task_current.json", status="active")
        next_task = self.write_task("task_next.json", status="queued", created_at="2026-05-31T00:01:00")

        with mock.patch.object(server, "read_video_status", return_value={
                "status": "waiting_preview_review",
                "current_process": "subtitle_preview_review",
                "preview_file": "C:\\work\\sample_preview.mp4",
            }), \
                mock.patch.object(server, "task_process_alive", return_value=False), \
                mock.patch.object(server, "start_task_worker") as start_worker:
            selected = server.continue_video_queue_after_task(current)

        self.assertEqual(Path(selected), next_task)
        start_worker.assert_called_once()
        updated = json.loads(current.read_text(encoding="utf-8"))
        self.assertEqual(updated["status"], "waiting_review")
        self.assertEqual(updated["statusLbl"], "확인 대기")


if __name__ == "__main__":
    unittest.main()
