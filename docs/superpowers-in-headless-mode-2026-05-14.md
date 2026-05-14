# Superpowers Skills in Headless `claude --print` Mode

**Date**: 2026-05-14
**Tested on**: mac3 (`darwin 25.3.0`), Claude Code CLI
**Goal**: Verify the Local-Autopilot's `headless_executor.py` (which shells
out to `claude --print --dangerously-skip-permissions`) can reach Superpowers
plugin skills so agent prompts can invoke `superpowers:brainstorming`,
`superpowers:systematic-debugging`, etc.

## TL;DR

**Superpowers skills ARE reachable from `claude --print`** as long as
`--bare` is NOT passed. The autopilot's headless executor currently does
NOT pass `--bare` (it was removed when it broke OAuth/keychain auth), so
the plugin loads normally and all skills are discoverable.

Recommendation: agent prompts will not *automatically* invoke skills —
they need a hint. Added one to the executor's prompt header. Not forced;
just discoverable.

## Installed Plugin

```
superpowers@superpowers-marketplace   v1.x   user scope   enabled
```

Plus marketplace siblings: `episodic-memory`, `superpowers-chrome`,
`superpowers-dev`, `claude-session-driver`, `double-shot-latte`,
`elements-of-style`.

Plugin path: `~/.claude/plugins/cache/superpowers-marketplace/superpowers/`

## Test 1 — Skill enumeration

Command:

```bash
claude --print --dangerously-skip-permissions --no-session-persistence \
  --max-budget-usd 0.10 \
  "List the names of every Superpowers skill available to you in this session..."
```

Output (14 skills, all `superpowers:` namespace):

```
superpowers:brainstorming
superpowers:executing-plans
superpowers:receiving-code-review
superpowers:finishing-a-development-branch
superpowers:requesting-code-review
superpowers:subagent-driven-development
superpowers:test-driven-development
superpowers:systematic-debugging
superpowers:using-git-worktrees
superpowers:using-superpowers
superpowers:dispatching-parallel-agents
superpowers:verification-before-completion
superpowers:writing-skills
superpowers:writing-plans
```

Plugin sync ran cleanly. No warnings, no errors.

## Test 2 — Invoking `superpowers:brainstorming`

With `--max-budget-usd 0.35`, the headless session described the
brainstorming skill's 6-step process (explore → clarify → propose → design
→ doc → transition) AND applied steps 2-3 to the sample question. Output
was skill-faithful — multiple-choice clarifier, 3 approaches with
trade-offs, explicit recommendation. Cost finished comfortably under the
cap.

## Test 3 — Invoking `superpowers:systematic-debugging`

Same setup. Output enumerated the 4 rigid phases (root-cause →
pattern-analysis → hypothesis → implementation) AND produced a concrete
debug plan keyed to the example failure. Identified the most likely
cause (launchd's stripped environment) and proposed the canonical
boundary-by-boundary trace. Skill behaviour intact.

## Budget note

At `--max-budget-usd 0.10`, skill invocations exceeded the cap and
returned `Error: Exceeded USD budget (0.1)`. The autopilot's default per-
agent cap is `_DEFAULT_MAX_BUDGET_USD = 0.20` which is also tight for any
skill that wants to read the bundled SKILL.md instructions. For agents
that should engage skills, raise the cap to `~0.35` or document that
skill use is opt-in only when the task is heavy.

## Why it works without `--bare`

`claude --bare` explicitly disables plugin sync, hooks, auto-memory, and
CLAUDE.md discovery (per `claude --help`). The autopilot used to pass
`--bare` for fast startup but Aaron removed it when it broke OAuth /
keychain auth (every agent returned "Not logged in"). Side-effect: plugin
loading is now active, which is exactly what we need for skills.

If anyone re-introduces `--bare`, they MUST also re-introduce a
`--plugin-dir` for `superpowers` or the autopilot loses skill access.

## Recommendation & change

Added a discoverability hint to the prompt header in
`local_autopilot/tools/headless_executor.py`:

> Hint: Superpowers skills (brainstorming, systematic-debugging,
> test-driven-development, verification-before-completion, writing-plans,
> etc.) are available via the Skill tool. Invoke them when the task
> warrants...

Soft hint, not a mandate — keeps trivial PASS/FAIL/SKIP checks fast while
making richer cognition discoverable for cross-exam and debugging
prompts.

## Files

- `local_autopilot/tools/headless_executor.py` (header updated)
- This doc
