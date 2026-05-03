"""
Hermes Agent Uninstaller.

Provides options for:
- Full uninstall: Remove everything including configs and data
- Keep data: Remove code but keep ~/.hermes/ (configs, sessions, logs)
"""

import os
import platform
import shutil
import subprocess
from pathlib import Path

from hermes_constants import get_hermes_home

from hermes_cli.colors import Colors, color

def log_info(msg: str):
    print(f"{color('→', Colors.CYAN)} {msg}")

def log_success(msg: str):
    print(f"{color('✓', Colors.GREEN)} {msg}")

def log_warn(msg: str):
    print(f"{color('⚠', Colors.YELLOW)} {msg}")

def get_project_root() -> Path:
    """Get the project installation directory."""
    return Path(__file__).parent.parent.resolve()


def find_shell_configs() -> list:
    """Find shell configuration files that might have PATH entries."""
    home = Path.home()
    configs = []
    
    candidates = [
        home / ".bashrc",
        home / ".bash_profile",
        home / ".profile",
        home / ".zshrc",
        home / ".zprofile",
    ]
    
    for config in candidates:
        if config.exists():
            configs.append(config)
    
    return configs


def remove_path_from_shell_configs():
    """Remove Hermes PATH entries from shell configuration files."""
    configs = find_shell_configs()
    removed_from = []
    
    for config_path in configs:
        try:
            content = config_path.read_text()
            original_content = content
            
            # Remove lines containing hermes-agent or hermes PATH entries
            new_lines = []
            skip_next = False
            
            for line in content.split('\n'):
                # Skip the "# Hermes Agent" comment and following line
                if '# Hermes Agent' in line or '# hermes-agent' in line:
                    skip_next = True
                    continue
                if skip_next and ('hermes' in line.lower() and 'PATH' in line):
                    skip_next = False
                    continue
                skip_next = False
                
                # Remove any PATH line containing hermes
                if 'hermes' in line.lower() and ('PATH=' in line or 'path=' in line.lower()):
                    continue
                    
                new_lines.append(line)
            
            new_content = '\n'.join(new_lines)
            
            # Clean up multiple blank lines
            while '\n\n\n' in new_content:
                new_content = new_content.replace('\n\n\n', '\n\n')
            
            if new_content != original_content:
                config_path.write_text(new_content)
                removed_from.append(config_path)
                
        except Exception as e:
            log_warn(f"Could not update {config_path}: {e}")
    
    return removed_from


def remove_wrapper_script():
    """Remove the hermes wrapper script if it exists."""
    wrapper_paths = [
        Path.home() / ".local" / "bin" / "hermes",
        Path("/usr/local/bin/hermes"),
    ]
    
    removed = []
    for wrapper in wrapper_paths:
        if wrapper.exists():
            try:
                # Check if it's our wrapper (contains hermes_cli reference)
                content = wrapper.read_text()
                if 'hermes_cli' in content or 'hermes-agent' in content:
                    wrapper.unlink()
                    removed.append(wrapper)
            except Exception as e:
                log_warn(f"Could not remove {wrapper}: {e}")
    
    return removed


