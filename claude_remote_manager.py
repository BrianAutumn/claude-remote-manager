#!/usr/bin/env python3
"""Keep tmux sessions running `claude remote-control` in line with a config file.

Each configured session gets a tmux session of the same name, in its directory,
running one remote-control server under the configured claude command. Tmux
sessions that aren't in the config are shut down by `sync`.

Commands: validate, status, sync, boot, install-agent, uninstall-agent. See README.md.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.toml"
AGENT_LABEL = "com.claude-remote-manager.boot"
AGENT_PATH = Path.home() / "Library/LaunchAgents" / f"{AGENT_LABEL}.plist"
LOG_PATH = Path.home() / "Library/Logs/claude-remote-manager.log"

SIGNATURE_OPTION = "@crm_signature"
SERVER_RE = re.compile(r"(?:^|[/\s])claude\s+remote-control(?:\s|$)")
SHELLS = {"zsh", "-zsh", "bash", "-bash", "sh", "fish"}
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
COMMAND_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

START_ATTEMPTS = 3
RETRY_DELAY = 5.0
START_TIMEOUT = 30.0
NO_PROCESS_TIMEOUT = 15.0
STOP_TIMEOUT = 20.0
NETWORK_TIMEOUT = 300.0


class ConfigError(Exception):
    """Raised when the config file is missing or invalid."""
    pass


@dataclass(frozen=True)
class Session:
    """One configured session: what should be running, where, and under which account."""
    name: str
    directory: str
    claude: str

    @property
    def signature(self) -> str:
        """Tag stored on the tmux session to detect later changes to the directory or account."""
        return f"{self.claude}:{self.directory}"

    @property
    def command(self) -> str:
        """Shell command typed into the pane to start remote control."""
        return f"{self.claude} remote-control --name {shlex.quote(self.name)}"


@dataclass
class Pane:
    """A tmux pane and the program it currently shows."""
    pid: int
    pane_id: str
    current_command: str


@dataclass
class Live:
    """A running tmux session and the remote-control servers found inside it."""
    name: str
    panes: list[Pane]
    signature: str
    servers: list[int]


@dataclass
class Action:
    """One step in a sync plan, for either a configured session or an excess one."""
    kind: str  # ok, create, relaunch, restart, trim, remove
    name: str
    reason: str = ""
    session: Session | None = None
    live: Live | None = None

    @property
    def disruptive(self) -> bool:
        """Whether this action can interrupt work, and so needs confirmation."""
        return self.kind in {"restart", "trim", "remove"}


# --- Config -----------------------------------------------------------------

def validate_config(path: Path) -> tuple[list[Session], list[str]]:
    """Check the config file and return its valid sessions plus every problem found."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return [], [f"config not found: {path} (copy config.example.toml to get started)"]
    except tomllib.TOMLDecodeError as e:
        return [], [f"{path} is not valid TOML: {e}"]

    problems: list[str] = []
    unknown = set(data) - {"session"}
    if unknown:
        problems.append(f"unknown top-level keys: {', '.join(sorted(unknown))}")

    entries = data.get("session", [])
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        return [], problems + ["session entries must be written as [[session]] tables"]
    if not entries:
        # An empty config would make sync shut down every tmux session.
        problems.append("no sessions configured")

    sessions: list[Session] = []
    seen_names: set[str] = set()
    seen_pairs: dict[tuple[str, str], str] = {}
    for i, entry in enumerate(entries, 1):
        session = validate_entry(entry, f"session #{i}", problems)
        if session is None:
            continue
        pair = (session.directory, session.claude)
        if session.name in seen_names:
            problems.append(f"duplicate session name: {session.name}")
        elif pair in seen_pairs:
            # Claude keeps one reattach record per directory per account, so a second
            # server for the same pair can't reliably find its session again.
            problems.append(
                f"{session.name}: {session.directory} is already used with {session.claude} "
                f"by {seen_pairs[pair]}; use a different directory or account"
            )
        else:
            seen_names.add(session.name)
            seen_pairs[pair] = session.name
            sessions.append(session)

    problems.extend(check_commands({s.claude for s in sessions}))
    return sessions, problems


