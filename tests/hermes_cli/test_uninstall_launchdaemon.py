"""Regression tests for macOS LaunchDaemon-safe uninstall cleanup."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import hermes_cli.uninstall as uninstall_mod


def test_full_uninstall_preflights_named_profile_launchdaemons_before_destructive_cleanup(monkeypatch, tmp_path):
    profile = SimpleNamespace(
        name="coder",
        path=tmp_path / ".hermes" / "profiles" / "coder",
        alias_path=None,
        is_default=False,
    )
    profile.path.mkdir(parents=True)
    project_root = tmp_path / "hermes-agent"
    project_root.mkdir()
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(exist_ok=True)

    monkeypatch.setattr(uninstall_mod, "_discover_named_profiles", lambda: [profile])
    monkeypatch.setattr(uninstall_mod, "_is_default_hermes_home", lambda _home: True)
    monkeypatch.setattr(uninstall_mod, "get_project_root", lambda: project_root)
    monkeypatch.setattr(uninstall_mod, "get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(uninstall_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(uninstall_mod.os, "geteuid", lambda: 501, raising=False)
    monkeypatch.setattr(uninstall_mod, "_profile_launchdaemon_path", lambda _home: tmp_path / "ai.hermes.daemon-coder.plist")
    (tmp_path / "ai.hermes.daemon-coder.plist").write_text("plist", encoding="utf-8")

    inputs = iter(["2", "y", "yes"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))

    with patch.object(uninstall_mod, "remove_wrapper_script") as remove_wrapper, pytest.raises(SystemExit):
        uninstall_mod.run_uninstall(SimpleNamespace(full=False, yes=False))

    remove_wrapper.assert_not_called()
    assert project_root.exists()
    assert hermes_home.exists()


def test_root_profile_uninstall_uses_sudo_user_for_user_launchagent_and_root_for_system(monkeypatch, tmp_path):
    profile = SimpleNamespace(
        name="coder",
        path=tmp_path / ".hermes" / "profiles" / "coder",
        alias_path=None,
    )
    profile.path.mkdir(parents=True)

    monkeypatch.setattr(uninstall_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(uninstall_mod.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setenv("SUDO_USER", "alice")
    user_agent = tmp_path / "ai.hermes.gateway-coder.plist"
    user_agent.write_text("plist", encoding="utf-8")
    monkeypatch.setattr(uninstall_mod, "_profile_launchagent_path", lambda _home, _user_home=None: user_agent)

    calls = []
    kwargs_by_cmd = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        kwargs_by_cmd.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(uninstall_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(uninstall_mod.shutil, "rmtree", lambda _path: None)

    uninstall_mod._uninstall_profile(profile)

    assert any(cmd[:3] == ["sudo", "-u", "alice"] and f"HERMES_HOME={profile.path}" in cmd and cmd[-2:] == ["gateway", "uninstall"] for cmd in calls)
    assert any(cmd[-3:] == ["gateway", "uninstall", "--system"] for cmd in calls)
    assert any(
        cmd[-3:] == ["gateway", "uninstall", "--system"]
        and kwargs.get("env", {}).get("HERMES_HOME") == str(profile.path)
        for cmd, kwargs in kwargs_by_cmd
    )


def test_profile_uninstall_aborts_profile_deletion_when_gateway_uninstall_fails(monkeypatch, tmp_path):
    profile = SimpleNamespace(
        name="coder",
        path=tmp_path / ".hermes" / "profiles" / "coder",
        alias_path=None,
    )
    profile.path.mkdir(parents=True)

    monkeypatch.setattr(uninstall_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(uninstall_mod.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setenv("SUDO_USER", "alice")

    def fake_run(cmd, **kwargs):
        if cmd[-1] == "uninstall" or cmd[-2:] == ["uninstall", "--system"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(uninstall_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        uninstall_mod.shutil,
        "rmtree",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("profile data must not be removed after gateway uninstall failure")
        ),
    )

    with pytest.raises(SystemExit):
        uninstall_mod._uninstall_profile(profile)


def test_root_profile_uninstall_skips_user_scope_when_only_system_daemon_exists(monkeypatch, tmp_path):
    profile = SimpleNamespace(
        name="coder",
        path=tmp_path / ".hermes" / "profiles" / "coder",
        alias_path=None,
    )
    profile.path.mkdir(parents=True)

    monkeypatch.setattr(uninstall_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(uninstall_mod.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setenv("SUDO_USER", "alice")
    missing_user_agent = tmp_path / "missing-user-agent.plist"
    monkeypatch.setattr(
        uninstall_mod,
        "_profile_launchagent_path",
        lambda _home, _user_home=None: missing_user_agent,
    )

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(uninstall_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(uninstall_mod.shutil, "rmtree", lambda _path: None)

    uninstall_mod._uninstall_profile(profile)

    assert not any(cmd[:3] == ["sudo", "-u", "alice"] for cmd in calls)
    assert any(cmd[-3:] == ["gateway", "uninstall", "--system"] for cmd in calls)


def test_root_default_uninstall_removes_sudo_user_launchagent_not_root(monkeypatch, tmp_path):
    import pwd

    import hermes_cli.gateway as gateway_mod

    sudo_home = tmp_path / "Users" / "alice"
    user_agents = sudo_home / "Library" / "LaunchAgents"
    user_agents.mkdir(parents=True)
    user_plist = user_agents / "ai.hermes.gateway.plist"
    user_plist.write_text("plist", encoding="utf-8")
    root_plist = tmp_path / "var" / "root" / "Library" / "LaunchAgents" / "ai.hermes.gateway.plist"
    system_plist = tmp_path / "Library" / "LaunchDaemons" / "ai.hermes.daemon.plist"

    monkeypatch.setattr(uninstall_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(uninstall_mod.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(uninstall_mod.os, "getuid", lambda: 0, raising=False)
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setattr(
        pwd,
        "getpwnam",
        lambda name: SimpleNamespace(pw_uid=501, pw_dir=str(sudo_home)),
    )
    monkeypatch.setattr(gateway_mod, "find_gateway_pids", lambda: [])
    monkeypatch.setattr(gateway_mod, "kill_gateway_processes", lambda: 0)
    monkeypatch.setattr(
        gateway_mod,
        "get_launchd_label",
        lambda system=False: "ai.hermes.daemon" if system else "ai.hermes.gateway",
    )
    monkeypatch.setattr(
        gateway_mod,
        "get_launchd_plist_path",
        lambda system=False: system_plist if system else root_plist,
    )

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(uninstall_mod.subprocess, "run", fake_run)

    assert uninstall_mod.uninstall_gateway_service() is True

    assert ["launchctl", "bootout", "gui/501/ai.hermes.gateway"] in calls
    assert ["launchctl", "unload", str(user_plist)] in calls
    assert not user_plist.exists()
    assert not any("gui/0/ai.hermes.gateway" in " ".join(cmd) for cmd in calls)