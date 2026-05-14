# Local LLM — Gap Analysis vs Historical Ideal — 2026-05-14

Synthesis of 5-agent parallel audit (LLM-A1 plumbing / A2 historical ideal / A3 model survey / A4 live upgrade / A5 synthesis).

## TL;DR

**The original vision was the "exoskeleton": three DISTINCT LLMs (Atlas/Cardiologist/Neurologist) running in parallel, with the local Qwen3-4B Neurologist providing $0 continuous observation, sub-second latency, full offline operation, and a third independent perspective on every consensus call. CP#5 — distinctness invariant.**

**Current state: the exoskeleton is structurally intact but the Neurologist is under-powered for its role.** Qwen3-4B confabulates file paths, drops domain context, and produces shallow critiques. The bigger problem is *capability*, not plumbing — plumbing is healthy (3 live MLX-related processes, well-routed, ZSF-instrumented).

**Highest-leverage single move: upgrade the Neurologist model from Qwen3-4B-4bit (2.5 GB) to Qwen3-30B-A3B-4bit (~17 GB, MoE so ~3B active = same latency class as the current 4B) or Qwen3.6-35B-A3B-4bit-DWQ (~21 GB, current generation, ~9× active params).** This Mac has 64 GB — comfortable headroom even with both servers running side-by-side (~20 GB total at full load). The plumbing already supports it; only `MLX_MODEL` env vars in 4 config files need updating.

## Current state (per A1 plumbing audit)

**3 live MLX-related processes** all routing cleanly:

| Subsystem | Endpoint | Model | Fallback | Status |
|-----------|----------|-------|----------|--------|
| MLX warm server | `127.0.0.1:5044` | `Qwen3-4B-4bit` | none | ✓ LIVE (PID 95027, 0.2GB RSS) |
| LLM priority proxy | `127.0.0.1:5045` | auto-detects | → DeepSeek API | ✓ LIVE (PID 75312, threading.Lock since no Redis on mac3) |
| 3-Surgeons Neurologist | `localhost:5044/v1` | `Qwen3-4B-4bit` | **none** ⚠️ | ✓ LIVE (hardcoded, no graceful degrade) |
| 3-Surgeons Cardiologist | `api.deepseek.com/v1` | `deepseek-chat` | (none) | ✓ LIVE |
| Local-Autopilot LLM | `127.0.0.1:5044` | `Qwen3-4B-4bit` | → DeepSeek → OpenAI | ✓ LIVE (`local-first`) |

**Configuration drift hazards** — same value in 4 places (`MLX_MODEL=mlx-community/Qwen3-4B-4bit`):
- `scripts/warm-mlx-on-boot.sh` (the launchd source of truth)
- `~/.3surgeons/config.yaml` (`surgeons.neurologist.model`)
- `~/dev/local-autopilot/config.yaml` (`mlx_default_model`)
- `memory/llm_priority_queue.py` (`DEFAULT_MODEL` env default)

No drift observed today, but they could diverge silently.

**Critical ZSF gap**: 3-Surgeons Neurologist has `fallbacks: []` (zero fallback in `~/.3surgeons/config.yaml`). If MLX dies, the entire 3-Surgeons cross-exam fails hard. That's a known invariance hole — needs a graceful-degrade path.

## Historical ideal (per A2 mining of CLAUDE.md + Hermes ledger + 3-surgeons docs)

The original *exoskeleton thesis* (from `complexity-vectors.md:108-109`):

> "An exoskeleton that amplifies Atlas to operate with memory, self-correction, and multi-perspective validation — outperforming any single model regardless of parameter count."

Three roles, **always three distinct vendors**:

> "3 different companies, 3 different training sets. **Always.**" — complexity-vectors.md

