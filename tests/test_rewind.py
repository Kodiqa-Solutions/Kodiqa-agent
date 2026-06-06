"""Tests for /rewind — revert all file changes from the last turn(s)."""

import os
from unittest.mock import MagicMock

import kodiqa
from kodiqa import Kodiqa
from actions import (
    do_write_file, do_edit_file, set_batch_mode,
    get_turn_snapshot, clear_turn_snapshot, do_rewind,
)


class TestTurnSnapshot:
    def test_new_file_snapshot_is_none(self, tmp_path):
        set_batch_mode(False)
        clear_turn_snapshot()
        p = str(tmp_path / "new.txt")
        do_write_file(p, "hello")
        assert get_turn_snapshot()[os.path.abspath(p)] is None  # created this turn

    def test_existing_file_keeps_original_first_touch(self, tmp_path):
        set_batch_mode(False)
        f = tmp_path / "f.txt"
        f.write_text("orig")
        clear_turn_snapshot()
        do_edit_file(str(f), "orig", "EDITED")
        do_edit_file(str(f), "EDITED", "AGAIN")  # second edit must not overwrite the snapshot
        assert get_turn_snapshot()[os.path.abspath(str(f))] == "orig"


class TestDoRewind:
    def test_restores_edits_and_deletes_creations(self, tmp_path):
        set_batch_mode(False)
        clear_turn_snapshot()
        edited = tmp_path / "e.txt"
        edited.write_text("v0")
        created = str(tmp_path / "c.txt")
        do_edit_file(str(edited), "v0", "v1")
        do_write_file(created, "brand new")
        res = do_rewind(get_turn_snapshot())
        assert edited.read_text() == "v0"       # restored to pre-turn content
        assert not os.path.isfile(created)       # created file removed
        assert len(res["restored"]) == 1 and len(res["deleted"]) == 1


class TestRewindCommand:
    def _agent(self, tmp_path):
        k = MagicMock()
        k.cwd = str(tmp_path)
        k.auto_commit = False
        k.git_info = None
        return k

    def test_merges_oldest_wins_and_pops(self, tmp_path, monkeypatch):
        f = tmp_path / "a.txt"
        f.write_text("CURRENT")
        ap = os.path.abspath(str(f))
        k = self._agent(tmp_path)
        # oldest turn first; rewinding both should restore the oldest pre-state
        k._turn_stack = [{ap: "turn1-orig"}, {ap: "turn2-orig"}]
        monkeypatch.setattr(kodiqa.Prompt, "ask", lambda *a, **kw: "y")
        Kodiqa._cmd_rewind(k, "2")
        assert f.read_text() == "turn1-orig"
        assert k._turn_stack == []

    def test_single_turn_default(self, tmp_path, monkeypatch):
        f = tmp_path / "a.txt"
        f.write_text("CURRENT")
        ap = os.path.abspath(str(f))
        k = self._agent(tmp_path)
        k._turn_stack = [{ap: "old1"}, {ap: "old2"}]
        monkeypatch.setattr(kodiqa.Prompt, "ask", lambda *a, **kw: "y")
        Kodiqa._cmd_rewind(k, "")  # default: last 1 turn
        assert f.read_text() == "old2"          # only the most recent turn reverted
        assert len(k._turn_stack) == 1

    def test_cancel_leaves_files(self, tmp_path, monkeypatch):
        f = tmp_path / "a.txt"
        f.write_text("CURRENT")
        ap = os.path.abspath(str(f))
        k = self._agent(tmp_path)
        k._turn_stack = [{ap: "orig"}]
        monkeypatch.setattr(kodiqa.Prompt, "ask", lambda *a, **kw: "n")
        Kodiqa._cmd_rewind(k, "")
        assert f.read_text() == "CURRENT"       # untouched
        assert len(k._turn_stack) == 1          # not popped

    def test_nothing_to_rewind(self, tmp_path):
        k = self._agent(tmp_path)
        k._turn_stack = []
        Kodiqa._cmd_rewind(k, "")               # must not raise
        k.console.print.assert_called()

    def test_registered_command(self):
        assert "/rewind" in Kodiqa._COMMAND_HANDLERS
