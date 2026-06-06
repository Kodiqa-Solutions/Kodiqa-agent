"""Tests for the swallowed-exception logging sweep.

Broad `except Exception: pass` handlers now log at DEBUG via the package logger
("kodiqa") with exc_info, so failures are traceable in error.log when
KODIQA_DEBUG is set — while staying silent at the default WARNING level.
"""

import logging
from unittest.mock import MagicMock

from session_store import SessionStore


def _failing_save_agent():
    """An agent whose session_file write will raise (dir doesn't exist)."""
    agent = MagicMock()
    agent.session_file = "/nonexistent_dir_xyz_123/session.json"
    agent.model = "m"
    agent.cwd = "/"
    agent.history = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
    return agent


class TestSwallowedExceptionLogging:
    def test_logged_at_debug_with_exc_info(self, caplog):
        agent = _failing_save_agent()
        with caplog.at_level(logging.DEBUG, logger="kodiqa"):
            SessionStore(agent).save()  # write fails → swallowed, but logged
        recs = [r for r in caplog.records if "ignored error in save" in r.getMessage()]
        assert recs, "swallowed error was not logged at DEBUG"
        assert recs[0].exc_info is not None  # exception captured for debugging

    def test_silent_at_default_warning_level(self, caplog):
        agent = _failing_save_agent()
        with caplog.at_level(logging.WARNING, logger="kodiqa"):
            SessionStore(agent).save()
        # At WARNING the DEBUG record is filtered → no console/log noise by default
        assert not any("ignored error in save" in r.getMessage() for r in caplog.records)

    def test_save_still_swallows_and_returns(self):
        # Behavior is unchanged: the failure does not propagate.
        SessionStore(_failing_save_agent()).save()  # must not raise


class TestLoggerWiring:
    def test_package_logger_used_by_modules(self):
        import session_store
        import context_builder
        import ollama_manager
        import model_registry
        import agent_team
        for mod in (session_store, context_builder, ollama_manager, model_registry, agent_team):
            assert mod._logger.name == "kodiqa", mod.__name__

    def test_setup_keeps_single_file_handler(self):
        import kodiqa
        kodiqa._setup_error_log()
        kodiqa._setup_error_log()  # idempotent — must not stack file handlers
        fh = [h for h in logging.getLogger("kodiqa").handlers if isinstance(h, logging.FileHandler)]
        assert len(fh) == 1
