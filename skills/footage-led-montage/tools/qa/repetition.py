#!/usr/bin/env python3
"""Duplication between the rendered variants of one batch.

Two different questions live here and they are reported separately, because
answering one does not answer the other:

  * source reuse - read from the batch plan: do two variants cut the same
    continuous source scene?  The runner already refuses a batch above the
    recipe's limit, so this section is a re-read of the plan, not a new gate.
  * visual duplication - measured from the delivered files: do two finished
    videos actually show the same pixels, including duplicates that survive a
    time shift?  Overlays and the tail are excluded by the comparison region.

Only the comparison region, the body length and the canvas come from the
recipe.  The method constants below are part of the metric: changing one
changes the number, so they are deliberately not configurable.

Requires ffmpeg/ffprobe.  Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

# --- method constants (part of the metric; do not tune to make a batch pass) --
SAMPLE_FPS = 6
SAMPLE_WIDTH, SAMPLE_HEIGHT = 64, 48
HASH_GRID = 8                     # 8x8 average hash over the sampled frame
HASH_MAX_DISTANCE = 10            # of 64 bits, for the coarse screen
CORR_MIN = 0.99                   # pixel confirmation of a coarse hit
MSE_MAX = 50.0
MIN_RUN_SAMPLES = SAMPLE_FPS      # one second of continuous duplication
METHOD_VERSION = "v2"


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


def run(binary, args):
    return subprocess.run([binary, *args], capture_output=True, check=True)


# --- sampling -----------------------------------------------------------------

def sample_gray_frames(video, cache_dir, body_frames, fps, region):
    """Sample the body of one delivered file as small gray frames.

    Cached by content: the key covers the file's own hash and every parameter
    that shapes the samples, so a stale cache cannot silently feed the metric.
    """
    label = (f"body-{SAMPLE_FPS}fps-gray-{SAMPLE_WIDTH}x{SAMPLE_HEIGHT}"
             f"-x{region['x']}-y{region['y']}-w{region['width']}-h{region['height']}-{METHOD_VERSION}")
    key = hashlib.sha256(f"{sha256_file(video)}|{label}".encode("utf-8")).hexdigest()
    os.makedirs(cache_dir, exist_ok=True)
    cached = os.path.join(cache_dir, f"{key}.gray")
    expected = round(body_frames * SAMPLE_FPS / fps)
    if not os.path.exists(cached):
        crop = f"crop={region['width']}:{region['height']}:{region['x']}:{region['y']}"
        vf = (f"trim=end_frame={body_frames},setpts=PTS-STARTPTS,fps={SAMPLE_FPS},{crop},"
              f"scale={SAMPLE_WIDTH}:{SAMPLE_HEIGHT}:flags=area,format=gray")
        partial = f"{cached}.{os.getpid()}.part"
        with open(partial, "wb") as handle:
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-i", video,
                "-vf", vf, "-frames:v", str(expected),
                "-f", "rawvideo", "-pix_fmt", "gray", "-",
            ], stdout=handle, check=True)
        os.replace(partial, cached)
    with open(cached, "rb") as handle:
        data = handle.read()
    size = SAMPLE_WIDTH * SAMPLE_HEIGHT
    frames = [data[offset:offset + size] for offset in range(0, len(data) - size + 1, size)]
    # The fps filter decides on its own when it has enough input for a sample; a
    # count that drifts from the body length would quietly rescale every ratio.
    if abs(len(frames) - expected) > 1:
        raise SystemExit(f"{video}: sampled {len(frames)} frames, expected about {expected}")
    return frames, key


def average_hash(frame):
    bins = []
    for row in range(HASH_GRID):
        for column in range(HASH_GRID):
            total = 0
            count = 0
            for y in range(row * SAMPLE_HEIGHT // HASH_GRID, (row + 1) * SAMPLE_HEIGHT // HASH_GRID):
                base = y * SAMPLE_WIDTH + column * SAMPLE_WIDTH // HASH_GRID
                span = (column + 1) * SAMPLE_WIDTH // HASH_GRID - column * SAMPLE_WIDTH // HASH_GRID
                for x in range(base, base + span):
                    total += frame[x]
                    count += 1
            bins.append(total / count)
    median = statistics.median(bins)
    bits = 0
    for index, value in enumerate(bins):
        if value > median:
            bits |= 1 << index
    return bits


def hamming(left, right):
    return bin(left ^ right).count("1")


def pixel_match(left, right):
    """Confirm a coarse hit on the actual samples: same picture, not just similar."""
    count = len(left)
    mean_left = sum(left) / count
    mean_right = sum(right) / count
    covariance = 0.0
    variance_left = 0.0
    variance_right = 0.0
    squared_error = 0.0
    for index in range(count):
        delta_left = left[index] - mean_left
        delta_right = right[index] - mean_right
        covariance += delta_left * delta_right
        variance_left += delta_left * delta_left
        variance_right += delta_right * delta_right
        error = left[index] - right[index]
        squared_error += error * error
    if variance_left == 0 or variance_right == 0:
        return False
    correlation = covariance / math.sqrt(variance_left * variance_right)
    return correlation >= CORR_MIN and squared_error / count <= MSE_MAX


def runs(values, minimum):
    """Consecutive True runs of at least `minimum` entries, as (start, length)."""
    found = []
    start = None
    for index, value in enumerate(values):
        if value and start is None:
            start = index
        elif not value and start is not None:
            if index - start >= minimum:
                found.append((start, index - start))
            start = None
    if start is not None and len(values) - start >= minimum:
        found.append((start, len(values) - start))
    return found


def compare(frames_left, hashes_left, frames_right, hashes_right):
    """Every alignment of one video against the other, coarse screen then pixels.

    A duplicate is only counted when it holds for at least a second, which is
    why the sweep is over time offsets rather than over equal timestamps: two
    variants can show the same footage at different moments.
    """
    left_count = len(frames_left)
    right_count = len(frames_right)
    confirmed = {}
    matched = [False] * left_count
    segments = []

    def is_match(left_index, right_index):
        if hamming(hashes_left[left_index], hashes_right[right_index]) > HASH_MAX_DISTANCE:
            return False
        key = (left_index, right_index)
        if key not in confirmed:
            confirmed[key] = pixel_match(frames_left[left_index], frames_right[right_index])
        return confirmed[key]

    for offset in range(-left_count + 1, right_count):
        first = max(0, offset)
        last = min(left_count, right_count + offset)
        if last - first < MIN_RUN_SAMPLES:
            continue
        flags = [is_match(index, index - offset) for index in range(first, last)]
        for start, length in runs(flags, MIN_RUN_SAMPLES):
            for index in range(first + start, first + start + length):
                matched[index] = True
            segments.append({
                "start_seconds": (first + start) / SAMPLE_FPS,
                "seconds": length / SAMPLE_FPS,
                "offset_seconds": offset / SAMPLE_FPS,
            })
    return matched, segments


# --- plan re-read -------------------------------------------------------------

def source_reuse(plan, fps):
    """What the plan says about footage sharing, in the plan's own terms."""
    placements = [shot for variant in plan["variants"] for shot in variant["shots"]]
    unique_hashes = {shot["original_sha256"] for shot in placements}
    scenes = {}
    for variant in plan["variants"]:
        for shot in variant["shots"]:
            scenes.setdefault(shot["continuous_scene_id"], []).append(
                {"variant_id": variant["variant_id"], "frames": shot["frames"]})
    shared = {}
    for scene_id, uses in scenes.items():
        if len(uses) < 2:
            continue
        for index, left in enumerate(uses):
            for right in uses[index + 1:]:
                if left["variant_id"] == right["variant_id"]:
                    continue
                pair = "|".join(sorted([left["variant_id"], right["variant_id"]]))
                overlap = min(left["frames"], right["frames"]) / fps
                entry = shared.setdefault(pair, {"shared_scenes": 0, "overlapping_seconds": 0.0})
                entry["shared_scenes"] += 1
                # Below one frame the two uses do not share a picture at all.
                if overlap > 1 / fps:
                    entry["overlapping_seconds"] = round(entry["overlapping_seconds"] + overlap, 6)
    per_variant = {}
    for variant in plan["variants"]:
        hashes = [shot["original_sha256"] for shot in variant["shots"]]
        per_variant[variant["variant_id"]] = {
            "shots": len(hashes),
            "unique_source_files": len(set(hashes)),
            "source_file_reuse_ratio": round(1 - len(set(hashes)) / len(hashes), 6),
        }
    return {
        "placements": len(placements),
        "unique_source_files": len(unique_hashes),
        "source_file_reuse_ratio": round(1 - len(unique_hashes) / len(placements), 6),
        "per_variant": per_variant,
        "pairs_sharing_a_continuous_scene": shared,
    }


