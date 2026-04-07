from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import numpy as np
import aiohttp


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
RUNS_DIR = ROOT / "eval" / "runs"
DEFAULT_SYSTEM_PROMPT = "أجب بالعربية في أقل من 40 كلمة، وحاول الإجابة مباشرة عن سؤال المستخدم."


def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")
    return slug or "audio"


def normalize_wav(path: Path) -> tuple[bytes, int, int]:
    """Return (int16_pcm_bytes, sample_rate, channels) for any WAV format.

    Handles float32 (format 3), float64, int32, and uint8 inputs by
    converting to int16 in-memory via numpy — no disk write needed.
    """
    import struct

    with open(path, "rb") as f:
        f.seek(20)
        fmt_code = struct.unpack_from("<H", f.read(2))[0]
        channels = struct.unpack_from("<H", f.read(2))[0]
        sample_rate = struct.unpack_from("<I", f.read(4))[0]
        f.seek(34)
        bits_per_sample = struct.unpack_from("<H", f.read(2))[0]

    if fmt_code == 1 and bits_per_sample == 16:
        with wave.open(str(path), "rb") as wf:
            raw = wf.readframes(wf.getnframes())
        return raw, sample_rate, channels

    raw_bytes = path.read_bytes()
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

    if fmt_code == 3:
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
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def audio_metadata(path: Path) -> dict[str, object]:
    pcm, sample_rate, channels = normalize_wav(path)
    frames = len(pcm) // (channels * 2)
    duration_s = frames / sample_rate if sample_rate else 0.0
    return {
        "path": str(path),
        "filename": path.name,
        "bytes": path.stat().st_size,
        "sample_rate": sample_rate,
        "channels": channels,
        "duration_s": round(duration_s, 3),
    }


def output_audio_metadata(path: Path) -> dict[str, object]:
    if not path.exists():
        return {
            "path": str(path),
            "bytes": 0,
            "sample_rate": 0,
            "channels": 0,
            "duration_s": 0.0,
        }
    return audio_metadata(path)


def write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def host_accessible_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.hostname != "host.docker.internal":
        return url
    netloc = parts.netloc.replace("host.docker.internal", "localhost")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class VisibleTextFilter:
    def __init__(self) -> None:
        self._in_think = False
        self._buffer = ""
        self._visible_parts: list[str] = []
        self._first_visible_at: float | None = None

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
                self._buffer = self._buffer[end + len("</think>") :]
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
            self._buffer = self._buffer[start + len("<think>") :]
            self._in_think = True

        visible = "".join(out)
        if visible and self._first_visible_at is None:
            self._first_visible_at = now_s
        if visible:
            self._visible_parts.append(visible)
        return visible

    def finish(self, now_s: float) -> tuple[str, float | None]:
        tail = ""
        if not self._in_think and self._buffer:
            tail = self._buffer.replace("<think>", "")
            if tail:
                self._visible_parts.append(tail)
                if self._first_visible_at is None:
                    self._first_visible_at = now_s
        self._buffer = ""
        return "".join(self._visible_parts).strip(), self._first_visible_at


async def run_llm(
    session: aiohttp.ClientSession,
    env: dict[str, str],
    transcript: str,
) -> dict[str, object]:
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

    start = time.perf_counter()
    first_chunk_at: float | None = None
    chunk_count = 0
    char_count = 0
    filter_ = VisibleTextFilter()

    async with session.post(
        env["CUSTOM_LLM_URL"].rstrip("/") + "/chat/completions",
        json=payload,
        headers={
            "Authorization": f"Bearer {env['GROQ']}",
            "Content-Type": "application/json",
        },
    ) as response:
        response.raise_for_status()
        async for raw in response.content:
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
            chunk_count += 1
            char_count += len(delta)
            if first_chunk_at is None:
                first_chunk_at = now
            filter_.push(delta, now)

    end = time.perf_counter()
    visible_text, first_visible_at = filter_.finish(end - start)
    total_s = end - start
    visible_chars = len(visible_text)
    visible_words = len(visible_text.split())

    return {
        "status": 200,
        "system_prompt": system_prompt,
        "reply_text": visible_text,
        "api_ttft_s": round(first_chunk_at, 3) if first_chunk_at is not None else None,
        "visible_ttft_s": round(first_visible_at, 3) if first_visible_at is not None else None,
        "total_time_s": round(total_s, 3),
        "chunk_count": chunk_count,
        "raw_char_count": char_count,
        "visible_char_count": visible_chars,
        "visible_word_count": visible_words,
        "visible_chars_per_second": round(visible_chars / total_s, 2) if total_s else None,
    }


async def run_stt(
    session: aiohttp.ClientSession,
    env: dict[str, str],
    audio_path: Path,
    input_meta: dict[str, object],
) -> dict[str, object]:
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
    ) as response:
        response.raise_for_status()
        payload = await response.json()
    end = time.perf_counter()

    duration_s = float(input_meta["duration_s"])
    wall_time_s = end - start
    backend_s = float(payload.get("processing_time_seconds") or 0.0)
    return {
        "status": 200,
        "transcript": payload.get("transcription_text", "").strip(),
        "transcription_id": payload.get("transcription_id"),
        "wall_time_s": round(wall_time_s, 3),
        "backend_processing_time_s": round(backend_s, 4),
        "rtf_wall": round(wall_time_s / duration_s, 4) if duration_s else None,
        "rtf_backend": round(backend_s / duration_s, 4) if duration_s else None,
    }