def validate_entry(entry: dict, where: str, problems: list[str]) -> Session | None:
    """Check one [[session]] table, adding to problems, and return it if it's usable."""
    before = len(problems)
    allowed = {"name", "directory", "claude"}
    missing = allowed - set(entry)
    extra = set(entry) - allowed
    if missing:
        problems.append(f"{where}: missing {', '.join(sorted(missing))}")
    if extra:
        problems.append(f"{where}: unknown keys {', '.join(sorted(extra))}")

    name = entry.get("name")
    if "name" in entry:
        if isinstance(name, str) and NAME_RE.match(name):
            where = name
        else:
            problems.append(f"{where}: name must use only letters, digits, - and _")

    claude = entry.get("claude")
    if "claude" in entry and not (isinstance(claude, str) and COMMAND_RE.match(claude)):
        problems.append(f"{where}: claude must be a command name, such as claude or claudeb")

    directory = None
    if "directory" in entry:
        if isinstance(entry["directory"], str):
            directory = os.path.realpath(os.path.expanduser(entry["directory"]))
            if not os.path.isdir(directory):
                problems.append(f"{where}: directory does not exist: {directory}")
        else:
            problems.append(f"{where}: directory must be a string")

    return None if len(problems) > before else Session(name, directory, claude)


def load_config(path: Path) -> list[Session]:
    """Return the configured sessions, raising ConfigError if there are any problems."""
    sessions, problems = validate_config(path)
    if problems:
        raise ConfigError("\n".join(f"  - {p}" for p in problems))
    return sessions


def interactive_shell(command: str) -> subprocess.CompletedProcess:
    """Run a command in an interactive shell, where aliases such as claudeb are defined."""
    shell = os.environ.get("SHELL", "/bin/zsh")
    return subprocess.run(
        [shell, "-ic", command],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
    )


def check_commands(commands: set[str]) -> list[str]:
    """Return a problem for each claude command the shell can't find."""
    return [
        f"claude command not found in your shell: {command}"
        for command in sorted(commands)
        if interactive_shell(f"type {shlex.quote(command)}").returncode != 0
    ]


def account_status(command: str) -> tuple[bool, str]:
    """Ask a claude command whether it's signed in, returning that and its account email."""
    result = interactive_shell(f"{shlex.quote(command)} auth status --json")
    # Shell startup files can print text before the JSON.
    start = result.stdout.find("{")
    try:
        status = json.loads(result.stdout[start:]) if start >= 0 else {}
    except json.JSONDecodeError:
        status = {}
    return bool(status.get("loggedIn")), status.get("email") or "unknown account"


# --- Tmux and process state -------------------------------------------------

def tmux(*args: str) -> subprocess.CompletedProcess:
    """Run a tmux command and return the result without raising on failure."""
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def process_table() -> dict[int, list[tuple[int, str]]]:
    """Map each parent pid to its (child pid, command line) pairs."""
    out = subprocess.run(
        ["ps", "-axww", "-o", "pid=,ppid=,command="], capture_output=True, text=True, check=True,
    ).stdout
    children: dict[int, list[tuple[int, str]]] = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            children.setdefault(int(parts[1]), []).append((int(parts[0]), parts[2]))
    return children


def servers_under(pid: int, children: dict[int, list[tuple[int, str]]]) -> list[int]:
    """Find remote-control servers at or below a pane's process."""
    commands = {child: command for kids in children.values() for child, command in kids}
    if SERVER_RE.search(commands.get(pid, "")):
        return [pid]  # The pane runs the server directly, with no shell in between.
    found: list[int] = []
    stack = [pid]
    while stack:
        for child, command in children.get(stack.pop(), []):
            if SERVER_RE.search(command):
                found.append(child)
            else:
                stack.append(child)
    return found


def get_panes(name: str) -> list[Pane]:
    """List every pane in the named tmux session."""
    result = tmux("list-panes", "-s", "-t", f"={name}", "-F",
                  "#{pane_pid}|#{pane_id}|#{pane_current_command}")
    panes = []
    for line in result.stdout.splitlines():
        pid, pane_id, command = line.split("|", 2)
        panes.append(Pane(int(pid), pane_id, command))
    return panes


