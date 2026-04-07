# Evaluation

Tools for benchmarking the S2S pipeline latency, both in isolation (direct API calls) and end-to-end through LiveKit (full stack).

## Scripts

| Script | Purpose |
|--------|---------|
| `run_pipeline_eval.py` | Direct STT → LLM → TTS API calls; saves audio artifacts |
| `compare.py` | Side-by-side latency comparison: direct vs. LiveKit |

## Test data

```
testdata/
├── chunk_0000.wav  (11.36s)
├── chunk_0001.wav  (11.36s)
├── chunk_0002.wav   (3.28s)
...
└── chunk_0019.wav  (12.45s)
```

Both scripts accept **any WAV format** — float32, float64, int32, int8, and int16 PCM are all handled automatically via an in-memory `normalize_wav()` conversion using numpy. No manual pre-conversion needed.

The constraint is a Python tooling limitation, not a LiveKit server limitation:
- `wave` module only reads int16 PCM (format 1)
- `livekit.rtc.AudioFrame` requires int16 data (`sizeof(int16)` hardcoded)
- LiveKit server itself receives Opus-encoded WebRTC audio and doesn't care about WAV format

---

## run_pipeline_eval.py

Runs STT → LLM → TTS as direct HTTP calls and saves all artifacts. Does not require LiveKit or the agent to be running.

```bash
python3 eval/run_pipeline_eval.py eval/testdata/chunk_0005.wav

# Multiple files
python3 eval/run_pipeline_eval.py eval/testdata/*.wav
```

Each run writes to `eval/runs/<timestamp>/`:

```
runs/20260401-160111/
└── chunk_0005/
    ├── input.wav
    ├── output.wav
    ├── llm_response.txt
    └── result.json
```

`result.json` contains per-stage timing, RTF, and the LLM reply text.

---

## compare.py

Measures and compares latency across two modes.

### Modes

| Mode | What runs | LiveKit required? |
|------|-----------|-------------------|
| `direct` | STT + LLM + TTS via HTTP | No |
| `livekit` | Audio through LiveKit room → agent → audio back | Yes |
| `both` | Both modes, prints overhead diff | Yes |

### Usage

```bash
# Both modes (default)
python3 eval/compare.py eval/testdata/chunk_0011.wav

# Direct only
python3 eval/compare.py --mode direct eval/testdata/*.wav

# LiveKit only, 3 repeated runs
python3 eval/compare.py --mode livekit --runs 3 eval/testdata/chunk_0005.wav

# Full batch comparison
python3 eval/compare.py --mode both eval/testdata/*.wav
```

### What it measures

**Direct mode**

| Metric | Definition |
|--------|-----------|
| `stt_wall_s` | Wall time for STT API call |
| `llm_ttft_s` | Time to first visible LLM token (skips `<think>` blocks) |
| `llm_total_s` | Full LLM streaming time |
| `tts_wall_s` | Wall time for TTS API call |
| `e2e_approx_s` | `stt + llm_ttft + tts` — approximates latency from end-of-speech to first audio |

**LiveKit mode**

| Metric | Definition |
|--------|-----------|
| `room_connect_s` | WebSocket connect to LiveKit server |
| `agent_join_delay_s` | Time from connect until agent joins the room |
| `ttfa_from_end_s` | End-of-speech → first agent audio frame (can be negative if VAD triggers mid-file) |
| `ttfa_from_start_s` | Start-of-speech → first agent audio frame (always ≥ 0) |
| `agent_audio_duration_s` | Duration of agent's speech response |
| `total_wall_s` | Connect → agent audio complete |

**Overhead**

```
overhead = ttfa_from_start_s - input_duration_s - direct_e2e_s
```

This isolates the cost added by WebRTC transport, VAD endpointing, and agent coordination on top of the raw API latency.

### Sample output

```
────────────────────────────────────────────────────────────
  Audio: chunk_0011.wav
────────────────────────────────────────────────────────────
  DIRECT (no LiveKit)
    STT wall:         0.088s   (backend  0.050s)
    LLM TTFT:         0.648s   (total  0.753s)
    TTS wall:         0.350s   (output  7.509s)
    ─────────────────────────────
    E2E (approx):     1.069s   [STT + LLM_TTFT + TTS]

  LIVEKIT (full stack)
    Room connect:     0.478s
    Agent join:       0.308s
    TTFA (from end):  1.314s   [end-of-speech → first agent audio]
    TTFA (from start): 2.664s  [includes 1.23s audio duration]
    Agent audio:      4.240s
    Total wall:       9.278s

  OVERHEAD (TTFA_start − audio_dur − Direct E2E):   0.367s
```

---

## Latency analysis

### Observed baseline (local stack, Arabic audio)

