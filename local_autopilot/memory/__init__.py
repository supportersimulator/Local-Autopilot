"""Local Autopilot — standalone memory shim.

This package is a deliberately minimal substitute for the superrepo's
sprawling `memory/` package. It exposes only what the autopilot loop needs:

    from memory.llm_priority_queue import llm_generate, Priority

Both classes are real — they route to a local MLX server (default port 5044)
or a DeepSeek/OpenAI HTTPS fallback per `LLM_EXTERNAL_PROVIDER`.

There is no priority queue, no JetStream coupling, no Redis publication.
This is intentional: Local Autopilot is a single-process tool, not the
multi-fleet `webhook + injection + butler` orchestrator.
"""