def uninstall_gateway_service():
    """Stop and uninstall the gateway service (systemd, launchd, Windows
    Scheduled Task / Startup folder) and kill any standalone gateway processes.

    Delegates to the gateway module which handles:
    - Linux: user + system systemd services (with proper DBUS env setup)
    - macOS: launchd plists
    - Windows: Scheduled Task + Startup-folder fallback, via ``gateway_windows``
    - All platforms: standalone ``hermes gateway run`` processes
    - Termux/Android: skips systemd (no systemd on Android), still kills standalone processes
    """
    import platform
    stopped_something = False

    # 1. Kill any standalone gateway processes (all platforms, including Termux)
    try:
        from hermes_cli.gateway import kill_gateway_processes, find_gateway_pids
        pids = find_gateway_pids()
        if pids:
            killed = kill_gateway_processes()
            if killed:
                log_success(f"Killed {killed} running gateway process(es)")
                stopped_something = True
    except Exception as e:
        log_warn(f"Could not check for gateway processes: {e}")

    system = platform.system()

    # Termux/Android has no systemd and no launchd — nothing left to do.
    prefix = os.getenv("PREFIX", "")
    is_termux = bool(os.getenv("TERMUX_VERSION") or "com.termux/files/usr" in prefix)
    if is_termux:
        return stopped_something

    # 2. Linux: uninstall systemd services (both user and system scopes)
    if system == "Linux":
        try:
            from hermes_cli.gateway import (
                get_systemd_unit_path,
                get_service_name,
                _systemctl_cmd,
            )
            svc_name = get_service_name()

            for is_system in (False, True):
                unit_path = get_systemd_unit_path(system=is_system)
                if not unit_path.exists():
                    continue

                scope = "system" if is_system else "user"
                try:
                    if is_system and os.geteuid() != 0:  # windows-footgun: ok — Linux systemd uninstall path, guarded by `if system == "Linux"` above
                        log_warn(f"System gateway service exists at {unit_path} "
                                 f"but needs sudo to remove")
                        continue

                    cmd = _systemctl_cmd(is_system)
                    subprocess.run(cmd + ["stop", svc_name],
                                   capture_output=True, check=False)
                    subprocess.run(cmd + ["disable", svc_name],
                                   capture_output=True, check=False)
                    unit_path.unlink()
                    subprocess.run(cmd + ["daemon-reload"],
                                   capture_output=True, check=False)
                    log_success(f"Removed {scope} gateway service ({unit_path})")
                    stopped_something = True
                except Exception as e:
                    log_warn(f"Could not remove {scope} gateway service: {e}")
        except Exception as e:
            log_warn(f"Could not check systemd gateway services: {e}")

    # 3. macOS: uninstall launchd plist
    elif system == "Darwin":
        try:
            from hermes_cli.gateway import get_launchd_label, get_launchd_plist_path

            sudo_user_info = _macos_sudo_user_info() if os.geteuid() == 0 else None  # windows-footgun: ok — Darwin-only launchd uninstall path
            for is_system in (False, True):
                label = get_launchd_label(system=is_system)
                if not is_system and sudo_user_info is not None:
                    sudo_uid, sudo_home = sudo_user_info
                    plist_path = sudo_home / "Library" / "LaunchAgents" / f"{label}.plist"
                    domain = f"gui/{sudo_uid}"
                else:
                    plist_path = get_launchd_plist_path(system=is_system)
                    domain = "system" if is_system else f"gui/{os.getuid()}"  # windows-footgun: ok — Darwin-only launchd uninstall path

                if not plist_path.exists():
                    continue

                scope = "system LaunchDaemon" if is_system else "user LaunchAgent"
                if is_system and os.geteuid() != 0:  # windows-footgun: ok — Darwin-only launchd uninstall path
                    log_warn(
                        f"macOS {scope} exists at {plist_path} but needs sudo to remove"
                    )
                    log_warn("Re-run `sudo hermes uninstall` before removing code or data.")
                    raise SystemExit(1)

                subprocess.run(
                    ["launchctl", "bootout", f"{domain}/{label}"],
                    capture_output=True,
                    check=False,
                )
                # Older launchctl accepts unload-by-plist and it is harmless as
                # a fallback if bootout reported that the service was not loaded.
                subprocess.run(["launchctl", "unload", str(plist_path)],
                               capture_output=True, check=False)
                plist_path.unlink()
                log_success(f"Removed macOS {scope} ({plist_path})")
                stopped_something = True
        except Exception as e:
            log_warn(f"Could not remove launchd gateway service: {e}")

    # 4. Windows: uninstall Scheduled Task + Startup-folder entry.  The
    #    gateway_windows module already knows how to locate and remove both
    #    code paths (schtasks /Delete + .cmd unlink) and how to stop any
    #    running detached pythonw gateway process.  We call into it so the
    #    uninstall logic stays in exactly one place.
    elif system == "Windows":
        try:
            from hermes_cli import gateway_windows
            if gateway_windows.is_installed() or gateway_windows.is_task_registered() \
                    or gateway_windows.is_startup_entry_installed():
                try:
                    gateway_windows.stop()
                except Exception as e:
                    log_warn(f"Could not stop Windows gateway cleanly: {e}")
                try:
                    gateway_windows.uninstall()
                    log_success("Removed Windows gateway (Scheduled Task + Startup entry)")
                    stopped_something = True
                except Exception as e:
                    log_warn(f"Could not fully uninstall Windows gateway: {e}")
        except Exception as e:
            log_warn(f"Could not check Windows gateway service: {e}")

    return stopped_something


