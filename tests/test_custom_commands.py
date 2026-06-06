"""Tests for custom prompt-template commands (.kodiqa/commands/*.md)."""

from unittest.mock import MagicMock

from kodiqa import Kodiqa


class TestRenderTemplate:
    def test_arguments_placeholder(self):
        out = Kodiqa._render_template("Review $ARGUMENTS for bugs", "foo.py bar.py")
        assert out == "Review foo.py bar.py for bugs"

    def test_positional_placeholders(self):
        out = Kodiqa._render_template("Compare $1 with $2", "a.py b.py")
        assert out == "Compare a.py with b.py"

    def test_appends_args_when_no_placeholder(self):
        out = Kodiqa._render_template("Summarize this file", "notes.md")
        assert out == "Summarize this file\n\nnotes.md"

    def test_strips_frontmatter(self):
        tpl = "---\ndescription: do a thing\n---\nDo $ARGUMENTS now"
        assert Kodiqa._render_template(tpl, "X") == "Do X now"

    def test_no_args_no_placeholder(self):
        assert Kodiqa._render_template("Just run the tests", "") == "Just run the tests"


class TestDiscovery:
    def _agent(self, tmp_path):
        k = MagicMock()
        k.cwd = str(tmp_path)
        # bind the real instance methods under test
        for m in ("_command_dirs", "_find_custom_command", "_custom_commands"):
            setattr(k, m, getattr(Kodiqa, m).__get__(k))
        k._template_description = Kodiqa._template_description  # staticmethod → set directly
        return k

    def test_find_and_list(self, tmp_path, monkeypatch):
        import kodiqa
        monkeypatch.setattr(kodiqa, "KODIQA_DIR", str(tmp_path / "global"))
        cmd_dir = tmp_path / ".kodiqa" / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "review.md").write_text("---\ndescription: review code\n---\nReview $ARGUMENTS")
        k = self._agent(tmp_path)
        assert k._find_custom_command("review").endswith("review.md")
        assert k._find_custom_command("nope") is None
        cmds = k._custom_commands()
        assert "review" in cmds and cmds["review"][1] == "review code"

    def test_rejects_path_traversal(self, tmp_path):
        k = self._agent(tmp_path)
        assert k._find_custom_command("../secrets") is None
        assert k._find_custom_command("a/b") is None

    def test_project_overrides_global(self, tmp_path, monkeypatch):
        import kodiqa
        gdir = tmp_path / "global" / "commands"
        gdir.mkdir(parents=True)
        (gdir / "x.md").write_text("GLOBAL")
        monkeypatch.setattr(kodiqa, "KODIQA_DIR", str(tmp_path / "global"))
        pdir = tmp_path / ".kodiqa" / "commands"
        pdir.mkdir(parents=True)
        (pdir / "x.md").write_text("PROJECT")
        k = self._agent(tmp_path)
        assert k._find_custom_command("x").startswith(str(pdir))   # project wins


class TestDispatch:
    def test_slash_runs_custom_command(self, tmp_path):
        sent = {}
        k = MagicMock()
        k._COMMAND_HANDLERS = Kodiqa._COMMAND_HANDLERS
        cmd_path = str(tmp_path / "greet.md")
        with open(cmd_path, "w") as f:
            f.write("Say hello to $ARGUMENTS")
        k._find_custom_command = lambda n: cmd_path if n == "greet" else None
        k._run_custom_command = Kodiqa._run_custom_command.__get__(k)
        k._render_template = Kodiqa._render_template
        k._chat = lambda msg: sent.setdefault("msg", msg)
        Kodiqa._handle_slash(k, "/greet World")
        assert sent["msg"] == "Say hello to World"

    def test_registered(self):
        assert "/commands" in Kodiqa._COMMAND_HANDLERS
