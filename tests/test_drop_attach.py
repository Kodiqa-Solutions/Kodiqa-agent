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
              "_read_image_for_embed", "_read_file_for_embed",
              "_resolve_dropped_path", "_looks_like_dropped_path",
              "_attach_summary", "_attach_pasted_paths"):
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


class TestPasteTimeAttach:
    """The robust path: read the file the instant it's dropped (bracketed paste),
    not at Enter — so a macOS screenshot temp is read before it's deleted."""

    def test_dropped_image_attached_on_paste(self, tmp_path):
        img = tmp_path / "Screen shot.png"
        img.write_bytes(_PNG)
        k = _agent(tmp_path)
        summary = k._attach_pasted_paths(str(img).replace(" ", r"\ "))
        assert summary is not None and "attached" in summary
        assert len(k._pending_images) == 1

    def test_ordinary_paste_returns_none(self, tmp_path):
        k = _agent(tmp_path)
        assert k._attach_pasted_paths("def foo():\n    return 1\n") is None
        assert k._pending_images == [] and k._pending_files == []

    def test_vanished_screenshot_falls_back_to_desktop(self, tmp_path, monkeypatch):
        # macOS deletes the preview temp but saves the same-named file to the Desktop.
        desktop = tmp_path / "Desktop"
        desktop.mkdir()
        (desktop / "Screen Shot.png").write_bytes(_PNG)
        monkeypatch.setattr("os.path.expanduser",
                            lambda p: p.replace("~", str(tmp_path)) if p.startswith("~") else p)
        k = _agent(tmp_path)
        gone = r"/var/folders/x/T/TemporaryItems/NSIRD_x/Screen\ Shot.png"  # temp no longer exists
        assert k._maybe_attach_dropped(gone) is True
        assert len(k._pending_images) == 1  # recovered from Desktop

    def test_vanished_with_no_fallback_shows_message_not_command(self, tmp_path):
        k = _agent(tmp_path)
        gone = r"/var/folders/x/T/TemporaryItems/NSIRD_x/Screen\ Shot.png"
        # nothing on Desktop/cwd → handled (True) with a guidance message, NOT passed
        # to the slash-command handler
        assert k._maybe_attach_dropped(gone) is True
        assert k._pending_images == []
        printed = " ".join(str(c) for c in k.console.print.call_args_list)
        assert "!img" in printed