# ============================================================================
# Windows-specific uninstall helpers
# ============================================================================
#
# The installer (``scripts/install.ps1``) does four Windows-only things that
# ``remove_path_from_shell_configs`` / ``remove_wrapper_script`` don't cover:
#
#   1. Sets User-scope env vars ``HERMES_HOME`` and ``HERMES_GIT_BASH_PATH``
#      via ``[Environment]::SetEnvironmentVariable(..., "User")``.  These
#      don't live in ~/.bashrc — they're in the Windows registry at
#      HKCU\Environment.
#   2. Prepends to User-scope ``PATH`` (same registry location) entries
#      like ``%LOCALAPPDATA%\hermes\git\cmd``, ``%LOCALAPPDATA%\hermes\git\bin``,
#      ``%LOCALAPPDATA%\hermes\git\usr\bin``, ``%LOCALAPPDATA%\hermes\node``.
#      Again not in any rc file — only accessible via the registry or the
#      .NET [Environment] API.
#   3. Downloads PortableGit to ``%LOCALAPPDATA%\hermes\git\`` and Node to
#      ``%LOCALAPPDATA%\hermes\node\`` as user-scoped, isolated copies.
#      These are ~200MB combined and serve no purpose after uninstall.
#   4. On the ``hermes dashboard`` + gateway paths, drops files into
#      ``%LOCALAPPDATA%\hermes\gateway-service\`` and sometimes
#      ``%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`` — the
#      latter is handled by ``gateway_windows.uninstall()`` already.
#
# Running a PowerShell one-liner per operation is overkill and fragile on
# locked-down machines (Constrained Language Mode, restricted ExecutionPolicy).
# Direct registry writes via ``winreg`` work without spawning any subprocess
# and apply immediately for new shells (SendMessage WM_SETTINGCHANGE would
# be nicer but requires ctypes and buys us nothing — the user will log out
# or open a new terminal anyway).


def _hermes_path_markers(hermes_home: Path) -> list[str]:
    """Path-entry substrings that identify Hermes-owned User-PATH entries."""
    root = str(hermes_home).rstrip("\\/")
    # Match on prefix so sub-entries (git\cmd, git\bin, git\usr\bin, node, etc.)
    # all get swept.  Also match the bare hermes-agent install dir.
    markers = [root + "\\hermes-agent", root + "\\git", root + "\\node", root + "\\venv"]
    # Also match if HERMES_HOME was customised to somewhere else — find-and-nuke
    # any entry whose path component contains "hermes".  We don't want to catch
    # unrelated entries like "chermes-foo" or "ephermeral", so we look for
    # backslash-hermes as a word-ish boundary.
    return markers


