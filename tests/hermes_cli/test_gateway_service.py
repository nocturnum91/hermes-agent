"""Tests for gateway service management helpers."""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pwd = pytest.importorskip("pwd")

import hermes_cli.gateway as gateway_cli
from gateway import status
from gateway.restart import (
    DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT,
    GATEWAY_SERVICE_RESTART_EXIT_CODE,
)


class TestUserSystemdPrivateSocketPreflight:
    def test_preflight_accepts_private_socket_without_dbus_bus(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "_ensure_user_systemd_env", lambda: None)
        monkeypatch.setattr(gateway_cli, "_user_dbus_socket_path", lambda: Path("/tmp/missing-bus"))
        monkeypatch.setattr(gateway_cli, "_user_systemd_private_socket_path", lambda: Path("/tmp/private-socket"))
        monkeypatch.setattr(Path, "exists", lambda self: str(self) == "/tmp/private-socket")

        gateway_cli._preflight_user_systemd(auto_enable_linger=False)

    def test_wait_for_user_dbus_socket_accepts_private_socket(self, monkeypatch):
        calls = []
        monkeypatch.setattr(gateway_cli, "_ensure_user_systemd_env", lambda: calls.append("env"))
        monkeypatch.setattr(gateway_cli, "_user_dbus_socket_path", lambda: Path("/tmp/missing-bus"))
        monkeypatch.setattr(gateway_cli, "_user_systemd_private_socket_path", lambda: Path("/tmp/private-socket"))
        monkeypatch.setattr(Path, "exists", lambda self: str(self) == "/tmp/private-socket")

        assert gateway_cli._wait_for_user_dbus_socket(timeout=0.1) is True
        assert calls == ["env"]


class TestSystemdServiceRefresh:
    def test_systemd_install_repairs_outdated_unit_without_force(self, tmp_path, monkeypatch):
        unit_path = tmp_path / "hermes-gateway.service"
        unit_path.write_text("old unit\n", encoding="utf-8")

        monkeypatch.setattr(gateway_cli, "get_systemd_unit_path", lambda system=False: unit_path)
        monkeypatch.setattr(gateway_cli, "generate_systemd_unit", lambda system=False, run_as_user=None: "new unit\n")

        calls = []

        def fake_run(cmd, check=True, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        gateway_cli.systemd_install()

        assert unit_path.read_text(encoding="utf-8") == "new unit\n"
        assert calls[:2] == [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", gateway_cli.get_service_name()],
        ]

    def test_systemd_start_refreshes_outdated_unit(self, tmp_path, monkeypatch):
        unit_path = tmp_path / "hermes-gateway.service"
        unit_path.write_text("old unit\n", encoding="utf-8")

        monkeypatch.setattr(gateway_cli, "get_systemd_unit_path", lambda system=False: unit_path)
        monkeypatch.setattr(gateway_cli, "generate_systemd_unit", lambda system=False, run_as_user=None: "new unit\n")
        monkeypatch.setattr(gateway_cli, "_preflight_user_systemd", lambda **_: None)

        calls = []

        def fake_run(cmd, check=True, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        gateway_cli.systemd_start()

        assert unit_path.read_text(encoding="utf-8") == "new unit\n"
        assert calls[:2] == [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "start", gateway_cli.get_service_name()],
        ]

    def test_systemd_restart_refreshes_outdated_unit(self, tmp_path, monkeypatch):
        unit_path = tmp_path / "hermes-gateway.service"
        unit_path.write_text("old unit\n", encoding="utf-8")

        monkeypatch.setattr(gateway_cli, "get_systemd_unit_path", lambda system=False: unit_path)
        monkeypatch.setattr(gateway_cli, "generate_systemd_unit", lambda system=False, run_as_user=None: "new unit\n")
        monkeypatch.setattr(gateway_cli, "_preflight_user_systemd", lambda **_: None)

        calls = []
        monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
        monkeypatch.setattr(gateway_cli, "_recover_pending_systemd_restart", lambda system=False, previous_pid=None: False)
        monkeypatch.setattr(
            gateway_cli,
            "_wait_for_systemd_service_restart",
            lambda system=False, previous_pid=None: calls.append(("wait", system, previous_pid)) or True,
        )

        def fake_run(cmd, check=True, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        gateway_cli.systemd_restart()

        assert unit_path.read_text(encoding="utf-8") == "new unit\n"
        assert calls[:5] == [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "show", gateway_cli.get_service_name(), "--no-pager", "--property", "ActiveState,SubState,Result,ExecMainStatus,MainPID"],
            ["systemctl", "--user", "reset-failed", gateway_cli.get_service_name()],
            ["systemctl", "--user", "restart", gateway_cli.get_service_name()],
            ("wait", False, None),
        ]

    def test_systemd_stop_marks_running_gateway_as_planned_stop(self, monkeypatch):
        calls = []
        markers = []

        monkeypatch.setattr(gateway_cli, "_select_systemd_scope", lambda system=False: False)
        monkeypatch.setattr(gateway_cli, "_require_service_installed", lambda action, system=False: None)
        monkeypatch.setattr(status, "get_running_pid", lambda cleanup_stale=True: 321)
        monkeypatch.setattr(
            status,
            "write_planned_stop_marker",
            lambda pid: markers.append(pid) or True,
        )

        def fake_run_systemctl(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli, "_run_systemctl", fake_run_systemctl)

        gateway_cli.systemd_stop()

        assert markers == [321]
        assert calls == [["stop", gateway_cli.get_service_name()]]

    def test_systemd_stop_timeout_prints_status_guidance(self, monkeypatch, capsys):
        markers = []

        monkeypatch.setattr(gateway_cli, "_select_systemd_scope", lambda system=False: False)
        monkeypatch.setattr(gateway_cli, "_require_service_installed", lambda action, system=False: None)
        monkeypatch.setattr(status, "get_running_pid", lambda cleanup_stale=True: 321)
        monkeypatch.setattr(
            status,
            "write_planned_stop_marker",
            lambda pid: markers.append(pid) or True,
        )

        def fake_run_systemctl(args, **kwargs):
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))

        monkeypatch.setattr(gateway_cli, "_run_systemctl", fake_run_systemctl)

        gateway_cli.systemd_stop()

        assert markers == [321]
        output = capsys.readouterr().out
        assert "still stopping after 90s" in output
        assert "hermes gateway status" in output

    def test_systemd_restart_timeout_prints_status_guidance(self, monkeypatch, capsys):
        """`hermes gateway restart` must not surface a raw TimeoutExpired traceback.

        The dashboard spawns `hermes gateway restart` in the background; when a
        wedged adapter websocket pushes drain past the 90s CLI timeout, the
        dashboard would previously show a Python traceback (issue #19937
        follow-up: the same failure mode applies to restart, not just stop).
        """
        monkeypatch.setattr(gateway_cli, "_select_systemd_scope", lambda system=False: False)
        monkeypatch.setattr(gateway_cli, "_require_service_installed", lambda action, system=False: None)
        monkeypatch.setattr(gateway_cli, "_preflight_user_systemd", lambda: None)
        monkeypatch.setattr(gateway_cli, "refresh_systemd_unit_if_needed", lambda system=False: None)
        monkeypatch.setattr(status, "get_running_pid", lambda cleanup_stale=True: None)
        monkeypatch.setattr(gateway_cli, "_systemd_main_pid", lambda system=False: None)
        monkeypatch.setattr(
            gateway_cli,
            "_recover_pending_systemd_restart",
            lambda system=False, previous_pid=None: False,
        )
        monkeypatch.setattr(
            gateway_cli,
            "_systemd_service_is_start_limited",
            lambda system=False: False,
        )

        def fake_run_systemctl(args, **kwargs):
            # reset-failed is a pre-step (check=False, 30s) — let it pass.
            if args and args[0] == "reset-failed":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))

        monkeypatch.setattr(gateway_cli, "_run_systemctl", fake_run_systemctl)

        gateway_cli.systemd_restart()

        output = capsys.readouterr().out
        assert "still restarting after 90s" in output
        assert "hermes gateway status" in output

    def test_run_gateway_refreshes_outdated_unit_on_boot(self, tmp_path, monkeypatch):
        """run_gateway() should refresh the systemd unit on boot so that
        restart settings take effect even when the process was respawned
        via exit-code-75 (bypassing `hermes gateway restart`)."""
        unit_path = tmp_path / "hermes-gateway.service"
        unit_path.write_text("old unit\n", encoding="utf-8")

        monkeypatch.setattr(gateway_cli, "get_systemd_unit_path", lambda system=False: unit_path)
        monkeypatch.setattr(gateway_cli, "generate_systemd_unit", lambda system=False, run_as_user=None: "new unit\n")
        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: True)

        calls = []

        def fake_run(cmd, check=True, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        # Prevent run_gateway from actually starting the gateway
        async def fake_start_gateway(**kwargs):
            return True

        monkeypatch.setattr("gateway.run.start_gateway", fake_start_gateway)

        gateway_cli.run_gateway()

        assert unit_path.read_text(encoding="utf-8") == "new unit\n"
        assert ["systemctl", "--user", "daemon-reload"] in calls

    def test_refresh_refuses_to_bake_pytest_tmpdir_into_real_user_unit(
        self, tmp_path, monkeypatch
    ):
        """Defense in depth: ``refresh_systemd_unit_if_needed()`` runs every
        time ``run_gateway()`` starts. The user-scope unit path resolves
        under ``Path.home()`` (NOT sandboxed by conftest), and
        ``generate_systemd_unit()`` bakes ``HERMES_HOME`` into the unit's
        ``Environment=`` line. Without this guard, any test that drives
        ``run_gateway()`` end-to-end on a real Linux dev box silently
        rewrites the developer's installed gateway unit with a
        ``/tmp/pytest-of-.../hermes_test`` HERMES_HOME — silently breaking
        their gateway on the next boot. The guard sniffs the generated
        unit body for tmpdir markers and refuses the write. Tests that
        legitimately exercise the refresh flow patch
        ``generate_systemd_unit`` to return synthetic content that doesn't
        carry those markers.
        """
        unit_path = tmp_path / "hermes-gateway.service"
        unit_path.write_text("old unit\n", encoding="utf-8")

        monkeypatch.setattr(
            gateway_cli, "get_systemd_unit_path", lambda system=False: unit_path
        )
        # Realistic generated unit referencing a pytest tmpdir HERMES_HOME
        polluted_unit = (
            "[Service]\n"
            'Environment="HERMES_HOME=/tmp/pytest-of-alice/pytest-42/'
            'popen-gw0/test_x/hermes_test"\n'
        )
        monkeypatch.setattr(
            gateway_cli,
            "generate_systemd_unit",
            lambda system=False, run_as_user=None: polluted_unit,
        )

        # If the guard fails, daemon-reload would be called — record it.
        ran = []

        def fake_run(cmd, check=True, **kwargs):
            ran.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        result = gateway_cli.refresh_systemd_unit_if_needed(system=False)

        assert result is False, "refresh should refuse to write a polluted unit"
        assert (
            unit_path.read_text(encoding="utf-8") == "old unit\n"
        ), "installed unit must be left untouched"
        assert not any(
            "daemon-reload" in str(c) for c in ran
        ), "daemon-reload must not run when write was refused"


class TestRequireServiceInstalled:
    def test_exits_with_install_hint_when_unit_missing(self, tmp_path, monkeypatch, capsys):
        unit_path = tmp_path / "hermes-gateway.service"
        monkeypatch.setattr(gateway_cli, "get_systemd_unit_path", lambda system=False: unit_path)

        with pytest.raises(SystemExit) as exc_info:
            gateway_cli._require_service_installed("start")

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "not installed" in out
        assert "hermes gateway install" in out

    def test_passes_when_unit_exists(self, tmp_path, monkeypatch):
        unit_path = tmp_path / "hermes-gateway.service"
        unit_path.write_text("[Unit]\n", encoding="utf-8")
        monkeypatch.setattr(gateway_cli, "get_systemd_unit_path", lambda system=False: unit_path)

        gateway_cli._require_service_installed("start")


class TestGeneratedSystemdUnits:
    def _expected_timeout_stop_sec(self) -> str:
        timeout = int(max(60, DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT) + 30)
        return f"TimeoutStopSec={timeout}"

    def test_user_unit_avoids_recursive_execstop_and_uses_extended_stop_timeout(self, monkeypatch):
        monkeypatch.setattr(
            gateway_cli,
            "_get_restart_drain_timeout",
            lambda: DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT,
        )
        unit = gateway_cli.generate_systemd_unit(system=False)

        assert "ExecStart=" in unit
        assert "ExecStop=" not in unit
        assert "ExecReload=/bin/kill -USR1 $MAINPID" in unit
        assert f"RestartForceExitStatus={GATEWAY_SERVICE_RESTART_EXIT_CODE}" in unit
        # TimeoutStopSec must exceed the default drain_timeout (180s) so
        # systemd doesn't SIGKILL the cgroup before post-interrupt cleanup
        # (tool subprocess kill, adapter disconnect) runs — issue #8202.
        assert self._expected_timeout_stop_sec() in unit

    def test_user_unit_includes_resolved_node_directory_in_path(self, monkeypatch):
        monkeypatch.setattr(gateway_cli.shutil, "which", lambda cmd: "/home/test/.nvm/versions/node/v24.14.0/bin/node" if cmd == "node" else None)

        unit = gateway_cli.generate_systemd_unit(system=False)

        assert "/home/test/.nvm/versions/node/v24.14.0/bin" in unit

    def test_user_unit_includes_wsl_windows_interop_paths(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "is_wsl", lambda: True)
        monkeypatch.setenv(
            "PATH",
            "/usr/local/bin:/mnt/c/WINDOWS/system32:/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/",
        )
        monkeypatch.setattr(gateway_cli.shutil, "which", lambda cmd: None)

        unit = gateway_cli.generate_systemd_unit(system=False)

        assert "/mnt/c/WINDOWS/system32" in unit
        assert "/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/" in unit

    def test_user_unit_omits_windows_interop_paths_outside_wsl(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "is_wsl", lambda: False)
        monkeypatch.setenv("PATH", "/usr/local/bin:/mnt/c/WINDOWS/system32")
        monkeypatch.setattr(gateway_cli.shutil, "which", lambda cmd: None)

        unit = gateway_cli.generate_systemd_unit(system=False)

        assert "/mnt/c/WINDOWS/system32" not in unit

    def test_system_unit_includes_wsl_windows_interop_paths(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "is_wsl", lambda: True)
        monkeypatch.setattr(
            gateway_cli,
            "_system_service_identity",
            lambda run_as_user=None: ("alice", "alice", "/home/alice"),
        )
        monkeypatch.setattr(gateway_cli, "_hermes_home_for_target_user", lambda home: "/home/alice/.hermes")
        monkeypatch.setenv("PATH", "/usr/local/bin:/mnt/c/WINDOWS/system32")
        monkeypatch.setattr(gateway_cli.shutil, "which", lambda cmd: None)

        unit = gateway_cli.generate_systemd_unit(system=True, run_as_user="alice")

        assert "/mnt/c/WINDOWS/system32" in unit

    def test_system_unit_avoids_recursive_execstop_and_uses_extended_stop_timeout(self, monkeypatch):
        monkeypatch.setattr(
            gateway_cli,
            "_get_restart_drain_timeout",
            lambda: DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT,
        )
        unit = gateway_cli.generate_systemd_unit(system=True)

        assert "ExecStart=" in unit
        assert "ExecStop=" not in unit
        assert "ExecReload=/bin/kill -USR1 $MAINPID" in unit
        assert f"RestartForceExitStatus={GATEWAY_SERVICE_RESTART_EXIT_CODE}" in unit
        # TimeoutStopSec must exceed the default drain_timeout (180s) so
        # systemd doesn't SIGKILL the cgroup before post-interrupt cleanup
        # (tool subprocess kill, adapter disconnect) runs — issue #8202.
        assert self._expected_timeout_stop_sec() in unit
        assert "WantedBy=multi-user.target" in unit


class TestGatewayStopCleanup:
    def test_stop_only_kills_current_profile_by_default(self, tmp_path, monkeypatch):
        """Without --all, stop uses systemd (if available) and does NOT call
        the global kill_gateway_processes()."""
        unit_path = tmp_path / "hermes-gateway.service"
        unit_path.write_text("unit\n", encoding="utf-8")

        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_termux", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(gateway_cli, "get_systemd_unit_path", lambda system=False: unit_path)

        service_calls = []
        kill_calls = []

        monkeypatch.setattr(gateway_cli, "systemd_stop", lambda system=False: service_calls.append("stop"))
        monkeypatch.setattr(
            gateway_cli,
            "kill_gateway_processes",
            lambda force=False, all_profiles=False: kill_calls.append(force) or 2,
        )

        gateway_cli.gateway_command(SimpleNamespace(gateway_command="stop"))

        assert service_calls == ["stop"]
        # Global kill should NOT be called without --all
        assert kill_calls == []

    def test_stop_all_sweeps_all_gateway_processes(self, tmp_path, monkeypatch):
        """With --all, stop uses systemd AND calls the global kill_gateway_processes()."""
        unit_path = tmp_path / "hermes-gateway.service"
        unit_path.write_text("unit\n", encoding="utf-8")

        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_termux", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(gateway_cli, "get_systemd_unit_path", lambda system=False: unit_path)

        service_calls = []
        kill_calls = []

        monkeypatch.setattr(gateway_cli, "systemd_stop", lambda system=False: service_calls.append("stop"))
        monkeypatch.setattr(
            gateway_cli,
            "kill_gateway_processes",
            lambda force=False, all_profiles=False: kill_calls.append(force) or 2,
        )

        gateway_cli.gateway_command(SimpleNamespace(gateway_command="stop", **{"all": True}))

        assert service_calls == ["stop"]
        assert kill_calls == [False]


class TestLaunchdServiceRecovery:
    def test_get_restart_drain_timeout_prefers_env_then_config_then_default(self, monkeypatch):
        monkeypatch.delenv("HERMES_RESTART_DRAIN_TIMEOUT", raising=False)
        monkeypatch.setattr(gateway_cli, "read_raw_config", lambda: {})

        assert (
            gateway_cli._get_restart_drain_timeout()
            == DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
        )

        monkeypatch.setattr(
            gateway_cli,
            "read_raw_config",
            lambda: {"agent": {"restart_drain_timeout": 14}},
        )
        assert gateway_cli._get_restart_drain_timeout() == 14.0

        monkeypatch.setenv("HERMES_RESTART_DRAIN_TIMEOUT", "9")
        assert gateway_cli._get_restart_drain_timeout() == 9.0

        monkeypatch.setenv("HERMES_RESTART_DRAIN_TIMEOUT", "invalid")
        assert (
            gateway_cli._get_restart_drain_timeout()
            == DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
        )

    def test_launchd_install_repairs_outdated_plist_without_force(self, tmp_path, monkeypatch):
        plist_path = tmp_path / "ai.hermes.gateway.plist"
        plist_path.write_text("<plist>old content</plist>", encoding="utf-8")

        monkeypatch.setattr(
            gateway_cli, "get_launchd_plist_path", lambda system=False: plist_path
        )

        calls = []

        def fake_run(cmd, check=False, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        gateway_cli.launchd_install()

        label = gateway_cli.get_launchd_label()
        domain = gateway_cli._launchd_domain()
        assert "--replace" in plist_path.read_text(encoding="utf-8")
        assert calls[:2] == [
            ["launchctl", "bootout", f"{domain}/{label}"],
            ["launchctl", "bootstrap", domain, str(plist_path)],
        ]

    def test_launchd_start_reloads_unloaded_job_and_retries(self, tmp_path, monkeypatch):
        plist_path = tmp_path / "ai.hermes.gateway.plist"
        plist_path.write_text(gateway_cli.generate_launchd_plist(), encoding="utf-8")
        label = gateway_cli.get_launchd_label()

        calls = []
        domain = gateway_cli._launchd_domain()
        target = f"{domain}/{label}"

        def fake_run(cmd, check=False, **kwargs):
            if cmd and cmd[0] == "launchctl":
                calls.append(cmd)
            if cmd == ["launchctl", "kickstart", target] and calls.count(cmd) == 1:
                raise gateway_cli.subprocess.CalledProcessError(3, cmd, stderr="Could not find service")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda system=False: plist_path)
        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        gateway_cli.launchd_start()

        assert calls == [
            ["launchctl", "kickstart", target],
            ["launchctl", "bootstrap", domain, str(plist_path)],
            ["launchctl", "kickstart", target],
        ]

    def test_launchd_start_reloads_on_kickstart_exit_code_113(self, tmp_path, monkeypatch):
        """Exit code 113 (\"Could not find service\") should also trigger bootstrap recovery."""
        plist_path = tmp_path / "ai.hermes.gateway.plist"
        plist_path.write_text(gateway_cli.generate_launchd_plist(), encoding="utf-8")
        label = gateway_cli.get_launchd_label()

        calls = []
        domain = gateway_cli._launchd_domain()
        target = f"{domain}/{label}"

        def fake_run(cmd, check=False, **kwargs):
            if cmd and cmd[0] == "launchctl":
                calls.append(cmd)
            if cmd == ["launchctl", "kickstart", target] and calls.count(cmd) == 1:
                raise gateway_cli.subprocess.CalledProcessError(113, cmd, stderr="Could not find service")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda system=False: plist_path)
        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        gateway_cli.launchd_start()

        assert calls == [
            ["launchctl", "kickstart", target],
            ["launchctl", "bootstrap", domain, str(plist_path)],
            ["launchctl", "kickstart", target],
        ]

    def test_launchd_restart_drains_running_gateway_before_kickstart(self, monkeypatch):
        calls = []
        target = f"{gateway_cli._launchd_domain()}/{gateway_cli.get_launchd_label()}"

        monkeypatch.setattr(gateway_cli, "_get_restart_drain_timeout", lambda: 12.0)
        monkeypatch.setattr(gateway_cli, "_request_gateway_self_restart", lambda pid: False)
        monkeypatch.setattr(gateway_cli, "_wait_for_gateway_exit", lambda timeout, force_after=None: True)
        monkeypatch.setattr(gateway_cli, "terminate_pid", lambda pid, force=False: calls.append(("term", pid, force)))
        monkeypatch.setattr(
            "gateway.status.get_running_pid",
            lambda: 321,
        )

        def fake_run(cmd, check=False, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        gateway_cli.launchd_restart()

        assert calls == [
            ("term", 321, False),
            ["launchctl", "kickstart", "-k", target],
        ]

    def test_launchd_restart_self_requests_graceful_restart_without_kickstart(self, monkeypatch, capsys):
        calls = []

        monkeypatch.setattr(
            "gateway.status.get_running_pid",
            lambda: 321,
        )
        monkeypatch.setattr(
            gateway_cli,
            "_request_gateway_self_restart",
            lambda pid: calls.append(("self", pid)) or True,
        )
        monkeypatch.setattr(
            gateway_cli.subprocess,
            "run",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("launchctl should not run")),
        )

        gateway_cli.launchd_restart()

        assert calls == [("self", 321)]
        assert "restart requested" in capsys.readouterr().out.lower()

    def test_launchd_stop_uses_bootout_not_kill(self, monkeypatch):
        """launchd_stop must bootout the service so KeepAlive doesn't respawn it."""
        label = gateway_cli.get_launchd_label()
        domain = gateway_cli._launchd_domain()
        target = f"{domain}/{label}"

        calls = []

        def fake_run(cmd, check=False, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)
        monkeypatch.setattr(gateway_cli, "_wait_for_gateway_exit", lambda **kw: None)

        gateway_cli.launchd_stop()

        assert calls == [["launchctl", "bootout", target]]

    def test_launchd_stop_tolerates_already_unloaded(self, monkeypatch, capsys):
        """launchd_stop silently handles exit codes 3/113 (job not loaded)."""
        label = gateway_cli.get_launchd_label()
        domain = gateway_cli._launchd_domain()
        target = f"{domain}/{label}"

        def fake_run(cmd, check=False, **kwargs):
            if "bootout" in cmd:
                raise gateway_cli.subprocess.CalledProcessError(3, cmd, stderr="Could not find service")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)
        monkeypatch.setattr(gateway_cli, "_wait_for_gateway_exit", lambda **kw: None)

        # Should not raise — exit code 3 means already unloaded
        gateway_cli.launchd_stop()

        output = capsys.readouterr().out
        assert "stopped" in output.lower()

    def test_launchd_stop_waits_for_process_exit(self, monkeypatch):
        """launchd_stop calls _wait_for_gateway_exit after bootout."""
        wait_called = []

        def fake_run(cmd, check=False, **kwargs):
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        def fake_wait(**kwargs):
            wait_called.append(kwargs)

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)
        monkeypatch.setattr(gateway_cli, "_wait_for_gateway_exit", fake_wait)

        gateway_cli.launchd_stop()

        assert len(wait_called) == 1
        assert wait_called[0] == {"timeout": 10.0, "force_after": 5.0}

    def test_launchd_status_reports_local_stale_plist_when_unloaded(self, tmp_path, monkeypatch, capsys):
        plist_path = tmp_path / "ai.hermes.gateway.plist"
        plist_path.write_text("<plist>old content</plist>", encoding="utf-8")

        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda system=False: plist_path)
        monkeypatch.setattr(
            gateway_cli.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(returncode=113, stdout="", stderr="Could not find service"),
        )

        gateway_cli.launchd_status()

        output = capsys.readouterr().out
        assert str(plist_path) in output
        assert "stale" in output.lower()
        assert "not loaded" in output.lower()


