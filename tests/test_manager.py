"""Integration tests against a real, private tmux server and stub claude commands."""
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "claude_remote_manager.py"

REAL_STUB = """#!/bin/zsh
if [[ "$1" == auth ]]; then
  if [[ -e "$STUB_DIR/logged_out_$STUB_ACCOUNT" ]]; then
    echo '{"loggedIn": false}'
    exit 1
  fi
  echo "{\\"loggedIn\\": true, \\"email\\": \\"${STUB_EMAIL:-$STUB_ACCOUNT@example.test}\\"}"
  exit 0
fi
echo "$STUB_ACCOUNT $PWD $*" >> "$STUB_DIR/starts.log"
if [[ -e "$STUB_DIR/fail_next" ]]; then
  rm "$STUB_DIR/fail_next"
  echo "Error: This folder is already served"
  exit 1
fi
echo "Connected"
trap 'kill $child 2>/dev/null; exit 0' TERM
sleep 1000 &
child=$!
wait $child
"""


class Env:
    """A throwaway HOME, tmux server and stub claude commands for one test."""
    def __init__(self, root: Path):
        """Create the directories, stubs and environment variables under root."""
        self.root = root
        self.home = root / "home"
        self.bin = root / "bin"
        self.config = root / "config.toml"
        (self.bin / "real").mkdir(parents=True)
        self.home.mkdir()
        (self.home / ".zshrc").write_text("")
        for sub in ("one", "two"):
            (root / sub).mkdir()

        self._executable(self.bin / "real" / "claude", REAL_STUB)
        for command, account in (("claude", "a"), ("claudeb", "b")):
            self._executable(
                self.bin / command,
                f'#!/bin/zsh\nSTUB_ACCOUNT={account} exec "{self.bin}/real/claude" "$@"\n',
            )

        self.env = {
            "HOME": str(self.home),
            "USER": os.environ.get("USER", "test"),
            "SHELL": "/bin/zsh",
            "TERM": "xterm-256color",
            "LANG": "en_US.UTF-8",
            "PATH": f"{self.bin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "TMUX_TMPDIR": str(root),
            "STUB_DIR": str(root),
        }

    @staticmethod
    def _executable(path: Path, text: str):
        """Write a file and mark it executable."""
        path.write_text(text)
        path.chmod(0o755)

    def write_config(self, *sessions):
        """Write config.toml from (name, subdirectory, claude command) tuples."""
        blocks = [
            f'[[session]]\nname = "{name}"\ndirectory = "{self.root / sub}"\nclaude = "{claude}"\n'
            for name, sub, claude in sessions
        ]
        self.config.write_text("\n".join(blocks))

    def run(self, *args):
        """Run the manager CLI against this environment, without a terminal."""
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--config", str(self.config), *args],
            env=self.env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120,
        )

    def tmux(self, *args):
        """Run a tmux command against this environment's private server."""
        return subprocess.run(["tmux", *args], env=self.env, capture_output=True, text=True)

    def sessions(self):
        """Return the sorted names of running tmux sessions."""
        out = self.tmux("list-sessions", "-F", "#{session_name}").stdout
        return sorted(out.split())

    def starts(self):
        """Return one line for each time a stub claude was started."""
        log = self.root / "starts.log"
        return log.read_text().splitlines() if log.exists() else []

    def server_pids(self, name):
        """Return the processes running under the session's first pane."""
        pane = self.tmux("list-panes", "-t", f"={name}:", "-F", "#{pane_pid}").stdout.split()[0]
        out = subprocess.run(["pgrep", "-P", pane], capture_output=True, text=True).stdout
        return [int(p) for p in out.split()]


@pytest.fixture
def env():
    # tmux socket paths have a short length limit, so keep this path short.
    """Provide a fresh Env and clean up its tmux server and files afterwards."""
    root = Path(tempfile.mkdtemp(prefix="crm", dir="/tmp")).resolve()
    e = Env(root)
    yield e
    e.tmux("kill-server")
    shutil.rmtree(root, ignore_errors=True)


def test_sync_creates_sessions_with_correct_account_and_directory(env):
    """Each session starts in its directory under its configured command."""
    env.write_config(("alpha", "one", "claude"), ("beta", "two", "claudeb"))
    result = env.run("sync")
    assert result.returncode == 0, result.stdout + result.stderr
    assert env.sessions() == ["alpha", "beta"]
    assert sorted(env.starts()) == sorted([
        f"a {env.root}/one remote-control --name alpha",
        f"b {env.root}/two remote-control --name beta",
    ])


def test_sync_is_idempotent(env):
    """A second sync leaves a matching session and its server alone."""
    env.write_config(("alpha", "one", "claude"))
    env.run("sync")
    before = env.server_pids("alpha")
    result = env.run("sync", "--yes")
    assert result.returncode == 0
    assert result.stdout.startswith("ok ")
    assert env.server_pids("alpha") == before
    assert len(env.starts()) == 1


