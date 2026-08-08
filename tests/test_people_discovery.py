import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

from app.core import face_match


class _VideoInfo:
    duration = 20.0


def _embedding(index=0):
    value = np.zeros(128, dtype=np.float32)
    value[index] = 1.0
    return value


def _face(index=0, confidence=0.9, box=None):
    box = box or [40, 30, 100, 90]
    return {
        "xyxy": box,
        "landmarks": [[55.0, 50.0], [82.0, 50.0], [69.0, 63.0],
                      [58.0, 76.0], [80.0, 76.0]],
        "confidence": confidence,
        "embedding": _embedding(index),
    }


class PeopleDiscoveryTest(unittest.TestCase):
    def test_faces_client_success_schema(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"faces": [[{
            "xyxy": [40, 30, 100, 90],
            "landmarks": [[55.0, 50.0], [82.0, 50.0], [69.0, 63.0],
                          [58.0, 76.0], [80.0, 76.0]],
            "confidence": 0.92,
            "embedding": _embedding(0).tolist(),
        }], []]}
        frames = [np.full((120, 160, 3), 127, np.uint8) for _ in range(2)]
        with patch.object(face_match.httpx, "post", return_value=response) as post:
            result = face_match._detect_gpu_faces(frames)
        self.assertEqual(len(result[0]), 1)
        self.assertAlmostEqual(np.linalg.norm(result[0][0]["embedding"]), 1.0)
        self.assertTrue(post.call_args.args[0].endswith("/v1/faces:batch"))
        self.assertEqual(post.call_args.kwargs["data"], {
            "face_confidence": "0.85", "min_face_size": "48"})

    def test_invalid_embedding_is_rejected(self):
        response = Mock()
        response.raise_for_status.return_value = None
        bad = _face()
        bad["embedding"] = np.ones(128).tolist()
        response.json.return_value = {"faces": [[bad]]}
        with patch.object(face_match.httpx, "post", return_value=response):
            with self.assertRaises(ValueError):
                face_match._detect_gpu_faces([np.zeros((120, 160, 3), np.uint8)])

    def test_service_failure_has_no_haar_fallback_and_no_candidates(self):
        frame = np.full((120, 160, 3), 127, np.uint8)
        sampled = [(float(i * 5), frame) for i in range(9)]
        info = _VideoInfo()
        info.duration = 45.0
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch("app.core.ffmpeg_proc.probe_video", return_value=info), \
                patch.object(face_match, "_iter_discovery_frames", return_value=iter(sampled)), \
                patch.object(face_match.httpx, "post", side_effect=OSError("unavailable")) as post, \
                patch.object(face_match, "detect_faces") as haar:
            people = face_match.discover_people(Path("mock.mp4"), Path(temp_dir), 5.0)
        self.assertEqual(people, [])
        self.assertEqual(post.call_count, 1)
        haar.assert_not_called()

    def test_text_or_no_face_returns_empty(self):
        frame = np.full((120, 160, 3), (180, 30, 180), np.uint8)
        sampled = [(0.0, frame), (5.0, frame)]
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch("app.core.ffmpeg_proc.probe_video", return_value=_VideoInfo()), \
                patch.object(face_match, "_iter_discovery_frames", return_value=iter(sampled)), \
                patch.object(face_match, "_detect_gpu_faces", return_value=[[], []]):
            self.assertEqual(face_match.discover_people(Path("mock.mp4"), Path(temp_dir), 5.0), [])

    def test_same_embedding_clusters_and_different_embedding_splits(self):
        frame = np.full((180, 240, 3), 127, np.uint8)
        sampled = [(0.0, frame), (5.0, frame)]
        detections = [[_face(0), _face(1, box=[130, 30, 190, 90])],
                      [_face(0), _face(1, box=[130, 30, 190, 90])]]
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch("app.core.ffmpeg_proc.probe_video", return_value=_VideoInfo()), \
                patch.object(face_match, "_iter_discovery_frames", return_value=iter(sampled)), \
                patch.object(face_match, "_detect_gpu_faces", return_value=detections):
            people = face_match.discover_people(Path("mock.mp4"), Path(temp_dir), 5.0)
        self.assertEqual(len(people), 2)
        self.assertEqual([p["hit_count"] for p in people], [2, 2])

    def test_top_three_sorted_by_unique_timestamp_and_renumbered(self):
        frame = np.full((240, 400, 3), 127, np.uint8)
        sampled = [(float(i * 5), frame) for i in range(5)]
        batches = []
        for i in range(5):
            batches.append([_face(identity, box=[10 + identity * 80, 30,
                                                 70 + identity * 80, 90])
                            for identity, hits in enumerate([5, 4, 3, 2]) if i < hits])
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch("app.core.ffmpeg_proc.probe_video", return_value=_VideoInfo()), \
                patch.object(face_match, "_iter_discovery_frames", return_value=iter(sampled)), \
                patch.object(face_match, "_detect_gpu_faces", return_value=batches):
            people = face_match.discover_people(Path("mock.mp4"), Path(temp_dir), 5.0)
        self.assertEqual([p["hit_count"] for p in people], [5, 4, 3])
        self.assertEqual([p["person_id"] for p in people],
                         ["person_001", "person_002", "person_003"])

    def test_clear_natural_thumbnail_selected_from_original_frame(self):
        blurry = np.full((200, 200, 3), 127, np.uint8)
        clear = blurry.copy()
        clear[30:90, 40:100] = np.indices((60, 60)).sum(axis=0)[:, :, None] % 2 * 255
        sampled = [(0.0, blurry), (5.0, clear)]
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch("app.core.ffmpeg_proc.probe_video", return_value=_VideoInfo()), \
                patch.object(face_match, "_iter_discovery_frames", return_value=iter(sampled)), \
                patch.object(face_match, "_detect_gpu_faces",
                             return_value=[[_face(0, 0.8)], [_face(0, 0.9)]]):
            people = face_match.discover_people(Path("mock.mp4"), Path(temp_dir), 5.0)
            saved = cv2.imread(people[0]["thumbnail_path"])
        self.assertGreater(saved.std(), 10.0)
        self.assertAlmostEqual(people[0]["confidence"], 0.85)
        self.assertEqual(saved.shape[0], saved.shape[1])

    def test_merge_person_hits_deduplicates_and_keeps_gaps(self):
        segments = face_match._merge_person_hits(
            [(0.0, 0.5), (0.0, 0.8), (5.0, 0.7), (20.0, 0.9)], 30.0, 5.0)
        self.assertEqual([(s["start_sec"], s["end_sec"]) for s in segments],
                         [(0.0, 10.0), (20.0, 25.0)])
        self.assertAlmostEqual(segments[0]["confidence"], 0.75)


if __name__ == "__main__":
    unittest.main()
