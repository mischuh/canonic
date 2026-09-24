"""Tests for canonic/log.py — central logging configuration."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING

import pytest

from canonic.log import _effective_log_params, configure_logging, rotate_file_on_start

if TYPE_CHECKING:
    from pathlib import Path


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


class TestRotation:
    def test_max_bytes_uses_rotating_handler(self, tmp_path: Path):
        configure_logging(level="INFO", file=str(tmp_path / "c.log"), max_bytes=200, backup_count=2)
        handlers = logging.getLogger("canonic").handlers
        assert isinstance(handlers[0], RotatingFileHandler)

    def test_file_rotates_and_keeps_backup_count(self, tmp_path: Path):
        log_file = tmp_path / "c.log"
        configure_logging(level="INFO", file=str(log_file), max_bytes=200, backup_count=2)
        for i in range(60):
            logging.getLogger("canonic.rot").info("message number %d", i)
        assert log_file.exists()
        assert (tmp_path / "c.log.1").exists()
        assert (tmp_path / "c.log.2").exists()
        assert not (tmp_path / "c.log.3").exists()

    def test_zero_max_bytes_keeps_plain_handler(self, tmp_path: Path):
        configure_logging(level="INFO", file=str(tmp_path / "c.log"))
        handler = logging.getLogger("canonic").handlers[0]
        assert not isinstance(handler, RotatingFileHandler)


class TestRotateFileOnStart:
    def test_below_limit_untouched(self, tmp_path: Path):
        path = tmp_path / "mcp.log"
        path.write_text("x" * 10)
        rotate_file_on_start(path, max_bytes=100, backup_count=3)
        assert path.read_text() == "x" * 10
        assert not (tmp_path / "mcp.log.1").exists()

    def test_missing_file_is_noop(self, tmp_path: Path):
        rotate_file_on_start(tmp_path / "mcp.log", max_bytes=100, backup_count=3)

    def test_disabled_with_zero(self, tmp_path: Path):
        path = tmp_path / "mcp.log"
        path.write_text("x" * 500)
        rotate_file_on_start(path, max_bytes=0, backup_count=3)
        assert path.exists()

    def test_shifts_backups_and_drops_oldest(self, tmp_path: Path):
        path = tmp_path / "mcp.log"
        for name, text in [("mcp.log.2", "old2"), ("mcp.log.1", "old1"), ("mcp.log", "z" * 500)]:
            (tmp_path / name).write_text(text)
        rotate_file_on_start(path, max_bytes=100, backup_count=2)
        assert not path.exists()
        assert (tmp_path / "mcp.log.1").read_text() == "z" * 500
        assert (tmp_path / "mcp.log.2").read_text() == "old1"
        assert not (tmp_path / "mcp.log.3").exists()