async def run_tts(
    session: aiohttp.ClientSession,
    env: dict[str, str],
    reply_text: str,
    output_path: Path,
) -> dict[str, object]:
    start = time.perf_counter()
    async with session.post(
        host_accessible_url(env["CUSTOM_TTS_URL"]).rstrip("/") + "/api/synthesize/",
        json={
            "text": reply_text,
            "output_format": env.get("CUSTOM_TTS_AUDIO_FORMAT", "wav"),
            "sample_rate": int(env.get("CUSTOM_TTS_SAMPLE_RATE", "24000")),
        },
        headers={"Authorization": f"Bearer {env['CUSTOM_TTS_ACCESS_TOKEN']}"},
    ) as response:
        status = response.status
        headers = dict(response.headers)
        audio_bytes = await response.read()
        if status >= 400:
            try:
                detail = audio_bytes.decode("utf-8", errors="ignore")
            except Exception:
                detail = ""
            raise RuntimeError(f"TTS request failed with status {status}: {detail}")
    end = time.perf_counter()

    output_path.write_bytes(audio_bytes)
    output_meta = output_audio_metadata(output_path)
    output_duration_s = float(output_meta["duration_s"])
    wall_time_s = end - start
    backend_s = float(headers.get("x-processing-time") or 0.0)
    return {
        "status": status,
        "wall_time_s": round(wall_time_s, 3),
        "backend_processing_time_s": round(backend_s, 4),
        "output_duration_s": round(output_duration_s, 3),
        "audio_bytes": len(audio_bytes),
        "rtf_wall": round(wall_time_s / output_duration_s, 4) if output_duration_s else None,
        "rtf_backend": round(backend_s / output_duration_s, 4) if output_duration_s else None,
        "output_audio_path": str(output_path),
    }


async def evaluate_audio(
    session: aiohttp.ClientSession,
    env: dict[str, str],
    audio_path: Path,
    run_dir: Path,
) -> dict[str, object]:
    input_meta = audio_metadata(audio_path)
    copied_input_path = run_dir / f"input{audio_path.suffix.lower()}"
    shutil.copy2(audio_path, copied_input_path)

    pipeline_start = time.perf_counter()
    stt_result = await run_stt(session, env, audio_path, input_meta)
    llm_result = await run_llm(session, env, stt_result["transcript"])
    if not llm_result["reply_text"]:
        raise RuntimeError("LLM returned an empty visible reply")
    output_path = run_dir / "output.wav"
    tts_result = await run_tts(session, env, llm_result["reply_text"], output_path)
    pipeline_end = time.perf_counter()

    input_duration_s = float(input_meta["duration_s"])
    output_duration_s = float(tts_result["output_duration_s"])
    total_s = pipeline_end - pipeline_start
    result = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input_audio": {
            **input_meta,
            "copied_path": str(copied_input_path),
        },
        "stt": {
            "provider": env.get("CUSTOM_STT_PROVIDER"),
            "url": env.get("CUSTOM_STT_URL"),
            **stt_result,
        },
        "llm": {
            "provider": env.get("CUSTOM_LLM_PROVIDER"),
            "url": env.get("CUSTOM_LLM_URL"),
            "model": env.get("CUSTOM_LLM_MODEL"),
            **llm_result,
        },
        "tts": {
            "provider": env.get("CUSTOM_TTS_PROVIDER"),
            "url": env.get("CUSTOM_TTS_URL"),
            **tts_result,
        },
        "pipeline": {
            "total_time_s": round(total_s, 3),
            "rtf_vs_input": round(total_s / input_duration_s, 4) if input_duration_s else None,
            "rtf_vs_output": round(total_s / output_duration_s, 4) if output_duration_s else None,
        },
    }

    write_json(run_dir / "result.json", result)
    (run_dir / "llm_response.txt").write_text(
        result["llm"]["reply_text"] + "\n",
        encoding="utf-8",
    )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate the full STT -> LLM -> TTS pipeline and save artifacts.",
    )
    parser.add_argument("audio_files", nargs="+", help="WAV input files to evaluate")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    env = read_env(ENV_PATH)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    batch_dir = RUNS_DIR / timestamp
    batch_dir.mkdir(parents=True, exist_ok=True)

    summary: list[dict[str, object]] = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
        for audio in args.audio_files:
            audio_path = Path(audio).expanduser().resolve()
            if not audio_path.exists():
                raise FileNotFoundError(f"Audio file not found: {audio_path}")
            run_dir = batch_dir / slugify(audio_path.stem)
            run_dir.mkdir(parents=True, exist_ok=True)
            try:
                result = await evaluate_audio(session, env, audio_path, run_dir)
                summary.append(
                    {
                        "audio": audio_path.name,
                        "run_dir": str(run_dir),
                        "status": "success",
                        "pipeline_total_time_s": result["pipeline"]["total_time_s"],
                        "pipeline_rtf_vs_input": result["pipeline"]["rtf_vs_input"],
                        "reply_text": result["llm"]["reply_text"],
                    }
                )
            except Exception as exc:
                failure = {
                    "audio": audio_path.name,
                    "run_dir": str(run_dir),
                    "status": "error",
                    "error": str(exc),
                }
                summary.append(failure)
                write_json(run_dir / "result.json", failure)

    summary_payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "batch_dir": str(batch_dir),
        "results": summary,
    }
    write_json(batch_dir / "summary.json", summary_payload)
    print(json.dumps(summary_payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