# --- self test ----------------------------------------------------------------

def synthetic(seed, count):
    generator = random.Random(seed)
    return [bytes(generator.randrange(256) for _ in range(SAMPLE_WIDTH * SAMPLE_HEIGHT))
            for _ in range(count)]


def shifted(frames, amount):
    return [bytes(min(255, value + amount) for value in frame) for frame in frames]


def self_test():
    source_start, embed_start, length = 4, 17, 20
    source = synthetic(11, 40)
    other = synthetic(29, 40)
    # The same footage appears in `other` thirteen samples later than in `source`.
    other[embed_start:embed_start + length] = source[source_start:source_start + length]
    hashes_source = [average_hash(frame) for frame in source]
    hashes_other = [average_hash(frame) for frame in other]

    matched, segments = compare(source, hashes_source, other, hashes_other)
    union_seconds = sum(matched) / SAMPLE_FPS
    longest = max((segment["seconds"] for segment in segments), default=0.0)
    offsets = {segment["offset_seconds"] for segment in segments}
    print(f"  found {union_seconds:.3f}s across {len(segments)} segment(s), longest {longest:.3f}s, offsets {sorted(offsets)}")
    assert union_seconds >= 3.0, f"missed the embedded duplicate: {union_seconds}"
    assert longest >= 3.0, f"longest duplicate too short: {longest}"
    assert sum(matched) == length, f"matched {sum(matched)} samples, expected exactly the embedded {length}"
    expected_offset = -(embed_start - source_start) / SAMPLE_FPS
    assert expected_offset in offsets, f"alignment {expected_offset} not found in {sorted(offsets)}"

    absent, _ = compare(source[:20], [average_hash(frame) for frame in source[:20]],
                        other[:20], [average_hash(frame) for frame in other[:20]])
    print(f"  unrelated windows: {sum(absent) / SAMPLE_FPS:.3f}s")
    assert sum(absent) == 0, "unrelated footage reported as duplicated"

    # A brightness shift must not read as the same picture.
    bright = shifted(source, 40)
    assert not pixel_match(source[0], bright[0]), "a brightness shift passed the pixel gate"
    bright_matched, _ = compare(source, hashes_source, bright, [average_hash(frame) for frame in bright])
    print(f"  brightness-shifted copy: {sum(bright_matched) / SAMPLE_FPS:.3f}s")
    assert sum(bright_matched) == 0, "a brightness-shifted copy was reported as duplicated"

    # Two identical files are duplicates of themselves, everywhere.
    same, _ = compare(source, hashes_source, source, hashes_source)
    print(f"  identical input: {sum(same) / SAMPLE_FPS:.3f}s of {len(source) / SAMPLE_FPS:.3f}s")
    assert sum(same) == len(source), "an identical copy was not fully matched"
    print("selftest OK")