class TestGatewayServiceDetection:
    def test_supports_systemd_services_requires_systemctl_binary(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_termux", lambda: False)
        monkeypatch.setattr(gateway_cli.shutil, "which", lambda name: None)

        assert gateway_cli.supports_systemd_services() is False

    def test_supports_systemd_services_returns_true_when_systemctl_present(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_termux", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_wsl", lambda: False)
        monkeypatch.setattr(gateway_cli.shutil, "which", lambda name: "/usr/bin/systemctl")

        assert gateway_cli.supports_systemd_services() is True

    def test_is_service_running_checks_system_scope_when_user_scope_is_inactive(self, monkeypatch):
        user_unit = SimpleNamespace(exists=lambda: True)
        system_unit = SimpleNamespace(exists=lambda: True)

        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_termux", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(
            gateway_cli,
            "get_systemd_unit_path",
            lambda system=False: system_unit if system else user_unit,
        )

        def fake_run(cmd, capture_output=True, text=True, **kwargs):
            if cmd == ["systemctl", "--user", "is-active", gateway_cli.get_service_name()]:
                return SimpleNamespace(returncode=0, stdout="inactive\n", stderr="")
            if cmd == ["systemctl", "is-active", gateway_cli.get_service_name()]:
                return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
            raise AssertionError(f"Unexpected command: {cmd}")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        assert gateway_cli._is_service_running() is True

    def test_is_service_running_returns_false_when_systemctl_missing(self, monkeypatch):
        unit = SimpleNamespace(exists=lambda: True)

        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: True)
        monkeypatch.setattr(
            gateway_cli,
            "get_systemd_unit_path",
            lambda system=False: unit,
        )

        def fake_run(*args, **kwargs):
            raise FileNotFoundError("systemctl")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        assert gateway_cli._is_service_running() is False

class TestGatewaySystemServiceRouting:
    def test_systemd_restart_gracefully_restarts_running_service_and_waits(self, monkeypatch, capsys):
        calls = []

        monkeypatch.setattr(gateway_cli, "_select_systemd_scope", lambda system=False: False)
        monkeypatch.setattr(gateway_cli, "_preflight_user_systemd", lambda **_: None)
        monkeypatch.setattr(gateway_cli, "_require_service_installed", lambda action, system=False: None)
        monkeypatch.setattr(gateway_cli, "refresh_systemd_unit_if_needed", lambda system=False: calls.append(("refresh", system)))
        monkeypatch.setattr(gateway_cli, "_get_restart_drain_timeout", lambda: 12.0)
        monkeypatch.setattr(
            "gateway.status.get_running_pid",
            lambda: 654,
        )
        monkeypatch.setattr(
            gateway_cli,
            "_graceful_restart_via_sigusr1",
            lambda pid, timeout: calls.append(("graceful", pid, timeout)) or True,
        )

        # Simulate systemctl reset-failed/restart followed by an active unit.
        # A plain start does not break systemd's auto-restart timer once the
        # old gateway has exited with the planned restart code.
        def fake_subprocess_run(cmd, **kwargs):
            if "reset-failed" in cmd:
                calls.append(("reset-failed", cmd))
                return SimpleNamespace(stdout="", returncode=0)
            if "restart" in cmd:
                calls.append(("restart", cmd))
                return SimpleNamespace(stdout="", returncode=0)
            raise AssertionError(f"Unexpected systemctl call: {cmd}")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_subprocess_run)
        monkeypatch.setattr(
            gateway_cli,
            "_wait_for_systemd_service_restart",
            lambda system=False, previous_pid=None: calls.append(("wait", system, previous_pid)) or True,
        )

        gateway_cli.systemd_restart()

        assert ("graceful", 654, 17.0) in calls
        assert any(call[0] == "reset-failed" for call in calls)
        assert any(call[0] == "restart" for call in calls)
        assert ("wait", False, 654) in calls
        out = capsys.readouterr().out.lower()
        assert "restarting gracefully" in out

    def test_systemd_restart_uses_systemd_main_pid_when_pid_file_is_missing(self, monkeypatch, capsys):
        calls = []

        monkeypatch.setattr(gateway_cli, "_select_systemd_scope", lambda system=False: False)
        monkeypatch.setattr(gateway_cli, "_preflight_user_systemd", lambda **_: None)
        monkeypatch.setattr(gateway_cli, "_require_service_installed", lambda action, system=False: None)
        monkeypatch.setattr(gateway_cli, "refresh_systemd_unit_if_needed", lambda system=False: None)
        monkeypatch.setattr(gateway_cli, "_get_restart_drain_timeout", lambda: 10.0)
        monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
        monkeypatch.setattr(
            gateway_cli,
            "_read_systemd_unit_properties",
            lambda system=False: {
                "ActiveState": "active",
                "SubState": "running",
                "Result": "success",
                "ExecMainStatus": "0",
                "MainPID": "777",
            },
        )
        monkeypatch.setattr(
            gateway_cli,
            "_graceful_restart_via_sigusr1",
            lambda pid, timeout: calls.append(("graceful", pid, timeout)) or True,
        )
        monkeypatch.setattr(gateway_cli, "_run_systemctl", lambda args, **kwargs: calls.append(args) or SimpleNamespace(stdout="", returncode=0))
        monkeypatch.setattr(
            gateway_cli,
            "_wait_for_systemd_service_restart",
            lambda system=False, previous_pid=None: calls.append(("wait", system, previous_pid)) or True,
        )

        gateway_cli.systemd_restart()

        assert ("graceful", 777, 15.0) in calls
        assert ("wait", False, 777) in calls
        assert "restarting gracefully (pid 777)" in capsys.readouterr().out.lower()

    def test_wait_for_systemd_restart_waits_for_runtime_running(self, monkeypatch, capsys):
        monkeypatch.setattr(
            gateway_cli,
            "_read_systemd_unit_properties",
            lambda system=False: {
                "ActiveState": "active",
                "SubState": "running",
                "Result": "success",
                "ExecMainStatus": "0",
                "MainPID": "999",
            },
        )
        monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
        monkeypatch.setattr(
            gateway_cli,
            "_gateway_runtime_status_for_pid",
            lambda pid: {"pid": pid, "gateway_state": "running"},
        )

        assert gateway_cli._wait_for_systemd_service_restart(previous_pid=777, timeout=0.1) is True
        assert "restarted (pid 999)" in capsys.readouterr().out.lower()

    def test_systemd_restart_reports_start_limit_hit(self, monkeypatch, capsys):
        calls = []

        monkeypatch.setattr(gateway_cli, "_select_systemd_scope", lambda system=False: False)
        monkeypatch.setattr(gateway_cli, "_preflight_user_systemd", lambda **_: None)
        monkeypatch.setattr(gateway_cli, "_require_service_installed", lambda action, system=False: None)
        monkeypatch.setattr(gateway_cli, "refresh_systemd_unit_if_needed", lambda system=False: None)
        monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
        monkeypatch.setattr(gateway_cli, "_recover_pending_systemd_restart", lambda system=False, previous_pid=None: False)

        def fake_run_systemctl(args, **kwargs):
            calls.append(args)
            if args[0] == "show":
                return SimpleNamespace(stdout="ActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\nMainPID=0\n", stderr="", returncode=0)
            if args[0] == "reset-failed":
                return SimpleNamespace(stdout="", stderr="", returncode=0)
            if args[0] == "restart":
                raise subprocess.CalledProcessError(
                    1,
                    ["systemctl", "--user", *args],
                    stderr="Job failed. See result 'start-limit-hit'.",
                )
            raise AssertionError(f"Unexpected args: {args}")

        monkeypatch.setattr(gateway_cli, "_run_systemctl", fake_run_systemctl)

        gateway_cli.systemd_restart()

        assert ["restart", gateway_cli.get_service_name()] in calls
        out = capsys.readouterr().out.lower()
        assert "rate-limited by systemd" in out
        assert "reset-failed" in out

    def test_systemd_restart_recovers_failed_planned_restart(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway_cli, "_select_systemd_scope", lambda system=False: False)
        monkeypatch.setattr(gateway_cli, "_preflight_user_systemd", lambda **_: None)
        monkeypatch.setattr(gateway_cli, "_require_service_installed", lambda action, system=False: None)
        monkeypatch.setattr(gateway_cli, "refresh_systemd_unit_if_needed", lambda system=False: None)
        monkeypatch.setattr(
            "gateway.status.read_runtime_status",
            lambda: {"restart_requested": True, "gateway_state": "stopped"},
        )
        monkeypatch.setattr(gateway_cli, "_request_gateway_self_restart", lambda pid: False)

        calls = []
        started = {"value": False}

        def fake_subprocess_run(cmd, **kwargs):
            if "show" in cmd:
                if not started["value"]:
                    return SimpleNamespace(
                        stdout=(
                            "ActiveState=failed\n"
                            "SubState=failed\n"
                            "Result=exit-code\n"
                            f"ExecMainStatus={GATEWAY_SERVICE_RESTART_EXIT_CODE}\n"
                        ),
                        returncode=0,
                    )
                return SimpleNamespace(
                    stdout="ActiveState=active\nSubState=running\nResult=success\nExecMainStatus=0\n",
                    returncode=0,
                )
            if "reset-failed" in cmd:
                calls.append(("reset-failed", cmd))
                return SimpleNamespace(stdout="", returncode=0)
            if "start" in cmd:
                started["value"] = True
                calls.append(("start", cmd))
                return SimpleNamespace(stdout="", returncode=0)
            raise AssertionError(f"Unexpected command: {cmd}")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_subprocess_run)
        monkeypatch.setattr(
            "gateway.status.get_running_pid",
            lambda: 999 if started["value"] else None,
        )
        monkeypatch.setattr(
            gateway_cli,
            "_gateway_runtime_status_for_pid",
            lambda pid: {"pid": pid, "gateway_state": "running"},
        )

        gateway_cli.systemd_restart()

        assert any(call[0] == "reset-failed" for call in calls)
        assert any(call[0] == "start" for call in calls)
        out = capsys.readouterr().out.lower()
        assert "restarted" in out

    def test_systemd_status_surfaces_planned_restart_failure(self, monkeypatch, capsys):
        unit = SimpleNamespace(exists=lambda: True)
        monkeypatch.setattr(gateway_cli, "_select_systemd_scope", lambda system=False: False)
        monkeypatch.setattr(gateway_cli, "get_systemd_unit_path", lambda system=False: unit)
        monkeypatch.setattr(gateway_cli, "has_conflicting_systemd_units", lambda: False)
        monkeypatch.setattr(gateway_cli, "has_legacy_hermes_units", lambda: False)
        monkeypatch.setattr(gateway_cli, "systemd_unit_is_current", lambda system=False: True)
        monkeypatch.setattr(gateway_cli, "_runtime_health_lines", lambda: ["⚠ Last shutdown reason: Gateway restart requested"])
        monkeypatch.setattr(gateway_cli, "get_systemd_linger_status", lambda: (True, ""))
        monkeypatch.setattr(gateway_cli, "_read_systemd_unit_properties", lambda system=False: {
            "ActiveState": "failed",
            "SubState": "failed",
            "Result": "exit-code",
            "ExecMainStatus": str(GATEWAY_SERVICE_RESTART_EXIT_CODE),
        })

        calls = []

        def fake_run_systemctl(args, **kwargs):
            calls.append(args)
            if args[:2] == ["status", gateway_cli.get_service_name()]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if args[:2] == ["is-active", gateway_cli.get_service_name()]:
                return SimpleNamespace(returncode=3, stdout="failed\n", stderr="")
            raise AssertionError(f"Unexpected args: {args}")

        monkeypatch.setattr(gateway_cli, "_run_systemctl", fake_run_systemctl)

        gateway_cli.systemd_status()

        out = capsys.readouterr().out
        assert "Planned restart is stuck in systemd failed state" in out

    def test_gateway_status_dispatches_full_flag(self, monkeypatch):
        user_unit = SimpleNamespace(exists=lambda: True)
        system_unit = SimpleNamespace(exists=lambda: False)

        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_termux", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(
            gateway_cli,
            "get_systemd_unit_path",
            lambda system=False: system_unit if system else user_unit,
        )
        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_runtime_snapshot",
            lambda system=False: gateway_cli.GatewayRuntimeSnapshot(
                manager="systemd (user)",
                service_installed=True,
                service_running=False,
                gateway_pids=(),
                service_scope="user",
            ),
        )

        calls = []
        monkeypatch.setattr(
            gateway_cli,
            "systemd_status",
            lambda deep=False, system=False, full=False: calls.append((deep, system, full)),
        )

        gateway_cli.gateway_command(
            SimpleNamespace(gateway_command="status", deep=False, system=False, full=True)
        )

        assert calls == [(False, False, True)]

    def test_gateway_install_passes_system_flags(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_termux", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)

        calls = []
        monkeypatch.setattr(
            gateway_cli,
            "systemd_install",
            lambda force=False, system=False, run_as_user=None: calls.append((force, system, run_as_user)),
        )

        gateway_cli.gateway_command(
            SimpleNamespace(gateway_command="install", force=True, system=True, run_as_user="alice")
        )

        assert calls == [(True, True, "alice")]

    def test_gateway_install_reports_termux_manual_mode(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway_cli, "is_termux", lambda: True)
        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)

        try:
            gateway_cli.gateway_command(
                SimpleNamespace(gateway_command="install", force=False, system=False, run_as_user=None)
            )
        except SystemExit as exc:
            assert exc.code == 1
        else:
            raise AssertionError("Expected gateway_command to exit on unsupported Termux service install")

        out = capsys.readouterr().out
        assert "not supported on Termux" in out
        assert "Run manually: hermes gateway" in out

    def test_gateway_status_prefers_system_service_when_only_system_unit_exists(self, monkeypatch):
        user_unit = SimpleNamespace(exists=lambda: False)
        system_unit = SimpleNamespace(exists=lambda: True)

        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_termux", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(
            gateway_cli,
            "get_systemd_unit_path",
            lambda system=False: system_unit if system else user_unit,
        )

        calls = []
        monkeypatch.setattr(
            gateway_cli,
            "systemd_status",
            lambda deep=False, system=False, full=False: calls.append((deep, system, full)),
        )

        gateway_cli.gateway_command(SimpleNamespace(gateway_command="status", deep=False, system=False))

        assert calls == [(False, False, False)]

    def test_gateway_status_reports_manual_process_when_service_is_stopped(self, monkeypatch, capsys):
        user_unit = SimpleNamespace(exists=lambda: True)
        system_unit = SimpleNamespace(exists=lambda: False)

        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_termux", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(
            gateway_cli,
            "get_systemd_unit_path",
            lambda system=False: system_unit if system else user_unit,
        )
        monkeypatch.setattr(
            gateway_cli,
            "systemd_status",
            lambda deep=False, system=False, full=False: print("service stopped"),
        )
        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_runtime_snapshot",
            lambda system=False: gateway_cli.GatewayRuntimeSnapshot(
                manager="systemd (user)",
                service_installed=True,
                service_running=False,
                gateway_pids=(4321,),
                service_scope="user",
            ),
        )

        gateway_cli.gateway_command(SimpleNamespace(gateway_command="status", deep=False, system=False))

        out = capsys.readouterr().out
        assert "service stopped" in out
        assert "Gateway process is running for this profile" in out
        assert "PID(s): 4321" in out

    def test_gateway_status_on_termux_shows_manual_guidance(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_termux", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(gateway_cli, "find_gateway_pids", lambda exclude_pids=None: [])
        monkeypatch.setattr(gateway_cli, "_runtime_health_lines", lambda: [])

        gateway_cli.gateway_command(SimpleNamespace(gateway_command="status", deep=False, system=False))

        out = capsys.readouterr().out
        assert "Gateway is not running" in out
        assert "nohup hermes gateway" in out
        assert "install as user service" not in out

    def test_gateway_restart_does_not_fallback_to_foreground_when_launchd_restart_fails(self, tmp_path, monkeypatch):
        plist_path = tmp_path / "ai.hermes.gateway.plist"
        plist_path.write_text("plist\n", encoding="utf-8")

        monkeypatch.setattr(gateway_cli, "is_linux", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: True)
        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda system=False: plist_path)
        monkeypatch.setattr(
            gateway_cli,
            "launchd_restart",
            lambda system=False: (_ for _ in ()).throw(
                gateway_cli.subprocess.CalledProcessError(5, ["launchctl", "kickstart", "-k", "gui/501/ai.hermes.gateway"])
            ),
        )

        run_calls = []
        monkeypatch.setattr(gateway_cli, "run_gateway", lambda verbose=0, quiet=False, replace=False: run_calls.append((verbose, quiet, replace)))
        monkeypatch.setattr(gateway_cli, "kill_gateway_processes", lambda force=False: 0)

        try:
            gateway_cli.gateway_command(SimpleNamespace(gateway_command="restart", system=False))
        except SystemExit as exc:
            assert exc.code == 1
        else:
            raise AssertionError("Expected gateway_command to exit when service restart fails")

        assert run_calls == []


