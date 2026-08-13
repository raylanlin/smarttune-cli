"""
tests/test_mcp_security.py

Security validation tests for the MCP server's path validation logic.
"""

from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="mcp extra requires Python >=3.10")

from smarttune.mcp_server import (
    PathValidationError,
    _ALLOWED_EXTENSIONS,
    validate_log_path,
)


@pytest.fixture
def temp_log(tmp_path):
    """Create a small temporary .bin file inside an allowed root."""
    log_file = tmp_path / "test_flight.bin"
    log_file.write_bytes(b"\x00" * 1024)
    return log_file


@pytest.fixture(autouse=True)
def allow_tmp(monkeypatch, tmp_path):
    """Set SMARTTUNE_MCP_ALLOWED_ROOTS to include tmp_path so test files pass root check."""
    monkeypatch.setenv("SMARTTUNE_MCP_ALLOWED_ROOTS", str(tmp_path))


class TestAllowedExtensions:
    """Verify that allowed extensions are accepted and others rejected."""

    @pytest.mark.parametrize("ext", [".bin", ".log", ".bbl", ".bfl", ".ulg"])
    def test_allowed_extension_accepted(self, tmp_path, ext):
        f = tmp_path / f"flight{ext}"
        f.write_bytes(b"\x00" * 512)
        result = validate_log_path(str(f))
        assert result.is_file()

    @pytest.mark.parametrize("ext", [".txt", ".csv", ".py", ".json", ".exe", ".sh", ".zip"])
    def test_disallowed_extension_rejected(self, tmp_path, ext):
        f = tmp_path / f"flight{ext}"
        f.write_bytes(b"\x00" * 512)
        with pytest.raises(PathValidationError, match="Disallowed file extension"):
            validate_log_path(str(f))


class TestPathTraversal:
    """Verify that paths outside allowed roots are rejected."""

    def test_path_outside_allowed_roots(self, monkeypatch, tmp_path):
        # Only allow a specific subdirectory
        allowed = tmp_path / "safe"
        allowed.mkdir()
        monkeypatch.setenv("SMARTTUNE_MCP_ALLOWED_ROOTS", str(allowed))

        unsafe = tmp_path / "unsafe" / "flight.bin"
        unsafe.parent.mkdir()
        unsafe.write_bytes(b"\x00" * 512)

        with pytest.raises(PathValidationError, match="outside allowed directories"):
            validate_log_path(str(unsafe))

    def test_traversal_dotdot_blocked(self, monkeypatch, tmp_path):
        allowed = tmp_path / "safe"
        allowed.mkdir()
        monkeypatch.setenv("SMARTTUNE_MCP_ALLOWED_ROOTS", str(allowed))

        # Create a file outside via ..
        outside_file = tmp_path / "secret.bin"
        outside_file.write_bytes(b"\x00" * 512)

        traversal = str(allowed / ".." / "secret.bin")
        with pytest.raises(PathValidationError, match="outside allowed directories"):
            validate_log_path(traversal)

    def test_nonexistent_path_rejected(self):
        with pytest.raises(PathValidationError, match="Cannot resolve path"):
            validate_log_path("/nonexistent/path/to/flight.bin")

    def test_directory_rejected(self, tmp_path):
        d = tmp_path / "subdir.bin"
        d.mkdir()
        with pytest.raises(PathValidationError, match="Not a regular file"):
            validate_log_path(str(d))

    def test_etc_passwd_rejected(self, tmp_path, monkeypatch):
        """Verify /etc/passwd is blocked even if it exists."""
        monkeypatch.setenv("SMARTTUNE_MCP_ALLOWED_ROOTS", str(tmp_path))
        with pytest.raises(PathValidationError):
            validate_log_path("/etc/passwd")


class TestSymlinkEscape:
    """Verify that symlinks escaping allowed root are rejected."""

    def test_symlink_escape_blocked(self, monkeypatch, tmp_path):
        allowed = tmp_path / "safe"
        allowed.mkdir()
        monkeypatch.setenv("SMARTTUNE_MCP_ALLOWED_ROOTS", str(allowed))

        # Target outside allowed root
        target = tmp_path / "outside.bin"
        target.write_bytes(b"\x00" * 512)

        link = allowed / "sneaky.bin"
        link.symlink_to(target)

        with pytest.raises(PathValidationError, match="outside allowed directories"):
            validate_log_path(str(link))


class TestFileSize:
    """Verify that oversized files are rejected."""

    def test_oversized_file_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SMARTTUNE_MCP_MAX_FILE_MB", "0.001")  # ~1 KB limit
        big = tmp_path / "big_log.bin"
        big.write_bytes(b"\x00" * (2 * 1024))  # 2 KB

        with pytest.raises(PathValidationError, match="File too large"):
            validate_log_path(str(big))

    def test_small_file_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SMARTTUNE_MCP_MAX_FILE_MB", "1")
        small = tmp_path / "small_log.bin"
        small.write_bytes(b"\x00" * 512)
        result = validate_log_path(str(small))
        assert result.is_file()
