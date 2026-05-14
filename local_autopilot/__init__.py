"""Local Autopilot — Synaptic-driven autonomous hardening loop.

Top-level package. Submodules:
    local_autopilot.tools.autopilot_state   — F1 state machine
    local_autopilot.tools.autopilot_cli     — `autopilot status/on/off/temp` CLI
    local_autopilot.tools.archloop_runner   — F2 8-stage cycle runner
    local_autopilot.tools.synaptic_client   — Synaptic LLM wrapper
    local_autopilot.tools.deep_exploration  — G1 brainstorm + steelman stage
    local_autopilot.tools.agent_dispatch    — disk-based agent prompt fan-out
    local_autopilot.memory.llm_priority_queue — minimal LLM router (MLX/DeepSeek/OpenAI)
"""

__version__ = "0.1.0"