class TestDetectVenvDir:
    """Tests for _detect_venv_dir() virtualenv detection."""

    def test_detects_active_virtualenv_via_sys_prefix(self, tmp_path, monkeypatch):
        venv_path = tmp_path / "my-custom-venv"
        venv_path.mkdir()
        monkeypatch.setattr("sys.prefix", str(venv_path))
        monkeypatch.setattr("sys.base_prefix", "/usr")

        result = gateway_cli._detect_venv_dir()
        assert result == venv_path

    def test_falls_back_to_dot_venv_directory(self, tmp_path, monkeypatch):
        # Not inside a virtualenv
        monkeypatch.setattr("sys.prefix", "/usr")
        monkeypatch.setattr("sys.base_prefix", "/usr")
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.setattr(gateway_cli, "PROJECT_ROOT", tmp_path)

        dot_venv = tmp_path / ".venv"
        dot_venv.mkdir()

        result = gateway_cli._detect_venv_dir()
        assert result == dot_venv

    def test_falls_back_to_venv_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sys.prefix", "/usr")
        monkeypatch.setattr("sys.base_prefix", "/usr")
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.setattr(gateway_cli, "PROJECT_ROOT", tmp_path)

        venv = tmp_path / "venv"
        venv.mkdir()

        result = gateway_cli._detect_venv_dir()
        assert result == venv

    def test_prefers_dot_venv_over_venv(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sys.prefix", "/usr")
        monkeypatch.setattr("sys.base_prefix", "/usr")
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.setattr(gateway_cli, "PROJECT_ROOT", tmp_path)

        (tmp_path / ".venv").mkdir()
        (tmp_path / "venv").mkdir()

        result = gateway_cli._detect_venv_dir()
        assert result == tmp_path / ".venv"

    def test_returns_none_when_no_virtualenv(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sys.prefix", "/usr")
        monkeypatch.setattr("sys.base_prefix", "/usr")
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.setattr(gateway_cli, "PROJECT_ROOT", tmp_path)

        result = gateway_cli._detect_venv_dir()
        assert result is None


class TestSystemUnitHermesHome:
    """HERMES_HOME in system units must reference the target user, not root."""

    def test_system_unit_uses_target_user_home_not_calling_user(self, monkeypatch):
        # Simulate sudo: Path.home() returns /root, target user is alice
        monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/root")))
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(
            gateway_cli, "_system_service_identity",
            lambda run_as_user=None: ("alice", "alice", "/home/alice"),
        )
        monkeypatch.setattr(
            gateway_cli, "_build_user_local_paths",
            lambda home, existing: [],
        )

        unit = gateway_cli.generate_systemd_unit(system=True, run_as_user="alice")

        assert 'HERMES_HOME=/home/alice/.hermes' in unit
        assert '/root/.hermes' not in unit

    def test_system_unit_remaps_profile_to_target_user(self, monkeypatch):
        # Simulate sudo with a profile: HERMES_HOME was resolved under root
        monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/root")))
        monkeypatch.setenv("HERMES_HOME", "/root/.hermes/profiles/coder")
        monkeypatch.setattr(
            gateway_cli, "_system_service_identity",
            lambda run_as_user=None: ("alice", "alice", "/home/alice"),
        )
        monkeypatch.setattr(
            gateway_cli, "_build_user_local_paths",
            lambda home, existing: [],
        )

        unit = gateway_cli.generate_systemd_unit(system=True, run_as_user="alice")

        assert 'HERMES_HOME=/home/alice/.hermes/profiles/coder' in unit
        assert '/root/' not in unit

    def test_system_unit_preserves_custom_hermes_home(self, monkeypatch):
        # Custom HERMES_HOME not under any user's home — keep as-is
        monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/root")))
        monkeypatch.setenv("HERMES_HOME", "/opt/hermes-shared")
        monkeypatch.setattr(
            gateway_cli, "_system_service_identity",
            lambda run_as_user=None: ("alice", "alice", "/home/alice"),
        )
        monkeypatch.setattr(
            gateway_cli, "_build_user_local_paths",
            lambda home, existing: [],
        )

        unit = gateway_cli.generate_systemd_unit(system=True, run_as_user="alice")

        assert 'HERMES_HOME=/opt/hermes-shared' in unit

    def test_user_unit_unaffected_by_change(self):
        # User-scope units should still use the calling user's HERMES_HOME
        unit = gateway_cli.generate_systemd_unit(system=False)

        hermes_home = str(gateway_cli.get_hermes_home().resolve())
        assert f'HERMES_HOME={hermes_home}' in unit


class TestHermesHomeForTargetUser:
    """Unit tests for _hermes_home_for_target_user()."""

    def test_remaps_default_home(self, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/root")))
        monkeypatch.delenv("HERMES_HOME", raising=False)

        result = gateway_cli._hermes_home_for_target_user("/home/alice")
        assert result == "/home/alice/.hermes"

    def test_remaps_profile_path(self, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/root")))
        monkeypatch.setenv("HERMES_HOME", "/root/.hermes/profiles/coder")

        result = gateway_cli._hermes_home_for_target_user("/home/alice")
        assert result == "/home/alice/.hermes/profiles/coder"

    def test_keeps_custom_path(self, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/root")))
        monkeypatch.setenv("HERMES_HOME", "/opt/hermes")

        result = gateway_cli._hermes_home_for_target_user("/home/alice")
        assert result == "/opt/hermes"

    def test_noop_when_same_user(self, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/home/alice")))
        monkeypatch.delenv("HERMES_HOME", raising=False)

        result = gateway_cli._hermes_home_for_target_user("/home/alice")
        assert result == "/home/alice/.hermes"


class TestGeneratedUnitUsesDetectedVenv:
    def test_systemd_unit_uses_dot_venv_when_detected(self, tmp_path, monkeypatch):
        dot_venv = tmp_path / ".venv"
        dot_venv.mkdir()
        (dot_venv / "bin").mkdir()

        monkeypatch.setattr(gateway_cli, "_detect_venv_dir", lambda: dot_venv)
        monkeypatch.setattr(gateway_cli, "get_python_path", lambda: str(dot_venv / "bin" / "python"))

        unit = gateway_cli.generate_systemd_unit(system=False)

        assert f"VIRTUAL_ENV={dot_venv}" in unit
        assert f"{dot_venv}/bin" in unit
        # Must NOT contain a hardcoded /venv/ path
        assert "/venv/" not in unit or "/.venv/" in unit


class TestGeneratedUnitIncludesLocalBin:
    """~/.local/bin must be in PATH so uvx/pipx tools are discoverable."""

    def test_user_unit_includes_local_bin_in_path(self, monkeypatch):
        home = Path.home()
        monkeypatch.setattr(
            gateway_cli,
            "_build_user_local_paths",
            lambda home_path, existing: [str(home / ".local" / "bin")],
        )
        unit = gateway_cli.generate_systemd_unit(system=False)
        assert f"{home}/.local/bin" in unit

    def test_system_unit_includes_local_bin_in_path(self, monkeypatch):
        monkeypatch.setattr(
            gateway_cli,
            "_build_user_local_paths",
            lambda home_path, existing: [str(home_path / ".local" / "bin")],
        )
        unit = gateway_cli.generate_systemd_unit(system=True)
        # System unit uses the resolved home dir from _system_service_identity
        assert "/.local/bin" in unit


class TestSystemServiceIdentityRootHandling:
    """Root user handling in _system_service_identity()."""

    def test_auto_detected_root_is_rejected(self, monkeypatch):
        """When root is auto-detected (not explicitly requested), raise."""
        import grp

        monkeypatch.delenv("SUDO_USER", raising=False)
        monkeypatch.setenv("USER", "root")
        monkeypatch.setenv("LOGNAME", "root")

        with pytest.raises(ValueError, match="pass --run-as-user root to override"):
            gateway_cli._system_service_identity(run_as_user=None)

    def test_explicit_root_is_allowed(self, monkeypatch):
        """When root is explicitly passed via --run-as-user root, allow it."""
        import grp

        root_info = pwd.getpwnam("root")
        root_group = grp.getgrgid(root_info.pw_gid).gr_name

        username, group, home = gateway_cli._system_service_identity(run_as_user="root")
        assert username == "root"
        assert home == root_info.pw_dir

    def test_non_root_user_passes_through(self, monkeypatch):
        """Normal non-root user works as before."""
        import grp

        monkeypatch.delenv("SUDO_USER", raising=False)
        monkeypatch.setenv("USER", "nobody")
        monkeypatch.setenv("LOGNAME", "nobody")

        try:
            username, group, home = gateway_cli._system_service_identity(run_as_user=None)
            assert username == "nobody"
        except ValueError as e:
            # "nobody" might not exist on all systems
            assert "Unknown user" in str(e)


class TestEnsureUserSystemdEnv:
    """Tests for _ensure_user_systemd_env() D-Bus session bus auto-detection."""

    def test_sets_xdg_runtime_dir_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        monkeypatch.setattr(os, "getuid", lambda: 42)

        # Patch Path.exists so /run/user/42 appears to exist.
        # Using a FakePath subclass breaks on Python 3.12+ where
        # PosixPath.__new__ ignores the redirected path argument.
        _orig_exists = gateway_cli.Path.exists
        monkeypatch.setattr(
            gateway_cli.Path, "exists",
            lambda self: True if str(self) == "/run/user/42" else _orig_exists(self),
        )

        gateway_cli._ensure_user_systemd_env()

        assert os.environ.get("XDG_RUNTIME_DIR") == "/run/user/42"

    def test_sets_dbus_address_when_bus_socket_exists(self, tmp_path, monkeypatch):
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        bus_socket = runtime / "bus"
        bus_socket.touch()  # simulate the socket file

        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        monkeypatch.setattr(os, "getuid", lambda: 99)

        gateway_cli._ensure_user_systemd_env()

        assert os.environ["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={bus_socket}"

    def test_preserves_existing_env_vars(self, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/custom/runtime")
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/custom/bus")

        gateway_cli._ensure_user_systemd_env()

        assert os.environ["XDG_RUNTIME_DIR"] == "/custom/runtime"
        assert os.environ["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/custom/bus"

    def test_no_dbus_when_bus_socket_missing(self, tmp_path, monkeypatch):
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        # no bus socket created

        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        monkeypatch.setattr(os, "getuid", lambda: 99)

        gateway_cli._ensure_user_systemd_env()

        assert "DBUS_SESSION_BUS_ADDRESS" not in os.environ

    def test_systemctl_cmd_calls_ensure_for_user_mode(self, monkeypatch):
        calls = []
        monkeypatch.setattr(gateway_cli, "_ensure_user_systemd_env", lambda: calls.append("called"))

        result = gateway_cli._systemctl_cmd(system=False)
        assert result == ["systemctl", "--user"]
        assert calls == ["called"]

    def test_systemctl_cmd_skips_ensure_for_system_mode(self, monkeypatch):
        calls = []
        monkeypatch.setattr(gateway_cli, "_ensure_user_systemd_env", lambda: calls.append("called"))

        result = gateway_cli._systemctl_cmd(system=True)
        assert result == ["systemctl"]
        assert calls == []


class TestPreflightUserSystemd:
    """Tests for _preflight_user_systemd() — D-Bus reachability before systemctl --user.

    Covers issue #5130 / Rick's RHEL 9.6 SSH scenario: setup tries to start the
    gateway via ``systemctl --user start`` in a shell with no user D-Bus session,
    which previously failed with a raw ``CalledProcessError`` and no remediation.
    """

    def test_noop_when_bus_socket_exists(self, monkeypatch):
        """Socket already there (desktop / linger + prior login) → no-op."""
        monkeypatch.setattr(
            gateway_cli, "_user_dbus_socket_path",
            lambda: type("P", (), {"exists": lambda self: True})(),
        )
        monkeypatch.setattr(
            gateway_cli, "_user_systemd_private_socket_path",
            lambda: type("P", (), {"exists": lambda self: False})(),
        )
        # Should not raise, no subprocess calls needed.
        gateway_cli._preflight_user_systemd()

    def test_raises_when_linger_disabled_and_loginctl_denied(self, monkeypatch):
        """Rick's scenario: no D-Bus, no linger, non-root SSH → clear error."""
        monkeypatch.setattr(
            gateway_cli, "_user_dbus_socket_path",
            lambda: type("P", (), {"exists": lambda self: False})(),
        )
        monkeypatch.setattr(
            gateway_cli, "_user_systemd_private_socket_path",
            lambda: type("P", (), {"exists": lambda self: False})(),
        )
        monkeypatch.setattr(
            gateway_cli, "get_systemd_linger_status", lambda: (False, ""),
        )
        monkeypatch.setattr(gateway_cli.shutil, "which", lambda _: "/usr/bin/loginctl")

        class _Result:
            returncode = 1
            stdout = ""
            stderr = "Interactive authentication required."

        monkeypatch.setattr(
            gateway_cli.subprocess, "run", lambda *a, **kw: _Result(),
        )

        with pytest.raises(gateway_cli.UserSystemdUnavailableError) as exc_info:
            gateway_cli._preflight_user_systemd()

        msg = str(exc_info.value)
        assert "sudo loginctl enable-linger" in msg
        assert "hermes gateway run" in msg  # foreground fallback mentioned
        assert "Interactive authentication required" in msg

    def test_raises_when_loginctl_missing(self, monkeypatch):
        """No loginctl binary at all → suggest sudo install + manual fix."""
        monkeypatch.setattr(
            gateway_cli, "_user_dbus_socket_path",
            lambda: type("P", (), {"exists": lambda self: False})(),
        )
        monkeypatch.setattr(
            gateway_cli, "_user_systemd_private_socket_path",
            lambda: type("P", (), {"exists": lambda self: False})(),
        )
        monkeypatch.setattr(
            gateway_cli, "get_systemd_linger_status",
            lambda: (None, "loginctl not found"),
        )
        monkeypatch.setattr(gateway_cli.shutil, "which", lambda _: None)

        with pytest.raises(gateway_cli.UserSystemdUnavailableError) as exc_info:
            gateway_cli._preflight_user_systemd()

        assert "sudo loginctl enable-linger" in str(exc_info.value)

    def test_linger_enabled_but_socket_still_missing(self, monkeypatch):
        """Edge case: linger says yes but the bus socket never came up."""
        monkeypatch.setattr(
            gateway_cli, "_user_dbus_socket_path",
            lambda: type("P", (), {"exists": lambda self: False})(),
        )
        monkeypatch.setattr(
            gateway_cli, "_user_systemd_private_socket_path",
            lambda: type("P", (), {"exists": lambda self: False})(),
        )
        monkeypatch.setattr(
            gateway_cli, "get_systemd_linger_status", lambda: (True, ""),
        )
        monkeypatch.setattr(
            gateway_cli, "_wait_for_user_dbus_socket", lambda timeout=3.0: False,
        )

        with pytest.raises(gateway_cli.UserSystemdUnavailableError) as exc_info:
            gateway_cli._preflight_user_systemd()

        assert "linger is enabled" in str(exc_info.value)

    def test_enable_linger_succeeds_and_socket_appears(self, monkeypatch, capsys):
        """Happy remediation path: polkit allows enable-linger, socket spawns."""
        monkeypatch.setattr(
            gateway_cli, "_user_dbus_socket_path",
            lambda: type("P", (), {"exists": lambda self: False})(),
        )
        monkeypatch.setattr(
            gateway_cli, "_user_systemd_private_socket_path",
            lambda: type("P", (), {"exists": lambda self: False})(),
        )
        monkeypatch.setattr(
            gateway_cli, "get_systemd_linger_status", lambda: (False, ""),
        )
        monkeypatch.setattr(gateway_cli.shutil, "which", lambda _: "/usr/bin/loginctl")

        class _OkResult:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(
            gateway_cli.subprocess, "run", lambda *a, **kw: _OkResult(),
        )
        monkeypatch.setattr(
            gateway_cli, "_wait_for_user_dbus_socket",
            lambda timeout=5.0: True,
        )

        # Should not raise.
        gateway_cli._preflight_user_systemd()
        out = capsys.readouterr().out
        assert "Enabled linger" in out


class TestProfileArg:
    """Tests for _profile_arg — returns '--profile <name>' for named profiles."""

    def test_default_hermes_home_returns_empty(self, tmp_path, monkeypatch):
        """Default ~/.hermes should not produce a --profile flag."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        result = gateway_cli._profile_arg(str(hermes_home))
        assert result == ""

    def test_named_profile_returns_flag(self, tmp_path, monkeypatch):
        """~/.hermes/profiles/mybot should return '--profile mybot'."""
        profile_dir = tmp_path / ".hermes" / "profiles" / "mybot"
        profile_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        result = gateway_cli._profile_arg(str(profile_dir))
        assert result == "--profile mybot"

    def test_hash_path_returns_empty(self, tmp_path, monkeypatch):
        """Arbitrary non-profile HERMES_HOME should return empty string."""
        custom_home = tmp_path / "custom" / "hermes"
        custom_home.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        result = gateway_cli._profile_arg(str(custom_home))
        assert result == ""

    def test_nested_profile_path_returns_empty(self, tmp_path, monkeypatch):
        """~/.hermes/profiles/mybot/subdir should NOT match — too deep."""
        nested = tmp_path / ".hermes" / "profiles" / "mybot" / "subdir"
        nested.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        result = gateway_cli._profile_arg(str(nested))
        assert result == ""

    def test_invalid_profile_name_returns_empty(self, tmp_path, monkeypatch):
        """Profile names with invalid chars should not match the regex."""
        bad_profile = tmp_path / ".hermes" / "profiles" / "My Bot!"
        bad_profile.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        result = gateway_cli._profile_arg(str(bad_profile))
        assert result == ""

    def test_systemd_unit_includes_profile(self, tmp_path, monkeypatch):
        """generate_systemd_unit should include --profile in ExecStart for named profiles."""
        profile_dir = tmp_path / ".hermes" / "profiles" / "mybot"
        profile_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: profile_dir)
        unit = gateway_cli.generate_systemd_unit(system=False)
        assert "--profile mybot" in unit
        assert "gateway run --replace" in unit

    def test_launchd_plist_includes_profile(self, tmp_path, monkeypatch):
        """generate_launchd_plist should include --profile in ProgramArguments for named profiles."""
        profile_dir = tmp_path / ".hermes" / "profiles" / "mybot"
        profile_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: profile_dir)
        plist = gateway_cli.generate_launchd_plist()
        assert "<string>--profile</string>" in plist
        assert "<string>mybot</string>" in plist

    def test_launchd_plist_path_uses_real_user_home_not_profile_home(self, tmp_path, monkeypatch):
        profile_dir = tmp_path / ".hermes" / "profiles" / "orcha"
        profile_dir.mkdir(parents=True)
        machine_home = tmp_path / "machine-home"
        machine_home.mkdir()
        profile_home = profile_dir / "home"
        profile_home.mkdir()

        monkeypatch.setattr(Path, "home", lambda: profile_home)
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: profile_dir)
        monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(machine_home)))

        plist_path = gateway_cli.get_launchd_plist_path()

        assert plist_path == machine_home / "Library" / "LaunchAgents" / "ai.hermes.gateway-orcha.plist"


class TestRemapPathForUser:
    """Unit tests for _remap_path_for_user()."""

    def test_remaps_path_under_current_home(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "root")
        (tmp_path / "root").mkdir()
        result = gateway_cli._remap_path_for_user(
            str(tmp_path / "root" / ".hermes" / "hermes-agent"),
            str(tmp_path / "alice"),
        )
        assert result == str(tmp_path / "alice" / ".hermes" / "hermes-agent")

    def test_keeps_system_path_unchanged(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "root")
        (tmp_path / "root").mkdir()
        result = gateway_cli._remap_path_for_user("/opt/hermes", str(tmp_path / "alice"))
        assert result == "/opt/hermes"

    def test_noop_when_same_user(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "alice")
        (tmp_path / "alice").mkdir()
        original = str(tmp_path / "alice" / ".hermes" / "hermes-agent")
        result = gateway_cli._remap_path_for_user(original, str(tmp_path / "alice"))
        assert result == original


class TestSystemUnitPathRemapping:
    """System units must remap ALL paths from the caller's home to the target user."""

    def test_system_unit_has_no_root_paths(self, monkeypatch, tmp_path):
        root_home = tmp_path / "root"
        root_home.mkdir()
        project = root_home / ".hermes" / "hermes-agent"
        project.mkdir(parents=True)
        venv_bin = project / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").write_text("")

        target_home = "/home/alice"

        monkeypatch.setattr(Path, "home", lambda: root_home)
        monkeypatch.setenv("HERMES_HOME", str(root_home / ".hermes"))
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: root_home / ".hermes")
        monkeypatch.setattr(gateway_cli, "PROJECT_ROOT", project)
        monkeypatch.setattr(gateway_cli, "_detect_venv_dir", lambda: project / "venv")
        monkeypatch.setattr(gateway_cli, "get_python_path", lambda: str(venv_bin / "python"))
        monkeypatch.setattr(
            gateway_cli, "_system_service_identity",
            lambda run_as_user=None: ("alice", "alice", target_home),
        )

        unit = gateway_cli.generate_systemd_unit(system=True)

        # No root paths should leak into the unit
        assert str(root_home) not in unit
        # Target user paths should be present
        assert "/home/alice" in unit
        assert "WorkingDirectory=/home/alice/.hermes/hermes-agent" in unit


class TestDockerAwareGateway:
    """Tests for Docker container awareness in gateway commands."""

    def test_run_systemctl_raises_runtimeerror_when_missing(self, monkeypatch):
        """_run_systemctl raises RuntimeError with container guidance when systemctl is absent."""
        import pytest

        def fake_run(cmd, **kwargs):
            raise FileNotFoundError("systemctl")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="systemctl is not available"):
            gateway_cli._run_systemctl(["start", "hermes-gateway"])

    def test_run_systemctl_passes_through_on_success(self, monkeypatch):
        """_run_systemctl delegates to subprocess.run when systemctl exists."""
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        result = gateway_cli._run_systemctl(["status", "hermes-gateway"])
        assert result.returncode == 0
        assert len(calls) == 1
        assert "status" in calls[0]

    def test_install_in_container_prints_docker_guidance(self, monkeypatch, capsys):
        """'hermes gateway install' inside Docker exits 0 with container guidance."""
        import pytest

        monkeypatch.setattr(gateway_cli, "is_managed", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_termux", lambda: False)
        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_wsl", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_container", lambda: True)

        args = SimpleNamespace(gateway_command="install", force=False, system=False, run_as_user=None)
        with pytest.raises(SystemExit) as exc_info:
            gateway_cli.gateway_command(args)

        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "Docker" in out or "docker" in out
        assert "restart" in out.lower()

    def test_uninstall_in_container_prints_docker_guidance(self, monkeypatch, capsys):
        """'hermes gateway uninstall' inside Docker exits 0 with container guidance."""
        import pytest

        monkeypatch.setattr(gateway_cli, "is_managed", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_termux", lambda: False)
        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_container", lambda: True)

        args = SimpleNamespace(gateway_command="uninstall", system=False)
        with pytest.raises(SystemExit) as exc_info:
            gateway_cli.gateway_command(args)

        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "docker" in out.lower()

    def test_start_in_container_prints_docker_guidance(self, monkeypatch, capsys):
        """'hermes gateway start' inside Docker exits 0 with container guidance."""
        import pytest

        monkeypatch.setattr(gateway_cli, "is_termux", lambda: False)
        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_wsl", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_container", lambda: True)

        args = SimpleNamespace(gateway_command="start", system=False)
        with pytest.raises(SystemExit) as exc_info:
            gateway_cli.gateway_command(args)

        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "docker" in out.lower()
        assert "hermes gateway run" in out


class TestLegacyHermesUnitDetection:
    """Tests for _find_legacy_hermes_units / has_legacy_hermes_units.

    These guard against the scenario that tripped Luis in April 2026: an
    older install left a ``hermes.service`` unit behind when the service was
    renamed to ``hermes-gateway.service``. After PR #5646 (signal recovery
    via systemd), the two services began SIGTERM-flapping over the same
    Telegram bot token in a 30-second cycle.

    The detector must flag ``hermes.service`` ONLY when it actually runs our
    gateway, and must NEVER flag profile units
    (``hermes-gateway-<profile>.service``) or unrelated third-party services.
    """

    # Minimal ExecStart that looks like our gateway
    _OUR_UNIT_TEXT = (
        "[Unit]\nDescription=Hermes Gateway\n[Service]\n"
        "ExecStart=/usr/bin/python -m hermes_cli.main gateway run --replace\n"
    )

    @staticmethod
    def _setup_search_paths(tmp_path, monkeypatch):
        """Redirect the legacy search to user_dir + system_dir under tmp_path."""
        user_dir = tmp_path / "user"
        system_dir = tmp_path / "system"
        user_dir.mkdir()
        system_dir.mkdir()
        monkeypatch.setattr(
            gateway_cli,
            "_legacy_unit_search_paths",
            lambda: [(False, user_dir), (True, system_dir)],
        )
        return user_dir, system_dir

    def test_detects_legacy_hermes_service_in_user_scope(self, tmp_path, monkeypatch):
        user_dir, _ = self._setup_search_paths(tmp_path, monkeypatch)
        legacy = user_dir / "hermes.service"
        legacy.write_text(self._OUR_UNIT_TEXT, encoding="utf-8")

        results = gateway_cli._find_legacy_hermes_units()

        assert len(results) == 1
        name, path, is_system = results[0]
        assert name == "hermes.service"
        assert path == legacy
        assert is_system is False
        assert gateway_cli.has_legacy_hermes_units() is True

    def test_detects_legacy_hermes_service_in_system_scope(self, tmp_path, monkeypatch):
        _, system_dir = self._setup_search_paths(tmp_path, monkeypatch)
        legacy = system_dir / "hermes.service"
        legacy.write_text(self._OUR_UNIT_TEXT, encoding="utf-8")

        results = gateway_cli._find_legacy_hermes_units()

        assert len(results) == 1
        name, path, is_system = results[0]
        assert name == "hermes.service"
        assert path == legacy
        assert is_system is True

    def test_ignores_profile_unit_hermes_gateway_coder(self, tmp_path, monkeypatch):
        """CRITICAL: profile units must NOT be flagged as legacy.

        Teknium's concern — ``hermes-gateway-coder.service`` is our standard
        naming for the ``coder`` profile. The legacy detector is an explicit
        allowlist, not a glob, so profile units are safe.
        """
        user_dir, system_dir = self._setup_search_paths(tmp_path, monkeypatch)
        # Drop profile units in BOTH scopes with our ExecStart
        for base in (user_dir, system_dir):
            (base / "hermes-gateway-coder.service").write_text(
                self._OUR_UNIT_TEXT, encoding="utf-8"
            )
            (base / "hermes-gateway-orcha.service").write_text(
                self._OUR_UNIT_TEXT, encoding="utf-8"
            )
            (base / "hermes-gateway.service").write_text(
                self._OUR_UNIT_TEXT, encoding="utf-8"
            )

        results = gateway_cli._find_legacy_hermes_units()

        assert results == []
        assert gateway_cli.has_legacy_hermes_units() is False

    def test_ignores_unrelated_hermes_service(self, tmp_path, monkeypatch):
        """Third-party ``hermes.service`` that isn't ours stays untouched.

        If a user has some other package named ``hermes`` installed as a
        service, we must not flag it.
        """
        user_dir, _ = self._setup_search_paths(tmp_path, monkeypatch)
        (user_dir / "hermes.service").write_text(
            "[Unit]\nDescription=Some Other Hermes\n[Service]\n"
            "ExecStart=/opt/other-hermes/bin/daemon --foreground\n",
            encoding="utf-8",
        )

        results = gateway_cli._find_legacy_hermes_units()

        assert results == []
        assert gateway_cli.has_legacy_hermes_units() is False

    def test_returns_empty_when_no_legacy_files_exist(self, tmp_path, monkeypatch):
        self._setup_search_paths(tmp_path, monkeypatch)

        assert gateway_cli._find_legacy_hermes_units() == []
        assert gateway_cli.has_legacy_hermes_units() is False

    def test_detects_both_scopes_simultaneously(self, tmp_path, monkeypatch):
        """When a user has BOTH user-scope and system-scope legacy units,
        both are reported so the migration step can remove them together."""
        user_dir, system_dir = self._setup_search_paths(tmp_path, monkeypatch)
        (user_dir / "hermes.service").write_text(self._OUR_UNIT_TEXT, encoding="utf-8")
        (system_dir / "hermes.service").write_text(self._OUR_UNIT_TEXT, encoding="utf-8")

        results = gateway_cli._find_legacy_hermes_units()

        scopes = sorted(is_system for _, _, is_system in results)
        assert scopes == [False, True]

    def test_accepts_alternate_execstart_formats(self, tmp_path, monkeypatch):
        """Older installs may have used different python invocations.

        ExecStart variants we've seen in the wild:
          - python -m hermes_cli.main gateway run
          - python path/to/hermes_cli/main.py gateway run
          - hermes gateway run   (direct binary)
          - python path/to/gateway/run.py
        """
        user_dir, _ = self._setup_search_paths(tmp_path, monkeypatch)
        variants = [
            "ExecStart=/venv/bin/python -m hermes_cli.main gateway run --replace",
            "ExecStart=/venv/bin/python /opt/hermes/hermes_cli/main.py gateway run",
            "ExecStart=/usr/local/bin/hermes gateway run --replace",
            "ExecStart=/venv/bin/python /opt/hermes/gateway/run.py",
        ]
        for i, execstart in enumerate(variants):
            name = f"hermes.service" if i == 0 else f"hermes.service"  # same name
            # Test each variant fresh
            (user_dir / "hermes.service").write_text(
                f"[Unit]\nDescription=Old Hermes\n[Service]\n{execstart}\n",
                encoding="utf-8",
            )
            results = gateway_cli._find_legacy_hermes_units()
            assert len(results) == 1, f"Variant {i} not detected: {execstart!r}"

    def test_print_legacy_unit_warning_is_noop_when_empty(self, tmp_path, monkeypatch, capsys):
        self._setup_search_paths(tmp_path, monkeypatch)

        gateway_cli.print_legacy_unit_warning()
        out = capsys.readouterr().out

        assert out == ""

    def test_print_legacy_unit_warning_shows_migration_hint(self, tmp_path, monkeypatch, capsys):
        user_dir, _ = self._setup_search_paths(tmp_path, monkeypatch)
        (user_dir / "hermes.service").write_text(self._OUR_UNIT_TEXT, encoding="utf-8")

        gateway_cli.print_legacy_unit_warning()
        out = capsys.readouterr().out

        assert "Legacy" in out
        assert "hermes.service" in out
        assert "hermes gateway migrate-legacy" in out

    def test_handles_unreadable_unit_file_gracefully(self, tmp_path, monkeypatch):
        """A permission error reading a unit file must not crash detection."""
        user_dir, _ = self._setup_search_paths(tmp_path, monkeypatch)
        unreadable = user_dir / "hermes.service"
        unreadable.write_text(self._OUR_UNIT_TEXT, encoding="utf-8")
        # Simulate a read failure — monkeypatch Path.read_text to raise
        original_read_text = gateway_cli.Path.read_text

        def raising_read_text(self, *args, **kwargs):
            if self == unreadable:
                raise PermissionError("simulated")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(gateway_cli.Path, "read_text", raising_read_text)

        # Should not raise
        results = gateway_cli._find_legacy_hermes_units()
        assert results == []


class TestRemoveLegacyHermesUnits:
    """Tests for remove_legacy_hermes_units (the migration action)."""

    _OUR_UNIT_TEXT = (
        "[Unit]\nDescription=Hermes Gateway\n[Service]\n"
        "ExecStart=/usr/bin/python -m hermes_cli.main gateway run --replace\n"
    )

    @staticmethod
    def _setup(tmp_path, monkeypatch, as_root=False):
        user_dir = tmp_path / "user"
        system_dir = tmp_path / "system"
        user_dir.mkdir()
        system_dir.mkdir()
        monkeypatch.setattr(
            gateway_cli,
            "_legacy_unit_search_paths",
            lambda: [(False, user_dir), (True, system_dir)],
        )
        # Mock systemctl — return success for everything
        systemctl_calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            systemctl_calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 0 if as_root else 1000)
        return user_dir, system_dir, systemctl_calls

    def test_returns_zero_when_no_legacy_units(self, tmp_path, monkeypatch, capsys):
        self._setup(tmp_path, monkeypatch)

        removed, remaining = gateway_cli.remove_legacy_hermes_units(interactive=False)

        assert removed == 0
        assert remaining == []
        assert "No legacy" in capsys.readouterr().out

    def test_dry_run_lists_without_removing(self, tmp_path, monkeypatch, capsys):
        user_dir, _, calls = self._setup(tmp_path, monkeypatch)
        legacy = user_dir / "hermes.service"
        legacy.write_text(self._OUR_UNIT_TEXT, encoding="utf-8")

        removed, remaining = gateway_cli.remove_legacy_hermes_units(
            interactive=False, dry_run=True
        )

        assert removed == 0
        assert remaining == [legacy]
        assert legacy.exists()  # Not removed
        assert calls == []  # No systemctl invocations
        out = capsys.readouterr().out
        assert "dry-run" in out

    def test_removes_user_scope_legacy_unit(self, tmp_path, monkeypatch, capsys):
        user_dir, _, calls = self._setup(tmp_path, monkeypatch)
        legacy = user_dir / "hermes.service"
        legacy.write_text(self._OUR_UNIT_TEXT, encoding="utf-8")

        removed, remaining = gateway_cli.remove_legacy_hermes_units(interactive=False)

        assert removed == 1
        assert remaining == []
        assert not legacy.exists()
        # Must have invoked stop → disable → daemon-reload on user scope
        cmds_joined = [" ".join(c) for c in calls]
        assert any("--user stop hermes.service" in c for c in cmds_joined)
        assert any("--user disable hermes.service" in c for c in cmds_joined)
        assert any("--user daemon-reload" in c for c in cmds_joined)

    def test_system_scope_without_root_defers_removal(self, tmp_path, monkeypatch, capsys):
        _, system_dir, calls = self._setup(tmp_path, monkeypatch, as_root=False)
        legacy = system_dir / "hermes.service"
        legacy.write_text(self._OUR_UNIT_TEXT, encoding="utf-8")

        removed, remaining = gateway_cli.remove_legacy_hermes_units(interactive=False)

        assert removed == 0
        assert remaining == [legacy]
        assert legacy.exists()  # Not removed — requires sudo
        out = capsys.readouterr().out
        assert "sudo hermes gateway migrate-legacy" in out

    def test_system_scope_with_root_removes(self, tmp_path, monkeypatch, capsys):
        _, system_dir, calls = self._setup(tmp_path, monkeypatch, as_root=True)
        legacy = system_dir / "hermes.service"
        legacy.write_text(self._OUR_UNIT_TEXT, encoding="utf-8")

        removed, remaining = gateway_cli.remove_legacy_hermes_units(interactive=False)

        assert removed == 1
        assert remaining == []
        assert not legacy.exists()
        cmds_joined = [" ".join(c) for c in calls]
        # System-scope uses plain "systemctl" (no --user)
        assert any(
            c.startswith("systemctl stop hermes.service") for c in cmds_joined
        )
        assert any(
            c.startswith("systemctl disable hermes.service") for c in cmds_joined
        )

    def test_removes_both_scopes_with_root(self, tmp_path, monkeypatch, capsys):
        user_dir, system_dir, _ = self._setup(tmp_path, monkeypatch, as_root=True)
        user_legacy = user_dir / "hermes.service"
        system_legacy = system_dir / "hermes.service"
        user_legacy.write_text(self._OUR_UNIT_TEXT, encoding="utf-8")
        system_legacy.write_text(self._OUR_UNIT_TEXT, encoding="utf-8")

        removed, remaining = gateway_cli.remove_legacy_hermes_units(interactive=False)

        assert removed == 2
        assert remaining == []
        assert not user_legacy.exists()
        assert not system_legacy.exists()

    def test_does_not_touch_profile_units_during_migration(
        self, tmp_path, monkeypatch, capsys
    ):
        """Teknium's constraint: profile units (hermes-gateway-coder.service)
        must survive a migration call, even if we somehow include them in the
        search dir."""
        user_dir, _, _ = self._setup(tmp_path, monkeypatch, as_root=True)
        profile_unit = user_dir / "hermes-gateway-coder.service"
        profile_unit.write_text(self._OUR_UNIT_TEXT, encoding="utf-8")
        default_unit = user_dir / "hermes-gateway.service"
        default_unit.write_text(self._OUR_UNIT_TEXT, encoding="utf-8")

        removed, remaining = gateway_cli.remove_legacy_hermes_units(interactive=False)

        assert removed == 0
        assert remaining == []
        # Both the profile unit and the current default unit must survive
        assert profile_unit.exists()
        assert default_unit.exists()

    def test_interactive_prompt_no_skips_removal(self, tmp_path, monkeypatch, capsys):
        """When interactive=True and user answers no, no removal happens."""
        user_dir, _, _ = self._setup(tmp_path, monkeypatch)
        legacy = user_dir / "hermes.service"
        legacy.write_text(self._OUR_UNIT_TEXT, encoding="utf-8")

        monkeypatch.setattr(gateway_cli, "prompt_yes_no", lambda *a, **k: False)

        removed, remaining = gateway_cli.remove_legacy_hermes_units(interactive=True)

        assert removed == 0
        assert remaining == [legacy]
        assert legacy.exists()


class TestMigrateLegacyCommand:
    """Tests for the `hermes gateway migrate-legacy` subcommand dispatch."""

    def test_migrate_legacy_subparser_accepts_dry_run_and_yes(self):
        """Verify the argparse subparser is registered and parses flags."""
        import hermes_cli.main as cli_main

        parser = cli_main.build_parser() if hasattr(cli_main, "build_parser") else None
        # Fall back to calling main's setup helper if direct access isn't exposed
        # The key thing: the subparser must exist. We verify by constructing
        # a namespace through argparse directly — but if build_parser isn't
        # public, just confirm that `hermes gateway --help` shows it.
        import subprocess
        import sys

        project_root = cli_main.PROJECT_ROOT if hasattr(cli_main, "PROJECT_ROOT") else None
        if project_root is None:
            import hermes_cli.gateway as gw
            project_root = gw.PROJECT_ROOT

        result = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "gateway", "--help"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        assert "migrate-legacy" in result.stdout

    def test_gateway_command_migrate_legacy_dispatches(
        self, tmp_path, monkeypatch, capsys
    ):
        """gateway_command(args) with subcmd='migrate-legacy' calls the helper."""
        called = {}

        def fake_remove(interactive=True, dry_run=False):
            called["interactive"] = interactive
            called["dry_run"] = dry_run
            return 0, []

        monkeypatch.setattr(gateway_cli, "remove_legacy_hermes_units", fake_remove)
        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)

        args = SimpleNamespace(
            gateway_command="migrate-legacy", dry_run=False, yes=True
        )
        gateway_cli.gateway_command(args)

        assert called == {"interactive": False, "dry_run": False}


class TestGatewayStatusParser:
    def test_gateway_status_subparser_accepts_full_flag(self):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "gateway", "status", "-l", "--help"],
            cwd=str(gateway_cli.PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )

        assert result.returncode == 0
        assert "unrecognized arguments" not in result.stderr

    def test_gateway_command_migrate_legacy_dry_run_passes_through(
        self, monkeypatch
    ):
        called = {}

        def fake_remove(interactive=True, dry_run=False):
            called["interactive"] = interactive
            called["dry_run"] = dry_run
            return 0, []

        monkeypatch.setattr(gateway_cli, "remove_legacy_hermes_units", fake_remove)
        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)

        args = SimpleNamespace(
            gateway_command="migrate-legacy", dry_run=True, yes=False
        )
        gateway_cli.gateway_command(args)

        assert called == {"interactive": True, "dry_run": True}

    def test_migrate_legacy_on_unsupported_platform_prints_message(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)

        args = SimpleNamespace(
            gateway_command="migrate-legacy", dry_run=False, yes=True
        )
        gateway_cli.gateway_command(args)

        out = capsys.readouterr().out
        assert "only applies to systemd" in out


class TestSystemdInstallOffersLegacyRemoval:
    """Verify that systemd_install prompts to remove legacy units first."""

    def test_install_offers_removal_when_legacy_detected(
        self, tmp_path, monkeypatch, capsys
    ):
        """When legacy units exist, install flow should call the removal
        helper before writing the new unit."""
        remove_called = {}

        def fake_remove(interactive=True, dry_run=False):
            remove_called["invoked"] = True
            remove_called["interactive"] = interactive
            return 1, []

        # has_legacy_hermes_units must return True
        monkeypatch.setattr(gateway_cli, "has_legacy_hermes_units", lambda: True)
        monkeypatch.setattr(gateway_cli, "remove_legacy_hermes_units", fake_remove)
        monkeypatch.setattr(gateway_cli, "print_legacy_unit_warning", lambda: None)
        # Answer "yes" to the legacy-removal prompt
        monkeypatch.setattr(gateway_cli, "prompt_yes_no", lambda *a, **k: True)

        # Mock the rest of the install flow
        unit_path = tmp_path / "hermes-gateway.service"
        monkeypatch.setattr(
            gateway_cli, "get_systemd_unit_path", lambda system=False: unit_path
        )
        monkeypatch.setattr(
            gateway_cli,
            "generate_systemd_unit",
            lambda system=False, run_as_user=None: "unit text\n",
        )
        monkeypatch.setattr(
            gateway_cli.subprocess,
            "run",
            lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="", stderr=""),
        )
        monkeypatch.setattr(gateway_cli, "_ensure_linger_enabled", lambda: None)

        gateway_cli.systemd_install()

        assert remove_called.get("invoked") is True
        assert remove_called.get("interactive") is False  # prompted elsewhere

    def test_install_declines_legacy_removal_when_user_says_no(
        self, tmp_path, monkeypatch
    ):
        """When legacy units exist and user declines, install still proceeds
        but doesn't touch them."""
        remove_called = {"invoked": False}

        def fake_remove(interactive=True, dry_run=False):
            remove_called["invoked"] = True
            return 0, []

        monkeypatch.setattr(gateway_cli, "has_legacy_hermes_units", lambda: True)
        monkeypatch.setattr(gateway_cli, "remove_legacy_hermes_units", fake_remove)
        monkeypatch.setattr(gateway_cli, "print_legacy_unit_warning", lambda: None)
        monkeypatch.setattr(gateway_cli, "prompt_yes_no", lambda *a, **k: False)

        unit_path = tmp_path / "hermes-gateway.service"
        monkeypatch.setattr(
            gateway_cli, "get_systemd_unit_path", lambda system=False: unit_path
        )
        monkeypatch.setattr(
            gateway_cli,
            "generate_systemd_unit",
            lambda system=False, run_as_user=None: "unit text\n",
        )
        monkeypatch.setattr(
            gateway_cli.subprocess,
            "run",
            lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="", stderr=""),
        )
        monkeypatch.setattr(gateway_cli, "_ensure_linger_enabled", lambda: None)

        gateway_cli.systemd_install()

        # Helper must NOT have been called
        assert remove_called["invoked"] is False
        # New unit should still have been written
        assert unit_path.exists()
        assert unit_path.read_text() == "unit text\n"

    def test_install_skips_legacy_check_when_none_present(
        self, tmp_path, monkeypatch
    ):
        """No legacy → no prompt, no helper call."""
        prompt_called = {"count": 0}

        def counting_prompt(*a, **k):
            prompt_called["count"] += 1
            return True

        remove_called = {"invoked": False}

        def fake_remove(interactive=True, dry_run=False):
            remove_called["invoked"] = True
            return 0, []

        monkeypatch.setattr(gateway_cli, "has_legacy_hermes_units", lambda: False)
        monkeypatch.setattr(gateway_cli, "remove_legacy_hermes_units", fake_remove)
        monkeypatch.setattr(gateway_cli, "prompt_yes_no", counting_prompt)

        unit_path = tmp_path / "hermes-gateway.service"
        monkeypatch.setattr(
            gateway_cli, "get_systemd_unit_path", lambda system=False: unit_path
        )
        monkeypatch.setattr(
            gateway_cli,
            "generate_systemd_unit",
            lambda system=False, run_as_user=None: "unit text\n",
        )
        monkeypatch.setattr(
            gateway_cli.subprocess,
            "run",
            lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="", stderr=""),
        )
        monkeypatch.setattr(gateway_cli, "_ensure_linger_enabled", lambda: None)

        gateway_cli.systemd_install()

        assert prompt_called["count"] == 0
        assert remove_called["invoked"] is False


