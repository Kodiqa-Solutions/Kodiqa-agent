"""Tests for terminal drag-and-drop file/image attachment."""

import base64
from unittest.mock import MagicMock

from kodiqa import Kodiqa

# Smallest valid 1x1 PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _agent(tmp_path):
    k = MagicMock()
    k.cwd = str(tmp_path)
    k.console = MagicMock()
    k._pending_files = []
    k._pending_images = []
    # bind the real methods under test
    for m in ("_split_dropped_paths", "_extract_dropped_files", "_maybe_attach_dropped",
              "_read_image_for_embed", "_read_file_for_embed"):
        setattr(k, m, getattr(Kodiqa, m).__get__(k))
    return k


class TestSplitPaths:
    def test_unescapes_spaces(self, tmp_path):
        k = _agent(tmp_path)
        assert k._split_dropped_paths(r"/a/Screenshot\ 2026\ at\ 4.png") == \
            ["/a/Screenshot 2026 at 4.png"]

    def test_quoted_path(self, tmp_path):
        k = _agent(tmp_path)
        assert k._split_dropped_paths('"/a/my file.png"') == ["/a/my file.png"]

    def test_multiple_paths(self, tmp_path):
        k = _agent(tmp_path)
        assert k._split_dropped_paths(r"/a/one.png /b/two.png") == ["/a/one.png", "/b/two.png"]


class TestExtractDropped:
    def test_dropped_image_with_escaped_spaces(self, tmp_path):
        img = tmp_path / "Screen shot.png"
        img.write_bytes(_PNG)
        k = _agent(tmp_path)
        escaped = str(img).replace(" ", r"\ ")
        files, images = k._extract_dropped_files(escaped)
        assert files == []
        assert len(images) == 1 and images[0]["media_type"] == "image/png"

    def test_dropped_text_file(self, tmp_path):
        f = tmp_path / "notes.txt"
        f.write_text("hello")
        k = _agent(tmp_path)
        files, images = k._extract_dropped_files(str(f))
        assert images == [] and len(files) == 1

    def test_slash_command_not_treated_as_drop(self, tmp_path):
        k = _agent(tmp_path)
        assert k._extract_dropped_files("/model") == ([], [])
        assert k._extract_dropped_files("/help") == ([], [])

    def test_normal_sentence_with_path_not_a_drop(self, tmp_path):
        f = tmp_path / "x.png"
        f.write_bytes(_PNG)
        k = _agent(tmp_path)
        # leading prose → not a pure drop, left for normal @/inline handling
        assert k._extract_dropped_files(f"look at {f}") == ([], [])

    def test_nonexistent_path_is_not_a_drop(self, tmp_path):
        k = _agent(tmp_path)
        assert k._extract_dropped_files("/no/such/file.png") == ([], [])

    def test_at_reference_left_alone(self, tmp_path):
        k = _agent(tmp_path)
        assert k._extract_dropped_files("@notes.txt") == ([], [])


class TestMaybeAttach:
    def test_attaches_and_returns_true(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(_PNG)
        k = _agent(tmp_path)
        assert k._maybe_attach_dropped(str(img)) is True
        assert len(k._pending_images) == 1

    def test_accumulates_across_drops(self, tmp_path):
        (tmp_path / "a.png").write_bytes(_PNG)
        (tmp_path / "b.png").write_bytes(_PNG)
        k = _agent(tmp_path)
        k._maybe_attach_dropped(str(tmp_path / "a.png"))
        k._maybe_attach_dropped(str(tmp_path / "b.png"))
        assert len(k._pending_images) == 2

    def test_non_drop_returns_false(self, tmp_path):
        k = _agent(tmp_path)
        assert k._maybe_attach_dropped("what is this?") is False
        assert k._pending_images == []