def remove_path_from_windows_registry(hermes_home: Path) -> list[str]:
    """Strip Hermes-owned entries from User-scope PATH in the registry.

    Returns the list of removed path entries.  Operates on HKCU\\Environment,
    same key the installer wrote to via ``[Environment]::SetEnvironmentVariable``.
    """
    try:
        import winreg
    except ImportError:
        return []  # not on Windows, nothing to do

    removed: list[str] = []
    key_path = "Environment"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as key:
            try:
                path_value, path_type = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                return []
            # Preserve REG_EXPAND_SZ vs REG_SZ so unexpanded %VARS% survive.
            entries = [e for e in path_value.split(";") if e]
            markers = _hermes_path_markers(hermes_home)
            kept: list[str] = []
            for entry in entries:
                entry_norm = entry.rstrip("\\/")
                matched = any(entry_norm.lower().startswith(m.lower()) for m in markers)
                if matched:
                    removed.append(entry)
                else:
                    kept.append(entry)
            if removed:
                new_value = ";".join(kept)
                winreg.SetValueEx(key, "Path", 0, path_type, new_value)
    except OSError as e:
        log_warn(f"Could not edit User PATH in registry: {e}")
    return removed


def remove_hermes_env_vars_windows() -> list[str]:
    """Delete HERMES_HOME and HERMES_GIT_BASH_PATH from User-scope env vars."""
    try:
        import winreg
    except ImportError:
        return []

    removed: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as key:
            for name in ("HERMES_HOME", "HERMES_GIT_BASH_PATH"):
                try:
                    winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                try:
                    winreg.DeleteValue(key, name)
                    removed.append(name)
                except OSError as e:
                    log_warn(f"Could not delete {name} from User env: {e}")
    except OSError as e:
        log_warn(f"Could not open User Environment key: {e}")
    return removed


def remove_portable_tooling_windows(hermes_home: Path) -> list[Path]:
    """Delete PortableGit and Node installs the Windows installer created under
    ``%LOCALAPPDATA%\\hermes\\``.  Only called on full uninstall; they're
    isolated from any system Git / Node so they cannot break other tools."""
    removed: list[Path] = []
    for sub in ("git", "node", "gateway-service"):
        target = hermes_home / sub
        if target.exists():
            try:
                shutil.rmtree(target, ignore_errors=False)
                removed.append(target)
            except Exception as e:
                log_warn(f"Could not remove {target}: {e}")
    return removed


def _is_windows() -> bool:
    import sys
    return sys.platform == "win32"


def _is_default_hermes_home(hermes_home: Path) -> bool:
    """Return True when ``hermes_home`` points at the default (non-profile) root."""
    try:
        from hermes_constants import get_default_hermes_root
        return hermes_home.resolve() == get_default_hermes_root().resolve()
    except Exception:
        return False


def _discover_named_profiles():
    """Return a list of ``ProfileInfo`` for every non-default profile, or ``[]``
    if profile support is unavailable or nothing is installed beyond the
    default root."""
    try:
        from hermes_cli.profiles import list_profiles
    except Exception:
        return []
    try:
        return [p for p in list_profiles() if not getattr(p, "is_default", False)]
    except Exception as e:
        log_warn(f"Could not enumerate profiles: {e}")
        return []


def _profile_launchdaemon_path(profile_home: Path) -> Path | None:
    """Return this profile's macOS system LaunchDaemon plist path, if resolvable."""
    old_home = os.environ.get("HERMES_HOME")
    try:
        os.environ["HERMES_HOME"] = str(profile_home)
        from hermes_cli.gateway import get_launchd_plist_path

        return get_launchd_plist_path(system=True)
    except (ImportError, OSError):
        return None
    finally:
        if old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old_home


def _profile_launchagent_path(profile_home: Path, user_home: Path | None = None) -> Path | None:
    """Return this profile's macOS user LaunchAgent plist path, if resolvable."""
    old_home = os.environ.get("HERMES_HOME")
    try:
        os.environ["HERMES_HOME"] = str(profile_home)
        from hermes_cli.gateway import get_launchd_label, get_launchd_plist_path

        if user_home is not None:
            return user_home / "Library" / "LaunchAgents" / f"{get_launchd_label(system=False)}.plist"
        return get_launchd_plist_path(system=False)
    except (ImportError, OSError):
        return None
    finally:
        if old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old_home


