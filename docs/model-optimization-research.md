# Local model optimization — research & roadmap

Research backing Kodiqa's local-model compression/speedup work (the v3.18+ "model
optimization" roadmap). Goal: let users **download smaller** models, run them
**faster**, use **less RAM/VRAM**, and optionally **build optimized variants**
on-device — while keeping models usable for coding. Stack: Python CLI → Ollama
(llama.cpp / GGUF) on Apple Silicon + Linux/NVIDIA.

> Researched 2026-06; figures are point-in-time. Vendor benchmarks (Unsloth,
> Cerebras/REAP) are self-reported unless noted. Verify model-specific numbers on
> the relevant HF model card / release notes.

## TL;DR — the four levers

1. **Quantization** = "compressed to download but still usable". `Q4_K_M` ≈ 70%
   smaller than fp16 at ~95% quality. This is what Ollama already serves.
2. **Runtime knobs** (flash attention, KV-cache quant, offload, speculative
   decoding) = "faster / less RAM" — mostly free, Ollama ships them off.
3. **Structural compression** (REAP expert-pruning, distillation) = smaller
   *models* (not just quantized) — download today as GGUF, but mostly large MoE.
4. **On-device building** (`ollama create --quantize`, HF→GGUF + imatrix) =
   "Kodiqa optimizes models itself".

**Hard constraint:** only **GGUF** runs in Ollama/llama.cpp. AWQ, GPTQ, EXL2/EXL3,
FP8, NVFP4 are GPU-engine formats (vLLM/ExLlama/TensorRT, mostly NVIDIA) — not
usable here.

## 1. Quantization (download size + usability)

