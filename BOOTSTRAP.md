# Local Autopilot — Bootstrap

This is the short path from `git clone` to a running daemon. The README has the
long version; this file is the cheat sheet plus the safety-critical opt-ins.

## 1. Install

```bash
git clone https://github.com/supportersimulator/Local-Autopilot.git ~/dev/local-autopilot
cd ~/dev/local-autopilot
bash install.sh
```

## 2. Install the daemon

macOS:

```bash
bash scripts/install-daemon-macos.sh
```

Linux (systemd user units):

```bash
bash scripts/install-daemon-linux.sh
```

## 3. Turn it on

```bash
.venv/bin/autopilot on
```

The daemon ticks on its interval, but only does work when autopilot is `on`.

---

### Optional: full autonomy via headless executor

By default, every cycle stops at AWAIT — the runner emits the proposed agent
prompts and waits for you to inspect them and decide whether to execute. You
stay in the loop.

The `--headless-executor` flag flips that. With it enabled, Atlas pipes each
cycle prompt through `claude --print` (Claude Code's non-interactive mode) and
runs the agent without human review. Atlas now decides what to do AND does it.
That is a meaningfully larger trust delegation — read the rails below before
opting in.

What's expanded:

- Atlas decides AND executes each cycle. No human checkpoint per prompt.
- Faster turnaround — the cycle completes end-to-end on the daemon tick.
- Side-effects (file edits, tool calls) happen autonomously.

Safety rails still in place:

- Per-agent budget cap (`--cost-cap-usd`, enforced inside the runner).
- Per-agent timeout — runaway invocations are killed.
- `--bare` mode on the executor — no extra MCP servers, minimal blast radius.
- State machine still authoritative — `autopilot off` halts everything.
- Cycle artifacts continue to land under `~/.context-dna/autopilot-logs/cycle-*`;
  this is your audit trail. Review it regularly.

Install with the flag:

```bash
bash scripts/install-daemon-macos.sh --headless-executor
```

Linux:

```bash
bash scripts/install-daemon-linux.sh --headless-executor
```

How to revert:

- Re-run the installer **without** `--headless-executor`. The plist / unit is
  re-rendered without the flag and reloaded. Behavior reverts to AWAIT.
- Or, for an immediate stop: `.venv/bin/autopilot off`. The next tick exits
  before doing any work, regardless of executor mode.

---

## 4. Daily commands

```bash
.venv/bin/autopilot status
.venv/bin/autopilot on | off | elevated
tail -f ~/.context-dna/autopilot-logs/launchd.out
```

## 5. Uninstall

```bash
bash scripts/uninstall-daemon-macos.sh   # or uninstall-daemon-linux.sh
bash scripts/uninstall.sh                # remove venv + installed bits
```
