import tempfile
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

from app.agents.workflow import JobManager, WorkflowNodes, WorkflowState


class PortraitWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.manager = JobManager()
        self.job_id = "portrait-test"
        self.state = WorkflowState({
            "job_id": self.job_id,
            "mode": "portrait",
            "status": "portrait_matching_done",
            "video_info": {"duration": 180.0},
            "segment_seconds": 60,
            "analysis_segments": [
                {"start_sec": 0.0, "end_sec": 60.0},
                {"start_sec": 60.0, "end_sec": 120.0},
                {"start_sec": 120.0, "end_sec": 180.0},
            ],
            "detected_people": [
                {"person_id": "person-1", "segments": [{"start_sec": 10.0, "end_sec": 20.0}]},
                {"person_id": "person-2", "segments": [{"start_sec": 70.0, "end_sec": 80.0}]},
            ],
            "final_outputs": [],
        })
        self.manager.jobs[self.job_id] = self.state

    @staticmethod
    def selections(*items):
        result = []
        for idx, item in enumerate(items):
            enabled, person_id, subsegments = item
            result.append({
                "window_idx": idx,
                "enabled": enabled,
                "person_id": person_id,
                "subsegments": subsegments,
            })
        return result

    def test_manual_regions_open_unified_window_editor_without_preview(self):
        manual_id = "manual-test"
        manual = WorkflowState({
            "job_id": manual_id,
            "mode": "manual",
            "status": "reviewing_regions",
            "video_info": {"duration": 180.0},
            "segment_seconds": 60,
            "final_outputs": [],
        })
        self.manager.jobs[manual_id] = manual
        with patch("app.agents.workflow.nodes.cut_preview") as preview, \
                patch("threading.Thread.start") as start:
            result = self.manager.confirm_regions(manual_id, None, [], [])
        self.assertEqual("reviewing_windows", result["status"])
        self.assertEqual(3, len(result["analysis_segments"]))
        preview.assert_not_called()
        start.assert_not_called()

    @patch("threading.Thread.start")
    def test_manual_confirms_all_windows_once_for_batch_render(self, start):
        manual_id = "manual-confirm-test"
        manual = WorkflowState({
            "job_id": manual_id,
            "mode": "manual",
            "status": "reviewing_windows",
            "video_info": {"duration": 120.0},
            "segment_seconds": 60,
            "analysis_segments": [
                {"start_sec": 0.0, "end_sec": 60.0},
                {"start_sec": 60.0, "end_sec": 120.0},
            ],
            "detected_people": [],
            "final_outputs": [],
        })
        self.manager.jobs[manual_id] = manual
        result = self.manager.confirm_windows(manual_id, [
            {"window_idx": 0, "enabled": True, "person_id": "ignored",
             "subsegments": [{"start_sec": 5, "end_sec": 15}, {"start_sec": 30, "end_sec": 40}]},
            {"window_idx": 1, "enabled": True, "person_id": None,
             "subsegments": [{"start_sec": 60, "end_sec": 120}]},
        ])
        self.assertEqual("rendering", result["status"])
        self.assertEqual([None, None], [segment["person_id"] for segment in result["segments"]])
        self.assertEqual([2, 1], [len(segment["subsegments"]) for segment in result["segments"]])
        start.assert_called_once()

    def test_fixed_window_count_uses_ceil(self):
        state = WorkflowState({"video_info": {"duration": 3600.0}, "segment_seconds": 900})
        self.assertEqual(4, len(self.manager._plan_analysis_segments(state)))
        state = WorkflowState({"video_info": {"duration": 4800.0}, "segment_seconds": 900})
        windows = self.manager._plan_analysis_segments(state)
        self.assertEqual(6, len(windows))
        self.assertEqual({"start_sec": 4500.0, "end_sec": 4800.0}, windows[-1])

    @patch("threading.Thread.start")
    def test_preserves_all_windows_and_independent_people(self, start):
        result = self.manager.confirm_portrait(
            self.job_id,
            window_selections=self.selections(
                (True, "person-1", [{"start_sec": 10, "end_sec": 20}]),
                (True, "person-2", [{"start_sec": 70, "end_sec": 80}]),
                (True, None, [{"start_sec": 120, "end_sec": 180}]),
            ),
        )
        self.assertEqual([0, 1, 2], [segment["idx"] for segment in result["segments"]])
        self.assertEqual([0, 1, 2], [segment["window_idx"] for segment in result["segments"]])
        self.assertEqual(["person-1", "person-2", None], [segment["person_id"] for segment in result["segments"]])
        self.assertTrue(all(segment["status"] == "pending" for segment in result["segments"]))
        start.assert_called_once()

    def test_rejects_cross_window_range(self):
        with self.assertRaisesRegex(ValueError, "必须位于当前固定窗口内"):
            self.manager.confirm_portrait(
                self.job_id,
                window_selections=self.selections(
                    (True, None, [{"start_sec": 50, "end_sec": 70}]),
                    (False, None, []),
                    (False, None, []),
                ),
            )

    def test_rejects_missing_window(self):
        with self.assertRaisesRegex(ValueError, "必须提交全部固定窗口"):
            self.manager.confirm_portrait(
                self.job_id,
                window_selections=self.selections(
                    (False, None, []),
                    (False, None, []),
                ),
            )

    def test_rejects_duplicate_window_idx(self):
        selections = self.selections(
            (False, None, []),
            (False, None, []),
            (False, None, []),
        )
        selections[1]["window_idx"] = 0
        with self.assertRaisesRegex(ValueError, "window_idx 重复"):
            self.manager.confirm_portrait(self.job_id, window_selections=selections)

    def test_rejects_enabled_window_without_subsegments(self):
        with self.assertRaisesRegex(ValueError, "启用时至少需要一个有效子片段"):
            self.manager.confirm_portrait(
                self.job_id,
                window_selections=self.selections(
                    (True, "person-1", []),
                    (False, None, []),
                    (False, None, []),
                ),
            )

    def test_all_skipped_completes_without_thread(self):
        with patch("threading.Thread.start") as start:
            result = self.manager.confirm_portrait(
                self.job_id,
                window_selections=self.selections(
                    (False, None, []),
                    (False, None, []),
                    (False, None, []),
                ),
            )
        self.assertEqual("completed", result["status"])
        self.assertEqual(["skipped"] * 3, [segment["status"] for segment in result["segments"]])
        self.assertEqual(3, len(result["segments"]))
        start.assert_not_called()

    @patch("threading.Thread.start")
    def test_first_pending_index_skips_leading_windows(self, start):
        result = self.manager.confirm_portrait(
            self.job_id,
            window_selections=self.selections(
                (False, None, []),
                (True, "person-2", [{"start_sec": 70, "end_sec": 80}]),
                (False, None, []),
            ),
        )
        self.assertEqual(1, result["current_segment_idx"])
        self.assertEqual(0, result["render_progress"]["current_seg"])
        self.assertEqual(1, result["render_progress"]["total_segs"])
        start.assert_called_once()

    @patch("threading.Thread.start")
    def test_portrait_batch_renders_all_pending_without_preview(self, start):
        result = self.manager.confirm_portrait(
            self.job_id,
            window_selections=self.selections(
                (True, "person-1", [{"start_sec": 10, "end_sec": 20}]),
                (False, None, []),
                (True, None, [{"start_sec": 130, "end_sec": 140}]),
            ),
        )

        def mark_rendered(state):
            state["segments"][state["current_segment_idx"]]["status"] = "rendered"

        with patch("app.agents.workflow.nodes.render_segment", side_effect=mark_rendered) as render, \
                patch("app.agents.workflow.nodes.cut_preview") as preview:
            self.manager._render_all_windows_in_background(self.job_id)
        self.assertEqual(2, render.call_count)
        preview.assert_not_called()
        self.assertEqual(["rendered", "skipped", "rendered"],
                         [segment["status"] for segment in result["segments"]])
        self.assertEqual(2, result["current_segment_idx"])
        self.assertEqual("completed", result["status"])

    def test_render_groups_subsegments_by_output_group(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            nodes = WorkflowNodes()
            state = WorkflowState({
                "job_id": "separate-output-test",
                "upload_path": str(Path(temp_dir) / "input.mp4"),
                "video_info": {"duration": 60.0},
                "current_segment_idx": 0,
                "logo_regions": [],
                "mask_regions": [],
                "final_outputs": [],
                "segments": [{
                    "start_sec": 0.0,
                    "end_sec": 60.0,
                    "final_end_sec": 60.0,
                    "subsegments": [
                        {"start_sec": 5.0, "end_sec": 10.0, "output_group": 1},
                        {"start_sec": 20.0, "end_sec": 30.0, "output_group": 1},
                    ],
                    "status": "human_review",
                }],
            })
            with patch.object(WorkflowNodes, "storage", new_callable=PropertyMock,
                              return_value=Path(temp_dir)), \
                    patch("app.agents.workflow.ffmpeg_proc.cut_and_process_segment") as cut, \
                    patch("app.agents.workflow.ffmpeg_proc.concat_processed_segments") as concat:
                nodes.render_segment(state)

        self.assertEqual(2, cut.call_count)
        concat.assert_called_once()
        outputs = state["final_outputs"]
        self.assertEqual(1, len(outputs))
        self.assertTrue(outputs[0].endswith("output_group_01.mp4"))
        self.assertEqual(outputs, state["segments"][0]["output_paths"])
        self.assertEqual(outputs[0], state["segments"][0]["output_path"])
        self.assertEqual("rendered", state["segments"][0]["status"])


if __name__ == "__main__":
    unittest.main()
