from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

from rera_intel.config import ROOT_DIR, get_settings


LABEL = "com.puneetjain.rera.autosync"


def build_launch_agent_plist(
    *,
    python_path: Path,
    script_path: Path,
    working_directory: Path,
    interval_minutes: int,
    stdout_path: Path,
    stderr_path: Path,
) -> dict:
    activate_script = working_directory / ".venv" / "bin" / "activate"
    if activate_script.exists():
        shell_command = (
            f"cd {shlex.quote(str(working_directory))} && "
            f"source {shlex.quote(str(activate_script))} && "
            f"python {shlex.quote(str(script_path))}"
        )
        program_arguments = ["/bin/zsh", "-lc", shell_command]
    else:
        program_arguments = [str(python_path), str(script_path)]

    return {
        "Label": LABEL,
        "ProgramArguments": program_arguments,
        "WorkingDirectory": str(working_directory),
        "RunAtLoad": True,
        "StartInterval": max(interval_minutes, 5) * 60,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        },
    }


def choose_python_path() -> Path:
    venv_python = ROOT_DIR / ".venv" / "bin" / "python"
    if venv_python.exists():
        return venv_python
    return Path(sys.executable)


def main() -> None:
    settings = get_settings(require_api_key=True)
    launch_agents_dir = Path.home() / "Library" / "LaunchAgents"
    launch_agents_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = ROOT_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    plist_path = launch_agents_dir / f"{LABEL}.plist"
    plist_payload = build_launch_agent_plist(
        python_path=choose_python_path(),
        script_path=ROOT_DIR / "auto_update.py",
        working_directory=ROOT_DIR,
        interval_minutes=settings.auto_sync_interval_minutes,
        stdout_path=logs_dir / "auto_update.out.log",
        stderr_path=logs_dir / "auto_update.err.log",
    )

    with plist_path.open("wb") as handle:
        plistlib.dump(plist_payload, handle, sort_keys=False)

    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", domain, str(plist_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        check=True,
    )
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"{domain}/{LABEL}"],
        check=False,
    )

    print(f"Installed LaunchAgent: {plist_path}")
    print(f"Schedule check interval: every {settings.auto_sync_interval_minutes} minutes")
    print(
        "The auto updater will run once on load, then keep checking. "
        "It only performs a real sync when it is due and the internet is available."
    )
    print(f"Stdout log: {logs_dir / 'auto_update.out.log'}")
    print(f"Stderr log: {logs_dir / 'auto_update.err.log'}")


if __name__ == "__main__":
    main()
