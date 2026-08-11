"""Tests for file_handler helper functions."""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccgram.handlers.file_handler import (
    _describe_media,
    _generate_photo_filename,
    _media_extension,
    _sanitize_caption,
    _sanitize_filename,
    _unique_dest,
    _validate_dest_path,
)


class TestSanitizeFilename:
    @pytest.mark.parametrize(
        ("input_name", "expected"),
        [
            ("document.pdf", "document.pdf"),
            ("file-name_123.txt", "file-name_123.txt"),
            ("/etc/passwd", "passwd"),
            ("../../../etc/passwd", "passwd"),
            ("../../etc/passwd", "passwd"),
            ("hello world!.txt", "hello_world_.txt"),
            ("file@#$.txt", "file___.txt"),
            ("..", "unnamed"),
            (".", "unnamed"),
            ("...", "unnamed"),
            ("", "unnamed"),
        ],
    )
    def test_sanitize(self, input_name: str, expected: str) -> None:
        assert _sanitize_filename(input_name) == expected

    def test_truncates_long_names_preserving_extension(self) -> None:
        long = "a" * 250 + ".pdf"
        result = _sanitize_filename(long)
        assert len(result) <= 200
        assert result.endswith(".pdf")


class TestUniqueDest:
    def test_returns_original_if_not_exists(self, tmp_path: Path) -> None:
        assert _unique_dest(tmp_path / "file.txt") == tmp_path / "file.txt"

    @pytest.mark.parametrize(
        ("existing_files", "expected_name"),
        [
            (["file.txt"], "file_1.txt"),
            (["file.txt", "file_1.txt", "file_2.txt"], "file_3.txt"),
            (["file"], "file_1"),
        ],
    )
    def test_increments_suffix(
        self, tmp_path: Path, existing_files: list[str], expected_name: str
    ) -> None:
        for name in existing_files:
            (tmp_path / name).write_text("x")
        assert _unique_dest(tmp_path / existing_files[0]) == tmp_path / expected_name

    def test_fallback_to_timestamp_after_100(self, tmp_path: Path) -> None:
        dest = tmp_path / "file.txt"
        for i in range(100):
            name = "file.txt" if i == 0 else f"file_{i}.txt"
            (tmp_path / name).write_text(str(i))
        result = _unique_dest(dest)
        assert result.name.startswith("file_") and result.name.endswith(".txt")
        assert result != dest

    def test_broken_symlink_treated_as_existing(self, tmp_path: Path) -> None:
        dest = tmp_path / "file.txt"
        dest.symlink_to(tmp_path / "nonexistent_target")
        assert _unique_dest(dest) == tmp_path / "file_1.txt"


class TestValidateDestPath:
    @pytest.mark.parametrize(
        ("rel_dest", "expected"),
        [
            ("file.txt", True),
            ("subdir/file.txt", True),
            ("../outside.txt", False),
        ],
    )
    def test_path_validation(
        self, tmp_path: Path, rel_dest: str, expected: bool
    ) -> None:
        upload = tmp_path / "upload"
        upload.mkdir()
        if "/" in rel_dest and not rel_dest.startswith(".."):
            (upload / Path(rel_dest).parent).mkdir(parents=True, exist_ok=True)
        assert _validate_dest_path(upload / rel_dest, upload) is expected

    def test_rejects_absolute_path_outside(self, tmp_path: Path) -> None:
        upload = tmp_path / "upload"
        upload.mkdir()
        assert _validate_dest_path(tmp_path / "outside.txt", upload) is False


class TestSanitizeCaption:
    @pytest.mark.parametrize(
        ("input_text", "expected"),
        [
            ("", ""),
            ("hello\x00\x01\x02world", "helloworld"),
            ("hello\x07\x1bworld", "helloworld"),
            ("line1\nline2\r\nline3\ttab", "line1 line2  line3\ttab"),
        ],
    )
    def test_sanitize(self, input_text: str, expected: str) -> None:
        assert _sanitize_caption(input_text) == expected

    def test_limits_to_500_chars(self) -> None:
        assert len(_sanitize_caption("a" * 600)) == 500


class TestGeneratePhotoFilename:
    def test_format(self) -> None:
        result = _generate_photo_filename("ABCDEFGHIJKLMNOP")
        assert re.match(r"^photo_\d{8}_\d{6}_ABCDEFGH\.jpg$", result)


def _media_message(**kwargs: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "audio": None,
        "video": None,
        "animation": None,
        "video_note": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


class TestMediaExtension:
    def test_from_known_mime(self) -> None:
        assert _media_extension("audio/mpeg", ".fallback") == ".mp3"
        assert _media_extension("video/mp4", ".fallback") == ".mp4"

    def test_fallback_on_none_or_unknown(self) -> None:
        assert _media_extension(None, ".mp4") == ".mp4"
        assert _media_extension("application/x-nope", ".mp4") == ".mp4"


class TestDescribeMedia:
    def test_audio_with_filename(self) -> None:
        msg = _media_message(
            audio=SimpleNamespace(
                file_name="song.mp3",
                file_id="AUD1",
                file_unique_id="uniq1234",
                file_size=999,
                mime_type="audio/mpeg",
            )
        )
        assert _describe_media(msg) == ("song.mp3", "AUD1", 999, "Audio")

    def test_audio_without_filename_derives_name(self) -> None:
        msg = _media_message(
            audio=SimpleNamespace(
                file_name=None,
                file_id="AUD2",
                file_unique_id="abcdefgh9999",
                file_size=None,
                mime_type="audio/ogg",
            )
        )
        filename, file_id, file_size, label = _describe_media(msg)
        assert (file_id, file_size, label) == ("AUD2", None, "Audio")
        assert re.match(r"^audio_\d{8}_\d{6}_abcdefgh\.ogg$", filename)

    def test_video_uses_original_name(self) -> None:
        msg = _media_message(
            video=SimpleNamespace(
                file_name="clip.mov",
                file_id="VID1",
                file_unique_id="v",
                file_size=42,
                mime_type="video/quicktime",
            )
        )
        assert _describe_media(msg) == ("clip.mov", "VID1", 42, "Video")

    def test_video_note_without_name_or_mime(self) -> None:
        msg = _media_message(
            video_note=SimpleNamespace(file_id="VN1", file_unique_id="xy", file_size=10)
        )
        filename, file_id, _size, label = _describe_media(msg)
        assert (file_id, label) == ("VN1", "Video note")
        assert filename.startswith("video_note_") and filename.endswith(".mp4")

    def test_returns_none_without_media(self) -> None:
        assert _describe_media(_media_message()) is None