def live_sessions() -> dict[str, Live]:
    """Return the current state of every tmux session, keyed by name."""
    listing = tmux("list-sessions", "-F", "#{session_name}")
    if listing.returncode != 0:
        return {}  # No tmux server is running.
    children = process_table()
    live = {}
    for name in listing.stdout.splitlines():
        panes = get_panes(name)
        signature = tmux("show-options", "-qv", "-t", f"={name}:", SIGNATURE_OPTION).stdout.strip()
        servers = [s for p in panes for s in servers_under(p.pid, children)]
        live[name] = Live(name, panes, signature, servers)
    return live


def session_servers(name: str) -> list[int]:
    """Return the pids of remote-control servers now running in a session."""
    children = process_table()
    return [s for p in get_panes(name) for s in servers_under(p.pid, children)]


# --- Planning ---------------------------------------------------------------

def build_plan(sessions: list[Session], live: dict[str, Live]) -> list[Action]:
    """Compare the config with running sessions and return the action for each."""
    actions = []
    for session in sessions:
        current = live.get(session.name)
        if current is None:
            actions.append(Action("create", session.name, "not running", session))
        elif current.signature != session.signature:
            reason = ("not started by this tool, so its account is unknown"
                      if not current.signature else "directory or claude command changed")
            actions.append(Action("restart", session.name, reason, session, current))
        elif not current.servers:
            if any(p.current_command in SHELLS for p in current.panes):
                actions.append(Action("relaunch", session.name, "remote control not running", session, current))
            else:
                actions.append(Action("restart", session.name, "pane is busy with another program", session, current))
        elif len(current.servers) > 1:
            actions.append(Action("trim", session.name, f"{len(current.servers)} servers running", session, current))
        else:
            actions.append(Action("ok", session.name, session.claude, session, current))

    configured = {s.name for s in sessions}
    for name, current in sorted(live.items()):
        if name not in configured:
            actions.append(Action("remove", name, "not in config", live=current))
    return actions


def print_plan(actions: list[Action]) -> None:
    """Print one aligned line per action."""
    if not actions:
        print("No sessions configured or running.")
        return
    width = max(len(a.name) for a in actions)
    for action in actions:
        print(f"{action.kind:<9} {action.name:<{width}}  {action.reason}")


# --- Acting -----------------------------------------------------------------

def stop_servers(pids: list[int]) -> None:
    """SIGTERM lets a server shut down cleanly and keep its environment for reattach."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        if not any(pid_alive(pid) for pid in pids):
            return
        time.sleep(0.25)
    for pid in pids:
        if pid_alive(pid):
            print(f"  server {pid} didn't stop in time, killing it")
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def pid_alive(pid: int) -> bool:
    """Return whether a process is still running, treating zombies as stopped."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A killed child stays a zombie until its shell reaps it.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return bool(state.strip()) and not state.strip().startswith("Z")


def create_session(session: Session) -> bool:
    """Create the tmux session and tag it, returning whether that succeeded."""
    result = tmux("new-session", "-d", "-s", session.name, "-c", session.directory, "-x", "200", "-y", "50")
    if result.returncode != 0:
        print(f"  could not create tmux session: {result.stderr.strip()}")
        return False
    tagged = tmux("set-option", "-t", f"={session.name}:", SIGNATURE_OPTION, session.signature)
    if tagged.returncode != 0:
        print(f"  could not tag tmux session: {tagged.stderr.strip()}")
        return False
    return True


def launch(session: Session) -> bool:
    """Start remote control. It reattaches if Claude has a session for this directory
    and account, otherwise it creates one. `--continue` isn't used because it runs
    in single-session mode. Starts can fail briefly after a crash, so retry."""
    pane = ""
    for attempt in range(1, START_ATTEMPTS + 1):
        panes = get_panes(session.name)
        shell_pane = next((p for p in panes if p.current_command in SHELLS), panes[0] if panes else None)
        if shell_pane is None:
            print("  session has no panes")
            return False
        pane = shell_pane.pane_id
        tmux("send-keys", "-t", pane, "clear", "Enter")
        tmux("send-keys", "-t", pane, "-l", session.command)
        tmux("send-keys", "-t", pane, "Enter")

        status = wait_for_server(session.name, pane)
        if status == "connected":
            print("  connected")
            return True
        if status == "running":
            print("  running, but not connected yet. Check it with: "
                  f"tmux attach -t {session.name}")
            return True
        if attempt < START_ATTEMPTS:
            print(f"  attempt {attempt} failed, retrying")
            time.sleep(RETRY_DELAY)

    print("  failed to start. Last output:")
    for line in pane_text(pane).splitlines()[-6:]:
        print(f"    {line[:160]}")
    return False