def smoke_test(video, body_frames, fps, region):
    """Prove the extraction path on a real file: itself, and a copy shifted in time."""
    cache = os.path.join(tempfile.gettempdir(), "footage-led-montage-smoke")
    frames, _ = sample_gray_frames(video, cache, body_frames, fps, region)
    hashes = [average_hash(frame) for frame in frames]
    matched, _ = compare(frames, hashes, frames, hashes)
    coverage = sum(matched) / len(frames)
    print(f"  self comparison: {coverage * 100:.1f}% of {len(frames)} samples")
    assert coverage >= 0.95, f"a file does not match itself: {coverage}"

    window = frames[:2 * SAMPLE_FPS]
    doubled = window + window
    doubled_matched, segments = compare(
        window, [average_hash(frame) for frame in window],
        doubled, [average_hash(frame) for frame in doubled])
    print(f"  copied window: {sum(doubled_matched) / SAMPLE_FPS:.3f}s (expected {len(window) / SAMPLE_FPS:.3f}s)")
    assert sum(doubled_matched) == len(window), "a copied window was not fully matched"
    assert all(segment["seconds"] >= 1.0 for segment in segments), "a short segment slipped through"
    print("smoke test OK")


# --- main ---------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--recipe", help="recipe.json of the project to audit")
    parser.add_argument("--out", help="report path (default: <qa>/final-repetition<qa_suffix>.json)")
    parser.add_argument("--output-dir", help="where the rendered files live (default: the plan's directory)")
    parser.add_argument("--cache-dir", help="sampled-frame cache (default: <qa>/final-repetition-frames)")
    parser.add_argument("--selftest", action="store_true", help="check the metric on synthetic footage")
    parser.add_argument("--smoke-video", help="check the extraction path on one real file")
    parser.add_argument("--smoke-body-frames", type=int, default=600)
    parser.add_argument("--smoke-fps", type=int, default=30)
    args = parser.parse_args()

    if args.selftest:
        self_test()
        return 0
    if args.smoke_video:
        smoke_test(args.smoke_video, args.smoke_body_frames, args.smoke_fps,
                   {"width": 64, "height": 48, "x": 0, "y": 0})
        return 0
    if not args.recipe:
        parser.error("--recipe is required (or use --selftest / --smoke-video)")

    recipe_path = os.path.abspath(args.recipe)
    root = os.path.dirname(recipe_path)
    recipe = read_json(recipe_path)
    plan_path = os.path.join(root, recipe["paths"]["plan"])
    plan = read_json(plan_path)
    output_dir = os.path.abspath(args.output_dir) if args.output_dir else os.path.dirname(plan_path)

    fps = recipe["canvas"]["fps"]
    body_frames = recipe["timeline"]["body_frames"]
    body_seconds = body_frames / fps
    region = recipe["batch"]["duplicate_policy"]["comparison_region"]
    qa_dir = os.path.join(root, recipe["paths"]["qa"])
    suffix = recipe["output_naming"]["qa_suffix"]
    out_path = args.out or os.path.join(qa_dir, f"final-repetition{suffix}.json")
    cache_dir = args.cache_dir or os.path.join(qa_dir, "final-repetition-frames")

    outputs = []
    for variant in plan["variants"]:
        video = os.path.join(output_dir, variant["file"])
        frames, key = sample_gray_frames(video, cache_dir, body_frames, fps, region)
        timeline_path = os.path.join(
            output_dir, f"{recipe['output_naming']['timeline_prefix']}{variant['variant_id'].split('-')[-1]}.json")
        timeline_match = None
        if os.path.exists(timeline_path):
            timeline_match = read_json(timeline_path).get("output_sha256") == sha256_file(video)
        outputs.append({
            "variant_id": variant["variant_id"],
            "file": variant["file"],
            "sha256": sha256_file(video),
            "timeline_sha256_matches": timeline_match,
            "sample_count": len(frames),
            "sample_seconds": len(frames) / SAMPLE_FPS,
            "cache_key": key,
            "frames": frames,
        })
        print(f"{variant['variant_id']}: {len(frames)} samples "
              f"({len(frames) / SAMPLE_FPS:.3f}s of the {body_seconds:.3f}s body)")

    hashes = [[average_hash(frame) for frame in output["frames"]] for output in outputs]

    pairs = []
    for left in range(len(outputs)):
        for right in range(left + 1, len(outputs)):
            matched, segments = compare(outputs[left]["frames"], hashes[left],
                                        outputs[right]["frames"], hashes[right])
            union_seconds = sum(matched) / SAMPLE_FPS
            pairs.append({
                "left": outputs[left]["variant_id"],
                "right": outputs[right]["variant_id"],
                "union_seconds": round(union_seconds, 6),
                "union_ratio": round(union_seconds / body_seconds, 6),
                "longest_segment_seconds": round(max((segment["seconds"] for segment in segments), default=0.0), 6),
                "segments": segments,
            })
            print(f"{outputs[left]['variant_id']} vs {outputs[right]['variant_id']}: "
                  f"{union_seconds:.3f}s ({union_seconds / body_seconds * 100:.2f}%)")

    worst = max(pairs, key=lambda pair: pair["union_ratio"]) if pairs else None
    limit = recipe["batch"]["duplicate_policy"]["max_pairwise_ratio"]
    limit_percent = limit * 100
    failures = []
    if worst and worst["union_ratio"] * 100 >= limit_percent:
        failures.append(f"visual duplication {worst['union_ratio'] * 100:.2f}% "
                        f"({worst['left']} vs {worst['right']}) reaches the {limit_percent:g}% limit")
    for output in outputs:
        if output["timeline_sha256_matches"] is False:
            failures.append(f"{output['variant_id']}: the file on disk is not the one the timeline recorded")

    report = {
        "recipe_id": recipe["id"],
        "batch_id": plan.get("batch"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "recipe_sha256": sha256_file(recipe_path),
        "plan_sha256": sha256_file(plan_path),
        "method": {
            "sample_fps": SAMPLE_FPS,
            "sample_size": [SAMPLE_WIDTH, SAMPLE_HEIGHT],
            "hash_grid": HASH_GRID,
            "hash_max_distance": HASH_MAX_DISTANCE,
            "corr_min": CORR_MIN,
            "mse_max": MSE_MAX,
            "min_run_seconds": MIN_RUN_SAMPLES / SAMPLE_FPS,
            "time_offset_alignment": True,
            "comparison_region": region,
            "excluded_layers": recipe["batch"]["duplicate_policy"]["excluded_layers"],
        },
        "body_seconds": body_seconds,
        "outputs": [{key: value for key, value in output.items() if key != "frames"} for output in outputs],
        "pairs": pairs,
        "max_pairwise_visual_duplication": None if worst is None else {
            "left": worst["left"], "right": worst["right"],
            "ratio": worst["union_ratio"], "percent": round(worst["union_ratio"] * 100, 4),
        },
        "limit": {"max_pairwise_ratio": limit, "percent": limit_percent, "passed": not failures},
        "source_reuse": source_reuse(plan, fps),
        "status": "FAIL" if failures else "PASS",
        "failures": failures,
    }
    write_json(out_path, report)
    print(f"\nmax pairwise visual duplication: "
          f"{report['max_pairwise_visual_duplication']['percent']:.2f}% "
          f"(limit {limit_percent:g}%)")
    print(f"{report['status']}: {out_path}")
    for failure in failures:
        print(f"  - {failure}")
    return 2 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
