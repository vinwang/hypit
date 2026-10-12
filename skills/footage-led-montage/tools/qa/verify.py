#!/usr/bin/env python3
"""Technical gates for the delivered files of one batch.

Everything here is measured on the encoded files that will actually be
delivered, never on an intermediate render, and every threshold comes from the
recipe.  The one check that is not a property of a single file - the plan's
pairwise footage reuse - is recomputed from the batch plan and compared with
what the runner recorded, so a plan edited after the render cannot pass.

Requires ffmpeg/ffprobe.  Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

DURATION_TOLERANCE_SECONDS = 0.08
BLACK_MIN_SECONDS = 0.04
BLACK_PIXEL_THRESHOLD = 0.08


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, value):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).digest().hex()


def probe(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
        capture_output=True, check=True, text=True)
    return json.loads(result.stdout)


def first_stream(media, kind):
    for stream in media["streams"]:
        if stream["codec_type"] == kind:
            return stream
    return None


def technical_profile(path, expected):
    media = probe(path)
    video = first_stream(media, "video")
    audio = first_stream(media, "audio")
    failures = []
    if video is None:
        return {"video": None, "audio": None}, ["no video stream"]
    if audio is None:
        failures.append("no audio stream")
    rates = {video.get("avg_frame_rate"), video.get("r_frame_rate")}
    observed = {
        "width": video["width"],
        "height": video["height"],
        "frame_rates": sorted(rate for rate in rates if rate),
        "frames": int(video["nb_frames"]) if video.get("nb_frames") else None,
        "video_codec": video["codec_name"],
        "pixel_format": video["pix_fmt"],
        "audio_codec": audio["codec_name"] if audio else None,
        "duration_seconds": float(media["format"]["duration"]),
    }
    if (observed["width"], observed["height"]) != (expected["width"], expected["height"]):
        failures.append(f"canvas is {observed['width']}x{observed['height']}, "
                        f"expected {expected['width']}x{expected['height']}")
    if expected["frame_rate"] not in rates:
        failures.append(f"frame rate is {observed['frame_rates']}, expected {expected['frame_rate']}")
    if observed["frames"] != expected["frames"]:
        failures.append(f"{observed['frames']} frames, expected {expected['frames']}")
    if observed["video_codec"] != expected["video_codec"]:
        failures.append(f"video codec is {observed['video_codec']}, expected {expected['video_codec']}")
    if observed["pixel_format"] != expected["pixel_format"]:
        failures.append(f"pixel format is {observed['pixel_format']}, expected {expected['pixel_format']}")
    if observed["audio_codec"] != expected["audio_codec"]:
        failures.append(f"audio codec is {observed['audio_codec']}, expected {expected['audio_codec']}")
    drift = abs(observed["duration_seconds"] - expected["seconds"])
    if drift > DURATION_TOLERANCE_SECONDS:
        failures.append(f"duration is {observed['duration_seconds']:.3f}s, "
                        f"expected {expected['seconds']:.3f}s (drift {drift:.3f}s)")
    return observed, failures


def faststart_failure(path):
    """The player must find the index before the media data, or streaming stalls."""
    with open(path, "rb") as handle:
        head = handle.read()
    moov = head.find(b"moov")
    mdat = head.find(b"mdat")
    if moov < 0 or mdat < 0:
        return f"container is missing moov ({moov}) or mdat ({mdat})"
    if moov > mdat:
        return f"moov at {moov} follows mdat at {mdat}: not faststart"
    return None


def audio_stream_hash(path):
    """Hash the audio elementary stream: two files carrying the same audio hash equal."""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0", "-c:a", "copy", "-f", "adts", "-"],
        capture_output=True, check=True)
    return sha256_bytes(result.stdout)


def black_segments(path):
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path,
         "-vf", f"blackdetect=d={BLACK_MIN_SECONDS}:pix_th={BLACK_PIXEL_THRESHOLD}",
         "-an", "-f", "null", "-"],
        capture_output=True, text=True)
    segments = []
    for line in result.stderr.splitlines():
        if "black_start:" not in line:
            continue
        fields = {}
        for token in line.split():
            if ":" in token:
                key, _, value = token.partition(":")
                fields[key] = value
        segments.append({
            "start_seconds": float(fields.get("black_start", "nan")),
            "end_seconds": float(fields.get("black_end", "nan")),
            "seconds": float(fields.get("black_duration", "nan")),
        })
    return segments


def plan_duplicate_ratio(plan, body_frames):
    """The runner's own definition, recomputed: two variants sharing a continuous
    source scene count that scene's shorter use as duplicated."""
    scene_uses = {}
    for variant in plan["variants"]:
        for shot in variant["shots"]:
            scene_uses.setdefault(shot["continuous_scene_id"], []).append(
                {"variant_id": variant["variant_id"], "frames": shot["frames"]})
    worst = 0.0
    for left in range(len(plan["variants"])):
        for right in range(left + 1, len(plan["variants"])):
            left_id = plan["variants"][left]["variant_id"]
            right_id = plan["variants"][right]["variant_id"]
            duplicate_frames = 0
            for uses in scene_uses.values():
                left_use = next((use for use in uses if use["variant_id"] == left_id), None)
                right_use = next((use for use in uses if use["variant_id"] == right_id), None)
                if left_use and right_use:
                    duplicate_frames += min(left_use["frames"], right_use["frames"])
            worst = max(worst, duplicate_frames / body_frames)
    return worst