| | Min | Avg | Notes |
|--|-----|-----|-------|
| Direct E2E | 0.55s | 0.70s | STT + LLM TTFT + TTS, no WebRTC |
| TTFA (from end) | 1.30s | 1.80s | Varies with VAD delay |
| LiveKit overhead | 0.20s | 0.80s | Spikes to 3s+ on ambiguous audio endings |

### Agent-side timeline (from `docker compose logs agent`)

For a 1.23s audio clip (`chunk_0011.wav`):

```
+0.00s  Job dispatch received
+0.32s  Stream attached — reading user audio
+1.55s  Audio publishing ends  (0.32 + 1.23s)
+2.02s  STT request starts     ← +0.47s VAD endpointing delay
+2.12s  STT done               (0.09s)
+2.62s  TTS starts             (0.49s LLM TTFT)
+2.85s  TTS done               (0.23s)
~2.90s  First audio frame received by client
+2.85s  AEC warmup begins      (3.0s timer)
+5.85s  AEC warmup expires
```

**TTFA from end = VAD delay (0.47s) + STT (0.09s) + LLM TTFT (0.49s) + TTS (0.23s) = 1.28s**

The STT, LLM, and TTS latencies closely match the direct mode — they are not the bottleneck.

### Root causes of LiveKit overhead

#### 1. VAD endpointing delay (dominant, 0.5–5s)

The Silero VAD must observe silence for `min_endpointing_delay` before triggering STT. For audio that ends without a clean sentence boundary, the agent waits up to `max_endpointing_delay`.

```python
# agent/config.py
min_endpointing_delay: float = Field(default=0.5)   # silence wait before STT
max_endpointing_delay: float = Field(default=5.0)   # fallback for ambiguous endings
false_interruption_timeout: float | None = Field(default=2.0)
```

This is the primary variable. Short, clean utterances: ~0.5s delay. Longer or trailing utterances: up to 3–5s.

#### 2. AEC warmup (3.0s, first response per session)

After the agent sends its first TTS response, an Acoustic Echo Cancellation warmup runs for 3 seconds. During this window, user interruptions are ignored. This does not delay TTFA for the first turn but makes rapid back-and-forth conversation unresponsive.

```
aec warmup active, disabling interruptions for 3.00s
```

#### 3. Agent join delay (0.3–1.5s, per room)

The agent is dispatched fresh per room. The fork + init takes ~0.14s, but the full join delay observed was 0.3–1.5s depending on server load.

#### 4. WebRTC transport (negligible, <50ms)

On a local stack, WebRTC encoding/decoding and network latency are below measurement noise.

---

## Mitigation strategies

### ① Enable Turn Detector — highest impact

Replaces VAD-based silence detection with a semantic turn detection model. The model predicts when the user has actually finished their turn, reducing endpointing delay from 0.5–5s to ~0.1–0.3s.

The `MultilingualModel` is already imported and conditionally loaded in `agent/agent.py`.

Enable via `.env`:

```
AGENT_USE_TURN_DETECTOR=true
```

Expected improvement: 0.3–2.5s reduction in TTFA depending on audio content.

### ② Reduce endpointing delays

Lower the minimum silence window and the fallback maximum:

```
AGENT_MIN_ENDPOINTING_DELAY=0.2
AGENT_MAX_ENDPOINTING_DELAY=2.0
AGENT_FALSE_INTERRUPTION_TIMEOUT=1.0
```

Trade-off: lower values increase the chance of the agent cutting the user off mid-sentence.

### ③ Migrate to TurnHandlingOptions API

The current config fields (`min_endpointing_delay`, etc.) are deprecated in agents v1.5+. Migrating removes the warning and unlocks future improvements:

```python
from livekit.agents.voice import TurnHandlingOptions

session = AgentSession(
    stt=streaming_stt,
    llm=llm_provider,
    tts=tts_provider,
    vad=ctx.proc.userdata["vad"],
    turn_handling=TurnHandlingOptions(
        min_endpointing_delay=0.2,
        max_endpointing_delay=2.0,
    ),
    allow_interruptions=agent_settings.allow_interruptions,
)
```

### ④ Streaming STT

The current STT adapter buffers the entire utterance before sending. A streaming STT adapter could forward audio chunks as they arrive, allowing the LLM to start generating before transcription is complete. This would reduce TTFA by `stt_wall_s` (currently ~0.09–0.17s).

### Summary of expected gains

| Mitigation | Expected TTFA reduction | Risk |
|-----------|------------------------|------|
| Turn Detector | 0.3–2.5s | Slight increase in false positives |
| Reduce min_endpointing_delay 0.5→0.2 | 0.3s | May cut off slow speakers |
| Reduce max_endpointing_delay 5.0→2.0 | 0–3s on long pauses | May cut off long pauses |
| Streaming STT | ~0.1s | Adapter rewrite required |

---

## Requirements

```bash
pip install livekit livekit-api aiohttp
```

LiveKit server and agent must be running for `--mode livekit`:

```bash
docker compose up
```
