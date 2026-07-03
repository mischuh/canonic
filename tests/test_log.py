"""Tests for canonic/log.py — central logging configuration."""

from __future__ import annotations

import json
import logging

import pytest

from canonic.log import _effective_log_params, configure_logging


@pytest.fixture(autouse=True)
def _reset_canonic_logger():
    """Restore the canonic logger to a clean state after each test."""
    yield
    canonic_logger = logging.getLogger("canonic")
    canonic_logger.handlers.clear()
    canonic_logger.setLevel(logging.WARNING)
    canonic_logger.propagate = True


class TestConfigureLogging:
    def test_sets_level_on_canonic_logger(self):
        configure_logging(level="DEBUG")
        assert logging.getLogger("canonic").level == logging.DEBUG

    def test_defaults_to_warning(self):
        configure_logging()
        assert logging.getLogger("canonic").level == logging.WARNING

    def test_unknown_level_falls_back_to_warning(self):
        configure_logging(level="NOTAREAL")
        assert logging.getLogger("canonic").level == logging.WARNING

    def test_case_insensitive_level(self):
        configure_logging(level="info")
        assert logging.getLogger("canonic").level == logging.INFO

    def test_idempotent_single_handler(self):
        configure_logging(level="INFO")
        configure_logging(level="DEBUG")
        assert logging.getLogger("canonic").level == logging.DEBUG
        assert len(logging.getLogger("canonic").handlers) == 1

    def test_outputs_to_stderr_by_default(self, capsys):
        configure_logging(level="DEBUG")
        logging.getLogger("canonic.test_output").debug("hello logging")
        captured = capsys.readouterr()
        assert "hello logging" in captured.err
        assert captured.out == ""

    def test_propagate_false(self):
        configure_logging()
        assert logging.getLogger("canonic").propagate is False

    def test_file_handler_created(self, tmp_path):
        log_file = tmp_path / "canonic.log"
        configure_logging(level="DEBUG", file=str(log_file))
        handlers = logging.getLogger("canonic").handlers
        assert len(handlers) == 1
        assert isinstance(handlers[0], logging.FileHandler)

    def test_file_receives_output(self, tmp_path):
        log_file = tmp_path / "canonic.log"
        configure_logging(level="DEBUG", file=str(log_file))
        logging.getLogger("canonic.test_file").debug("written to file")
        logging.getLogger("canonic").handlers[0].flush()
        content = log_file.read_text()
        assert "written to file" in content

    def test_json_format_emits_valid_json_to_stderr(self, capsys):
        configure_logging(level="DEBUG", format="json")
        logging.getLogger("canonic.test_json").info("hello json")
        captured = capsys.readouterr()
        assert captured.out == ""
        record = json.loads(captured.err)
        assert record["message"] == "hello json"
        assert record["level"] == "INFO"
        assert record["logger"] == "canonic.test_json"
        assert "timestamp" in record

    def test_json_format_one_object_per_line(self, capsys):
        configure_logging(level="DEBUG", format="json")
        logger = logging.getLogger("canonic.test_json_lines")
        logger.info("first")
        logger.info("second")
        lines = capsys.readouterr().err.strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["message"] == "first"
        assert json.loads(lines[1])["message"] == "second"

    def test_text_format_is_default(self, capsys):
        configure_logging(level="DEBUG")
        logging.getLogger("canonic.test_default").info("plain text")
        captured = capsys.readouterr()
        with pytest.raises(json.JSONDecodeError):
            json.loads(captured.err)


class TestEffectiveLogParams:
    def test_returns_config_values_when_no_env(self, monkeypatch):
        monkeypatch.delenv("CANONIC_LOG_LEVEL", raising=False)
        monkeypatch.delenv("CANONIC_LOG_FILE", raising=False)
        monkeypatch.delenv("CANONIC_LOG_FORMAT", raising=False)
        level, file, format = _effective_log_params("INFO", "/tmp/canonic.log", "json")
        assert level == "INFO"
        assert file == "/tmp/canonic.log"
        assert format == "json"

    def test_env_level_overrides_config(self, monkeypatch):
        monkeypatch.setenv("CANONIC_LOG_LEVEL", "DEBUG")
        monkeypatch.delenv("CANONIC_LOG_FILE", raising=False)
        monkeypatch.delenv("CANONIC_LOG_FORMAT", raising=False)
        level, file, format = _effective_log_params("WARNING", None)
        assert level == "DEBUG"
        assert file is None
        assert format == "text"

    def test_env_file_overrides_config(self, monkeypatch):
        monkeypatch.delenv("CANONIC_LOG_LEVEL", raising=False)
        monkeypatch.setenv("CANONIC_LOG_FILE", "/tmp/override.log")
        monkeypatch.delenv("CANONIC_LOG_FORMAT", raising=False)
        level, file, format = _effective_log_params("WARNING", "/tmp/config.log")
        assert level == "WARNING"
        assert file == "/tmp/override.log"

    def test_env_format_overrides_config(self, monkeypatch):
        monkeypatch.delenv("CANONIC_LOG_LEVEL", raising=False)
        monkeypatch.delenv("CANONIC_LOG_FILE", raising=False)
        monkeypatch.setenv("CANONIC_LOG_FORMAT", "json")
        level, file, format = _effective_log_params("WARNING", None, "text")
        assert format == "json"

    def test_both_env_vars_override(self, monkeypatch):
        monkeypatch.setenv("CANONIC_LOG_LEVEL", "ERROR")
        monkeypatch.setenv("CANONIC_LOG_FILE", "/tmp/env.log")
        monkeypatch.delenv("CANONIC_LOG_FORMAT", raising=False)
        level, file, format = _effective_log_params("INFO", "/tmp/config.log")
        assert level == "ERROR"
        assert file == "/tmp/env.log"

    def test_defaults_when_no_env_and_no_config(self, monkeypatch):
        monkeypatch.delenv("CANONIC_LOG_LEVEL", raising=False)
        monkeypatch.delenv("CANONIC_LOG_FILE", raising=False)
        monkeypatch.delenv("CANONIC_LOG_FORMAT", raising=False)
        level, file, format = _effective_log_params("WARNING", None)
        assert level == "WARNING"
        assert file is None
        assert format == "text"