class TestSystemScopeRequiresRootError:
    """Tests for the SystemScopeRequiresRootError replacement of sys.exit(1).

    Before this change, ``_require_root_for_system_service`` called
    ``sys.exit(1)`` when non-root code tried a system-scope systemd
    operation. The wizard's ``except Exception`` guards don't catch
    ``SystemExit`` (it's a ``BaseException`` subclass), so the user was
    dumped at a bare shell prompt mid-setup. The fix raises a typed
    exception instead, which the wizard intercepts and handles with
    actionable remediation.
    """

    def test_require_root_raises_when_non_root(self, monkeypatch):
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 1000)

        with pytest.raises(gateway_cli.SystemScopeRequiresRootError) as excinfo:
            gateway_cli._require_root_for_system_service("start")

        assert excinfo.value.args[0] == "System gateway start requires root. Re-run with sudo."
        assert excinfo.value.args[1] == "start"
        # str(e) renders only the message, not the tuple repr, so that
        # wizard format strings like f"Failed: {e}" print cleanly.
        assert str(excinfo.value) == "System gateway start requires root. Re-run with sudo."
        assert f"Failed: {excinfo.value}" == "Failed: System gateway start requires root. Re-run with sudo."

    def test_require_root_noop_when_root(self, monkeypatch):
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 0)

        # Should not raise, should not exit
        gateway_cli._require_root_for_system_service("start")

    def test_error_is_runtime_error_subclass(self):
        """Wizards use ``except Exception`` guards — the error must be a
        ``RuntimeError`` (catchable by ``Exception``), NOT a ``SystemExit``
        (``BaseException``), so the wizard can recover from it.
        """
        err = gateway_cli.SystemScopeRequiresRootError("msg", "start")
        assert isinstance(err, RuntimeError)
        assert isinstance(err, Exception)
        assert not isinstance(err, SystemExit)