def test_sync_shuts_down_sessions_not_in_config(env):
    """Removing a session from the config stops its server and session."""
    env.write_config(("alpha", "one", "claude"), ("beta", "two", "claude"))
    env.run("sync")
    pids = env.server_pids("beta")
    env.write_config(("alpha", "one", "claude"))
    result = env.run("sync", "--yes")
    assert result.returncode == 0
    assert env.sessions() == ["alpha"]
    time.sleep(0.5)
    assert subprocess.run(["ps", "-p", str(pids[0])], capture_output=True).returncode != 0


def test_noninteractive_sync_without_yes_skips_disruptive_changes(env):
    """Without a terminal or --yes, sync adds sessions but removes none."""
    env.write_config(("alpha", "one", "claude"))
    env.run("sync")
    env.tmux("new-session", "-d", "-s", "extra")
    env.write_config(("alpha", "one", "claude"), ("beta", "two", "claude"))
    result = env.run("sync")
    assert "Skipped: extra" in result.stdout
    assert env.sessions() == ["alpha", "beta", "extra"]


def test_dry_run_changes_nothing(env):
    """--dry-run prints the plan and takes priority over --yes."""
    env.write_config(("alpha", "one", "claude"))
    env.tmux("new-session", "-d", "-s", "extra")
    result = env.run("sync", "--dry-run", "--yes")
    assert "create" in result.stdout and "remove" in result.stdout
    assert env.sessions() == ["extra"]
    assert env.starts() == []


def test_changing_account_restarts_session(env):
    """Changing the claude command restarts the session under the new one."""
    env.write_config(("alpha", "one", "claude"))
    env.run("sync")
    env.write_config(("alpha", "one", "claudeb"))
    result = env.run("sync", "--yes")
    assert result.returncode == 0, result.stdout
    assert env.starts()[-1].startswith("b ")
    assert len(env.server_pids("alpha")) == 1


def test_changing_directory_restarts_session(env):
    """Changing the directory restarts the session there."""
    env.write_config(("alpha", "one", "claude"))
    env.run("sync")
    env.write_config(("alpha", "two", "claude"))
    env.run("sync", "--yes")
    assert f"{env.root}/two " in env.starts()[-1]


def test_untagged_session_with_configured_name_is_restarted(env):
    """A session this tool didn't start is replaced, since its account is unknown."""
    env.tmux("new-session", "-d", "-s", "alpha")
    env.write_config(("alpha", "one", "claude"))
    result = env.run("sync", "--yes")
    assert "not started by this tool" in result.stdout
    assert len(env.starts()) == 1


def test_dead_server_is_relaunched_in_same_session(env):
    """A stopped server is started again without replacing the tmux session."""
    env.write_config(("alpha", "one", "claude"))
    env.run("sync")
    created = env.tmux("display-message", "-p", "-t", "=alpha:", "#{session_id}").stdout
    subprocess.run(["kill", "-9", str(env.server_pids("alpha")[0])])
    time.sleep(1)
    assert "relaunch" in env.run("status").stdout
    result = env.run("sync")
    assert result.returncode == 0, result.stdout
    assert len(env.starts()) == 2
    assert env.tmux("display-message", "-p", "-t", "=alpha:", "#{session_id}").stdout == created


def test_extra_server_in_session_is_stopped(env):
    """A session running two servers is reduced to one."""
    env.write_config(("alpha", "one", "claude"))
    env.run("sync")
    env.tmux("split-window", "-t", "=alpha:", f"{env.bin}/claude remote-control --name other")
    time.sleep(1.5)
    assert "trim" in env.run("status").stdout
    env.run("sync", "--yes")
    time.sleep(0.5)
    assert "ok" in env.run("status").stdout


def test_failed_start_is_retried(env):
    """A start that fails once is tried again and succeeds."""
    env.write_config(("alpha", "one", "claude"))
    (env.root / "fail_next").touch()
    result = env.run("sync")
    assert result.returncode == 0, result.stdout
    assert "retrying" in result.stdout
    assert len(env.starts()) == 2


def test_boot_starts_missing_and_never_stops_anything(env):
    """boot starts missing sessions and leaves excess ones running."""
    env.write_config(("alpha", "one", "claude"))
    env.tmux("new-session", "-d", "-s", "extra")
    result = env.run("boot")
    assert result.returncode == 0, result.stdout
    assert env.sessions() == ["alpha", "extra"]


