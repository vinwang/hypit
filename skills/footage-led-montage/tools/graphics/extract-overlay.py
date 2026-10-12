#!/usr/bin/env python3
"""Extract the lettering layer of a reference video out of its own frames.

The layer is the reference video's own graphics - captions, the top status
strip, the link panel - recovered as an alpha channel over the source colours,
so a re-cut video can carry the same lettering instead of rebuilding the type
from scratch.  That rebuild is where the debugging used to go.

Everything that was a hand-tuned constant is a parameter: the fixed UI
rectangles, the glyph threshold and dilation, and the caption band, which is
derived from the OCR boxes instead of being written down as a fraction of the
frame.  The derivation is reported together with every cluster it considered,
so a wrong pick is visible rather than silent.

    extract-overlay.py --params projects/<project>/tools/overlay-params.json \
        --root projects/<project>

Writes one RGBA PNG per source frame, optionally an alpha QuickTime movie for
the renderer, and a self-check report whose exposure count must be zero.
Requires ffmpeg.  Standard library only.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import glyphs


def mov_pattern(names):
    """A frame pattern for the movie encoder, derived from the actual names."""
    stem = os.path.splitext(names[0])[0]
    if not stem.isdigit():
        raise SystemExit(f"{names[0]}: cannot build a frame pattern from this name")
    return f"%0{len(stem)}d.png"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--params", required=True, help="graphics-layer parameter file")
    parser.add_argument("--root", default=os.getcwd(),
                        help="directory the parameter file's paths are relative to (default: working directory)")
    parser.add_argument("--out-dir", help="where the overlay frames are written (default: the parameter file's out_dir)")
    parser.add_argument("--report", help="self-check report (default: next to the parameter file)")
    parser.add_argument("--mov", help="also encode the layer as an alpha QuickTime movie")
    parser.add_argument("--mov-fps", type=float, help="frame rate of that movie; required with --mov")
    parser.add_argument("--limit", type=int, help="only process the first N frames (smoke runs)")
    args = parser.parse_args()

    params_path = os.path.abspath(args.params)
    root = os.path.abspath(args.root)
    params = glyphs.read_params(params_path)
    frame = params["frame"]
    glyph = params["glyph"]
    padding = params["box_padding"]
    fixed = params["fixed_regions"]
    panel = params["panel_regions"]

    records = glyphs.read_jsonl(os.path.join(root, params["boxes"]))
    if params["caption_band"] is None:
        band, clusters, chosen = glyphs.derive_caption_band(records, frame, padding, fixed, panel)
        band_source = "derived"
    else:
        band, clusters, chosen = params["caption_band"], None, None
        band_source = "declared"
    captions, filled = glyphs.caption_lines(records, band)

    frames_dir = os.path.join(root, params["frames_dir"])
    out_dir = os.path.abspath(args.out_dir) if args.out_dir else os.path.join(root, params["out_dir"])
    report_path = (os.path.abspath(args.report) if args.report
                   else os.path.join(os.path.dirname(params_path), "overlay-report.json"))
    os.makedirs(out_dir, exist_ok=True)

    available = glyphs.frame_files(frames_dir)
    names = available[:args.limit] if args.limit else available
    missing = [name for name in names if name not in captions]
    if missing:
        raise SystemExit(f"{len(missing)} frames have no OCR record, starting with {missing[0]}")

    totals = {"exposed_pixels": 0, "leaked_pixels": 0, "nonbinary_pixels": 0, "panel_gaps": 0}
    exposed_rects = []
    started = time.time()
    for index, name in enumerate(names, 1):
        source_path = os.path.join(frames_dir, name)
        source_rgb = glyphs.read_rgb(source_path, frame["width"], frame["height"])
        regions = glyphs.frame_regions(frame, captions, name, fixed, panel, padding)
        mask = bytes(glyphs.compose_mask(source_rgb, frame, regions, glyph))
        glyphs.write_overlay(source_path, mask, frame["width"], frame["height"], os.path.join(out_dir, name))
        findings = glyphs.audit_frame(source_rgb, mask, frame, regions, glyph)
        for key in totals:
            totals[key] += findings[key]
        exposed_rects.extend(findings["exposed_rects"])
        if index % 25 == 0 or index == len(names):
            print(f"  {index}/{len(names)} frames, {time.time() - started:.0f}s", flush=True)

    movie = None
    if args.mov:
        if not args.mov_fps:
            raise SystemExit("--mov needs --mov-fps")
        movie = os.path.abspath(args.mov)
        os.makedirs(os.path.dirname(movie), exist_ok=True)
        glyphs.ffmpeg(["-y", "-framerate", str(args.mov_fps), "-i", os.path.join(out_dir, mov_pattern(names)),
                       "-c:v", "qtrle", "-pix_fmt", "argb", movie])

    failures = glyphs.audit_failures(totals)
    report = {
        "tool": "extract-overlay.py",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "params": {"path": os.path.relpath(params_path, root), "sha256": glyphs.sha256_file(params_path)},
        "frame": frame,
        "glyph": glyph,
        "box_padding": padding,
        "fixed_regions": fixed,
        "panel_regions": panel,
        "caption_band": {"value": band, "source": band_source,
                         "chosen_cluster": chosen, "clusters": clusters},
        "frames": {"available": len(available), "processed": len(names),
                   "captions_in_band": len(available) - filled, "filled_from_nearest_frame": filled},
        "outputs": {"dir": out_dir, "frames": len(names), "format": "rgba png", "movie": movie},
        "checks": totals,
        "exposed_rects": exposed_rects[:8],
        "status": "FAIL" if failures else "PASS",
        "failures": failures,
    }
    glyphs.write_json(report_path, report)

    print(f"caption band {band} ({band_source})")
    if chosen:
        print(f"  chosen cluster: {chosen}")
    print(f"  checks: {totals}")
    print(f"{report['status']}: {report_path}")
    for failure in failures:
        print(f"  - {failure}")
    return 2 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