def pane_text(pane: str) -> str:
    """Return the visible text of a pane, without blank lines."""
    return "\n".join(l for l in tmux("capture-pane", "-p", "-t", pane).stdout.splitlines() if l.strip())


def wait_for_server(name: str, pane: str) -> str:
    """Wait for a server to start, returning connected, running or failed."""
    started = time.monotonic()
    seen = False
    while True:
        time.sleep(0.5)
        elapsed = time.monotonic() - started
        alive = bool(session_servers(name))
        if alive:
            seen = True
            if "Connected" in pane_text(pane):
                return "connected"
        elif seen or elapsed > NO_PROCESS_TIMEOUT:
            return "failed"
        if elapsed > START_TIMEOUT:
            return "running" if alive else "failed"


def current_tmux_session() -> str | None:
    """Return the tmux session this process runs in, if any."""
    if not os.environ.get("TMUX"):
        return None
    return tmux("display-message", "-p", "#{session_name}").stdout.strip() or None


def apply_action(action: Action) -> bool:
    """Carry out one action, returning whether it succeeded."""
    print(f"{action.kind}: {action.name} ({action.reason})")
    if action.kind == "ok":
        return True
    if action.kind == "create":
        return create_session(action.session) and launch(action.session)
    if action.kind == "relaunch":
        return launch(action.session)
    if action.kind == "trim":
        stop_servers(action.live.servers[1:])
        return True
    if action.kind == "restart":
        stop_servers(action.live.servers)
        tmux("kill-session", "-t", f"={action.name}")
        return create_session(action.session) and launch(action.session)
    if action.kind == "remove":
        stop_servers(action.live.servers)
        tmux("kill-session", "-t", f"={action.name}")
        return True
    raise ValueError(action.kind)


def run_order(actions: list[Action]) -> list[Action]:
    """Starts first, removals after, and the session we're running in last."""
    rank = {"ok": 0, "create": 1, "relaunch": 1, "trim": 2, "restart": 3, "remove": 4}
    me = current_tmux_session()
    return sorted(actions, key=lambda a: (a.name == me, rank[a.kind]))


def confirm(disruptive: list[Action]) -> bool:
    """Ask the user to approve actions that interrupt work."""
    print("\nThis interrupts any work running in these sessions:")
    for action in disruptive:
        print(f"  {action.kind} {action.name}: {action.reason}")
    return input("Continue? [y/N] ").strip().lower() in {"y", "yes"}


# --- Commands ---------------------------------------------------------------

