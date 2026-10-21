"""Behavior tests for pomo/service.py: platform detection and service
file generation. Subprocess calls (launchctl/systemctl) are mocked;
pure functions are tested directly."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from pomo import service


class TestPlatform:
    def test_platform_darwin(self):
        # When: running on macOS
        with patch.object(service.sys, "platform", "darwin"):
            # Then: _platform returns "macos"
            assert service._platform() == "macos"

    def test_platform_linux(self):
        # When: running on Linux
        with patch.object(service.sys, "platform", "linux"):
            # Then: _platform returns "linux"
            assert service._platform() == "linux"

    def test_platform_other_exits(self):
        # When: running on an unsupported OS
        with patch.object(service.sys, "platform", "win32"):
            # Then: _platform exits with error
            with pytest.raises(SystemExit):
                service._platform()


class TestBinary:
    def test_binary_found(self):
        # When: pomo-agent is on PATH
        with patch.object(service.shutil, "which", return_value="/usr/bin/pomo-agent"):
            path = service._binary(server=False)
            # Then: a truthy path is returned
            assert path == "/usr/bin/pomo-agent"

    def test_binary_not_found_exits(self):
        # When: binary is not on PATH
        with patch.object(service.shutil, "which", return_value=None):
            with patch.object(service.Path, "exists", return_value=False):
                # Then: _binary exits with error message
                with pytest.raises(SystemExit):
                    service._binary(server=False)

    def test_binary_server(self):
        # When: requesting server binary
        with patch.object(service.shutil, "which", return_value="/usr/bin/pomo-server"):
            path = service._binary(server=True)
            # Then: returns the path and called which with correct name
            assert path == "/usr/bin/pomo-server"


class TestEnv:
    def test_pomo_env_captures_pomo_vars(self, monkeypatch):
        # Given: POMO_* and non-POMO env vars are set
        monkeypatch.setenv("POMO_SERVER_URL", "http://example:8787")
        monkeypatch.setenv("POMO_TOKEN", "secret")
        monkeypatch.setenv("OTHER_VAR", "ignored")
        # When: _pomo_env is called
        env = service._pomo_env()
        # Then: only POMO_* vars are captured
        assert "POMO_SERVER_URL" in env
        assert "POMO_TOKEN" in env
        assert "OTHER_VAR" not in env

    def test_pomo_env_empty_when_no_vars(self, monkeypatch):
        # Given: no POMO_* vars set
        for key in list(os.environ):
            if key.startswith("POMO_"):
                monkeypatch.delenv(key)
        # When: _pomo_env is called
        env = service._pomo_env()
        # Then: empty dict
        assert env == {}


class TestPlistContent:
    def test_plist_content_contains_label(self):
        # Given: binary is on PATH
        with patch.object(service, "_binary", return_value="/usr/bin/pomo-agent"):
            content = service._macos_plist_content(server=False)
            # Then: plist contains the expected label
            assert "pomo.agent" in content

    def test_plist_content_contains_binary(self):
        # Given: binary is on PATH
        with patch.object(service, "_binary", return_value="/usr/bin/pomo-agent"):
            content = service._macos_plist_content(server=False)
            # Then: plist contains the binary path
            assert "/usr/bin/pomo-agent" in content

    def test_plist_content_contains_env(self):
        # Given: POMO_SERVER_URL is set
        with patch.object(service, "_binary", return_value="/usr/bin/pomo-agent"):
            with patch.object(
                service, "_pomo_env", return_value={"POMO_SERVER_URL": "http://s:8787"}
            ):
                content = service._macos_plist_content(server=False)
                # Then: plist contains the env var
                assert "POMO_SERVER_URL" in content
                assert "http://s:8787" in content

    def test_plist_content_server_label(self):
        # Given: server binary
        with patch.object(service, "_binary", return_value="/usr/bin/pomo-server"):
            content = service._macos_plist_content(server=True)
            # Then: plist uses the server label
            assert "pomo.server" in content


class TestLinuxServiceContent:
    def test_service_content_contains_binary(self):
        # Given: binary is on PATH
        with patch.object(service, "_binary", return_value="/usr/bin/pomo-agent"):
            content = service._linux_service_content(server=False)
            # Then: service file contains the binary path
            assert "/usr/bin/pomo-agent" in content

    def test_service_content_contains_env(self):
        # Given: POMO_TOKEN is set
        with patch.object(service, "_binary", return_value="/usr/bin/pomo-agent"):
            with patch.object(service, "_pomo_env", return_value={"POMO_TOKEN": "secret"}):
                content = service._linux_service_content(server=False)
                # Then: service file contains the env var
                assert "POMO_TOKEN" in content

    def test_service_content_server(self):
        # Given: server binary
        with patch.object(service, "_binary", return_value="/usr/bin/pomo-server"):
            content = service._linux_service_content(server=True)
            # Then: service file uses server name
            assert "pomo-server" in content
            assert "sync server" in content


class TestPath:
    def test_macos_plist_path(self, isolated):
        # When: on macOS, requesting the agent plist path
        with patch.object(service.Path, "home", return_value=isolated):
            path = service._macos_plist(server=False)
            # Then: path is under ~/Library/LaunchAgents
            expected = isolated / "Library" / "LaunchAgents" / "pomo.agent.plist"
            assert path == expected

    def test_linux_service_path(self, isolated):
        # When: on Linux, requesting the agent service path
        with patch.object(service.Path, "home", return_value=isolated):
            path = service._linux_service_path(server=False)
            # Then: path is under ~/.config/systemd/user
            expected = isolated / ".config" / "systemd" / "user" / "pomo-agent.service"
            assert path == expected