def sample_frames(video, times, out_dir, prefix):
    os.makedirs(out_dir, exist_ok=True)
    samples = []
    for label, seconds in times:
        path = os.path.join(out_dir, f"{prefix}-{label}.png")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{seconds:.3f}", "-i", video,
                        "-frames:v", "1", path], check=True)
        samples.append({"label": label, "seconds": round(seconds, 3),
                        "file": os.path.relpath(path, out_dir)})
    return samples


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--recipe", required=True, help="recipe.json of the project to audit")
    parser.add_argument("--qa-dir", help="where reports and frames are written (default: the recipe's qa path)")
    parser.add_argument("--output-dir", help="where the rendered files live (default: the plan's directory)")
    args = parser.parse_args()

    recipe_path = os.path.abspath(args.recipe)
    root = os.path.dirname(recipe_path)
    recipe = read_json(recipe_path)
    plan_path = os.path.join(root, recipe["paths"]["plan"])
    plan = read_json(plan_path)
    suffix = recipe["output_naming"]["qa_suffix"]
    qa_dir = args.qa_dir or os.path.join(root, recipe["paths"]["qa"])
    output_dir = os.path.abspath(args.output_dir) if args.output_dir else os.path.dirname(plan_path)

    canvas = recipe["canvas"]
    body_frames = recipe["timeline"]["body_frames"]
    tail_frames = recipe["timeline"]["tail_frames"]
    total_frames = body_frames + tail_frames
    fps = canvas["fps"]
    body_seconds = body_frames / fps
    tail_seconds = tail_frames / fps
    total_seconds = total_frames / fps
    expected = {
        "width": canvas["width"], "height": canvas["height"], "frame_rate": f"{fps}/1",
        "frames": total_frames, "video_codec": "h264", "pixel_format": "yuv420p",
        "audio_codec": "aac", "seconds": total_seconds,
    }
    policy = recipe["batch"]["duplicate_policy"]

    times = [(label, seconds) for label, seconds in [
        ("body-early", 0.75),
        ("body-middle", body_seconds / 2),
        ("body-late", body_seconds - 0.5),
        ("tail-start", body_seconds + 0.5),
        ("tail-middle", body_seconds + tail_seconds / 2),
        ("total-end", total_seconds - 0.5),
    ] if 0 <= seconds < total_seconds]

    failures = []
    outputs = []
    for variant in plan["variants"]:
        video = os.path.join(output_dir, variant["file"])
        variant_id = variant["variant_id"]
        entry = {"variant_id": variant_id, "file": variant["file"], "sha256": sha256_file(video)}
        profile, problems = technical_profile(video, expected)
        entry["observed"] = profile
        entry["failures"] = problems
        faststart = faststart_failure(video)
        entry["faststart"] = faststart is None
        if faststart:
            problems.append(faststart)
        entry["audio_stream_sha256"] = audio_stream_hash(video)
        entry["black_segments"] = black_segments(video)
        entry["frame_samples"] = sample_frames(video, times, os.path.join(qa_dir, f"review-frames{suffix}"),
                                               f"{variant_id}{suffix}")
        failures.extend(f"{variant_id}: {problem}" for problem in problems)
        outputs.append(entry)
        print(f"{variant_id}: {profile['width']}x{profile['height']} {profile['frames']}f "
              f"{profile['duration_seconds']:.3f}s faststart={entry['faststart']} "
              f"black={len(entry['black_segments'])} "
              f"{'OK' if not problems else 'FAIL: ' + '; '.join(problems)}")

    audio_hashes = {output["audio_stream_sha256"] for output in outputs}
    if len(audio_hashes) != 1:
        failures.append(f"variants do not share one audio track: {len(audio_hashes)} distinct streams")
    # Informational: an mp4 edit list can add a priming frame, so an exact match
    # with the mix file is evidence but not a gate.
    shared_mix = os.path.join(root, recipe["audio_mix"]["render_path"])
    shared_mix_hash = audio_stream_hash(shared_mix) if os.path.exists(shared_mix) else None

    recomputed = plan_duplicate_ratio(plan, body_frames)
    recorded = plan.get("max_pairwise_duplicate_ratio")
    if recorded is None:
        failures.append("the plan does not record max_pairwise_duplicate_ratio")
    elif abs(recomputed - recorded) > 1e-12:
        failures.append(f"plan reuse ratio no longer matches the recorded value: "
                        f"recomputed {recomputed!r}, recorded {recorded!r}")
    if recomputed > policy["max_pairwise_ratio"]:
        failures.append(f"plan reuse ratio {recomputed} exceeds the recipe limit {policy['max_pairwise_ratio']}")

    report = {
        "recipe_id": recipe["id"],
        "batch_id": plan.get("batch"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "recipe_sha256": sha256_file(recipe_path),
        "plan_sha256": sha256_file(plan_path),
        "expected_profile": expected,
        "outputs": outputs,
        "audio_identity": {
            "distinct_streams": len(audio_hashes),
            "sha256": sorted(audio_hashes),
            "shared_mix_sha256": shared_mix_hash,
            "matches_shared_mix": shared_mix_hash in audio_hashes if shared_mix_hash else None,
        },
        "plan_duplicate_ratio": {
            "recomputed": recomputed,
            "recorded": recorded,
            "limit": policy["max_pairwise_ratio"],
            "passed": not any("reuse ratio" in failure for failure in failures),
        },
        "frame_sample_times": [{"label": label, "seconds": round(seconds, 3)} for label, seconds in times],
        "status": "FAIL" if failures else "PASS",
        "failures": failures,
    }
    write_json(os.path.join(qa_dir, f"verify{suffix}.json"), report)
    write_json(os.path.join(qa_dir, f"frame-samples{suffix}.json"),
               {"times": report["frame_sample_times"],
                "variants": {output["variant_id"]: output["frame_samples"] for output in outputs}})

    print(f"\nplan pairwise footage reuse: {recomputed!r} "
          f"(recorded {recorded!r}, limit {policy['max_pairwise_ratio']})")
    print(f"{report['status']}: {os.path.join(qa_dir, f'verify{suffix}.json')}")
    for failure in failures:
        print(f"  - {failure}")
    return 2 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