class TestSystemScopeWizardPreCheck:
    """Tests for _system_scope_wizard_would_need_root — the guard the
    wizard uses to detect the dead-end BEFORE prompting the user to start
    a service that will fail without sudo.
    """

    @staticmethod
    def _setup_units(tmp_path, monkeypatch, system_present: bool, user_present: bool):
        sys_dir = tmp_path / "sys"
        usr_dir = tmp_path / "usr"
        sys_dir.mkdir()
        usr_dir.mkdir()
        if system_present:
            (sys_dir / "hermes-gateway.service").write_text("[Unit]\n")
        if user_present:
            (usr_dir / "hermes-gateway.service").write_text("[Unit]\n")
        monkeypatch.setattr(
            gateway_cli,
            "get_systemd_unit_path",
            lambda system=False: (sys_dir if system else usr_dir) / "hermes-gateway.service",
        )

    def test_non_root_with_only_system_unit_returns_true(self, tmp_path, monkeypatch):
        self._setup_units(tmp_path, monkeypatch, system_present=True, user_present=False)
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 1000)

        assert gateway_cli._system_scope_wizard_would_need_root() is True

    def test_root_never_needs_root(self, tmp_path, monkeypatch):
        self._setup_units(tmp_path, monkeypatch, system_present=True, user_present=False)
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 0)

        assert gateway_cli._system_scope_wizard_would_need_root() is False

    def test_non_root_with_user_unit_present_returns_false(self, tmp_path, monkeypatch):
        # User-scope unit present — user can start it themselves, no sudo needed.
        self._setup_units(tmp_path, monkeypatch, system_present=True, user_present=True)
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 1000)

        assert gateway_cli._system_scope_wizard_would_need_root() is False

    def test_non_root_with_no_units_returns_false(self, tmp_path, monkeypatch):
        self._setup_units(tmp_path, monkeypatch, system_present=False, user_present=False)
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 1000)

        assert gateway_cli._system_scope_wizard_would_need_root() is False

    def test_non_root_with_explicit_system_arg_returns_true(self, tmp_path, monkeypatch):
        # Caller passed system=True explicitly (e.g. ``hermes gateway start --system``).
        self._setup_units(tmp_path, monkeypatch, system_present=False, user_present=False)
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 1000)

        assert gateway_cli._system_scope_wizard_would_need_root(system=True) is True


class TestSystemScopeRemediationOutput:
    """Tests for _print_system_scope_remediation — the actionable guidance
    shown when the wizard detects a system-scope-only setup as non-root.
    """

    def test_start_remediation_mentions_sudo_systemctl_and_uninstall(self, capsys, monkeypatch):
        monkeypatch.setattr(gateway_cli, "get_service_name", lambda: "hermes-gateway")

        gateway_cli._print_system_scope_remediation("start")
        out = capsys.readouterr().out

        assert "system-wide service" in out
        assert "start requires root" in out
        assert "sudo systemctl start hermes-gateway" in out
        assert "sudo hermes gateway uninstall --system" in out
        assert "hermes gateway install" in out

    def test_restart_remediation_uses_systemctl_restart(self, capsys, monkeypatch):
        monkeypatch.setattr(gateway_cli, "get_service_name", lambda: "hermes-gateway")

        gateway_cli._print_system_scope_remediation("restart")
        out = capsys.readouterr().out

        assert "restart requires root" in out
        assert "sudo systemctl restart hermes-gateway" in out

    def test_stop_remediation_uses_systemctl_stop(self, capsys, monkeypatch):
        monkeypatch.setattr(gateway_cli, "get_service_name", lambda: "hermes-gateway")

        gateway_cli._print_system_scope_remediation("stop")
        out = capsys.readouterr().out

        assert "stop requires root" in out
        assert "sudo systemctl stop hermes-gateway" in out


class TestGatewayCommandCatchesSystemScopeError:
    """The direct CLI path (``hermes gateway start --system`` etc.) must
    still exit 1 with a clean message when non-root. The top-level
    ``gateway_command`` catches ``SystemScopeRequiresRootError`` and
    converts it back to ``sys.exit(1)``, preserving existing CLI behavior.
    """

    def test_non_root_system_start_exits_one_with_clean_message(self, tmp_path, monkeypatch, capsys):
        sys_dir = tmp_path / "sys"
        usr_dir = tmp_path / "usr"
        sys_dir.mkdir()
        usr_dir.mkdir()
        (sys_dir / "hermes-gateway.service").write_text("[Unit]\n")
        monkeypatch.setattr(
            gateway_cli,
            "get_systemd_unit_path",
            lambda system=False: (sys_dir if system else usr_dir) / "hermes-gateway.service",
        )
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_termux", lambda: False)
        monkeypatch.setattr(gateway_cli, "kill_gateway_processes", lambda **kw: 0)

        args = SimpleNamespace(gateway_command="start", system=True, all=False)

        with pytest.raises(SystemExit) as excinfo:
            gateway_cli.gateway_command(args)

        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        # Renders the message, NOT the ``('msg', 'action')`` tuple repr
        assert "System gateway start requires root. Re-run with sudo." in out
        assert "('" not in out  # no tuple repr leaking through


