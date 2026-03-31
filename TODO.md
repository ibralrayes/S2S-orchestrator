# TODO

- [x] Validate the implementation plan against current LiveKit docs
- [x] Scaffold the new repo structure and base config
- [x] Add Docker Compose stack with LiveKit, Redis, agent, and token server
- [x] Implement token generation service
- [x] Implement agent worker skeleton with VAD prewarm and session hooks
- [x] Add first-pass external STT, LLM, and TTS adapter modules
- [x] Make LiveKit room I/O options explicit in the agent
- [x] Surface interruption and turn-handling controls in config
- [ ] Confirm the exact ASR endpoint contract and audio format requirements
- [ ] Confirm the exact TTS endpoint contract and streaming capabilities
- [ ] Confirm the exact LLM streaming format and tool-calling support
- [ ] Exercise the stack locally with real credentials and endpoints
- [ ] Add external transcript/session persistence only if product requirements need it
- [ ] Add integration tests and structured observability
