"""The README badges are shown on GitHub AND on the PyPI project page, so a stale
number is a public inaccuracy. These are the same kind of registry-integrity guard as
TestCommandRegistry: they fail the build when a claim drifts from reality.

The test-count claim is asserted as "never overstated" rather than exact, so adding a
test doesn't force a README edit in the same commit — but claiming more tests than
exist can never ship.
"""

import re
from pathlib import Path

from kodiqa import Kodiqa

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
TESTS_DIR = Path(__file__).resolve().parent


def _actual_test_count():
    return sum(len(re.findall(r"^\s*def test_", p.read_text(encoding="utf-8"), re.M))
               for p in TESTS_DIR.glob("test_*.py"))


class TestReadmeClaims:
    def test_command_badge_matches_the_registry(self):
        badge = re.search(r"commands-(\d+)-orange", README)
        assert badge, "commands badge missing from README"
        assert int(badge.group(1)) == len(Kodiqa._COMMAND_SPECS)

    def test_command_tagline_matches_the_registry(self):
        tagline = re.search(r"(\d+) slash commands", README)
        assert tagline, "slash-command tagline missing from README"
        assert int(tagline.group(1)) == len(Kodiqa._COMMAND_SPECS)

    def test_test_badge_is_never_overstated(self):
        badge = re.search(r"tests-(\d+)%20passing", README)
        assert badge, "tests badge missing from README"
        claimed, actual = int(badge.group(1)), _actual_test_count()
        assert claimed <= actual, (
            f"README claims {claimed} tests but only {actual} exist — "
            "update the badge before releasing")

    def test_prose_test_counts_are_never_overstated(self):
        actual = _actual_test_count()
        for claimed in re.findall(r"(\d+) tests", README):
            assert int(claimed) <= actual, (
                f"README claims {claimed} tests but only {actual} exist")

    def test_provider_count_is_consistent(self):
        from config import OPENAI_COMPAT_PROVIDERS
        total = len(OPENAI_COMPAT_PROVIDERS) + 1  # + Claude (not an OpenAI-compat entry)
        badge = re.search(r"providers-(\d+)-cyan", README)
        assert badge and int(badge.group(1)) == total
        for claimed in re.findall(r"all (\d+) providers", README):
            assert int(claimed) == total