class TestLaunchdSystemDaemon:
    """macOS gateway status must recognize system LaunchDaemons, not only the user agent."""

    def _force_macos(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_termux", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: False)
        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway_cli, "find_gateway_pids", lambda: [])

    def test_daemon_label_and_path_use_profile_suffix(self, tmp_path, monkeypatch):
        profile_dir = tmp_path / ".hermes" / "profiles" / "company"
        profile_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: profile_dir)

        assert gateway_cli.get_launchd_daemon_label() == "ai.hermes.daemon-company"
        assert gateway_cli.get_launchd_daemon_plist_path() == Path(
            "/Library/LaunchDaemons/ai.hermes.daemon-company.plist"
        )

    def test_daemon_label_default_profile(self, tmp_path, monkeypatch):
        # Default HERMES_HOME (~/.hermes) → no suffix.
        from hermes_constants import get_default_hermes_root

        default_home = get_default_hermes_root()
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: default_home)
        assert gateway_cli.get_launchd_daemon_label() == "ai.hermes.daemon"
        assert gateway_cli.get_launchd_daemon_plist_path() == Path(
            "/Library/LaunchDaemons/ai.hermes.daemon.plist"
        )

    def test_snapshot_recognizes_loaded_system_daemon_without_user_agent(
        self, tmp_path, monkeypatch
    ):
        self._force_macos(monkeypatch)

        agent_plist = tmp_path / "ai.hermes.gateway.plist"  # does not exist
        daemon_plist = tmp_path / "ai.hermes.daemon-nocturnum.plist"
        daemon_plist.write_text("<plist/>", encoding="utf-8")

        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda system=False: agent_plist)
        monkeypatch.setattr(
            gateway_cli, "get_launchd_daemon_plist_path", lambda: daemon_plist
        )
        monkeypatch.setattr(
            gateway_cli, "get_launchd_daemon_label", lambda: "ai.hermes.daemon-nocturnum"
        )

        recorded = []

        def fake_run(cmd, capture_output=True, text=True, timeout=10, **kwargs):
            recorded.append(list(cmd))
            if cmd[:2] == ["launchctl", "print"]:
                return SimpleNamespace(returncode=0, stdout="loaded\n", stderr="")
            return SimpleNamespace(returncode=113, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        snapshot = gateway_cli.get_gateway_runtime_snapshot()

        assert snapshot.service_installed is True
        assert snapshot.service_running is True
        assert snapshot.service_scope == "launchd-system"
        assert "system" in snapshot.manager
        # Probe must be read-only (no sudo, uses `launchctl print system/...`).
        assert any(
            cmd[:2] == ["launchctl", "print"] and cmd[2] == "system/ai.hermes.daemon-nocturnum"
            for cmd in recorded
        )
        assert not any("sudo" in part for cmd in recorded for part in cmd)

    def test_snapshot_marks_daemon_installed_but_not_running(self, tmp_path, monkeypatch):
        self._force_macos(monkeypatch)

        agent_plist = tmp_path / "ai.hermes.gateway.plist"  # does not exist
        daemon_plist = tmp_path / "ai.hermes.daemon.plist"
        daemon_plist.write_text("<plist/>", encoding="utf-8")

        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda system=False: agent_plist)
        monkeypatch.setattr(
            gateway_cli, "get_launchd_daemon_plist_path", lambda: daemon_plist
        )

        monkeypatch.setattr(
            gateway_cli.subprocess,
            "run",
            lambda *a, **kw: SimpleNamespace(
                returncode=113, stdout="", stderr="Could not find service"
            ),
        )

        snapshot = gateway_cli.get_gateway_runtime_snapshot()

        assert snapshot.service_installed is True
        assert snapshot.service_running is False
        assert snapshot.service_scope == "launchd-system"

    def test_snapshot_prefers_running_user_agent_over_stopped_daemon(
        self, tmp_path, monkeypatch
    ):
        """Mixed install: both plists present, only the user agent is loaded.
        Snapshot must reflect the live owner (agent), not the dormant daemon."""
        self._force_macos(monkeypatch)

        agent_plist = tmp_path / "ai.hermes.gateway.plist"
        agent_plist.write_text("<plist/>", encoding="utf-8")
        daemon_plist = tmp_path / "ai.hermes.daemon-nocturnum.plist"
        daemon_plist.write_text("<plist/>", encoding="utf-8")

        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda system=False: agent_plist)
        monkeypatch.setattr(
            gateway_cli, "get_launchd_daemon_plist_path", lambda: daemon_plist
        )
        monkeypatch.setattr(
            gateway_cli, "get_launchd_daemon_label", lambda: "ai.hermes.daemon-nocturnum"
        )

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["launchctl", "list"]:
                return SimpleNamespace(returncode=0, stdout="loaded\n", stderr="")
            if cmd[:2] == ["launchctl", "print"]:
                return SimpleNamespace(returncode=113, stdout="", stderr="not loaded")
            return SimpleNamespace(returncode=113, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        snapshot = gateway_cli.get_gateway_runtime_snapshot()

        assert snapshot.service_installed is True
        assert snapshot.service_running is True
        assert snapshot.manager == "launchd"
        assert snapshot.service_scope == "launchd"

    def test_snapshot_unchanged_when_neither_agent_nor_daemon_installed(
        self, tmp_path, monkeypatch
    ):
        self._force_macos(monkeypatch)

        agent_plist = tmp_path / "ai.hermes.gateway.plist"
        daemon_plist = tmp_path / "ai.hermes.daemon.plist"
        # Neither file is created.
        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda system=False: agent_plist)
        monkeypatch.setattr(
            gateway_cli, "get_launchd_daemon_plist_path", lambda: daemon_plist
        )

        ran = []

        def fake_run(cmd, **kwargs):
            ran.append(list(cmd))
            return SimpleNamespace(returncode=113, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        snapshot = gateway_cli.get_gateway_runtime_snapshot()

        assert snapshot.service_installed is False
        assert snapshot.service_running is False
        assert snapshot.service_scope == "launchd"
        # Both probes short-circuit on the missing plist files — no launchctl
        # subprocess should be invoked at all.
        assert ran == []

    def test_launchd_status_reports_loaded_system_daemon(self, tmp_path, monkeypatch, capsys):
        self._force_macos(monkeypatch)

        agent_plist = tmp_path / "ai.hermes.gateway.plist"  # absent
        daemon_plist = tmp_path / "ai.hermes.daemon-nocturnum.plist"
        daemon_plist.write_text("<plist/>", encoding="utf-8")

        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda system=False: agent_plist)
        monkeypatch.setattr(
            gateway_cli, "get_launchd_daemon_plist_path", lambda: daemon_plist
        )
        monkeypatch.setattr(
            gateway_cli, "get_launchd_daemon_label", lambda: "ai.hermes.daemon-nocturnum"
        )

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["launchctl", "print"]:
                return SimpleNamespace(returncode=0, stdout="loaded", stderr="")
            return SimpleNamespace(returncode=113, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        gateway_cli.launchd_status()

        out = capsys.readouterr().out
        assert "System LaunchDaemon" in out
        assert "ai.hermes.daemon-nocturnum" in out
        assert "is loaded" in out
        # Should not falsely claim the user-agent is unloaded when no agent
        # plist exists.
        assert "User LaunchAgent is not loaded" not in out

    def test_launchd_status_mixed_install_does_not_flag_agent_when_daemon_loaded(
        self, tmp_path, monkeypatch, capsys
    ):
        """When both plists exist but only the system daemon is loaded, status
        must not present the user agent as the primary failed service."""
        self._force_macos(monkeypatch)

        agent_plist = tmp_path / "ai.hermes.gateway.plist"
        agent_plist.write_text("<plist/>", encoding="utf-8")
        daemon_plist = tmp_path / "ai.hermes.daemon-nocturnum.plist"
        daemon_plist.write_text("<plist/>", encoding="utf-8")

        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda system=False: agent_plist)
        monkeypatch.setattr(
            gateway_cli, "get_launchd_daemon_plist_path", lambda: daemon_plist
        )
        monkeypatch.setattr(
            gateway_cli, "get_launchd_daemon_label", lambda: "ai.hermes.daemon-nocturnum"
        )
        monkeypatch.setattr(gateway_cli, "launchd_plist_is_current", lambda system=False: True)

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["launchctl", "print"]:
                return SimpleNamespace(returncode=0, stdout="loaded", stderr="")
            if cmd[:2] == ["launchctl", "list"]:
                return SimpleNamespace(returncode=113, stdout="", stderr="not loaded")
            return SimpleNamespace(returncode=113, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        gateway_cli.launchd_status()

        out = capsys.readouterr().out
        # Healthy system daemon must still be reported.
        assert "System LaunchDaemon" in out
        assert "ai.hermes.daemon-nocturnum" in out
        assert "is loaded" in out
        # User LaunchAgent plist may be mentioned, but must not be flagged as
        # the primary failed service when the system daemon owns the gateway.
        assert "User LaunchAgent is not loaded" not in out
        # Remediation hint must not nudge the user to start the user agent
        # when the system daemon is already healthy.
        assert "Run: hermes gateway start" not in out


class TestLaunchdRunAsUserResolution:
    """``_resolve_launchd_run_as_user`` must never silently emit UserName=root."""

    def test_explicit_run_as_user_wins(self, monkeypatch):
        # Even under sudo, an explicit value is honored verbatim.
        monkeypatch.setenv("SUDO_USER", "alice")
        monkeypatch.setattr(os, "getuid", lambda: 0)
        monkeypatch.setattr(
            pwd, "getpwnam",
            lambda name: SimpleNamespace(pw_name=name, pw_dir=f"/home/{name}", pw_uid=1000, pw_gid=1000),
        )
        assert gateway_cli._resolve_launchd_run_as_user("bob") == "bob"

    def test_explicit_run_as_user_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("SUDO_USER", "alice")
        monkeypatch.setattr(
            pwd, "getpwnam",
            lambda name: SimpleNamespace(pw_name=name, pw_dir=f"/home/{name}", pw_uid=1000, pw_gid=1000),
        )
        assert gateway_cli._resolve_launchd_run_as_user("  bob  ") == "bob"

    def test_explicit_run_as_user_missing_account_raises(self, monkeypatch):
        # --run-as-user must validate against the local password database
        # before the resolver returns. Missing accounts must fail before any
        # plist is written or launchctl is invoked.
        def boom(name):
            raise KeyError(name)

        monkeypatch.setattr(pwd, "getpwnam", boom)
        with pytest.raises(ValueError, match="unknown account"):
            gateway_cli._resolve_launchd_run_as_user("nonexistent-xyzzy")

    def test_explicit_root_is_allowed(self, monkeypatch):
        # Operator opt-in to root is still allowed when stated explicitly.
        monkeypatch.delenv("SUDO_USER", raising=False)
        monkeypatch.setattr(os, "getuid", lambda: 0)
        assert gateway_cli._resolve_launchd_run_as_user("root") == "root"

    def test_falls_back_to_sudo_user_when_running_as_root(self, monkeypatch):
        # `sudo hermes gateway install --system`: euid=0, SUDO_USER points at
        # the invoking operator. The resolver picks SUDO_USER, NOT root.
        real_user = pwd.getpwuid(os.getuid()).pw_name
        if real_user == "root":
            pytest.skip("test must run as a non-root user to exercise this path")
        monkeypatch.setenv("SUDO_USER", real_user)
        monkeypatch.setattr(os, "getuid", lambda: 0)
        resolved = gateway_cli._resolve_launchd_run_as_user(None)
        assert resolved == real_user
        assert resolved != "root"

    def test_ignores_sudo_user_equal_to_root(self, monkeypatch):
        # SUDO_USER=root is meaningless (root sudo'd to root). Move on to the
        # current-process-user fallback; if that's also root, raise.
        monkeypatch.setenv("SUDO_USER", "root")
        monkeypatch.setattr(os, "getuid", lambda: 0)
        with pytest.raises(ValueError, match="--run-as-user"):
            gateway_cli._resolve_launchd_run_as_user(None)

    def test_ignores_unknown_sudo_user(self, monkeypatch):
        # SUDO_USER references a deleted account → skip and try the next
        # fallback. With euid 0 and no other non-root option, raise.
        monkeypatch.setenv("SUDO_USER", "nonexistent-user-xyzzy")
        monkeypatch.setattr(os, "getuid", lambda: 0)
        with pytest.raises(ValueError, match="--run-as-user"):
            gateway_cli._resolve_launchd_run_as_user(None)

    def test_falls_back_to_current_user_when_non_root(self, monkeypatch):
        monkeypatch.delenv("SUDO_USER", raising=False)
        # Non-root invocation: just use whoami.
        username = pwd.getpwuid(os.getuid()).pw_name
        # Skip if test happens to run as root; the fallback would correctly
        # raise instead.
        if username == "root":
            pytest.skip("test must run as a non-root user to exercise this path")
        assert gateway_cli._resolve_launchd_run_as_user(None) == username

    def test_root_with_no_non_root_candidate_raises(self, monkeypatch):
        # euid 0, no SUDO_USER, current process *is* root → must refuse.
        import pwd as _pwd

        monkeypatch.delenv("SUDO_USER", raising=False)
        monkeypatch.setattr(os, "getuid", lambda: 0)
        # Force pwd.getpwuid(...).pw_name = "root" for any uid lookup the
        # resolver performs.
        monkeypatch.setattr(
            _pwd,
            "getpwuid",
            lambda _uid: SimpleNamespace(pw_name="root", pw_dir="/root", pw_gid=0),
        )
        with pytest.raises(ValueError, match="--run-as-user"):
            gateway_cli._resolve_launchd_run_as_user(None)

    def test_generated_system_plist_uses_resolved_non_root_user(self, monkeypatch, tmp_path):
        # The plist payload must reflect whatever the resolver picked.
        real_user = pwd.getpwuid(os.getuid()).pw_name
        if real_user == "root":
            pytest.skip("test must run as a non-root user to exercise this path")
        monkeypatch.setenv("SUDO_USER", real_user)
        monkeypatch.setattr(os, "getuid", lambda: 0)
        # Avoid touching the real ~/.hermes/logs directory under sudo.
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: tmp_path)
        plist = gateway_cli.generate_launchd_plist(system=True)
        assert "<key>UserName</key>" in plist
        assert f"<string>{real_user}</string>" in plist
        # No silent UserName=root payload should ever appear.
        assert "<key>UserName</key>\n    <string>root</string>" not in plist