def cmd_validate(args) -> int:
    """Check the config and each account's sign-in, reporting every problem found."""
    sessions, problems = validate_config(args.config)
    commands_by_email: dict[str, list[str]] = {}
    for command in sorted({s.claude for s in sessions}):
        if not any(command in p for p in problems):
            signed_in, email = account_status(command)
            if signed_in:
                print(f"{command}: signed in as {email}")
                commands_by_email.setdefault(email, []).append(command)
            else:
                problems.append(f"{command} is not signed in; run: {command} auth login")
    for email, commands in commands_by_email.items():
        if len(commands) > 1:
            print(f"Warning: {' and '.join(commands)} use the same account ({email})")
    if problems:
        print(f"{args.config} has {len(problems)} problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"{args.config} is valid, with {len(sessions)} session(s):")
    width = max(len(s.name) for s in sessions)
    for session in sessions:
        print(f"  {session.name:<{width}}  {session.claude:<8}  {session.directory}")
    return 0


def cmd_status(args) -> int:
    """Print how running sessions compare with the config."""
    print_plan(build_plan(load_config(args.config), live_sessions()))
    return 0


def cmd_sync(args) -> int:
    """Bring running sessions in line with the config, returning an exit code."""
    actions = build_plan(load_config(args.config), live_sessions())
    print_plan(actions)
    if args.dry_run:
        return 0

    disruptive = [a for a in actions if a.disruptive]
    skipped: list[Action] = []
    if disruptive and not args.yes:
        if sys.stdin.isatty():
            approved = confirm(disruptive)
        else:
            print("\nNot a terminal, so skipping changes that interrupt work. Use --yes to apply them.")
            approved = False
        if not approved:
            skipped = disruptive
    return apply_all([a for a in actions if a not in skipped], skipped)


def cmd_boot(args) -> int:
    """Login path: only starts what's missing, and never stops anything."""
    print(time.strftime("%F %T"), "boot")
    wait_for_network()
    actions = build_plan(load_config(args.config), live_sessions())
    return apply_all([a for a in actions if a.kind in {"create", "relaunch"}], [])


def apply_all(actions: list[Action], skipped: list[Action]) -> int:
    """Run the actions in order, report the outcome and return an exit code."""
    failed = [a.name for a in run_order(actions) if a.kind != "ok" and not apply_action(a)]
    if skipped:
        print("Skipped: " + ", ".join(a.name for a in skipped))
    if failed:
        print("Failed: " + ", ".join(failed))
        return 1
    print("In sync." if not skipped else "Done, with changes skipped.")
    return 0


def wait_for_network() -> None:
    """Wait until Claude's API is reachable, or give up after NETWORK_TIMEOUT."""
    deadline = time.monotonic() + NETWORK_TIMEOUT
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen("https://api.anthropic.com", timeout=5)
            return
        except urllib.error.HTTPError:
            return  # Any HTTP response means the network is up.
        except OSError:
            time.sleep(5)
    print("Network not reachable after waiting, trying anyway.")


def cmd_install_agent(args) -> int:
    """Write the LaunchAgent that runs boot at every login."""
    config = args.config.resolve()
    load_config(config)
    home = str(Path.home())
    plist = {
        "Label": AGENT_LABEL,
        "ProgramArguments": [sys.executable, str(ROOT / "claude_remote_manager.py"),
                             "--config", str(config), "boot"],
        "RunAtLoad": True,
        "WorkingDirectory": home,
        "EnvironmentVariables": {
            "PATH": f"/opt/homebrew/bin:{home}/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "SHELL": os.environ.get("SHELL", "/bin/zsh"),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        },
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
    }
    AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(AGENT_PATH, "wb") as f:
        plistlib.dump(plist, f)
    print(f"Wrote {AGENT_PATH}. It runs at your next login. Log: {LOG_PATH}")
    return 0


def cmd_uninstall_agent(args) -> int:
    """Unload and delete the LaunchAgent."""
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}"], capture_output=True)
    AGENT_PATH.unlink(missing_ok=True)
    print(f"Removed {AGENT_PATH}")
    return 0


def main() -> int:
    """Parse the command line, run the chosen command and return its exit code."""
    parser = argparse.ArgumentParser(description="Manage tmux sessions running claude remote-control.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="config file (default: %(default)s)")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("validate", help="check the config file and account sign-ins").set_defaults(func=cmd_validate)
    commands.add_parser("status", help="compare running sessions with the config").set_defaults(func=cmd_status)
    sync = commands.add_parser("sync", help="start missing sessions and stop excess ones")
    sync.add_argument("--dry-run", action="store_true", help="show the plan without changing anything")
    sync.add_argument("--yes", action="store_true", help="don't ask before interrupting sessions")
    sync.set_defaults(func=cmd_sync)
    commands.add_parser("boot", help="wait for network, then start what's missing").set_defaults(func=cmd_boot)
    commands.add_parser("install-agent", help="run boot at every login").set_defaults(func=cmd_install_agent)
    commands.add_parser("uninstall-agent", help="remove the login agent").set_defaults(func=cmd_uninstall_agent)

    args = parser.parse_args()
    if not shutil.which("tmux"):
        print("tmux not found on PATH", file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except ConfigError as e:
        print(f"Config error:\n{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