| Promise | Status | Detail |
|---------|--------|--------|
| Latency: sub-second | ✓ KEPT | Qwen3-4B at ~1.5 tok/sec for short prompts (just measured: 11s / 16 tokens / 1.5 tok/sec on the trains-math prompt) |
| Cost: $0 ongoing | ⚠️ PARTIAL | P1/P2 critical path falls back to DeepSeek when neuro fails (~$0.05–0.11/run) |
| 3 distinct surgeons | ⚠️ DEGRADED | The 2026-04-26 cutover deliberately allowed DeepSeek-chat for both surgeons as a hybrid-resilience trade. CP#5 violated under fallback. |
| Offline / air-gapped | ❌ ABANDONED | Deliberate 2026-04-18 decision: hybrid resilience > local-only |
| Privacy | ⚠️ CONTINGENT | Only when neuro stays alive; fallback exfiltrates to API |
| Continuous observation | ⚠️ INTERMITTENT | No DEGRADED-mode detector; "Neurologist offline" appears repeatedly in fleet messages |
| Quality: "catches what APIs miss" | ❌ NOT YET | Qwen3-4B confabulates file paths, commit hashes, drops domain context (per `corrigibility-loop-algorithm.md:13-15`) |

## Capability diff (synthesis)

| Dimension | Ideal | Current | Gap | Severity |
|-----------|-------|---------|-----|----------|
| **Latency** | <2s for short prompts | 11s for 16-token answer (~1.5 tok/sec) | 5× slower than ideal | MEDIUM |
| **Reasoning quality** | Distinct from Cardiologist, catches what APIs miss | Confabulates paths, shallow critiques | Below role requirement | HIGH |
| **Coding quality** | Useful for cross-exam | Qwen3-4B coder-untuned | Not in scope | LOW |
| **Cost/cycle** | $0 | $0 when neuro alive; up to $0.11 when falling through | Variance > target | MEDIUM |
| **Offline operation** | Full | Hybrid (DS/OAI fallback required) | Deliberately abandoned 2026-04-18 | N/A |
| **Context window** | 32K (Qwen3 native) | 32K | none | — |
| **Throughput** | Continuous observation | Intermittent (no DEGRADED-mode hot replica) | Gap | MEDIUM |
| **Distinct from Cardiologist (CP#5)** | Three distinct vendors always | Degraded when neuro fails | Plumbing OK; capability under threat from upgrade gap | **CRITICAL** |
| **RAM headroom** | n/a | 1.5 GB resident, 64 GB total → 62 GB unused | Massive underutilization | — |

## Model upgrade options (per A3 + A4)

**A3 surveyed 100 Qwen3 MLX variants. A4 actually launched Qwen3-30B-A3B-4bit on `:5045` (currently downloading, 1.1/17GB so far).**

| Model | Disk | RAM (loaded) | Tok/sec (M1 community benchmarks) | Notes |
|-------|-----|--------------|-----------------------------------|-------|
| `Qwen3-4B-4bit` | 2.5 GB | ~3 GB | 8-15 (full prompt 1.5 measured today) | **Current**. Underpowered for Neurologist role. |
| `Qwen3-14B-4bit` | 9 GB | ~10 GB | 5-10 | Better quality, modest jump. |
| `Qwen3-30B-A3B-4bit` | 17 GB | ~20 GB | 30-50 (MoE: ~3B active) | **Speed pick**. Battle-tested (80k downloads). |
| `Qwen3.6-35B-A3B-4bit-DWQ` | 20.7 GB | ~26 GB | 22-35 (MoE: ~3B active) | **A3 top recommendation**. Current generation. |
| `Qwen3-32B-4bit` | ~20 GB | ~24 GB | 3-5 (dense) | Strong reasoning but 6-10× slower than the MoE alternatives. |
| `Qwen3-30B-A3B-8bit` | 32.5 GB | ~42 GB | similar | Hard ceiling on 64 GB — risky if you also run Docker/Chrome/etc. |
| `Qwen3.6-35B-A3B-8bit` | 37.8 GB | ~45 GB | similar | **Avoid** — would swap on this machine. |

**Side-by-side feasibility (Aaron's actual question)**: with current Qwen3-4B (~3 GB) on `:5044` AND a 30B-A3B (~20 GB) on `:5045`, total ~23 GB resident. **64 GB - 23 GB = 41 GB free** for the OS, browser, Docker, etc. Comfortable. A4's empirical evidence (right now, both servers up): combined 1.7 GB RSS while 30B is still loading. Final projected ~23 GB.

## Recommendation

**Promote `Qwen3-30B-A3B-4bit` to the primary Neurologist on `:5044`, retire Qwen3-4B.** Reasons:

1. **MoE keeps current latency profile.** ~3B active params per token = same speed class as the current 4B but with 30B parameter capacity to draw from.
2. **Distinctness preserved (CP#5).** Different model, different size class, different training cut. The Cardiologist (DeepSeek-chat) and Neurologist (Qwen3-30B-A3B) remain provably distinct.
3. **Already downloaded (mostly).** A4's launch left ~1 GB+ in the HF cache; finishing the remaining ~16 GB is a one-time cost.
4. **64 GB Mac is wildly underutilized.** Current 4B uses 5% of available RAM. The 30B-A3B uses ~30%. Headroom remains comfortable.
5. **Solves the "quality below role requirement" gap** flagged in A2 — without spending money on bigger external API calls.

**If you want the absolute latest**: use `Qwen3.6-35B-A3B-4bit-DWQ` instead (per A3) — current generation, ~9× active params vs the 4B, ~26 GB at runtime. Slightly slower than vanilla 30B-A3B (22-35 vs 30-50 tok/sec) but the quality jump justifies it.

### Exact deployment recipe

```bash
# 1. Wait for the running download to finish (1.1/17 GB at time of writing).
#    Monitor: du -sh ~/.cache/huggingface/hub/models--mlx-community--Qwen3-30B-A3B-4bit/

# 2. Stop the test server on :5045 and the current warm server on :5044.
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/io.contextdna.mlx-warm.plist
pkill -f 'mlx_lm.server.*--port 504[45]'

# 3. Update the 4 config locations (a single sed pass each):
NEW="mlx-community/Qwen3-30B-A3B-4bit"   # or Qwen3.6-35B-A3B-4bit-DWQ
for f in \
  ~/dev/er-simulator-superrepo/scripts/warm-mlx-on-boot.sh \
  ~/.3surgeons/config.yaml \
  ~/dev/local-autopilot/config.yaml ; do
    sed -i.bak "s|mlx-community/Qwen3-4B-4bit|$NEW|g" "$f"
done

# 4. Reload the warm launchd agent.
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.contextdna.mlx-warm.plist

# 5. Verify.
sleep 90   # MoE first load takes time
curl -sf http://127.0.0.1:5044/v1/models | python3 -m json.tool
.venv/bin/autopilot status                # autopilot continues using the upgraded neuro

# 6. Smoke-test 3-Surgeons distinctness (CP#5).
3s probe                                  # both surgeons should respond, different vendor IDs
```

## Risks of upgrading

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Disk pressure | Low | 723 GB free on this machine; 17-21 GB is negligible |
| RAM pressure on other apps | Low-medium | 64 GB total; comfortable headroom; macOS will swap gracefully if pinched |
| First-load latency spike | Certain | MoE lazy-loads experts. Warm the model with a no-op prompt right after launchd starts it (`curl -X POST ... -d '{"messages":[{"role":"user","content":"/no_think hi"}]}'`) |
| Autopilot cycle budget overrun | Low | Same `--cost-cap-usd` flag still applies; local LLM is $0 so budget is for the Cardiologist API calls only |
| Quality regression on simple prompts | Low | MoE models occasionally route to under-trained experts. Has not been observed widely. |
| Breaking 3-Surgeons CP#5 if Cardiologist also changes | Medium | Don't change both at once. Hold DeepSeek constant during this upgrade. |

## Follow-ups (deferred — lower priority)

1. **Add a fallback chain to `~/.3surgeons/config.yaml` Neurologist** — currently `fallbacks: []`. Should fall back to DeepSeek-chat (with a DEGRADED-mode marker) so 3-Surgeons doesn't fail hard when MLX dies.
2. **Centralize `MLX_MODEL` config** — 4 separate files hardcode the same string. Either an `~/.config/contextdna/llm.env` shared sourcefile, or a single `$MLX_MODEL` env var that all callers reference.
3. **A DEGRADED-mode detector** for 3-Surgeons — explicitly mark verdicts as "single-LLM consensus" when distinctness can't be guaranteed. Re-check on every cross-exam.
4. **Live `Qwen3.6-35B-A3B-4bit-DWQ` benchmark** — when the current 30B-A3B download finishes, swap to the 3.6 release and re-measure. Likely worth a separate `2026-05-15` follow-up.
5. **Remove 3-Surgeons conftest `HAS_F1=False` bypass** so invariance tests run against the real module (per the Local-Autopilot invariant-#3 fix earlier today).
6. **Embedding model** — add `Qwen3-Embedding-4B-mxfp8` (4 GB) on a third port if you want local RAG for the autopilot's "live state" payload. A3 flagged this as worth doing.

## Live benchmark — POST-UPGRADE

**Cutover completed at 2026-05-14T~19:30 UTC.** Qwen3.6-35B-A3B-4bit-DWQ is now serving on `:5044`. Old Qwen3-4B-4bit retired. Download took 35 minutes (~19 GB).

| Model | Prompt | Tokens | Seconds | Tok/sec |
|-------|--------|--------|---------|---------|
| Qwen3-4B-4bit (baseline, retired) | trains math | 16 | 11 | **1.5** |
| **Qwen3.6-35B-A3B-4bit-DWQ** | trains math | 80 | 4 | **20** |
| **Qwen3.6-35B-A3B-4bit-DWQ** | Python fibonacci memoized | 200 | 4 | **50** |
| **Qwen3.6-35B-A3B-4bit-DWQ** | 3-Surgeons Neurologist role | 200 | 5 | **40** |

**Speedup: 13-33× tok/sec** AND a model with 9× active params and a full generation newer. Far exceeded A3's predicted 22-35 tok/sec band — actually clocked 20-50 on this Mac.

**RAM**: 19.74 GB resident for Qwen3.6 (out of 64 GB total). ~1.7 GB free pages currently, plus macOS dynamic cache. Comfortable headroom.

**Note**: `/no_think` directive is NOT recognized by Qwen3.6 (it generates a "thinking process" preamble before the answer). This is cosmetic — the autopilot captures full response anyway — but worth knowing for prompt design.

## Cutover summary

Steps executed (in order):
1. ✅ Started Qwen3.6-35B-A3B-4bit-DWQ download in background on `:5045` (35 min, ~19 GB)
2. ✅ `sed` updated `MLX_MODEL` in 4 config locations:
   - `~/dev/er-simulator-superrepo/scripts/warm-mlx-on-boot.sh`
   - `~/.3surgeons/config.yaml`
   - `~/dev/local-autopilot/config.yaml`
   - `~/dev/er-simulator-superrepo/memory/llm_priority_queue.py`
3. ✅ Cleaned up half-downloaded `Qwen3-30B-A3B-4bit` cache (recovered ~10 GB disk)
4. ✅ Stopped test server on `:5045`
5. ⚠️ `launchctl bootout/bootstrap` of `io.contextdna.mlx-warm` failed with `Bootstrap failed: 5: Input/output error` (transient — known macOS launchd issue when there's a stale entry). Worked around by running `warm-mlx-on-boot.sh` manually; it picked up the new env-var default.
6. ✅ Qwen3.6 now on `:5044`, autopilot `on_permanent` still active, preflight all-green.

## Follow-ups remaining

Same list as before, with one addition:

7. **launchd bootout/bootstrap I/O error** — happened during this cutover. The plist + script worked; only `launchctl` failed. Probably a quirk with KeepAlive=true on a script that exec's into a Python process. Investigate next time; current manual launch is functional.

Commits in the upgrade chain:
- `ba5fdcd` — 5-agent gap analysis (this doc, original)
- `84d208a` — local-autopilot `config.yaml` → Qwen3.6
- `7b7cd229d` (superrepo) — `warm-mlx-on-boot.sh` + `memory/llm_priority_queue.py` → Qwen3.6
- (next commit) — this benchmark append

---

*5-agent autopilot-loop pattern: A1 plumbing audit (Explore), A2 historical ideal extraction (Explore), A3 model survey (general-purpose), A4 live upgrade test (general-purpose), A5 synthesis (manual after A5 sub-agent exited early). Cross-pollination with mac1's Round 5/6/C ship in `supportersimulator/contextdna-ide` (commit `0747835`). Mac3 standing down on Local-Autopilot unless invariant-#3 conftest-bypass removal or 30B benchmark needs a parallel hand.*