class TestLaunchdSystemPlistTargetUserRemapping:
    """Sudo-installed system LaunchDaemons must point at the *target* user's
    home, not the calling (root) home, for HERMES_HOME / HOME / USER /
    LOGNAME / VIRTUAL_ENV / paths / log files. Otherwise the daemon spawns
    as ``alice`` but reads root's profile and never finds ``~alice/.hermes``.
    """

    def _patch_sudo_alice(self, monkeypatch, tmp_path, *, root_home="/var/root"):
        """Simulate ``sudo hermes gateway install --system --run-as-user alice``
        from a root login shell where:

        * the calling process is root with HOME=/var/root
        * SUDO_USER=alice
        * pwd.getpwnam("alice") resolves to a tmp-backed home directory
        * ``get_hermes_home()`` resolves under root's home (the bug being fixed)
        """
        alice_home = tmp_path / "Users" / "alice"
        alice_home.mkdir(parents=True)
        # Calling-user state (root under sudo).
        monkeypatch.setenv("SUDO_USER", "alice")
        monkeypatch.setenv("HOME", root_home)
        monkeypatch.setenv("HERMES_HOME", f"{root_home}/.hermes")
        monkeypatch.setattr(os, "getuid", lambda: 0)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: Path(root_home)))
        # Force get_hermes_home() to resolve under root, mirroring real sudo.
        monkeypatch.setattr(
            gateway_cli, "get_hermes_home", lambda: Path(f"{root_home}/.hermes")
        )

        # pwd records: alice exists, root exists, missing accounts blow up.
        real_pwd = {
            "alice": SimpleNamespace(
                pw_name="alice", pw_dir=str(alice_home), pw_uid=1001, pw_gid=1001
            ),
            "root": SimpleNamespace(
                pw_name="root", pw_dir=root_home, pw_uid=0, pw_gid=0
            ),
        }

        def fake_getpwnam(name):
            try:
                return real_pwd[name]
            except KeyError as e:
                raise KeyError(name) from e

        monkeypatch.setattr(pwd, "getpwnam", fake_getpwnam)
        return alice_home

    def _capture_chowns(self, monkeypatch):
        """Hook both ``os.chown`` (path-based — must NOT fire for the
        target-user Hermes/log dirs, otherwise the TOCTOU window between
        verification and chown reopens) and ``os.fchown`` (handle-based —
        records ``(inode, uid, gid)`` so callers can map fds back to
        expected directories via ``os.stat(path).st_ino``).

        Returns ``(chown_calls, fchown_records)``.
        """
        chown_calls: list[tuple[str, int, int]] = []
        fchown_records: list[tuple[int, int, int]] = []

        def fake_chown(path, uid, gid):
            chown_calls.append((str(path), uid, gid))

        def fake_fchown(fd, uid, gid):
            # Resolve the fd to its inode while it is still open — the
            # helper closes fds immediately after fchown returns.
            fchown_records.append((os.fstat(fd).st_ino, uid, gid))

        monkeypatch.setattr(gateway_cli.os, "chown", fake_chown)
        monkeypatch.setattr(gateway_cli.os, "fchown", fake_fchown)
        return chown_calls, fchown_records

    def test_system_plist_remaps_home_user_logname_and_hermes_home_to_target(
        self, monkeypatch, tmp_path
    ):
        alice_home = self._patch_sudo_alice(monkeypatch, tmp_path)

        plist = gateway_cli.generate_launchd_plist(system=True, run_as_user="alice")

        assert "<key>UserName</key>" in plist
        assert "<string>alice</string>" in plist
        # Target-user environment must be set so HOME-aware code under the
        # daemon (config loaders, ssh keys, etc.) doesn't read /var/root.
        assert "<key>HOME</key>" in plist
        assert f"<string>{alice_home}</string>" in plist
        assert "<key>USER</key>" in plist
        assert "<key>LOGNAME</key>" in plist
        # HERMES_HOME must follow alice, not root.
        assert f"<string>{alice_home}/.hermes</string>" in plist
        # And no /var/root leakage anywhere in the plist payload.
        assert "/var/root" not in plist

    def test_system_plist_logs_under_target_hermes_home_not_root(
        self, monkeypatch, tmp_path
    ):
        alice_home = self._patch_sudo_alice(monkeypatch, tmp_path)

        plist = gateway_cli.generate_launchd_plist(system=True, run_as_user="alice")

        expected_log_dir = alice_home / ".hermes" / "logs"
        assert f"<string>{expected_log_dir}/gateway.log</string>" in plist
        assert f"<string>{expected_log_dir}/gateway.error.log</string>" in plist
        # Negative: the bug emitted /var/root/.hermes/logs.
        assert "/var/root/.hermes/logs" not in plist
        # Side-effect: the target log directory should now exist (best
        # effort) so launchd can open StandardOutPath on first run.
        assert expected_log_dir.exists()

    def test_system_plist_chowns_full_target_hermes_chain_not_just_logs(
        self, monkeypatch, tmp_path
    ):
        """When sudo/root generates a system LaunchDaemon plist, chowning
        only the leaf ``logs/`` dir leaves the ``HERMES_HOME`` (and any
        ``.hermes/profiles/<name>/`` parent we just mkdir-as-root) owned
        by root. The daemon then launches as ``alice`` and cannot write
        ``gateway.pid`` into its own HERMES_HOME. Ownership must be
        applied to the full target chain we created, not just the logs
        leaf — but never beyond the target user's home tree.
        """
        alice_home = self._patch_sudo_alice(monkeypatch, tmp_path)
        # Use a profile-mapped HERMES_HOME so we exercise the intermediate
        # ``.hermes/profiles/coder/`` directory that mkdir(parents=True)
        # creates as root.
        monkeypatch.setattr(
            gateway_cli,
            "get_hermes_home",
            lambda: Path("/var/root/.hermes/profiles/coder"),
        )
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 0)

        chown_calls, fchown_records = self._capture_chowns(monkeypatch)

        gateway_cli.generate_launchd_plist(system=True, run_as_user="alice")

        expected_hermes_home = alice_home / ".hermes" / "profiles" / "coder"
        expected_log_dir = expected_hermes_home / "logs"
        expected_profiles_dir = alice_home / ".hermes" / "profiles"
        expected_dot_hermes = alice_home / ".hermes"

        # Path-based ``os.chown`` is TOCTOU-vulnerable for target-user
        # paths and must not be used here — ownership flows through
        # ``os.fchown`` on a handle opened with ``O_NOFOLLOW``.
        assert chown_calls == []

        chowned_inodes = {ino for ino, _u, _g in fchown_records}
        # Daemon writes gateway.pid here — must be alice-owned.
        assert os.stat(expected_hermes_home).st_ino in chowned_inodes
        # StandardOutPath/StandardErrorPath open here.
        assert os.stat(expected_log_dir).st_ino in chowned_inodes
        # Intermediates created by mkdir as root must also flip ownership
        # so alice can traverse them.
        assert os.stat(expected_profiles_dir).st_ino in chowned_inodes
        assert os.stat(expected_dot_hermes).st_ino in chowned_inodes

        # Every fchown must target alice's uid/gid, never root's.
        for _ino, uid, gid in fchown_records:
            assert (uid, gid) == (1001, 1001)

        # Safety: never chown the user's home itself or anything outside
        # the target ``.hermes`` tree. Each verified inode must match a
        # directory under the target ``.hermes`` chain.
        allowed_inodes = {
            os.stat(p).st_ino
            for p in (
                expected_dot_hermes,
                expected_profiles_dir,
                expected_hermes_home,
                expected_log_dir,
            )
        }
        assert os.stat(alice_home).st_ino not in chowned_inodes
        for ino in chowned_inodes:
            assert ino in allowed_inodes, (
                f"fchown targeted an inode outside the target .hermes tree: {ino}"
            )

    def test_system_plist_skips_chown_for_custom_hermes_home_outside_user_tree(
        self, monkeypatch, tmp_path
    ):
        """Custom HERMES_HOME paths that don't live under the target
        user's home (e.g. /opt/hermes) must NOT trigger a chown walk
        across arbitrary parents. We only chown the explicit hermes_home
        + logs leaf and stop there.
        """
        self._patch_sudo_alice(monkeypatch, tmp_path)
        custom_home = tmp_path / "opt" / "custom-hermes"
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: custom_home)
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 0)

        chown_calls, fchown_records = self._capture_chowns(monkeypatch)

        gateway_cli.generate_launchd_plist(system=True, run_as_user="alice")

        # No path-based chown for the target-user dirs.
        assert chown_calls == []

        chowned_inodes = {ino for ino, _u, _g in fchown_records}
        # Only the explicit hermes_home + its logs leaf — no ancestor walk.
        assert chowned_inodes == {
            os.stat(custom_home).st_ino,
            os.stat(custom_home / "logs").st_ino,
        }

    def test_system_plist_skips_chown_when_not_root(
        self, monkeypatch, tmp_path
    ):
        """Non-root callers (e.g. running tests, plist regeneration via
        ``launchd_plist_is_current``) must never invoke os.chown — that
        would either fail with EPERM or, worse on misconfigured systems,
        flip ownership unexpectedly.
        """
        self._patch_sudo_alice(monkeypatch, tmp_path)
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 501)

        chown_calls, fchown_records = self._capture_chowns(monkeypatch)

        gateway_cli.generate_launchd_plist(system=True, run_as_user="alice")

        assert chown_calls == []
        assert fchown_records == []

    def test_system_plist_refuses_to_chown_when_dot_hermes_is_a_symlink(
        self, monkeypatch, tmp_path
    ):
        """Security: a pre-existing or attacker-planted symlink at any
        component of the target user's Hermes chain (e.g.
        ``~alice/.hermes -> /etc``) must NOT trigger any ``os.chown`` —
        otherwise root would flip ownership of the symlink target (here
        an arbitrary attacker-controlled directory).
        """
        alice_home = self._patch_sudo_alice(monkeypatch, tmp_path)
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 0)

        attacker_target = tmp_path / "attacker_target"
        attacker_target.mkdir()
        (alice_home / ".hermes").symlink_to(attacker_target)

        chown_calls, fchown_records = self._capture_chowns(monkeypatch)

        gateway_cli.generate_launchd_plist(system=True, run_as_user="alice")

        # Hostile component → entire walk aborts; nothing is chowned via
        # either the path-based or handle-based code path.
        assert chown_calls == []
        assert fchown_records == []
        # Defence in depth: the symlink target must remain untouched —
        # neither directly nor via the symlink path.
        assert not (attacker_target / "logs").exists(), (
            "log_dir was created inside the symlink target — symlink was followed"
        )

    def test_system_plist_refuses_to_chown_when_log_dir_is_a_symlink(
        self, monkeypatch, tmp_path
    ):
        """Security: a symlink at the log-leaf component (e.g.
        ``~alice/.hermes/logs -> /var/log/system_dir``) must also abort
        the chown walk — the entire chain is rejected when any element
        fails the lstat check, so neither the symlink nor any earlier
        verified component gets chowned.
        """
        alice_home = self._patch_sudo_alice(monkeypatch, tmp_path)
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 0)

        # ``.hermes`` is a real directory but ``logs`` is a symlink that
        # points outside the user's tree.
        (alice_home / ".hermes").mkdir()
        attacker_target = tmp_path / "stolen_logs"
        attacker_target.mkdir()
        (alice_home / ".hermes" / "logs").symlink_to(attacker_target)

        chown_calls, fchown_records = self._capture_chowns(monkeypatch)

        gateway_cli.generate_launchd_plist(system=True, run_as_user="alice")

        # No path-based chown is performed for any target-user dir.
        assert chown_calls == []
        # The symlink target's inode must NEVER appear in fchown records
        # (that would mean ``open(O_NOFOLLOW)`` traversed the symlink).
        chowned_inodes = {ino for ino, _u, _g in fchown_records}
        assert os.stat(attacker_target).st_ino not in chowned_inodes

    def test_system_plist_refuses_to_chown_when_intermediate_profile_dir_is_a_symlink(
        self, monkeypatch, tmp_path
    ):
        """Security: when HERMES_HOME maps to ``~alice/.hermes/profiles/coder``
        but ``~alice/.hermes/profiles`` is already a symlink, the walk must
        reject that intermediate component before mkdir traverses through it
        — otherwise root would create ``coder/`` and ``coder/logs/`` under
        the symlink target and chown them.
        """
        alice_home = self._patch_sudo_alice(monkeypatch, tmp_path)
        monkeypatch.setattr(
            gateway_cli,
            "get_hermes_home",
            lambda: Path("/var/root/.hermes/profiles/coder"),
        )
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 0)

        attacker_target = tmp_path / "stolen_profiles"
        attacker_target.mkdir()
        (alice_home / ".hermes").mkdir()
        (alice_home / ".hermes" / "profiles").symlink_to(attacker_target)

        chown_calls, fchown_records = self._capture_chowns(monkeypatch)

        gateway_cli.generate_launchd_plist(system=True, run_as_user="alice")

        # Walk aborted at the symlinked intermediate; no chown happens
        # via either code path, and the symlink target must NOT have
        # ``coder/`` written into it.
        assert chown_calls == []
        assert fchown_records == []
        assert not (attacker_target / "coder").exists(), (
            "intermediate symlink was followed and traversed by mkdir"
        )

    def test_system_plist_uses_handle_based_fchown_not_path_chown_for_target_dirs(
        self, monkeypatch, tmp_path
    ):
        """TOCTOU defence: ownership for the target-user Hermes/log
        chain MUST flow through ``os.fchown`` on a handle opened with
        ``O_RDONLY|O_DIRECTORY|O_NOFOLLOW``, never through path-based
        ``os.chown``. With path-based chown the target user could swap
        a verified component for a symlink between the lstat check and
        the chown syscall, redirecting ownership to an attacker-chosen
        path. Asserting both that ``os.chown`` was not called for these
        dirs and that ``os.fchown`` was invoked on fds whose inodes
        match the verified directories pins this contract.
        """
        alice_home = self._patch_sudo_alice(monkeypatch, tmp_path)
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 0)

        chown_calls, fchown_records = self._capture_chowns(monkeypatch)

        # Capture the (path, fd) pairs the helper actually opens so the
        # test can prove the fchown'd fd really refers to the directory
        # we expected, not e.g. a parent that happens to share an inode
        # collision in some other corner of the filesystem. The helper
        # walks child components using ``dir_fd`` so the recorded ``path``
        # for non-anchor opens is a *relative* name; we reconstruct the
        # absolute path by joining onto the dir_fd's recorded path.
        real_open = os.open
        opened_fds: dict[int, str] = {}

        def tracking_open(path, flags, mode=0o777, *args, **kwargs):
            dir_fd = kwargs.get("dir_fd")
            fd = real_open(path, flags, mode, *args, **kwargs)
            if flags & os.O_DIRECTORY and flags & os.O_NOFOLLOW:
                if dir_fd is not None and dir_fd in opened_fds:
                    opened_fds[fd] = os.path.join(opened_fds[dir_fd], str(path))
                else:
                    opened_fds[fd] = str(path)
            return fd

        monkeypatch.setattr(gateway_cli.os, "open", tracking_open)

        gateway_cli.generate_launchd_plist(system=True, run_as_user="alice")

        # Hard requirement: path-based chown must not touch the
        # target-user dirs — that is the TOCTOU vector being closed.
        assert chown_calls == [], (
            "os.chown was called for target-user dirs — this re-opens "
            "the lstat→chown TOCTOU window."
        )
        # And fchown must have done the actual ownership transfer for
        # both the HERMES_HOME and the log leaf, with the target user's
        # uid/gid.
        assert fchown_records, "os.fchown was never invoked for target dirs"
        for _ino, uid, gid in fchown_records:
            assert (uid, gid) == (1001, 1001)

        expected_dot_hermes = alice_home / ".hermes"
        expected_log_dir = expected_dot_hermes / "logs"
        chowned_inodes = {ino for ino, _u, _g in fchown_records}
        assert os.stat(expected_dot_hermes).st_ino in chowned_inodes
        assert os.stat(expected_log_dir).st_ino in chowned_inodes

        # And the helper opened those exact paths with O_NOFOLLOW (i.e.
        # rejected symlink traversal at the kernel level rather than
        # relying on a TOCTOU-prone lstat).
        opened_paths = set(opened_fds.values())
        assert str(expected_dot_hermes) in opened_paths
        assert str(expected_log_dir) in opened_paths

    def test_system_plist_descend_is_dir_fd_anchored_against_parent_swap(
        self, monkeypatch, tmp_path
    ):
        """TOCTOU defence (parent swap mid-walk): ``O_NOFOLLOW`` on an
        absolute-path open only protects the *final* component, so an
        iterative chain that re-opens each component by absolute path is
        racy — between iterations the target user can rename a verified
        parent (e.g. ``~alice/.hermes``) and replace it with a symlink,
        and the next absolute-path ``mkdir`` / ``open`` would create /
        chown the child *under the swapped target*. The helper must walk
        from a held ``target_home`` fd downward using
        ``dir_fd=parent_fd`` for every child mkdir/open so the kernel
        resolves child names against the inode we already pinned, not
        against the swapped absolute path.

        We simulate the swap by hooking ``os.open``: as soon as the
        helper opens ``~alice/.hermes`` (the verified parent), we
        rename that real directory aside and replace
        ``~alice/.hermes`` on the path with a symlink pointing at an
        attacker-controlled directory. With dir_fd-anchored descent the
        next ``mkdir("logs", dir_fd=fd_of_original_dot_hermes)`` lands
        inside the *original* (renamed-aside) directory, and nothing
        is created or chowned under the attacker target.
        """
        alice_home = self._patch_sudo_alice(monkeypatch, tmp_path)
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 0)

        # Pre-create ``~alice/.hermes`` so the first iteration verifies a
        # real directory and the swap fires after its fd is held.
        real_dot_hermes = alice_home / ".hermes"
        real_dot_hermes.mkdir()
        attacker_target = tmp_path / "attacker_dir"
        attacker_target.mkdir()
        moved_aside = alice_home / ".hermes_real_inode"

        chown_calls, fchown_records = self._capture_chowns(monkeypatch)

        # Track absolute mkdir paths (any mkdir without dir_fd that
        # touches the swapped path is a violation).
        real_mkdir = os.mkdir
        absolute_mkdir_calls: list[str] = []

        def tracking_mkdir(path, mode=0o777, *args, **kwargs):
            if kwargs.get("dir_fd") is None:
                absolute_mkdir_calls.append(str(path))
            return real_mkdir(path, mode, *args, **kwargs)

        monkeypatch.setattr(gateway_cli.os, "mkdir", tracking_mkdir)

        real_open = os.open
        swap_fired = {"done": False}

        def swapping_open(path, flags, mode=0o777, *args, **kwargs):
            fd = real_open(path, flags, mode, *args, **kwargs)
            if (
                not swap_fired["done"]
                and flags & os.O_DIRECTORY
                and flags & os.O_NOFOLLOW
                and kwargs.get("dir_fd") is not None
                and str(path) == ".hermes"
            ):
                # The helper has just verified and pinned the real
                # ``~alice/.hermes`` inode. Now race a parent swap: move
                # the real dir aside and put a symlink to an attacker
                # target in its place. Any later absolute-path
                # ``mkdir(~alice/.hermes/logs)`` would land in the
                # attacker dir; a dir_fd-anchored descent uses the held
                # fd and is unaffected.
                real_dot_hermes.rename(moved_aside)
                os.symlink(attacker_target, real_dot_hermes)
                swap_fired["done"] = True
            return fd

        monkeypatch.setattr(gateway_cli.os, "open", swapping_open)

        gateway_cli.generate_launchd_plist(system=True, run_as_user="alice")

        # The swap must actually have fired — otherwise the test isn't
        # exercising the parent-swap path.
        assert swap_fired["done"], (
            "test setup did not trigger the simulated parent swap"
        )

        # Hard requirement #1: no path-based ``os.chown`` for target dirs.
        assert chown_calls == [], (
            "path-based os.chown was called for target-user dirs — that "
            "re-opens the lstat→chown TOCTOU window."
        )

        # Hard requirement #2: no absolute-path ``os.mkdir`` ever
        # targeted a child of target_home/.hermes. The dir_fd descent
        # must use relative names anchored on the held parent fd, so
        # any absolute mkdir under the (now-swapped) ``~alice/.hermes``
        # path is exactly the bug.
        forbidden_prefix = str(alice_home / ".hermes") + os.sep
        for p in absolute_mkdir_calls:
            assert not p.startswith(forbidden_prefix), (
                f"absolute-path mkdir under swapped parent: {p!r} — "
                "child mkdirs must use dir_fd against the verified "
                "parent fd, not the swapped absolute path."
            )

        # Hard requirement #3: nothing was created inside the attacker
        # directory. If the helper had used absolute-path
        # mkdir/open after the swap, ``logs/`` would have appeared
        # under attacker_target via the symlink.
        assert not (attacker_target / "logs").exists(), (
            "log dir was created inside the attacker symlink target — "
            "child create followed the swapped parent path."
        )
        assert list(attacker_target.iterdir()) == [], (
            f"attacker target was written to: {list(attacker_target.iterdir())}"
        )

        # And the actual log dir should live inside the *original*
        # (renamed-aside) ``.hermes`` inode — that's where the held fd
        # pointed when we created ``logs``.
        assert (moved_aside / "logs").is_dir(), (
            "log dir was not created under the held parent fd's inode"
        )

        # fchown must have flipped ownership on alice's uid/gid and
        # only on inodes inside the original .hermes tree.
        chowned_inodes = {ino for ino, _u, _g in fchown_records}
        for _ino, uid, gid in fchown_records:
            assert (uid, gid) == (1001, 1001)
        attacker_inode = os.stat(attacker_target).st_ino
        assert attacker_inode not in chowned_inodes, (
            "fchown landed on attacker-controlled inode"
        )
        # The original .hermes and its logs leaf should be the chowned set.
        assert os.stat(moved_aside).st_ino in chowned_inodes
        assert os.stat(moved_aside / "logs").st_ino in chowned_inodes

    def test_system_plist_remaps_paths_living_under_calling_user_home(
        self, monkeypatch, tmp_path
    ):
        alice_home = self._patch_sudo_alice(monkeypatch, tmp_path)
        # Pretend the venv was detected under root's home — that's exactly
        # the wrong path to bake into a daemon that runs as alice.
        root_venv = Path("/var/root/hermes-agent/venv")
        monkeypatch.setattr(gateway_cli, "_detect_venv_dir", lambda: root_venv)
        monkeypatch.setattr(
            gateway_cli, "get_python_path", lambda: str(root_venv / "bin" / "python"),
        )
        # Likewise PROJECT_ROOT could live under root's home.
        monkeypatch.setattr(
            gateway_cli, "PROJECT_ROOT", Path("/var/root/hermes-agent"),
        )
        # Caller's PATH includes a /var/root/.local/bin entry that must be
        # rewritten so the daemon can execute it.
        monkeypatch.setenv(
            "PATH", "/var/root/.local/bin:/usr/local/bin:/usr/bin:/bin",
        )

        plist = gateway_cli.generate_launchd_plist(system=True, run_as_user="alice")

        # VIRTUAL_ENV / venv bin / python all under alice now.
        assert f"<string>{alice_home}/hermes-agent/venv</string>" in plist
        assert f"{alice_home}/hermes-agent/venv/bin/python" in plist
        # PATH no longer references /var/root.
        assert "/var/root" not in plist
        # Common system bin that did NOT live under root's home is preserved.
        assert "/usr/local/bin" in plist

    def test_system_plist_remaps_resolved_node_dir_under_calling_user_home(
        self, monkeypatch, tmp_path
    ):
        alice_home = self._patch_sudo_alice(monkeypatch, tmp_path)
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(
            gateway_cli.shutil,
            "which",
            lambda name: "/var/root/.nvm/versions/node/v22.0.0/bin/node" if name == "node" else None,
        )

        plist = gateway_cli.generate_launchd_plist(system=True, run_as_user="alice")

        assert "/var/root" not in plist
        assert f"{alice_home}/.nvm/versions/node/v22.0.0/bin" in plist

    def test_install_with_missing_run_as_user_fails_before_write_or_launchctl(
        self, monkeypatch, tmp_path
    ):
        # Validation must happen before the plist is written and before any
        # launchctl bootstrap is attempted, so partially-applied installs
        # don't leave dangling state.
        plist_path = tmp_path / "ai.hermes.gateway.plist"
        monkeypatch.setattr(
            gateway_cli, "get_launchd_plist_path", lambda system=False: plist_path,
        )
        monkeypatch.setattr(os, "geteuid", lambda: 0)  # pass _require_root preflight

        # Only "alice" exists in our fake pwd db; "ghost" must raise.
        real_pwd = {
            "alice": SimpleNamespace(
                pw_name="alice", pw_dir=str(tmp_path / "alice"), pw_uid=1001, pw_gid=1001
            ),
        }

        def fake_getpwnam(name):
            try:
                return real_pwd[name]
            except KeyError as e:
                raise KeyError(name) from e

        monkeypatch.setattr(pwd, "getpwnam", fake_getpwnam)

        launchctl_calls: list[list] = []

        def fake_run(cmd, *args, **kwargs):
            launchctl_calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        with pytest.raises(ValueError, match="unknown account"):
            gateway_cli.launchd_install(system=True, run_as_user="ghost")

        # Neither plist nor launchctl bootstrap should have happened.
        assert not plist_path.exists()
        assert launchctl_calls == []


