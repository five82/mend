import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from mend import cli


class OutputValidationTest(unittest.TestCase):
    def test_rejects_invalid_video_or_lost_container_data(self) -> None:
        video = {
            "codec_type": "video",
            "codec_name": "ffv1",
            "width": 1440,
            "height": 1080,
            "sample_aspect_ratio": "1:1",
            "pix_fmt": "yuv420p10le",
            "field_order": "progressive",
            "avg_frame_rate": "24000/1001",
            "color_range": "tv",
            "color_space": "smpte170m",
            "color_transfer": "smpte170m",
            "color_primaries": "smpte170m",
        }
        audio = {"codec_type": "audio"}
        probe = {"streams": [video, audio], "format": {"size": 11 * 1024 * 1024}}
        source_mkv = {
            "tracks": [
                {"type": "video", "properties": {"tag_duration": "00:00:10.000"}},
                {"type": "audio", "codec": "AC-3", "properties": {"language": "eng"}},
            ],
            "attachments": [{"file_name": "font.ttf"}],
            "chapters": [{"num_entries": 2}],
        }
        output_mkv = deepcopy(source_mkv)
        cases = [
            (
                "missing audio",
                "invalid streams",
                lambda p, s, o: p["streams"].pop(),
                None,
            ),
            (
                "wrong codec",
                "invalid video format",
                lambda p, s, o: p["streams"][0].update(codec_name="h264"),
                None,
            ),
            (
                "wrong color",
                "invalid color_range",
                lambda p, s, o: p["streams"][0].update(color_range="pc"),
                None,
            ),
            (
                "wrong rate",
                "invalid frame rate",
                lambda p, s, o: p["streams"][0].update(avg_frame_rate="25/1"),
                None,
            ),
            (
                "small file",
                "too small",
                lambda p, s, o: p["format"].update(size=1),
                None,
            ),
            (
                "tracks",
                "changed non-video tracks",
                None,
                lambda p, s, o: o["tracks"][1]["properties"].update(language="fra"),
            ),
            (
                "attachments",
                "changed attachments",
                None,
                lambda p, s, o: o["attachments"].clear(),
            ),
            (
                "chapters",
                "changed chapters",
                None,
                lambda p, s, o: o["chapters"].clear(),
            ),
            (
                "no duration",
                "no video duration",
                None,
                lambda p, s, o: o["tracks"][0]["properties"].clear(),
            ),
            (
                "duration mismatch",
                "duration differs",
                None,
                lambda p, s, o: o["tracks"][0]["properties"].update(
                    tag_duration="00:00:11.000"
                ),
            ),
        ]
        for name, message, change_probe, change_mkv in cases:
            with self.subTest(name=name):
                current_probe = deepcopy(probe)
                current_source = deepcopy(source_mkv)
                current_output = deepcopy(output_mkv)
                if change_probe:
                    change_probe(current_probe, current_source, current_output)
                if change_mkv:
                    change_mkv(current_probe, current_source, current_output)
                with (
                    patch("mend.cli.probe_container", return_value=current_probe),
                    patch(
                        "mend.cli.identify_matroska",
                        side_effect=[current_source, current_output],
                    ),
                    self.assertRaisesRegex(RuntimeError, message),
                ):
                    cli.validate_handoff_title(Path("source.mkv"), Path("output.mkv"))


if __name__ == "__main__":
    unittest.main()