GGUF quant ladder (bpw/size from llama.cpp's quantize README, Llama-3.1-8B):

| Quant | ~bpw | ~size (8B) | Notes |
|-------|------|-----------|-------|
| Q8_0 | 8.5 | 8.0 GiB | reference; fp16 buys ~nothing |
| Q6_K | 6.6 | 6.1 GiB | near-lossless |
| Q5_K_M | 5.7 | 5.3 GiB | high quality |
| **Q4_K_M** | **4.9** | **4.6 GiB** | **community sweet spot (~95% quality, ~70% smaller)** |
| IQ4_XS | 4.5 | 4.2 GiB | ~Q4_K_M quality, slightly smaller; slower on CPU |
| IQ3_XXS | 3.25 | 3.0 GiB | usable, degraded |
| IQ2_XXS | 2.4 | 2.2 GiB | only viable on big/MoE models |
| IQ1_S | 2.0 | 1.9 GiB | broken for coding |

- **Coding floor ≈ 4-bit.** Reliable at Q4_K_M / IQ4_XS; degraded at 3-bit; 2-bit
  only on large/MoE models; 1-bit collapses for code.
- **imatrix** (importance-matrix calibration) is effectively required below ~Q6 —
  but just download `bartowski`/`unsloth` imatrix GGUFs; rarely build your own.
- **Unsloth "Dynamic" (UD-) quants** (e.g. `UD-Q4_K_XL`, `UD-IQ2_M`) selectively
  keep important layers at higher precision → best quality-per-byte at low bits,
  and they're **plain GGUF** (run as-is in Ollama). The `_XL` tier is flagship.
- **I-quants decode slower on CPU** than K-quants — relevant for CPU-bound runs.

Sources: llama.cpp quantize README; Unsloth Dynamic 2.0 docs; kaitchup GGUF guide;
arXiv quant eval (2601.14277).

## 2. Runtime speed/memory knobs (Ollama / llama.cpp)

- **Flash attention** — `OLLAMA_FLASH_ATTENTION=1` (off by default). Lower memory
  as context grows; negligible quality cost.
- **KV-cache quantization** — `OLLAMA_KV_CACHE_TYPE` = `f16` (default) / `q8_0`
  (½ KV RAM, tiny loss) / `q4_0` (¼ KV RAM, small-medium loss). Global; needs
  flash attention on. → **Phase 1 enables q8_0 + flash by default.**
- **Per-request `options`** (API/Modelfile): `num_ctx` (KV scales linearly with
  it — don't run huge contexts blindly), `num_gpu` (layers offloaded; -1 = all),
  `num_batch` (512 default; higher = more VRAM/throughput), `num_thread`,
  `keep_alive`, `use_mmap`/`use_mlock`.
- **Speculative decoding** — ~1.5–3× tok/s, no quality loss. llama.cpp:
  `-md/--model-draft` + `--spec-draft-n-max`. Ollama: now supported via the
  `DRAFT` Modelfile instruction + `draft_num_predict` (MLX runner first,
  ~v0.23.1+). Draft must share tokenizer/family and be much smaller. **Not always
  a net win** (small-active MoE / older NVIDIA) — make it opt-in/measured.
- **Apple Metal** — unified memory (no separate VRAM, no copy overhead); bandwidth-
  bound so Q4 runs faster than Q8; plug in AC (battery throttles GPU). GPU memory
  cap ≈ 66–75% of RAM; raise via `sudo sysctl iogpu.wired_limit_mb=<MB>` (leave
  8–16 GB for macOS).
- **MoE expert offload** (llama.cpp `--n-cpu-moe` / `-ot`) — keep rarely-used
  expert FFNs on CPU RAM → run huge MoE in far less VRAM, at reduced tok/s.
  Ollama doesn't cleanly expose this yet.

Sources: Ollama FAQ + Modelfile docs; llama.cpp server README + speculative.md;
Doctor-Shotgun MoE offload guide; smcleod.net KV-quant.

## 3. Structural compression

- **REAP** (Router-weighted Expert Activation Pruning) — one-shot MoE expert
  pruning, no retrain. 25–50% experts removed with ~96–98% coding/agentic
  retention *(vendor-reported)*. Downloadable GGUF: `unsloth/GLM-4.x-REAP-*-GGUF`,
  `Qwen3-Coder-REAP`. Best structural lever for a coding agent — but still large.
- **Minitron** (depth/width pruning + distillation) — solid small dense models
  (Llama-3.1-8B→4B), but needs a distillation pass; naive layer removal degrades.
- **Distillation** — DeepSeek-R1 distills (1.5B–70B) are **reasoning-focused, not
  coder-tuned**; for code prefer the **Qwen-Coder lineage** (incl. REAP-Qwen3-Coder).

Sources: REAP (arXiv 2510.13999) + Cerebras blog; Minitron (arXiv 2408.11796);
DeepSeek-R1 distills (HF).

## 4. On-device building

- **Easy** — `ollama create --quantize q4_K_M <name>`: K-quants + legacy only (no
  IQ/UD), and only from an fp16/fp32 GGUF or safetensors input.
- **Advanced** — `convert_hf_to_gguf.py` → (opt.) `llama-imatrix` → `llama-quantize`.
  Full control (IQ + custom imatrix) but heavy: needs llama.cpp tools and RAM ≈ the
  fp16 size, so realistically ≤7–13B on a laptop.

## "What fits my machine" — the math

```
total_needed ≈ model_file_size + KV_cache + overhead(~0.5–1 GB)
KV_cache     = 2 × n_layers × n_kv_heads × head_dim × num_ctx × bytes_per_elem
               bytes_per_elem: f16=2, q8_0=1, q4_0=0.5
```
Worked: Llama-3-8B @ 8k, f16 KV ≈ 1.0 GiB (q8_0 ≈ 0.5, q4_0 ≈ 0.25); at 32k ≈ 4 GiB.
Rule of thumb: **model file ≈ 70–75% of free memory**, reserve the rest for KV +
OS; widen the reserve for long context. On Apple Silicon the usable pool is ~66–75%
of total RAM.

Sources: llama.cpp discussion #9936; KV calculators (lmcache, mbrenndoerfer).

## Kodiqa roadmap (phased)

- **Phase 1 (v3.18.0, done)** — flash attention + `q8_0` KV-cache quant by default
  on Kodiqa-spawned servers (`OllamaManager._serve_env`). Config: `flash_attention`,
  `kv_cache_type`.
- **Phase 2** — "fits my machine": detect RAM/VRAM budget, annotate model & quant
  lists with ✓ fits / ⚠ tight / ✗ too big; warn on sub-4-bit for coding.
- **Phase 3** — prefer/label imatrix + UD- quants in the HF fallback; per-model
  runtime auto-tune (`num_ctx`/offload).
- **Phase 4** — `/quantize` (ollama create), speculative-decoding auto-pairing
  (version-gated), REAP coder recommendations.

### Pitfalls
- Don't recommend sub-4-bit for coding.
- KV-quant is global + needs flash attention + only applies to Kodiqa-spawned
  servers (not a separate GUI app).
- Speculative decoding & on-device imatrix builds are version/RAM gated — guard them.
- REAP retention numbers are vendor self-benchmarks.