def _macos_sudo_user() -> str | None:
    user = str(os.environ.get("SUDO_USER") or "").strip()
    if not user or user == "root":
        return None
    return user


def _macos_sudo_user_info() -> tuple[int, Path] | None:
    """Return the original GUI user's uid/home when running under sudo.

    Root-owned launchd cleanup must remove system LaunchDaemons as root, but
    user LaunchAgents still live in the sudo user's home and gui/<uid> domain.
    """
    user = _macos_sudo_user()
    if not user:
        return None
    try:
        import pwd

        pw = pwd.getpwnam(user)  # windows-footgun: ok — Darwin-only sudo launchd cleanup helper
    except (KeyError, ImportError, OSError):
        return None
    return int(pw.pw_uid), Path(pw.pw_dir)


def _preflight_named_profile_launchdaemons(named_profiles) -> None:
    """Abort before destructive uninstall if profile LaunchDaemons need sudo."""
    if platform.system() != "Darwin" or os.geteuid() == 0:  # windows-footgun: ok — Darwin-only LaunchDaemon cleanup path
        return
    for profile in named_profiles:
        daemon_path = _profile_launchdaemon_path(profile.path)
        if daemon_path is not None and daemon_path.exists():
            log_warn(
                f"Profile '{profile.name}' has a system LaunchDaemon at {daemon_path} "
                "that needs sudo to remove"
            )
            log_warn("Re-run `sudo hermes uninstall` before removing code or data.")
            raise SystemExit(1)


def _uninstall_profile(profile) -> None:
    """Fully uninstall a single named profile: stop its gateway service,
    remove its alias wrapper, and wipe its HERMES_HOME directory.

    We shell out to ``hermes -p <name> gateway stop|uninstall`` because
    service names, unit paths, and plist paths are all derived from the
    current HERMES_HOME and can't be easily switched in-process.
    """
    import sys as _sys
    name = profile.name
    profile_home = profile.path

    log_info(f"Uninstalling profile '{name}'...")

    if platform.system() == "Darwin":
        daemon_path = _profile_launchdaemon_path(profile_home)
        if daemon_path is not None and daemon_path.exists() and os.geteuid() != 0:  # windows-footgun: ok — Darwin-only LaunchDaemon cleanup path
            log_warn(
                f"  System LaunchDaemon exists at {daemon_path} but needs sudo to remove"
            )
            log_warn("  Re-run `sudo hermes uninstall` before deleting this profile.")
            raise SystemExit(1)

    # 1. Stop and remove this profile's gateway service.
    #    Use `python -m hermes_cli.main` so we don't depend on a `hermes`
    #    wrapper that may be half-removed mid-uninstall.
    profile_env = os.environ.copy()
    profile_env["HERMES_HOME"] = str(profile_home)
    hermes_invocation = [_sys.executable, "-m", "hermes_cli.main", "--profile", name]
    command_variants = []
    sudo_user = _macos_sudo_user() if platform.system() == "Darwin" and os.geteuid() == 0 else None  # windows-footgun: ok — Darwin-only LaunchDaemon cleanup path
    if sudo_user:
        user_invocation = [
            "sudo",
            "-u",
            sudo_user,
            "env",
            f"HERMES_HOME={profile_home}",
        ] + hermes_invocation
        sudo_info = _macos_sudo_user_info()
        user_agent_path = _profile_launchagent_path(
            profile_home,
            sudo_info[1] if sudo_info is not None else None,
        )
        if user_agent_path is not None and user_agent_path.exists():
            command_variants.extend((user_invocation, subcmd, [], None) for subcmd in ("stop", "uninstall"))
        command_variants.extend((hermes_invocation, subcmd, ["--system"], profile_env) for subcmd in ("stop", "uninstall"))
    else:
        command_variants.extend((hermes_invocation, subcmd, [], profile_env) for subcmd in ("stop", "uninstall"))
        if platform.system() == "Darwin" and os.geteuid() == 0:  # windows-footgun: ok — Darwin-only LaunchDaemon cleanup path
            command_variants.extend((hermes_invocation, subcmd, ["--system"], profile_env) for subcmd in ("stop", "uninstall"))
    uninstall_failed = False
    for invocation, subcmd, extra_args, env in command_variants:
        try:
            result = subprocess.run(
                invocation + ["gateway", subcmd] + extra_args,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                env=env,
            )
            if subcmd == "uninstall" and result.returncode != 0:
                uninstall_failed = True
                detail = (result.stderr or result.stdout or "").strip()
                suffix = f": {detail}" if detail else ""
                log_warn(f"  Gateway uninstall failed for '{name}'{suffix}")
        except subprocess.TimeoutExpired:
            log_warn(f"  Gateway {subcmd} timed out for '{name}'")
            if subcmd == "uninstall":
                uninstall_failed = True
        except Exception as e:
            log_warn(f"  Could not run gateway {subcmd} for '{name}': {e}")
            if subcmd == "uninstall":
                uninstall_failed = True

    if uninstall_failed:
        raise SystemExit(
            f"Gateway service uninstall failed for profile '{name}'; refusing to delete {profile_home}"
        )

    # 2. Remove the wrapper alias script at ~/.local/bin/<name> (if any).
    alias_path = getattr(profile, "alias_path", None)
    if alias_path and alias_path.exists():
        try:
            alias_path.unlink()
            log_success(f"  Removed alias {alias_path}")
        except Exception as e:
            log_warn(f"  Could not remove alias {alias_path}: {e}")

    # 3. Wipe the profile's HERMES_HOME directory.
    try:
        if profile_home.exists():
            shutil.rmtree(profile_home)
            log_success(f"  Removed {profile_home}")
    except Exception as e:
        log_warn(f"  Could not remove {profile_home}: {e}")