@pytest.mark.parametrize("sessions, message", [
    ([("alpha", "one", "claude"), ("alpha", "two", "claude")], "duplicate session name"),
    ([("alpha", "one", "claude"), ("beta", "one", "claude")], "already used"),
    ([("alpha", "missing", "claude")], "does not exist"),
    ([("alpha", "one", "nosuchclaude")], "command not found"),
    ([("bad.name", "one", "claude")], "name must use"),
])
def test_invalid_config_is_rejected_without_changes(env, sessions, message):
    """A bad config exits with an error before anything is started."""
    env.write_config(*sessions)
    result = env.run("sync", "--yes")
    assert result.returncode == 2
    assert message in result.stderr
    assert env.sessions() == []


def test_same_directory_allowed_for_different_accounts(env):
    """Two accounts can each have a session in the same directory."""
    env.write_config(("alpha", "one", "claude"), ("beta", "one", "claudeb"))
    assert env.run("sync").returncode == 0
    assert env.sessions() == ["alpha", "beta"]


def test_validate_reports_a_valid_config_and_accounts(env):
    """validate lists the sessions and accounts and starts nothing."""
    env.write_config(("alpha", "one", "claude"), ("beta", "two", "claudeb"))
    result = env.run("validate")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "with 2 session(s)" in result.stdout
    assert "claude: signed in as a@example.test" in result.stdout
    assert "claudeb: signed in as b@example.test" in result.stdout
    assert env.sessions() == []


def test_validate_reports_every_problem_at_once(env):
    """validate doesn't stop at the first problem."""
    env.write_config(("bad.name", "one", "claude"), ("alpha", "missing", "claude"), ("beta", "one", "nosuch"))
    result = env.run("validate")
    assert result.returncode == 1
    assert "3 problem(s)" in result.stdout
    for message in ("name must use", "does not exist", "command not found"):
        assert message in result.stdout


def test_validate_flags_a_signed_out_account(env):
    """An account that isn't signed in fails validation, with the fix in the message."""
    env.write_config(("alpha", "one", "claudeb"))
    (env.root / "logged_out_b").touch()
    result = env.run("validate")
    assert result.returncode == 1
    assert "claudeb is not signed in" in result.stdout


@pytest.mark.parametrize("text, message", [
    ("", "no sessions configured"),
    ("not toml [", "not valid TOML"),
    ("session = 1\n", "[[session]] tables"),
    ('[[session]]\nname = "alpha"\n', "missing claude, directory"),
    ('other = 1\n[[session]]\nname = "a"\ndirectory = "/"\nclaude = "claude"\n', "unknown top-level keys"),
    ('[[session]]\nname = "a"\ndirectory = "/"\nclaude = "claude"\nx = 1\n', "unknown keys x"),
    ('[[session]]\nname = "a"\ndirectory = 1\nclaude = "claude"\n', "directory must be a string"),
    ('[[session]]\nname = "a"\ndirectory = "/"\nclaude = "rm -rf"\n', "claude must be a command name"),
])
def test_validate_rejects_malformed_config(env, text, message):
    """Malformed files are reported as problems rather than crashing."""
    env.config.write_text(text)
    result = env.run("validate")
    assert result.returncode == 1, result.stdout + result.stderr
    assert message in result.stdout


def test_validate_reports_missing_config(env):
    """A missing config points to the example file."""
    result = env.run("validate")
    assert result.returncode == 1
    assert "config.example.toml" in result.stdout


def test_empty_config_never_shuts_sessions_down(env):
    """sync refuses an empty config instead of removing every session."""
    env.tmux("new-session", "-d", "-s", "extra")
    env.config.write_text("")
    result = env.run("sync", "--yes")
    assert result.returncode == 2
    assert env.sessions() == ["extra"]


def test_example_config_is_valid_once_its_directory_exists(env):
    """The committed example passes validation, apart from its placeholder directory."""
    example = (SCRIPT.parent / "config.example.toml").read_text()
    assert "~/Projects/my-project" in example
    env.config.write_text(example.replace("~/Projects/my-project", str(env.root / "one")))
    result = env.run("validate")
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_config_is_gitignored_and_example_is_not():
    """config.toml must stay out of the public repo, while the example stays in."""
    def ignored(name):
        """Return whether git ignores the named file."""
        return subprocess.run(["git", "check-ignore", "-q", name], cwd=SCRIPT.parent).returncode == 0
    assert ignored("config.toml")
    assert not ignored("config.example.toml")


def test_validate_warns_when_commands_share_an_account(env):
    """Two commands on one account are valid, but probably not what was intended."""
    env.write_config(("alpha", "one", "claude"), ("beta", "two", "claudeb"))
    assert "Warning" not in env.run("validate").stdout
    env.env["STUB_EMAIL"] = "same@example.test"
    result = env.run("validate")
    assert result.returncode == 0
    assert "Warning: claude and claudeb use the same account" in result.stdout