class TestLaunchdSystemRoutingAndPreflight:
    """Default routing + non-root preflight for macOS system LaunchDaemon."""

    def _force_macos(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_termux", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: False)
        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_container", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_wsl", lambda: False)

    def test_select_launchd_scope_promotes_to_system_when_only_daemon_installed(
        self, tmp_path, monkeypatch
    ):
        self._force_macos(monkeypatch)
        agent_plist = tmp_path / "agent.plist"  # missing
        daemon_plist = tmp_path / "daemon.plist"
        daemon_plist.write_text("<plist/>", encoding="utf-8")
        monkeypatch.setattr(
            gateway_cli,
            "get_launchd_plist_path",
            lambda system=False: daemon_plist if system else agent_plist,
        )
        assert gateway_cli._select_launchd_scope(system=False) is True

    def test_select_launchd_scope_keeps_user_for_mixed_install(
        self, tmp_path, monkeypatch
    ):
        # Mixed install: don't promote silently. Operators can pass --system
        # explicitly to override.
        self._force_macos(monkeypatch)
        agent_plist = tmp_path / "agent.plist"
        agent_plist.write_text("<plist/>", encoding="utf-8")
        daemon_plist = tmp_path / "daemon.plist"
        daemon_plist.write_text("<plist/>", encoding="utf-8")
        monkeypatch.setattr(
            gateway_cli,
            "get_launchd_plist_path",
            lambda system=False: daemon_plist if system else agent_plist,
        )
        assert gateway_cli._select_launchd_scope(system=False) is False

    def test_select_launchd_scope_explicit_system_wins(self, tmp_path, monkeypatch):
        self._force_macos(monkeypatch)
        agent_plist = tmp_path / "agent.plist"
        agent_plist.write_text("<plist/>", encoding="utf-8")
        daemon_plist = tmp_path / "daemon.plist"  # missing
        monkeypatch.setattr(
            gateway_cli,
            "get_launchd_plist_path",
            lambda system=False: daemon_plist if system else agent_plist,
        )
        assert gateway_cli._select_launchd_scope(system=True) is True

    def test_launchd_start_routes_to_system_when_only_daemon_installed(
        self, tmp_path, monkeypatch, capsys
    ):
        # Plain `hermes gateway start` should target the system daemon when
        # that is the only installed scope, instead of bootstrapping a brand
        # new user LaunchAgent.
        self._force_macos(monkeypatch)
        agent_plist = tmp_path / "agent.plist"  # missing
        daemon_plist = tmp_path / "daemon.plist"
        daemon_plist.write_text(gateway_cli.generate_launchd_plist(), encoding="utf-8")
        monkeypatch.setattr(
            gateway_cli,
            "get_launchd_plist_path",
            lambda system=False: daemon_plist if system else agent_plist,
        )
        # Pretend we're root so the preflight passes.
        monkeypatch.setattr(os, "geteuid", lambda: 0)

        calls = []

        def fake_run(cmd, check=False, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        gateway_cli.launchd_start(system=False)

        # System scope was selected → kickstart targets system/<daemon-label>.
        daemon_label = gateway_cli.get_launchd_daemon_label()
        assert any(
            cmd == ["launchctl", "kickstart", f"system/{daemon_label}"]
            for cmd in calls
        )
        # And NOT the user agent label.
        agent_label = gateway_cli.get_launchd_label(system=False)
        assert not any(
            agent_label in part for cmd in calls for part in cmd if isinstance(part, str)
        )

    def test_launchd_start_non_root_preflight_blocks_system_scope(
        self, tmp_path, monkeypatch, capsys
    ):
        self._force_macos(monkeypatch)
        agent_plist = tmp_path / "agent.plist"  # missing
        daemon_plist = tmp_path / "daemon.plist"
        daemon_plist.write_text("<plist/>", encoding="utf-8")
        monkeypatch.setattr(
            gateway_cli,
            "get_launchd_plist_path",
            lambda system=False: daemon_plist if system else agent_plist,
        )
        monkeypatch.setattr(os, "geteuid", lambda: 1000)

        calls = []
        monkeypatch.setattr(
            gateway_cli.subprocess,
            "run",
            lambda *a, **kw: calls.append(("subprocess.run", a, kw)) or SimpleNamespace(returncode=0, stdout="", stderr=""),
        )

        with pytest.raises(SystemExit) as exc_info:
            gateway_cli.launchd_start(system=False)

        assert exc_info.value.code == 1
        # No mutating launchctl call should have been issued before the bail.
        assert calls == []
        out = capsys.readouterr().out
        assert "requires root" in out
        assert "sudo hermes gateway start" in out

    @pytest.mark.parametrize(
        "action,fn",
        [
            ("install", lambda: gateway_cli.launchd_install(system=True)),
            ("uninstall", lambda: gateway_cli.launchd_uninstall(system=True)),
            ("start", lambda: gateway_cli.launchd_start(system=True)),
            ("stop", lambda: gateway_cli.launchd_stop(system=True)),
            ("restart", lambda: gateway_cli.launchd_restart(system=True)),
        ],
    )
    def test_explicit_system_action_requires_root(
        self, action, fn, tmp_path, monkeypatch, capsys
    ):
        self._force_macos(monkeypatch)
        # Make both plists discoverable so _select_launchd_scope is a no-op.
        plist = tmp_path / f"{action}.plist"
        plist.write_text("<plist/>", encoding="utf-8")
        monkeypatch.setattr(
            gateway_cli, "get_launchd_plist_path", lambda system=False: plist
        )
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        # Subprocess must NEVER be invoked — the preflight has to bail first.
        monkeypatch.setattr(
            gateway_cli.subprocess,
            "run",
            lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("launchctl must not run as non-root for system scope")
            ),
        )

        with pytest.raises(SystemExit) as exc_info:
            fn()

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "requires root" in out
        assert f"sudo hermes gateway {action}" in out

    def test_get_service_pids_includes_system_launchdaemon_pid(
        self, monkeypatch
    ):
        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: True)

        agent_label = gateway_cli.get_launchd_label()
        daemon_label = gateway_cli.get_launchd_daemon_label()
        monkeypatch.setattr(
            gateway_cli, "_installed_launchd_user_agent_labels", lambda: {agent_label}
        )
        monkeypatch.setattr(
            gateway_cli, "_installed_launchd_system_daemon_labels", lambda: {daemon_label}
        )

        def fake_run(cmd, capture_output=True, text=True, timeout=5, **kwargs):
            if cmd == ["launchctl", "list", agent_label]:
                # No user-agent — empty list output.
                return SimpleNamespace(returncode=113, stdout="", stderr="")
            if cmd == ["launchctl", "print", f"system/{daemon_label}"]:
                # Real `launchctl print` output is verbose; the parser only
                # needs the `pid = N` line.
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        f"system/{daemon_label} = {{\n"
                        "    active count = 1\n"
                        "    pid = 4321\n"
                        "    state = running\n"
                        "}\n"
                    ),
                    stderr="",
                )
            raise AssertionError(f"unexpected command: {cmd}")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        pids = gateway_cli._get_service_pids()
        assert 4321 in pids

    def test_get_service_pids_includes_other_profile_launchdaemon_pid(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: True)

        launch_agents = tmp_path / "LaunchAgents"
        launch_daemons = tmp_path / "LaunchDaemons"
        launch_agents.mkdir()
        launch_daemons.mkdir()
        (launch_agents / "ai.hermes.gateway-coder.plist").write_text(
            "<plist/>\n", encoding="utf-8"
        )
        (launch_daemons / "ai.hermes.daemon-coder.plist").write_text(
            "<plist/>\n", encoding="utf-8"
        )
        monkeypatch.setattr(
            gateway_cli,
            "get_launchd_plist_path",
            lambda system=False: (
                launch_daemons / "ai.hermes.daemon.plist"
                if system
                else launch_agents / "ai.hermes.gateway.plist"
            ),
        )
        monkeypatch.setattr(
            gateway_cli,
            "get_launchd_label",
            lambda system=False: "ai.hermes.daemon" if system else "ai.hermes.gateway",
        )

        def fake_run(cmd, capture_output=True, text=True, timeout=5, **kwargs):
            if cmd == ["launchctl", "list", "ai.hermes.gateway"]:
                return SimpleNamespace(returncode=113, stdout="", stderr="")
            if cmd == ["launchctl", "list", "ai.hermes.gateway-coder"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout='{\n  "PID" = 7654;\n}\n',
                    stderr="",
                )
            if cmd == ["launchctl", "print", "system/ai.hermes.daemon"]:
                return SimpleNamespace(returncode=113, stdout="", stderr="")
            if cmd == ["launchctl", "print", "system/ai.hermes.daemon-coder"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout="system/ai.hermes.daemon-coder = {\n  pid = 8765\n}\n",
                    stderr="",
                )
            raise AssertionError(f"unexpected command: {cmd}")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        pids = gateway_cli._get_service_pids()

        assert 7654 in pids
        assert 8765 in pids

    def test_launchd_install_system_skips_subprocess_when_non_root(
        self, tmp_path, monkeypatch, capsys
    ):
        # The install preflight runs BEFORE we touch /Library/LaunchDaemons,
        # so no plist must be written and no subprocess invoked.
        self._force_macos(monkeypatch)
        plist = tmp_path / "ai.hermes.daemon.plist"
        monkeypatch.setattr(
            gateway_cli, "get_launchd_plist_path", lambda system=False: plist
        )
        monkeypatch.setattr(os, "geteuid", lambda: 501)
        monkeypatch.setattr(
            gateway_cli.subprocess,
            "run",
            lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("subprocess must not run as non-root for system install")
            ),
        )

        with pytest.raises(SystemExit) as exc_info:
            gateway_cli.launchd_install(system=True, run_as_user="alice")

        assert exc_info.value.code == 1
        assert not plist.exists()
        out = capsys.readouterr().out
        assert "requires root" in out


class TestLaunchdSystemPlistPermissions:
    """``/Library/LaunchDaemons/*.plist`` must be ``root:wheel`` and ``0644``
    before ``launchctl bootstrap`` — otherwise launchd silently refuses to
    load the job. Cover fresh install, stale-refresh and start self-heal.
    """

    def _install_grp_stub(self, monkeypatch, *, wheel_gid=0):
        import grp as real_grp

        original_getgrnam = real_grp.getgrnam

        def fake_getgrnam(name):
            if name == "wheel":
                return SimpleNamespace(gr_name="wheel", gr_gid=wheel_gid, gr_mem=[])
            return original_getgrnam(name)

        monkeypatch.setattr(real_grp, "getgrnam", fake_getgrnam)

    def _capture_perm_calls(self, monkeypatch):
        chmod_calls: list[tuple[str, int]] = []
        chown_calls: list[tuple[str, int, int]] = []

        original_chmod = os.chmod
        original_chown = os.chown

        def fake_chmod(path, mode, *args, **kwargs):
            chmod_calls.append((str(path), mode))
            return original_chmod(path, mode, *args, **kwargs)

        def fake_chown(path, uid, gid, *args, **kwargs):
            chown_calls.append((str(path), uid, gid))
            # Don't actually chown — we'd need root and we don't want to flip
            # ownership of test fixtures.

        monkeypatch.setattr(gateway_cli.os, "chmod", fake_chmod)
        monkeypatch.setattr(gateway_cli.os, "chown", fake_chown)
        return chmod_calls, chown_calls

    def test_fresh_install_sets_root_wheel_and_0644_before_bootstrap(
        self, tmp_path, monkeypatch
    ):
        plist_path = tmp_path / "ai.hermes.daemon.plist"
        monkeypatch.setattr(
            gateway_cli, "get_launchd_plist_path", lambda system=False: plist_path
        )
        monkeypatch.setattr(
            gateway_cli,
            "generate_launchd_plist",
            lambda system=False, run_as_user=None: "<plist/>\n",
        )
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 0)

        self._install_grp_stub(monkeypatch, wheel_gid=0)
        chmod_calls, chown_calls = self._capture_perm_calls(monkeypatch)

        bootstrap_seen_perms: dict = {}

        def fake_run(cmd, check=False, **kwargs):
            if cmd[:2] == ["launchctl", "bootstrap"]:
                # Snapshot what the plist looked like at bootstrap time.
                bootstrap_seen_perms["chmod"] = list(chmod_calls)
                bootstrap_seen_perms["chown"] = list(chown_calls)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        gateway_cli.launchd_install(system=True, run_as_user="alice")

        assert (str(plist_path), 0o644) in chmod_calls
        assert (str(plist_path), 0, 0) in chown_calls
        # Both must have been applied BEFORE launchctl bootstrap saw the file.
        assert bootstrap_seen_perms["chmod"], "chmod must precede bootstrap"
        assert bootstrap_seen_perms["chown"], "chown must precede bootstrap"

    def test_stale_refresh_reapplies_root_wheel_and_0644(
        self, tmp_path, monkeypatch
    ):
        plist_path = tmp_path / "ai.hermes.daemon.plist"
        plist_path.write_text("<plist>old</plist>", encoding="utf-8")
        monkeypatch.setattr(
            gateway_cli, "get_launchd_plist_path", lambda system=False: plist_path
        )
        monkeypatch.setattr(
            gateway_cli, "launchd_plist_is_current", lambda system=False: False
        )
        monkeypatch.setattr(
            gateway_cli,
            "_read_launchd_run_as_user_from_plist",
            lambda p: "alice",
        )
        monkeypatch.setattr(
            gateway_cli,
            "generate_launchd_plist",
            lambda system=False, run_as_user=None: "<plist>new</plist>",
        )
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 0)

        self._install_grp_stub(monkeypatch, wheel_gid=0)
        chmod_calls, chown_calls = self._capture_perm_calls(monkeypatch)

        order: list[str] = []

        def fake_run(cmd, check=False, **kwargs):
            if cmd[:2] == ["launchctl", "bootout"]:
                order.append("bootout")
            elif cmd[:2] == ["launchctl", "bootstrap"]:
                order.append("bootstrap")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        assert gateway_cli.refresh_launchd_plist_if_needed(system=True) is True

        assert (str(plist_path), 0o644) in chmod_calls
        assert (str(plist_path), 0, 0) in chown_calls
        # Refresh path must call bootout then bootstrap, both after perm fix.
        assert order == ["bootout", "bootstrap"]

    def test_current_system_plist_reapplies_root_wheel_and_0644_without_reload(
        self, tmp_path, monkeypatch
    ):
        # Contents can be current while permissions drift (e.g. manual edit or
        # previous buggy install). The refresh helper must repair perms even
        # when it does not rewrite/reload the plist, so later start/bootstrap
        # paths never present a bad /Library/LaunchDaemons plist to launchd.
        plist_path = tmp_path / "ai.hermes.daemon.plist"
        plist_path.write_text("<plist/>\n", encoding="utf-8")
        monkeypatch.setattr(
            gateway_cli, "get_launchd_plist_path", lambda system=False: plist_path
        )
        monkeypatch.setattr(
            gateway_cli, "launchd_plist_is_current", lambda system=False: True
        )
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 0)

        self._install_grp_stub(monkeypatch, wheel_gid=0)
        chmod_calls, chown_calls = self._capture_perm_calls(monkeypatch)
        run_calls: list[list[str]] = []
        monkeypatch.setattr(
            gateway_cli.subprocess,
            "run",
            lambda cmd, check=False, **kwargs: run_calls.append(cmd)
            or SimpleNamespace(returncode=0, stdout="", stderr=""),
        )

        assert gateway_cli.refresh_launchd_plist_if_needed(system=True) is False

        assert (str(plist_path), 0o644) in chmod_calls
        assert (str(plist_path), 0, 0) in chown_calls
        assert run_calls == []

    def test_install_existing_current_system_plist_repairs_perms_before_return(
        self, tmp_path, monkeypatch
    ):
        plist_path = tmp_path / "ai.hermes.daemon.plist"
        plist_path.write_text("<plist/>\n", encoding="utf-8")
        monkeypatch.setattr(
            gateway_cli, "get_launchd_plist_path", lambda system=False: plist_path
        )
        monkeypatch.setattr(
            gateway_cli, "launchd_plist_is_current", lambda system=False: True
        )
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 0)

        self._install_grp_stub(monkeypatch, wheel_gid=0)
        chmod_calls, chown_calls = self._capture_perm_calls(monkeypatch)
        run_calls: list[list[str]] = []
        monkeypatch.setattr(
            gateway_cli.subprocess,
            "run",
            lambda cmd, check=False, **kwargs: run_calls.append(cmd)
            or SimpleNamespace(returncode=0, stdout="", stderr=""),
        )

        gateway_cli.launchd_install(system=True, run_as_user="alice")

        assert (str(plist_path), 0o644) in chmod_calls
        assert (str(plist_path), 0, 0) in chown_calls
        assert run_calls == []

    def test_start_existing_current_system_plist_repairs_perms_before_kickstart(
        self, tmp_path, monkeypatch
    ):
        plist_path = tmp_path / "ai.hermes.daemon.plist"
        plist_path.write_text("<plist/>\n", encoding="utf-8")
        monkeypatch.setattr(
            gateway_cli, "get_launchd_plist_path", lambda system=False: plist_path
        )
        monkeypatch.setattr(
            gateway_cli, "_select_launchd_scope", lambda system=False: True
        )
        monkeypatch.setattr(
            gateway_cli, "launchd_plist_is_current", lambda system=False: True
        )
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 0)

        self._install_grp_stub(monkeypatch, wheel_gid=0)
        chmod_calls, chown_calls = self._capture_perm_calls(monkeypatch)
        kickstart_seen_perms: dict = {}

        def fake_run(cmd, check=False, **kwargs):
            if cmd[:2] == ["launchctl", "kickstart"]:
                kickstart_seen_perms["chmod"] = list(chmod_calls)
                kickstart_seen_perms["chown"] = list(chown_calls)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        gateway_cli.launchd_start(system=True)

        assert (str(plist_path), 0o644) in chmod_calls
        assert (str(plist_path), 0, 0) in chown_calls
        assert kickstart_seen_perms["chmod"], "chmod must precede kickstart"
        assert kickstart_seen_perms["chown"], "chown must precede kickstart"

    def test_restart_existing_current_system_plist_repairs_perms_before_bootstrap(
        self, tmp_path, monkeypatch
    ):
        plist_path = tmp_path / "ai.hermes.daemon.plist"
        plist_path.write_text("<plist/>\n", encoding="utf-8")
        label = "ai.hermes.daemon"
        domain = "system"
        target = f"{domain}/{label}"
        monkeypatch.setattr(
            gateway_cli, "get_launchd_plist_path", lambda system=False: plist_path
        )
        monkeypatch.setattr(gateway_cli, "get_launchd_label", lambda system=False: label)
        monkeypatch.setattr(gateway_cli, "_launchd_domain", lambda system=False: domain)
        monkeypatch.setattr(
            gateway_cli, "_select_launchd_scope", lambda system=False: True
        )
        monkeypatch.setattr(
            gateway_cli, "launchd_plist_is_current", lambda system=False: True
        )
        monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 0)

        self._install_grp_stub(monkeypatch, wheel_gid=0)
        chmod_calls, chown_calls = self._capture_perm_calls(monkeypatch)
        bootstrap_seen_perms: dict = {}

        def fake_run(cmd, check=False, **kwargs):
            if cmd == ["launchctl", "kickstart", "-k", target]:
                raise gateway_cli.subprocess.CalledProcessError(
                    3, cmd, stderr="Could not find service"
                )
            if cmd[:2] == ["launchctl", "bootstrap"]:
                bootstrap_seen_perms["chmod"] = list(chmod_calls)
                bootstrap_seen_perms["chown"] = list(chown_calls)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        gateway_cli.launchd_restart(system=True)

        assert (str(plist_path), 0o644) in chmod_calls
        assert (str(plist_path), 0, 0) in chown_calls
        assert bootstrap_seen_perms["chmod"], "chmod must precede bootstrap"
        assert bootstrap_seen_perms["chown"], "chown must precede bootstrap"

    def test_start_self_heal_sets_root_wheel_when_plist_missing(
        self, tmp_path, monkeypatch
    ):
        plist_path = tmp_path / "ai.hermes.daemon.plist"
        # Plist intentionally missing — exercises the self-heal branch.
        monkeypatch.setattr(
            gateway_cli, "get_launchd_plist_path", lambda system=False: plist_path
        )
        monkeypatch.setattr(
            gateway_cli, "_select_launchd_scope", lambda system=False: True
        )
        monkeypatch.setattr(
            gateway_cli,
            "generate_launchd_plist",
            lambda system=False, run_as_user=None: "<plist/>\n",
        )
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 0)

        self._install_grp_stub(monkeypatch, wheel_gid=0)
        chmod_calls, chown_calls = self._capture_perm_calls(monkeypatch)

        bootstrap_perms: dict = {}

        def fake_run(cmd, check=False, **kwargs):
            if cmd[:2] == ["launchctl", "bootstrap"]:
                bootstrap_perms["chmod"] = list(chmod_calls)
                bootstrap_perms["chown"] = list(chown_calls)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        gateway_cli.launchd_start(system=True)

        assert (str(plist_path), 0o644) in chmod_calls
        assert (str(plist_path), 0, 0) in chown_calls
        assert bootstrap_perms["chmod"], "chmod must precede bootstrap"
        assert bootstrap_perms["chown"], "chown must precede bootstrap"

    def test_user_launchagent_install_does_not_chown_root_wheel(
        self, tmp_path, monkeypatch
    ):
        # User-scope plist lives under ~/Library/LaunchAgents and must keep
        # the user's own ownership — we must never call chown(_, 0, wheel)
        # for non-system installs.
        plist_path = tmp_path / "ai.hermes.gateway.plist"
        monkeypatch.setattr(
            gateway_cli, "get_launchd_plist_path", lambda system=False: plist_path
        )
        monkeypatch.setattr(
            gateway_cli,
            "generate_launchd_plist",
            lambda system=False, run_as_user=None: "<plist/>\n",
        )
        # Even if we happen to be root, user-scope must not trigger root:wheel.
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 0)

        self._install_grp_stub(monkeypatch, wheel_gid=0)
        chmod_calls, chown_calls = self._capture_perm_calls(monkeypatch)

        monkeypatch.setattr(
            gateway_cli.subprocess,
            "run",
            lambda cmd, check=False, **kwargs: SimpleNamespace(
                returncode=0, stdout="", stderr=""
            ),
        )

        gateway_cli.launchd_install(system=False)

        assert chown_calls == []
        # No chmod against the user plist either — let the user's umask govern.
        assert all(p != str(plist_path) for p, _mode in chmod_calls)

    def test_perm_helper_skips_chown_when_wheel_group_missing(
        self, tmp_path, monkeypatch
    ):
        # Test environments without a ``wheel`` group (common on Linux CI)
        # must not blow up — chmod still happens, chown is silently skipped.
        plist_path = tmp_path / "ai.hermes.daemon.plist"
        plist_path.write_text("<plist/>\n", encoding="utf-8")
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 0)

        import grp as real_grp

        def fake_getgrnam(name):
            raise KeyError(name)

        monkeypatch.setattr(real_grp, "getgrnam", fake_getgrnam)

        chmod_calls, chown_calls = self._capture_perm_calls(monkeypatch)

        gateway_cli._enforce_system_launchd_plist_perms(plist_path)

        assert (str(plist_path), 0o644) in chmod_calls
        assert chown_calls == []

    def test_perm_helper_skips_chown_when_not_root(
        self, tmp_path, monkeypatch
    ):
        # Non-root callers (e.g. tests, plist drift checks) must not attempt
        # chown — the bare chmod is harmless.
        plist_path = tmp_path / "ai.hermes.daemon.plist"
        plist_path.write_text("<plist/>\n", encoding="utf-8")
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 501)

        self._install_grp_stub(monkeypatch, wheel_gid=0)
        chmod_calls, chown_calls = self._capture_perm_calls(monkeypatch)

        gateway_cli._enforce_system_launchd_plist_perms(plist_path)

        assert (str(plist_path), 0o644) in chmod_calls
        assert chown_calls == []
