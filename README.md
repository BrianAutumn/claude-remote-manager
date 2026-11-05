# claude-remote-manager

Keeps tmux sessions running `claude remote-control` in line with a config file. To add a remote control session, add it to the config and run `sync`.

Each configured session gets a tmux session of the same name, in its directory, with one remote control server under the configured `claude` command.

## Setup

Requires macOS, tmux and [pipenv](https://pipenv.pypa.io).

```sh
pipenv install --dev
cp config.example.toml config.toml   # then edit it
pipenv run crm validate              # check the config
pipenv run crm sync --dry-run        # review the plan
pipenv run crm sync
pipenv run crm install-agent         # start sessions at every login
```

## Configuration

```toml
[[session]]
name = "my-project"                  # tmux and remote control session name
directory = "~/Projects/my-project"
claude = "claude"                    # command to launch with, which selects the account
```

`config.toml` is gitignored because it lists your project names. `config.example.toml` is the committed template. Use `--config PATH` to read a different file.

`validate` also runs in simpler form before `status`, `sync`, `boot` and `install-agent`. They refuse to act on an invalid config, including one with no sessions, because `sync` would otherwise shut everything down.

A directory can appear twice only with different `claude` commands. Claude keeps one reattach record per directory per account, so two servers sharing both can't reliably find their sessions again.

## Commands

| Command | What it does |
| --- | --- |
| `pipenv run crm validate` | Checks the config and that each account is signed in, listing every problem |
| `pipenv run crm status` | Compares running sessions with the config, changing nothing |
| `pipenv run crm sync` | Applies the config, asking before it interrupts anything |
| `pipenv run crm sync --dry-run` | Shows what `sync` would do |
| `pipenv run crm sync --yes` | Applies the config without asking |
| `pipenv run crm boot` | Waits for the network, then starts what's missing and stops nothing |
| `pipenv run crm install-agent` | Runs `boot` at every login |
| `pipenv run crm uninstall-agent` | Removes the login agent |

## What `sync` does

| Status | Situation | Action |
| --- | --- | --- |
| `ok` | Running as configured | None |
| `create` | Session is missing | Creates it and starts remote control |
| `relaunch` | Remote control has stopped | Starts it again in the same session |
| `restart` | Directory or `claude` changed, the session wasn't started by this tool, or a pane is busy with another program | Replaces the session |
| `trim` | More than one server in the session | Stops the extras |
| `remove` | Tmux session isn't in the config | Stops the server and kills the session |

**`sync` manages every session on the default tmux server**, so any tmux session not in the config is shut down, whatever is running in it. `restart`, `trim` and `remove` interrupt running work, so `sync` asks first. Without a terminal or `--yes`, it skips them.

## Continuing sessions after a restart

Remote control is started without `--continue`, which would switch it to single-session mode. A plain start reattaches to the session Claude has recorded for that directory and account, or creates a new one if there isn't one.

- **After a restart:** tested by killing a server and its child processes, then starting it again. It came back with the same environment and session IDs.
- **After about 4 hours or more of downtime:** Claude may discard its record and create a new session. The old conversation can still be opened with `claude --resume`.
- **Work in progress:** anything running when the machine goes down stops and doesn't continue by itself.

## Starting at boot

The login agent runs when you log in, not when the Mac powers on. For sessions to return unattended, enable automatic login in System Settings > Users & Groups. This also means anyone with physical access to the machine gets a logged-in session, and it isn't available with FileVault on.

The agent is tied to the pipenv environment it was installed from. Run `install-agent` again if you recreate that environment.

The log is at `~/Library/Logs/claude-remote-manager.log`.

## Tests

```sh
pipenv run test
```

The tests use a private tmux server and stub `claude` commands, so they don't touch real sessions.
