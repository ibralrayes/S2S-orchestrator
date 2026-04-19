from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median


ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "eval" / "runs"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze paired direct/livekit compare runs to estimate VAD impact."
    )
    parser.add_argument(
        "--runs-dir",
        default=str(RUNS_DIR),
        help="Directory containing compare-* run folders",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Optional JSON output path",
    )
    return parser.parse_args()


def load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_mean(values: list[float]) -> float | None:
    return round(mean(values), 3) if values else None


def safe_median(values: list[float]) -> float | None:
    return round(median(values), 3) if values else None


def summarize_group(rows: list[dict[str, object]]) -> dict[str, object]:
    if not rows:
        return {"count": 0}

    numeric_keys = [
        "input_duration_s",
        "no_vad_first_token_s",
        "no_vad_first_audio_s",
        "no_vad_response_complete_s",
        "vad_ttfa_from_end_s",
        "vad_added_detection_s",
        "vad_estimated_first_token_s",
        "vad_response_complete_s",
        "livekit_total_wall_s",
    ]

    summary: dict[str, object] = {"count": len(rows)}
    for key in numeric_keys:
        values = [float(row[key]) for row in rows if row.get(key) is not None]
        summary[key] = {
            "avg": safe_mean(values),
            "median": safe_median(values),
            "min": round(min(values), 3) if values else None,
            "max": round(max(values), 3) if values else None,
        }
    return summary


def collect_pairs(runs_dir: Path) -> list[dict[str, object]]:
    pairs: list[dict[str, object]] = []

    for direct_path in sorted(runs_dir.glob("compare-*/*/direct_result.json")):
        livekit_path = direct_path.with_name("livekit_result.json")
        if not livekit_path.exists():
            continue

        direct = load_json(direct_path)
        livekit = load_json(livekit_path)

        direct_first_token = (
            float(direct["stt"]["wall_s"]) + float(direct["llm"]["visible_ttft_s"])
        )
        direct_first_audio = float(direct["pipeline"]["e2e_approx_s"])
        direct_response_complete = (
            direct_first_audio + float(direct["tts"]["output_duration_s"])
        )

        ttfa_end = livekit["timing"].get("ttfa_from_end_s")
        agent_audio_duration = livekit["timing"].get("agent_audio_duration_s")
        if ttfa_end is None or agent_audio_duration is None:
            continue

        ttfa_end = float(ttfa_end)
        agent_audio_duration = float(agent_audio_duration)
        detection_delta = ttfa_end - direct_first_audio

        pairs.append(
            {
                "compare_run": str(direct_path.parents[1]),
                "run_dir": str(direct_path.parent),
                "audio": direct["input"]["filename"],
                "input_duration_s": float(direct["input"]["duration_s"]),
                "no_vad_first_token_s": round(direct_first_token, 3),
                "no_vad_first_audio_s": round(direct_first_audio, 3),
                "no_vad_response_complete_s": round(direct_response_complete, 3),
                "vad_ttfa_from_end_s": round(ttfa_end, 3),
                "vad_added_detection_s": round(detection_delta, 3),
                "vad_estimated_first_token_s": round(direct_first_token + detection_delta, 3),
                "vad_response_complete_s": round(ttfa_end + agent_audio_duration, 3),
                "livekit_total_wall_s": float(livekit["timing"]["total_wall_s"]),
                "trigger_type": "mid_utterance" if detection_delta < 0 else "post_speech",
            }
        )

    return pairs


def build_report(runs_dir: Path) -> dict[str, object]:
    pairs = collect_pairs(runs_dir)
    post_speech = [row for row in pairs if row["trigger_type"] == "post_speech"]
    mid_utterance = [row for row in pairs if row["trigger_type"] == "mid_utterance"]

    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "runs_dir": str(runs_dir),
        "paired_runs": len(pairs),
        "post_speech_runs": len(post_speech),
        "mid_utterance_runs": len(mid_utterance),
        "all_runs": summarize_group(pairs),
        "post_speech_only": summarize_group(post_speech),
        "mid_utterance_only": summarize_group(mid_utterance),
        "top_examples": {
            "highest_vad_delay": sorted(
                post_speech,
                key=lambda row: row["vad_added_detection_s"],
                reverse=True,
            )[:5],
            "lowest_vad_delay": sorted(
                post_speech,
                key=lambda row: row["vad_added_detection_s"],
            )[:5],
            "mid_utterance_fastest": sorted(
                mid_utterance,
                key=lambda row: row["vad_added_detection_s"],
            )[:5],
        },
    }
    return report


def main() -> int:
    args = parse_args()
    runs_dir = Path(args.runs_dir).expanduser().resolve()
    report = build_report(runs_dir)

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
