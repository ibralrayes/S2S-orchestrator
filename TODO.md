# TODO

## Done

- [x] Validate the implementation plan against current LiveKit docs
- [x] Scaffold the new repo structure and base config
- [x] Add Docker Compose stack with LiveKit, Redis, agent, and token server
- [x] Implement token generation service
- [x] Implement agent worker skeleton with VAD prewarm and session hooks
- [x] Add first-pass external STT, LLM, and TTS adapter modules
- [x] Make LiveKit room I/O options explicit in the agent
- [x] Surface interruption and turn-handling controls in config
- [x] Fix LiveKit demo (STT/TTS/LLM URLs, Nusuk auth, wrapper TTS provider)
- [x] Build push-to-talk demo (PTT page, ASR/chat/TTS proxies, Nusuk JWT server-side)
- [x] Remove VAD per-call toggle (turn detection always on)
- [x] Clean up and comment all Python agent code
- [x] Strip markdown from TTS input (prevents **bold**, [4] refs being spoken)
- [x] Tune system prompt for proper Arabic punctuation (enables LiveKit sentence buffering)
- [x] Add eval comparison script (direct vs LiveKit, Nusuk + wrapper TTS)
- [x] Document production deployment in README (CPU/GPU split, public IP gotcha, VAD on CPU)

## Pending

### Latency
- [ ] Run full 20-file eval benchmark (direct vs LiveKit) and record results in EXPERIMENT_LOG.md
- [ ] Confirm Nusuk sentence boundaries trigger LiveKit sentence buffering correctly
- [ ] Measure TTFA improvement after system prompt + markdown strip fix

### Quality
- [ ] Confirm the exact ASR endpoint contract and audio format requirements
- [ ] Confirm the exact TTS endpoint contract and streaming capabilities
- [ ] Confirm Nusuk honors the system prompt (currently returns 150+ word responses despite 50-word limit)

### Production
- [ ] Add external transcript/session persistence only if product requirements need it
- [ ] Add integration tests and structured observability
- [ ] Test CPU/GPU split deployment on real machines
- [ ] Configure LiveKit `use_external_ip: true` in livekit.yaml before cloud deploy
