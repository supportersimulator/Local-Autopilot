# Autopilot API contract — what F1 + F2 must ship

The invariance harness in this directory tests against the following contract.
If a real implementation is missing from `tools/` the suite falls back to an
in-tree mock (`_mock_autopilot_state.py`) that satisfies *exactly* the surface
below. Whenever F1 / F2 ship the real modules, the same tests run against
those without modification.

## F1 — `tools/autopilot_state.py`

```python
class State:
    """Persistent autopilot state. JSON-backed, flock-serialised, atomic write."""

    # ── Class-level invariants ──────────────────────────────────────────
    VALID_MODES = {"off", "on_temporary", "on_permanent"}
    VALID_ACTORS = {"user", "atlas", "system"}

    # ── Constructor ─────────────────────────────────────────────────────
    def __init__(self, path: str): ...

    # ── Read ────────────────────────────────────────────────────────────
    def read(self) -> dict:
        """Return current state dict. Schema:
            {
              "mode": "off" | "on_temporary" | "on_permanent",
              "temporary_until": <iso8601 str | None>,
              "temporary_reason": <str | None>,
              "transition_history": [ { "ts": <iso8601>,
                                        "actor": str,
                                        "from": str, "to": str,
                                        "reason": str } ],
              "counters": { "autopilot_transitions_total": int, ... },
              "schema_version": 1
            }
        Raises:
            StateCorruption — JSON unparseable or schema invalid.
        """
        ...

    # ── Transition ──────────────────────────────────────────────────────
    def transition(self, *, to: str, actor: str, reason: str,
                   temporary_until: str | None = None) -> dict:
        """Atomically transition mode.

        Enforces (raises PermissionDenied on violation):
          - actor='atlas' MAY NOT set to='off' from 'on_permanent'
          - actor='atlas' MAY NOT set to='on_permanent'
          - to='on_temporary' requires temporary_until OR
            reason containing 'atlas-decision'
        """
        ...
```

Module-level exceptions:

```python
class StateCorruption(Exception): ...
class PermissionDenied(Exception): ...
```

## F1 — `tools/autopilot_cli.py`

CLI argparse interface:

```
autopilot_cli.py set --mode <off|on_temporary|on_permanent>
                     --actor <user|atlas>
                     --reason <str>
                     [--until <iso8601>]
                     [--state-path <path>]
autopilot_cli.py show [--state-path <path>]
```

Exit non-zero on PermissionDenied. Appends to `/tmp/autopilot-cli.log`
(append-only by convention; tested via inode + mode bits).

## F2 — `tools/archloop_runner.py`

```python
class Runner:
    def __init__(self, state: State, *,
                 cost_cap_usd: float = 5.0,
                 cycle_cap: int | None = None,
                 kill_file: str = "/tmp/autopilot.stop"): ...

    def run(self) -> int:
        """Drive cycles until: kill-file present, cycle_cap reached,
        cost-cap exceeded, or state.mode='off'. Returns number of
        cycles executed."""
        ...

    def cycle(self, cycle_id: str) -> dict:
        """Run one cycle. MUST:
          - write `.fleet/autopilot/cycle-<id>/cycle_summary.json`
          - increment counters.cycles_completed
          - re-read complexity vectors LIVE (no in-cycle caching across
            independent .read() calls inside a single cycle — same
            content; new cycle → new fetch).
        """
```

## F2 — `tools/synaptic_client.py`

```python
def fetch_complexity_vectors(db_path: str) -> list[dict]:
    """Live read of complexity_vectors.db. NEVER cached."""
```

## F2 — `tools/agent_dispatch.py`

```python
def dispatch_agents(specs: list[dict], *, dry_run: bool = False) -> list[dict]:
    """Spawn N parallel agents. Returns one result dict per agent."""
```

## File-system invariants

| Path | Mode | Append-only? |
|------|------|--------------|
| `/tmp/autopilot-cli.log` | 0644 | YES |
| `/tmp/autopilot-synaptic-trace.jsonl` | 0644 | YES |
| `.fleet/autopilot/cycle-*/cycle_summary.json` | 0644 | per-cycle |
| `<state-path>` | 0600 | atomic rename |

The invariance suite hard-fails if any of the above are missing or violate
their mode bits / append-only semantic during a real run.