def run_uninstall(args):
    """
    Run the uninstall process.
    
    Options:
    - Full uninstall: removes code + ~/.hermes/ (configs, data, logs)
    - Keep data: removes code but keeps ~/.hermes/ for future reinstall
    """
    project_root = get_project_root()
    hermes_home = get_hermes_home()

    # Detect named profiles when uninstalling from the default root —
    # offer to clean them up too instead of leaving zombie HERMES_HOMEs
    # and systemd units behind.
    is_default_profile = _is_default_hermes_home(hermes_home)
    named_profiles = _discover_named_profiles() if is_default_profile else []

    print()
    print(color("┌─────────────────────────────────────────────────────────┐", Colors.MAGENTA, Colors.BOLD))
    print(color("│            ⚕ Hermes Agent Uninstaller                  │", Colors.MAGENTA, Colors.BOLD))
    print(color("└─────────────────────────────────────────────────────────┘", Colors.MAGENTA, Colors.BOLD))
    print()
    
    # Show what will be affected
    print(color("Current Installation:", Colors.CYAN, Colors.BOLD))
    print(f"  Code:    {project_root}")
    print(f"  Config:  {hermes_home / 'config.yaml'}")
    print(f"  Secrets: {hermes_home / '.env'}")
    print(f"  Data:    {hermes_home / 'cron/'}, {hermes_home / 'sessions/'}, {hermes_home / 'logs/'}")
    print()

    if named_profiles:
        print(color("Other profiles detected:", Colors.CYAN, Colors.BOLD))
        for p in named_profiles:
            running = " (gateway running)" if getattr(p, "gateway_running", False) else ""
            print(f"  • {p.name}{running}: {p.path}")
        print()
    
    # Ask for confirmation
    print(color("Uninstall Options:", Colors.YELLOW, Colors.BOLD))
    print()
    print("  1) " + color("Keep data", Colors.GREEN) + " - Remove code only, keep configs/sessions/logs")
    print("     (Recommended - you can reinstall later with your settings intact)")
    print()
    print("  2) " + color("Full uninstall", Colors.RED) + " - Remove everything including all data")
    print("     (Warning: This deletes all configs, sessions, and logs permanently)")
    print()
    print("  3) " + color("Cancel", Colors.CYAN) + " - Don't uninstall")
    print()
    
    try:
        choice = input(color("Select option [1/2/3]: ", Colors.BOLD)).strip()
    except (KeyboardInterrupt, EOFError):
        print()
        print("Cancelled.")
        return
    
    if choice == "3" or choice.lower() in {"c", "cancel", "q", "quit", "n", "no"}:
        print()
        print("Uninstall cancelled.")
        return
    
    full_uninstall = (choice == "2")

    # When doing a full uninstall from the default profile, also offer to
    # remove any named profiles — stopping their gateway services, unlinking
    # their alias wrappers, and wiping their HERMES_HOME dirs. Otherwise
    # those leave zombie services and data behind.
    remove_profiles = False
    if full_uninstall and named_profiles:
        print()
        print(color("Other profiles will NOT be removed by default.", Colors.YELLOW))
        print(f"Found {len(named_profiles)} named profile(s): " +
              ", ".join(p.name for p in named_profiles))
        print()
        try:
            resp = input(color(
                f"Also stop and remove these {len(named_profiles)} profile(s)? [y/N]: ",
                Colors.BOLD
            )).strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            print("Cancelled.")
            return
        remove_profiles = resp in {"y", "yes"}

    # Final confirmation
    print()
    if full_uninstall:
        print(color("⚠️  WARNING: This will permanently delete ALL Hermes data!", Colors.RED, Colors.BOLD))
        print(color("   Including: configs, API keys, sessions, scheduled jobs, logs", Colors.RED))
        if remove_profiles:
            print(color(
                f"   Plus {len(named_profiles)} profile(s): " +
                ", ".join(p.name for p in named_profiles),
                Colors.RED
            ))
    else:
        print("This will remove the Hermes code but keep your configuration and data.")
    
    print()
    try:
        confirm = input(f"Type '{color('yes', Colors.YELLOW)}' to confirm: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        print("Cancelled.")
        return
    
    if confirm != "yes":
        print()
        print("Uninstall cancelled.")
        return
    
    print()
    print(color("Uninstalling...", Colors.CYAN, Colors.BOLD))
    print()

    if full_uninstall and remove_profiles and named_profiles:
        _preflight_named_profile_launchdaemons(named_profiles)
    
    # 1. Stop and uninstall gateway service + kill standalone processes
    log_info("Checking for running gateway...")
    if not uninstall_gateway_service():
        log_info("No gateway service or processes found")

    # 1b. Stop and remove each named profile's gateway service and alias
    #     wrapper before deleting this checkout. _uninstall_profile() shells
    #     out through `python -m hermes_cli.main`, so doing this after the code
    #     rmtree can leave a half-uninstalled profile cleanup path.
    if full_uninstall and remove_profiles and named_profiles:
        for prof in named_profiles:
            _uninstall_profile(prof)
    
    # 2. Remove PATH entries from shell configs (POSIX) AND from the Windows
    #    User-scope registry.  Both helpers no-op on the wrong platform so we
    #    can safely call them unconditionally.
    log_info("Removing PATH entries from shell configs...")
    removed_configs = remove_path_from_shell_configs()
    if removed_configs:
        for config in removed_configs:
            log_success(f"Updated {config}")
    else:
        log_info("No PATH entries found to remove in shell rc files")

    if _is_windows():
        log_info("Removing PATH entries from Windows User environment...")
        # Expand %LOCALAPPDATA% etc. in hermes_home so the marker matching is
        # against fully resolved paths — installer writes literal strings
        # like C:\Users\<u>\AppData\Local\hermes\git\cmd, not %LOCALAPPDATA%.
        removed_path_entries = remove_path_from_windows_registry(Path(os.path.expandvars(str(hermes_home))))
        if removed_path_entries:
            for entry in removed_path_entries:
                log_success(f"Removed from User PATH: {entry}")
        else:
            log_info("No Hermes-owned PATH entries in User environment")

        log_info("Removing HERMES_HOME / HERMES_GIT_BASH_PATH User env vars...")
        removed_env = remove_hermes_env_vars_windows()
        if removed_env:
            for name in removed_env:
                log_success(f"Removed User env var: {name}")
        else:
            log_info("No Hermes-set User env vars to remove")
    
    # 3. Remove wrapper script
    log_info("Removing hermes command...")
    removed_wrappers = remove_wrapper_script()
    if removed_wrappers:
        for wrapper in removed_wrappers:
            log_success(f"Removed {wrapper}")
    else:
        log_info("No wrapper script found")
    
    # 4. Remove installation directory (code)
    log_info("Removing installation directory...")
    
    # Check if we're running from within the install dir
    # We need to be careful here
    try:
        if project_root.exists():
            # If the install is inside ~/.hermes/, just remove the hermes-agent subdir
            if hermes_home in project_root.parents or project_root.parent == hermes_home:
                shutil.rmtree(project_root)
                log_success(f"Removed {project_root}")
            else:
                # Installation is somewhere else entirely
                shutil.rmtree(project_root)
                log_success(f"Removed {project_root}")
    except Exception as e:
        log_warn(f"Could not fully remove {project_root}: {e}")
        log_info("You may need to manually remove it")

    # 4b. Remove Windows-only installer artifacts that are NOT user data:
    #     PortableGit, bundled Node, gateway-service dir.  Installer put them
    #     under HERMES_HOME but they're install tooling, not config — safe to
    #     remove even in "keep data" mode.  If we're doing a full uninstall
    #     the step-5 rmtree(hermes_home) would sweep them anyway; calling
    #     this helper there is a no-op since they'll already be gone.
    if _is_windows():
        log_info("Removing Windows installer artifacts (PortableGit, Node, gateway-service)...")
        removed_artifacts = remove_portable_tooling_windows(hermes_home)
        if removed_artifacts:
            for path in removed_artifacts:
                log_success(f"Removed {path}")
        else:
            log_info("No Windows installer artifacts to remove")
    
    # 5. Optionally remove ~/.hermes/ data directory.
    if full_uninstall:
        log_info("Removing configuration and data...")
        try:
            if hermes_home.exists():
                shutil.rmtree(hermes_home)
                log_success(f"Removed {hermes_home}")
        except Exception as e:
            log_warn(f"Could not fully remove {hermes_home}: {e}")
            log_info("You may need to manually remove it")
    else:
        log_info(f"Keeping configuration and data in {hermes_home}")
    
    # Done
    print()
    print(color("┌─────────────────────────────────────────────────────────┐", Colors.GREEN, Colors.BOLD))
    print(color("│              ✓ Uninstall Complete!                      │", Colors.GREEN, Colors.BOLD))
    print(color("└─────────────────────────────────────────────────────────┘", Colors.GREEN, Colors.BOLD))
    print()
    
    if not full_uninstall:
        print(color("Your configuration and data have been preserved:", Colors.CYAN))
        print(f"  {hermes_home}/")
        print()
        print("To reinstall later with your existing settings:")
        if _is_windows():
            print(color("  iex (irm https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.ps1)", Colors.DIM))
        else:
            print(color("  curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash", Colors.DIM))
        print()

    if _is_windows():
        print(color("Open a new terminal (PowerShell / Windows Terminal) to pick up", Colors.YELLOW))
        print(color("the updated User PATH and environment variables.", Colors.YELLOW))
    else:
        print(color("Reload your shell to complete the process:", Colors.YELLOW))
        print("  source ~/.bashrc  # or ~/.zshrc")
    print()
    print("Thank you for using Hermes Agent! ⚕")
    print()
