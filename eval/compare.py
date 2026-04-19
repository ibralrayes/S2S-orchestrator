#!/usr/bin/env python3
"""
Compare S2S pipeline latency:
  direct  – STT → LLM → TTS via direct HTTP API calls (no LiveKit overhead)
  livekit – audio through LiveKit room → agent → audio back (full stack)

Usage:
    python3 eval/compare.py test.wav
    python3 eval/compare.py --mode direct test.wav
    python3 eval/compare.py --mode livekit test.wav
    python3 eval/compare.py --mode both --runs 3 test.wav
    python3 eval/compare.py --mode livekit --livekit-turn-mode explicit_eos test.wav

Requirements:
    pip install livekit livekit-api aiohttp
    LiveKit server + agent must be running for --mode livekit
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import numpy as np
import aiohttp
from livekit import rtc
from livekit.api import AccessToken, VideoGrants


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
RUNS_DIR = ROOT / "eval" / "runs"
DEFAULT_SYSTEM_PROMPT = "أجب بالعربية في أقل من 40 كلمة، وحاول الإجابة مباشرة عن سؤال المستخدم."

# LiveKit mode constants
PUBLISH_FRAME_MS = 20        # ms per published audio frame
AGENT_JOIN_TIMEOUT_S = 15.0  # max wait for agent to join
RESPONSE_TIMEOUT_S = 30.0    # max wait for agent first audio after speech ends
AGENT_SILENCE_END_S = 1.5    # silence gap that marks end of agent response
EXPLICIT_EOS_PAYLOAD = "__EOS__"


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def host_accessible_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.hostname != "host.docker.internal":
        return url
    netloc = parts.netloc.replace("host.docker.internal", "localhost")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def normalize_wav(path: Path) -> tuple[bytes, int, int]:
    """Return (int16_pcm_bytes, sample_rate, channels) for any WAV format.

    Python's wave module and livekit.rtc.AudioFrame both require int16 PCM.
    This handles float32 (format 3), float64, int32, int24, and uint8 inputs
    by converting them to int16 in-memory via numpy — no disk write needed.
    """
    import struct

    with open(path, "rb") as f:
        # Parse RIFF header to detect format code without wave.open()
        f.seek(20)
        fmt_code = struct.unpack_from("<H", f.read(2))[0]
        channels = struct.unpack_from("<H", f.read(2))[0]
        sample_rate = struct.unpack_from("<I", f.read(4))[0]
        f.seek(34)
        bits_per_sample = struct.unpack_from("<H", f.read(2))[0]

    if fmt_code == 1 and bits_per_sample == 16:
        # Already int16 PCM — read normally
        with wave.open(str(path), "rb") as wf:
            raw = wf.readframes(wf.getnframes())
        return raw, sample_rate, channels

    # Need conversion — read data chunk directly
    raw_bytes = path.read_bytes()
    # Find 'data' chunk
    offset = 12
    while offset < len(raw_bytes) - 8:
        chunk_id = raw_bytes[offset:offset + 4]
        chunk_size = struct.unpack_from("<I", raw_bytes, offset + 4)[0]
        if chunk_id == b"data":
            audio_bytes = raw_bytes[offset + 8: offset + 8 + chunk_size]
            break
        offset += 8 + chunk_size
    else:
        raise ValueError(f"No data chunk found in {path.name}")

    if fmt_code == 3:  # IEEE float
        dtype = np.float32 if bits_per_sample == 32 else np.float64
        samples = np.frombuffer(audio_bytes, dtype=dtype)
        int16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    elif fmt_code == 1 and bits_per_sample == 32:
        samples = np.frombuffer(audio_bytes, dtype=np.int32)
        int16 = (samples >> 16).astype(np.int16)
    elif fmt_code == 1 and bits_per_sample == 8:
        samples = np.frombuffer(audio_bytes, dtype=np.uint8).astype(np.int16)
        int16 = ((samples - 128) * 256).astype(np.int16)
    else:
        raise ValueError(f"Unsupported WAV format: code={fmt_code}, bits={bits_per_sample}")

    return int16.tobytes(), sample_rate, channels


def pcm_to_wav_bytes(pcm: bytes, sample_rate: int, channels: int) -> bytes:
    """Wrap raw int16 PCM bytes in a WAV container (in-memory)."""
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # int16 = 2 bytes
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def audio_meta(path: Path) -> dict:
    raw, sr, ch = normalize_wav(path)
    fr = len(raw) // (ch * 2)  # int16 = 2 bytes per sample
    return {
        "filename": path.name,
        "sample_rate": sr,
        "channels": ch,
        "duration_s": round(fr / sr, 3) if sr else 0.0,
    }


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# <think> block filter (Qwen / reasoning models emit these)
# ---------------------------------------------------------------------------

class VisibleTextFilter:
    def __init__(self) -> None:
        self._in_think = False
        self._buffer = ""
        self._parts: list[str] = []
        self._first_at: float | None = None

    def push(self, text: str, now_s: float) -> str:
        self._buffer += text
        out: list[str] = []
        while self._buffer:
            if self._in_think:
                end = self._buffer.find("</think>")
                if end == -1:
                    keep = max(0, len(self._buffer) - len("</think>") + 1)
                    self._buffer = self._buffer[keep:]
                    break
                self._buffer = self._buffer[end + len("</think>"):]
                self._in_think = False
                continue
            start = self._buffer.find("<think>")
            if start == -1:
                safe = max(0, len(self._buffer) - len("<think>") + 1)
                if safe:
                    out.append(self._buffer[:safe])
                    self._buffer = self._buffer[safe:]
                break
            if start:
                out.append(self._buffer[:start])
            self._buffer = self._buffer[start + len("<think>"):]
            self._in_think = True
        visible = "".join(out)
        if visible and self._first_at is None:
            self._first_at = now_s
        if visible:
            self._parts.append(visible)
        return visible

    def finish(self, now_s: float) -> tuple[str, float | None]:
        tail = ""
        if not self._in_think and self._buffer:
            tail = self._buffer.replace("<think>", "")
            if tail:
                self._parts.append(tail)
                if self._first_at is None:
                    self._first_at = now_s
        self._buffer = ""
        return "".join(self._parts).strip(), self._first_at


# ---------------------------------------------------------------------------
# Direct mode
# ---------------------------------------------------------------------------

async def direct_stt(
    session: aiohttp.ClientSession,
    env: dict[str, str],
    audio_path: Path,
    duration_s: float,
) -> dict:
    pcm, sr, ch = normalize_wav(audio_path)
    wav_bytes = pcm_to_wav_bytes(pcm, sr, ch)
    start = time.perf_counter()
    form = aiohttp.FormData()
    form.add_field(
        "file",
        wav_bytes,
        filename=audio_path.stem + ".wav",
        content_type="audio/wav",
    )
    async with session.post(
        host_accessible_url(env["CUSTOM_STT_URL"]).rstrip("/") + "/api/transcribe/",
        data=form,
        headers={"Authorization": f"Bearer {env['CUSTOM_STT_ACCESS_TOKEN']}"},
    ) as resp:
        resp.raise_for_status()
        payload = await resp.json()
    wall_s = time.perf_counter() - start
    backend_s = float(payload.get("processing_time_seconds") or 0.0)
    return {
        "transcript": payload.get("transcription_text", "").strip(),
        "wall_s": round(wall_s, 3),
        "backend_s": round(backend_s, 4),
        "rtf": round(wall_s / duration_s, 4) if duration_s else None,
    }


async def _nusuk_token(session: aiohttp.ClientSession, env: dict[str, str]) -> str:
    """Fetch a one-shot Nusuk bearer token using client credentials."""
    async with session.post(
        env["CUSTOM_LLM_URL"].rstrip("/") + "/auth/token",
        json={"client_id": env["CUSTOM_LLM_CLIENT_ID"], "client_secret": env["CUSTOM_LLM_CLIENT_SECRET"]},
    ) as resp:
        resp.raise_for_status()
        return (await resp.json())["access_token"]


async def direct_llm(
    session: aiohttp.ClientSession,
    env: dict[str, str],
    transcript: str,
    *,
    nusuk_token: str | None = None,
) -> dict:
    provider = env.get("CUSTOM_LLM_PROVIDER", "openai").strip().lower()
    start = time.perf_counter()
    first_chunk_at: float | None = None
    filt = VisibleTextFilter()

    if provider == "nusuk":
        # Nusuk SSE: POST /chat/stream, lines are  data: {"delta": "..."}
        payload = {
            "query": transcript,
            "session_id": "eval-direct",
            "language": env.get("CUSTOM_LLM_LANGUAGE", "ar"),
            "include_metadata": env.get("CUSTOM_LLM_INCLUDE_METADATA", "true").lower() == "true",
            "tool": env.get("CUSTOM_LLM_TOOL", "Knowledge"),
        }
        url = env["CUSTOM_LLM_URL"].rstrip("/") + "/chat/stream"
        headers = {"Authorization": f"Bearer {nusuk_token}"} if nusuk_token else {}
        async with session.post(url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            async for raw in resp.content:
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = event.get("delta")
                if not isinstance(delta, str) or not delta:
                    continue
                now = time.perf_counter() - start
                if first_chunk_at is None:
                    first_chunk_at = now
                filt.push(delta, now)
    else:
        # OpenAI-compatible SSE: POST /chat/completions
        system_prompt = env.get("AGENT_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)
        payload = {
            "model": env["CUSTOM_LLM_MODEL"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": transcript},
            ],
            "stream": True,
            "reasoning_effort": "none",
            "temperature": float(env.get("CUSTOM_LLM_TEMPERATURE", "0.2")),
            "max_tokens": int(env.get("CUSTOM_LLM_MAX_TOKENS", "96")),
        }
        access_token = env.get("GROQ") or env.get("CUSTOM_LLM_ACCESS_TOKEN", "")
        async with session.post(
            env["CUSTOM_LLM_URL"].rstrip("/") + "/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        ) as resp:
            resp.raise_for_status()
            async for raw in resp.content:
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = event.get("choices", [{}])[0].get("delta", {}).get("content")
                if delta is None:
                    continue
                now = time.perf_counter() - start
                if first_chunk_at is None:
                    first_chunk_at = now
                filt.push(delta, now)

    total_s = time.perf_counter() - start
    text, first_visible_at = filt.finish(total_s)
    return {
        "reply": text,
        "ttft_s": round(first_chunk_at, 3) if first_chunk_at is not None else None,
        "visible_ttft_s": round(first_visible_at, 3) if first_visible_at is not None else None,
        "total_s": round(total_s, 3),
        "words": len(text.split()),
    }


def _strip_markdown(text: str) -> str:
    import re
    text = re.sub(r'\*+([^*\n]+)\*+', r'\1', text)
    text = re.sub(r'^\s*>+\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\[\d+\]', '', text)
    text = re.sub(r'\n{2,}', ' ', text)
    return text.strip()


async def direct_tts(
    session: aiohttp.ClientSession,
    env: dict[str, str],
    text: str,
    output_path: Path,
) -> dict:
    text = _strip_markdown(text)
    provider = env.get("CUSTOM_TTS_PROVIDER", "local_api").strip().lower()
    tts_base = host_accessible_url(env["CUSTOM_TTS_URL"]).rstrip("/")
    access_token = env.get("CUSTOM_TTS_ACCESS_TOKEN", "")

    if provider == "wrapper":
        url = tts_base  # POST to root, no path suffix
        body = {"text": text}
        headers: dict[str, str] = {}
    else:  # local_api or generic
        url = tts_base + "/api/synthesize/"
        body = {
            "text": text,
            "output_format": env.get("CUSTOM_TTS_AUDIO_FORMAT", "wav"),
            "sample_rate": int(env.get("CUSTOM_TTS_SAMPLE_RATE", "24000")),
        }
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}

    start = time.perf_counter()
    async with session.post(url, json=body, headers=headers) as resp:
        hdr_processing = float(resp.headers.get("x-processing-time") or 0.0)
        audio_bytes = await resp.read()
        if resp.status >= 400:
            raise RuntimeError(f"TTS {resp.status}: {audio_bytes.decode('utf-8', errors='ignore')[:200]}")
    wall_s = time.perf_counter() - start
    output_path.write_bytes(audio_bytes)

    output_duration_s = 0.0
    try:
        with wave.open(str(output_path), "rb") as f:
            output_duration_s = f.getnframes() / f.getframerate()
    except Exception:
        pass

    return {
        "wall_s": round(wall_s, 3),
        "backend_s": round(hdr_processing, 4),
        "output_duration_s": round(output_duration_s, 3),
        "rtf": round(wall_s / output_duration_s, 4) if output_duration_s else None,
    }


async def run_direct(
    env: dict[str, str],
    audio_path: Path,
    run_dir: Path,
) -> dict:
    """STT → LLM → TTS via direct HTTP calls (same services as the agent)."""
    meta = audio_meta(audio_path)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as sess:
        # Pre-fetch Nusuk token outside the timed window so auth overhead isn't counted.
        nusuk_token: str | None = None
        if env.get("CUSTOM_LLM_PROVIDER", "").strip().lower() == "nusuk":
            nusuk_token = await _nusuk_token(sess, env)

        t0 = time.perf_counter()
        stt = await direct_stt(sess, env, audio_path, meta["duration_s"])
        llm = await direct_llm(sess, env, stt["transcript"], nusuk_token=nusuk_token)
        if not llm["reply"]:
            raise RuntimeError("LLM returned empty reply")
        tts = await direct_tts(sess, env, llm["reply"], run_dir / "direct_output.wav")
        total_s = time.perf_counter() - t0

    # E2E ≈ STT wall + LLM TTFT + TTS wall  (time from end-of-speech to first audio)
    e2e_s = stt["wall_s"] + (llm["ttft_s"] or llm["total_s"]) + tts["wall_s"]

    result = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "direct",
        "input": meta,
        "stt": stt,
        "llm": llm,
        "tts": tts,
        "pipeline": {
            "total_s": round(total_s, 3),
            "e2e_approx_s": round(e2e_s, 3),
        },
    }
    write_json(run_dir / "direct_result.json", result)
    return result


# ---------------------------------------------------------------------------
# LiveKit mode
# ---------------------------------------------------------------------------

def _make_token(env: dict[str, str], room: str, identity: str) -> str:
    from livekit.protocol.room import RoomConfiguration

    cfg = RoomConfiguration()
    dispatch = cfg.agents.add()
    dispatch.agent_name = env.get("AGENT_NAME", "nusuk-agent")

    return (
        AccessToken(env["LIVEKIT_API_KEY"], env["LIVEKIT_API_SECRET"])
        .with_identity(identity)
        .with_name("Eval Participant")
        .with_grants(VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True))
        .with_room_config(cfg)
        .to_jwt()
    )


async def _stream_wav(source: rtc.AudioSource, wav_path: Path) -> tuple[float, float]:
    """Publish WAV audio into source at real-time rate.
    Returns (t_start, t_end) perf_counter timestamps.
    Accepts any WAV format — normalizes to int16 PCM automatically.
    """
    raw, sr, ch = normalize_wav(wav_path)

    samples_per_frame = sr * PUBLISH_FRAME_MS // 1000
    frame_bytes = samples_per_frame * ch * 2  # int16

    t_start = time.perf_counter()
    offset = 0
    while offset < len(raw):
        chunk = raw[offset : offset + frame_bytes]
        if len(chunk) < frame_bytes:
            chunk = chunk + b"\x00" * (frame_bytes - len(chunk))
        await source.capture_frame(
            rtc.AudioFrame(
                data=chunk,
                sample_rate=sr,
                num_channels=ch,
                samples_per_channel=samples_per_frame,
            )
        )
        offset += frame_bytes
        await asyncio.sleep(PUBLISH_FRAME_MS / 1000)
    t_end = time.perf_counter()
    return t_start, t_end


async def run_livekit(
    env: dict[str, str],
    audio_path: Path,
    run_dir: Path,
    *,
    turn_mode: str,
) -> dict:
    """Publish audio into a LiveKit room and measure agent response timing."""
    lk_url = env.get("LIVEKIT_PUBLIC_URL", env.get("LIVEKIT_URL", "ws://localhost:7880"))
    room_name = f"eval-{uuid.uuid4().hex[:8]}"
    token = _make_token(env, room_name, "eval-user")
    meta = audio_meta(audio_path)

    room = rtc.Room()

    # Mutable timing state (captured via nonlocal in closures)
    t_agent_joined: list[float] = []  # list so nonlocal isn't needed
    t_first_audio: list[float] = []   # first non-silent frame
    t_last_audio: list[float] = []    # last non-silent frame
    speech_frames_rx: list[int] = [0]
    agent_done = asyncio.Event()
    silence_handle: list[asyncio.TimerHandle | None] = [None]

    # Amplitude threshold for int16: ~0.3% of full scale (avoids PCM-zero padding)
    SPEECH_AMPLITUDE_THRESHOLD = 200  # int16 units

    async def _drain_audio(track: rtc.RemoteAudioTrack) -> None:
        import struct
        loop = asyncio.get_running_loop()
        stream = rtc.AudioStream(track, sample_rate=meta["sample_rate"], num_channels=meta["channels"])
        async for frame_event in stream:
            raw = bytes(frame_event.frame.data)
            if not raw:
                continue
            # Check max amplitude to distinguish speech from silence
            samples = struct.unpack_from(f"{len(raw) // 2}h", raw)
            max_amp = max(abs(s) for s in samples) if samples else 0
            if max_amp < SPEECH_AMPLITUDE_THRESHOLD:
                continue  # silence frame — don't reset timer
            now = time.perf_counter()
            if not t_first_audio:
                t_first_audio.append(now)
            if t_last_audio:
                t_last_audio[0] = now
            else:
                t_last_audio.append(now)
            speech_frames_rx[0] += 1
            if silence_handle[0]:
                silence_handle[0].cancel()
            silence_handle[0] = loop.call_later(AGENT_SILENCE_END_S, agent_done.set)

    @room.on("participant_connected")
    def _on_participant(participant: rtc.RemoteParticipant) -> None:
        if participant.kind == 4 and not t_agent_joined:  # 4 = AGENT
            t_agent_joined.append(time.perf_counter())

    @room.on("track_subscribed")
    def _on_track(track, _publication, participant: rtc.RemoteParticipant) -> None:
        if participant.kind == 4 and track.kind == rtc.TrackKind.KIND_AUDIO:
            asyncio.ensure_future(_drain_audio(track))

    t_wall_start = time.perf_counter()
    await room.connect(lk_url, token)
    t_connected = time.perf_counter()

    # Agent may have already joined before our connect callbacks fired
    for p in room.remote_participants.values():
        if p.kind == 4 and not t_agent_joined:
            t_agent_joined.append(t_connected)

    # Wait for agent
    deadline = time.perf_counter() + AGENT_JOIN_TIMEOUT_S
    while not t_agent_joined:
        if time.perf_counter() > deadline:
            raise TimeoutError("Agent did not join within timeout")
        await asyncio.sleep(0.05)

    # Set up audio source and publish
    source = rtc.AudioSource(sample_rate=meta["sample_rate"], num_channels=meta["channels"])
    local_track = rtc.LocalAudioTrack.create_audio_track("microphone", source)
    await room.local_participant.publish_track(
        local_track,
        rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
    )

    t_speech_start, t_speech_end = await _stream_wav(source, audio_path)
    if turn_mode == "explicit_eos":
        await room.local_participant.publish_data(
            EXPLICIT_EOS_PAYLOAD,
            topic=env.get("AGENT_EXPLICIT_EOS_TOPIC", "eval.eos"),
        )

    # Wait for agent audio response
    try:
        await asyncio.wait_for(agent_done.wait(), timeout=RESPONSE_TIMEOUT_S)
    except asyncio.TimeoutError:
        pass

    await room.disconnect()
    t_wall_end = time.perf_counter()

    # Derived metrics
    room_connect_s = t_connected - t_wall_start
    agent_join_delay_s = (t_agent_joined[0] - t_connected) if t_agent_joined else None
    # TTFA from end of speech (can be negative: agent responded before we finished publishing)
    ttfa_from_end_s = (t_first_audio[0] - t_speech_end) if t_first_audio else None
    # TTFA from start of speech (always >= 0, includes audio duration)
    ttfa_from_start_s = (t_first_audio[0] - t_speech_start) if t_first_audio else None
    agent_duration_s = (t_last_audio[0] - t_first_audio[0]) if (t_first_audio and t_last_audio) else None
    total_wall_s = t_wall_end - t_wall_start

    result = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "livekit",
        "turn_mode": turn_mode,
        "room": room_name,
        "livekit_url": lk_url,
        "input": meta,
        "timing": {
            "room_connect_s": round(room_connect_s, 3),
            "agent_join_delay_s": round(agent_join_delay_s, 3) if agent_join_delay_s is not None else None,
            "ttfa_from_end_s": round(ttfa_from_end_s, 3) if ttfa_from_end_s is not None else None,
            "ttfa_from_start_s": round(ttfa_from_start_s, 3) if ttfa_from_start_s is not None else None,
            "agent_audio_duration_s": round(agent_duration_s, 3) if agent_duration_s is not None else None,
            "total_wall_s": round(total_wall_s, 3),
            "speech_start_offset_s": round(t_speech_start - t_wall_start, 3),
            "speech_end_offset_s": round(t_speech_end - t_wall_start, 3),
        },
        "speech_frames_received": speech_frames_rx[0],
    }
    write_json(run_dir / "livekit_result.json", result)
    return result


# ---------------------------------------------------------------------------
# Comparison output
# ---------------------------------------------------------------------------

def _fmt(v: float | None, unit: str = "s") -> str:
    if v is None:
        return "  n/a  "
    return f"{v:6.3f}{unit}"


def print_comparison(direct: dict | None, livekit: dict | None, audio_name: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  Audio: {audio_name}")
    print(f"{'─' * 60}")

    if direct:
        p = direct["pipeline"]
        s, l, t = direct["stt"], direct["llm"], direct["tts"]
        print("  DIRECT (no LiveKit)")
        print(f"    STT wall:        {_fmt(s['wall_s'])}   (backend {_fmt(s['backend_s'])})")
        print(f"    LLM TTFT:        {_fmt(l.get('visible_ttft_s') or l.get('ttft_s'))}   (total {_fmt(l['total_s'])})")
        print(f"    TTS wall:        {_fmt(t['wall_s'])}   (output {_fmt(t['output_duration_s'])})")
        print(f"    ─────────────────────────────")
        print(f"    E2E (approx):    {_fmt(p['e2e_approx_s'])}   [STT + LLM_TTFT + TTS]")
        print(f"    Total pipeline:  {_fmt(p['total_s'])}")
        if l.get("reply"):
            print(f'    Reply: "{l["reply"][:80]}"')

    if livekit:
        t = livekit["timing"]
        input_dur = livekit["input"]["duration_s"]
        print()
        print("  LIVEKIT (full stack)")
        print(f"    Room connect:    {_fmt(t['room_connect_s'])}")
        print(f"    Agent join:      {_fmt(t['agent_join_delay_s'])}")
        print(f"    TTFA (from end): {_fmt(t['ttfa_from_end_s'])}   [end-of-speech → first agent audio]")
        print(f"    TTFA (from start):{_fmt(t['ttfa_from_start_s'])}  [includes {input_dur:.2f}s audio duration]")
        print(f"    Agent audio:     {_fmt(t['agent_audio_duration_s'])}")
        print(f"    Total wall:      {_fmt(t['total_wall_s'])}")
        print(f"    Speech frames:   {livekit['speech_frames_received']}")

    if direct and livekit:
        t = livekit["timing"]
        input_dur = livekit["input"]["duration_s"]
        if t.get("ttfa_from_start_s") is not None:
            overhead = t["ttfa_from_start_s"] - input_dur - direct["pipeline"]["e2e_approx_s"]
            print()
            print(f"  OVERHEAD (TTFA_start − audio_dur − Direct E2E):  {_fmt(overhead)}")
            print(f"  (VAD detection delay + WebRTC round-trip)")

    print(f"{'─' * 60}")


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def _avg(vals: list[float]) -> float | None:
    clean = [v for v in vals if v is not None]
    return round(sum(clean) / len(clean), 3) if clean else None


def print_summary(summary: list[dict], mode: str) -> None:
    ok = [e for e in summary if "errors" not in e or not e["errors"]]
    errors = [e for e in summary if "errors" in e and e["errors"]]

    print(f"\n{'═' * 70}")
    print(f"  SUMMARY  ({len(ok)} ok / {len(errors)} errors / {len(summary)} total)")
    print(f"{'═' * 70}")

    if errors:
        print(f"  Errors:")
        for e in errors:
            print(f"    {e['audio']:30s}  {e['errors']}")
        print()

    if not ok:
        return

    if mode in ("direct", "both"):
        e2e_vals   = [e["direct"]["e2e_approx_s"] for e in ok if "direct" in e]
        total_vals = [e["direct"]["total_s"]       for e in ok if "direct" in e]

        print("  DIRECT pipeline")
        col = f"{'File':<28}  {'E2E':>7}  {'Total':>7}"
        print(f"  {col}")
        print(f"  {'─' * 46}")
        for e in ok:
            if "direct" not in e:
                continue
            d = e["direct"]
            print(f"  {e['audio']:<28}  {_fmt(d['e2e_approx_s'])}  {_fmt(d['total_s'])}")
        print(f"  {'─' * 46}")
        print(f"  {'avg':<28}  {_fmt(_avg(e2e_vals))}  {_fmt(_avg(total_vals))}")
        if e2e_vals:
            print(f"  min={_fmt(min(e2e_vals))}  max={_fmt(max(e2e_vals))}")

    if mode in ("livekit", "both"):
        ttfa_end_vals   = [e["livekit"]["ttfa_from_end_s"]   for e in ok if "livekit" in e and e["livekit"].get("ttfa_from_end_s") is not None]
        ttfa_start_vals = [e["livekit"]["ttfa_from_start_s"] for e in ok if "livekit" in e and e["livekit"].get("ttfa_from_start_s") is not None]
        total_vals      = [e["livekit"]["total_wall_s"]       for e in ok if "livekit" in e]
        print()
        print("  LIVEKIT pipeline")
        col = f"{'File':<28}  {'TTFA(end)':>10}  {'TTFA(start)':>12}  {'Total':>7}"
        print(f"  {col}")
        print(f"  {'─' * 62}")
        for e in ok:
            if "livekit" not in e:
                continue
            lk = e["livekit"]
            print(f"  {e['audio']:<28}  {_fmt(lk.get('ttfa_from_end_s')):>10}  {_fmt(lk.get('ttfa_from_start_s')):>12}  {_fmt(lk['total_wall_s'])}")
        print(f"  {'─' * 62}")
        print(f"  {'avg':<28}  {_fmt(_avg(ttfa_end_vals)):>10}  {_fmt(_avg(ttfa_start_vals)):>12}  {_fmt(_avg(total_vals))}")
        if ttfa_end_vals:
            print(f"  TTFA(end):   min={_fmt(min(ttfa_end_vals))}  max={_fmt(max(ttfa_end_vals))}")
        if ttfa_start_vals:
            print(f"  TTFA(start): min={_fmt(min(ttfa_start_vals))}  max={_fmt(max(ttfa_start_vals))}")

    if mode == "both":
        overhead_vals = [
            e["livekit"]["ttfa_from_start_s"] - e["livekit"]["input_duration_s"] - e["direct"]["e2e_approx_s"]
            for e in ok
            if "livekit" in e and "direct" in e and e["livekit"].get("ttfa_from_start_s") is not None
        ]
        if overhead_vals:
            print()
            print(f"  LiveKit overhead (TTFA_start − audio_dur − Direct E2E):")
            print(f"    avg={_fmt(_avg(overhead_vals))}  min={_fmt(min(overhead_vals))}  max={_fmt(max(overhead_vals))}")

    print(f"{'═' * 70}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("audio_files", nargs="+", help="WAV input files")
    p.add_argument(
        "--mode",
        choices=["direct", "livekit", "both"],
        default="both",
        help="Which pipeline(s) to benchmark (default: both)",
    )
    p.add_argument(
        "--runs",
        type=int,
        default=1,
        metavar="N",
        help="Number of runs per audio file (default: 1)",
    )
    p.add_argument(
        "--livekit-turn-mode",
        choices=["vad", "explicit_eos"],
        default="vad",
        help="Turn finalization mode for LiveKit benchmarks (default: vad)",
    )
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    env = read_env(ENV_PATH)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    batch_dir = RUNS_DIR / f"compare-{timestamp}"
    batch_dir.mkdir(parents=True, exist_ok=True)

    summary: list[dict] = []

    for audio in args.audio_files:
        audio_path = Path(audio).expanduser().resolve()
        if not audio_path.exists():
            print(f"ERROR: file not found: {audio_path}")
            continue

        for run_idx in range(args.runs):
            run_label = f"{audio_path.stem}_run{run_idx + 1}" if args.runs > 1 else audio_path.stem
            run_dir = batch_dir / run_label
            run_dir.mkdir(parents=True, exist_ok=True)

            direct_result: dict | None = None
            livekit_result: dict | None = None
            errors: dict[str, str] = {}

            if args.mode in ("direct", "both"):
                try:
                    direct_result = await run_direct(env, audio_path, run_dir)
                except Exception as exc:
                    errors["direct"] = str(exc)
                    print(f"[direct] ERROR: {exc}")

            if args.mode in ("livekit", "both"):
                try:
                    livekit_result = await run_livekit(
                        env,
                        audio_path,
                        run_dir,
                        turn_mode=args.livekit_turn_mode,
                    )
                except Exception as exc:
                    errors["livekit"] = str(exc)
                    print(f"[livekit] ERROR: {exc}")

            print_comparison(direct_result, livekit_result, audio_path.name)

            entry: dict = {
                "audio": audio_path.name,
                "run": run_idx + 1,
                "run_dir": str(run_dir),
            }
            if direct_result:
                entry["direct"] = {
                    "e2e_approx_s": direct_result["pipeline"]["e2e_approx_s"],
                    "total_s": direct_result["pipeline"]["total_s"],
                    "stt_wall_s": direct_result["stt"]["wall_s"],
                    "llm_ttft_s": direct_result["llm"].get("visible_ttft_s") or direct_result["llm"].get("ttft_s"),
                    "tts_wall_s": direct_result["tts"]["wall_s"],
                }
            if livekit_result:
                entry["livekit"] = {
                    "turn_mode": livekit_result.get("turn_mode", args.livekit_turn_mode),
                    "ttfa_from_end_s": livekit_result["timing"]["ttfa_from_end_s"],
                    "ttfa_from_start_s": livekit_result["timing"]["ttfa_from_start_s"],
                    "total_wall_s": livekit_result["timing"]["total_wall_s"],
                    "input_duration_s": livekit_result["input"]["duration_s"],
                }
            if errors:
                entry["errors"] = errors
            summary.append(entry)

    write_json(batch_dir / "summary.json", {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "batch_dir": str(batch_dir),
        "mode": args.mode,
        "runs_per_file": args.runs,
        "results": summary,
    })

    print_summary(summary, args.mode)
    print(f"\n  Results saved to: {batch_dir}\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted")
        raise SystemExit(130)
