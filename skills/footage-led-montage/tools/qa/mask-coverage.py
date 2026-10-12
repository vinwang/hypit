#!/usr/bin/env python3
"""The pixel gate on an extracted graphics layer.

Reads the same parameter file the extractor read, re-derives the coverage that
file declares, and checks the produced overlays frame by frame.  Nothing here
trusts the extractor's own report: the criterion, the declared regions and the
findings all come from lib/glyphs.py, so the gate and the extractor cannot
disagree about what the layer is supposed to be.

    mask-coverage.py --params projects/<project>/tools/overlay-params.json \
        --root projects/<project>
    mask-coverage.py --selftest

Requires ffmpeg.  Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import glyphs


def audit(params, root, overlay_dir, limit=None, quiet=False):
    """Check every overlay against the coverage the parameter file declares."""
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
    available = glyphs.frame_files(frames_dir)
    names = available[:limit] if limit else available
    totals = {"exposed_pixels": 0, "leaked_pixels": 0, "nonbinary_pixels": 0, "panel_gaps": 0}
    defective = []
    for name in names:
        source_rgb = glyphs.read_rgb(os.path.join(frames_dir, name), frame["width"], frame["height"])
        alpha = glyphs.read_alpha(os.path.join(overlay_dir, name), frame["width"], frame["height"])
        regions = glyphs.frame_regions(frame, captions, name, fixed, panel, padding)
        findings = glyphs.audit_frame(source_rgb, alpha, frame, regions, glyph)
        for key in totals:
            totals[key] += findings[key]
        if any(findings[key] for key in totals):
            defective.append({"frame": name, **{key: value for key, value in findings.items()
                                                if key != "exposed_rects"},
                              "exposed_rects": findings["exposed_rects"]})
    if not quiet:
        print(f"  {len(names)} frames checked, {len(defective)} with findings", flush=True)

    failures = glyphs.audit_failures(totals)
    return {
        "tool": "mask-coverage.py",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "params": {"path": os.path.relpath(os.path.abspath(params["_path"]), root),
                   "sha256": glyphs.sha256_file(params["_path"])},
        "overlay_dir": overlay_dir,
        "caption_band": {"value": band, "source": band_source,
                         "chosen_cluster": chosen, "clusters": clusters},
        "frames": {"available": len(available), "checked": len(names),
                   "captions_in_band": len(available) - filled, "filled_from_nearest_frame": filled},
        "checks": totals,
        "defective_frames": defective[:20],
        "status": "FAIL" if failures else "PASS",
        "failures": failures,
    }


def selftest():
    """The gate must pass a faithful layer and report each way one can be wrong.

    This proves the checker fires, not that any particular extraction is right;
    the extractor is judged by re-running a project whose layer is already
    approved and comparing pixels.
    """
    results = []

    def check(label, condition, detail=""):
        results.append((label, bool(condition), detail))

    with tempfile.TemporaryDirectory(prefix="mask-coverage-selftest-") as tmp:
        width, height = 64, 48
        frame = {"width": width, "height": height}
        glyph = dict(glyphs.DEFAULT_GLYPH)
        padding = {key: 0 for key in glyphs.DEFAULT_PADDING}
        frames_dir = os.path.join(tmp, "frames")
        overlays_dir = os.path.join(tmp, "overlays")
        os.makedirs(frames_dir)
        os.makedirs(overlays_dir)

        pixels = bytearray(width * height * 3)
        for x1, y1, x2, y2 in ((20, 10, 40, 20), (2, 2, 4, 4)):
            for y in range(y1, y2):
                for x in range(x1, x2):
                    start = (y * width + x) * 3
                    pixels[start:start + 3] = b"\xff\xff\xff"
        source_path = os.path.join(frames_dir, "001.png")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                        "-s", f"{width}x{height}", "-i", "-", "-frames:v", "1", source_path],
                       input=bytes(pixels), check=True)

        line = {"box": [20 / width, 1 - 20 / height, 20 / width, 10 / height],
                "confidence": 0.99, "text": "自检"}
        with open(os.path.join(tmp, "boxes.jsonl"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"file": "001.png", "lines": [line]}, ensure_ascii=False) + "\n")

        params_path = os.path.join(tmp, "params.json")
        glyphs.write_json(params_path, {
            "frames_dir": "frames", "boxes": "boxes.jsonl", "out_dir": "overlays", "frame": frame,
            "fixed_regions": [[0, 0, 8, 8]], "panel_regions": [[0, 40, 64, 48]],
            "glyph": glyph, "box_padding": padding, "caption_band": None,
        })
        params = glyphs.read_params(params_path)
        params["_path"] = params_path

        records = glyphs.read_jsonl(os.path.join(tmp, "boxes.jsonl"))
        band, _, _ = glyphs.derive_caption_band(records, frame, padding,
                                                params["fixed_regions"], params["panel_regions"])
        captions, _ = glyphs.caption_lines(records, band)
        regions = glyphs.frame_regions(frame, captions, "001.png",
                                       params["fixed_regions"], params["panel_regions"], padding)
        source_rgb = glyphs.read_rgb(source_path, width, height)
        faithful = bytes(glyphs.compose_mask(source_rgb, frame, regions, glyph))
        overlay_path = os.path.join(overlays_dir, "001.png")

        def write_and_audit(alpha):
            glyphs.write_overlay(source_path, alpha, width, height, overlay_path)
            return audit(params, tmp, overlays_dir, quiet=True)

        check("the derived band finds the only caption cluster",
              abs(band[0] - line["box"][1]) < 1e-9 and abs(band[1] - line["box"][1]) < 1e-9,
              f"band={band}")
        check("the caption rectangle is the OCR box",
              regions["caption"] == [(20, 10, 40, 20)], f"{regions['caption']}")

        good = write_and_audit(faithful)
        check("a faithful layer passes", good["status"] == "PASS", str(good["failures"]))

        exposed = bytearray(faithful)
        exposed[10 * width + 20] = 0
        bad_exposure = write_and_audit(bytes(exposed))
        check("a lettering pixel left transparent is reported",
              bad_exposure["status"] == "FAIL" and bad_exposure["checks"]["exposed_pixels"] > 0,
              f"exposed={bad_exposure['checks']['exposed_pixels']}")

        leaked = bytearray(faithful)
        leaked[30 * width + 60] = 255
        bad_leak = write_and_audit(bytes(leaked))
        check("opacity outside every declared region is reported",
              bad_leak["status"] == "FAIL" and bad_leak["checks"]["leaked_pixels"] > 0,
              f"leaked={bad_leak['checks']['leaked_pixels']}")

        soft = bytearray(faithful)
        soft[12 * width + 18] = 128
        bad_soft = write_and_audit(bytes(soft))
        check("a half-transparent pixel is reported",
              bad_soft["status"] == "FAIL" and bad_soft["checks"]["nonbinary_pixels"] > 0,
              f"nonbinary={bad_soft['checks']['nonbinary_pixels']}")

        gap = bytearray(faithful)
        gap[45 * width + 10] = 0
        bad_gap = write_and_audit(bytes(gap))
        check("a gap in a declared panel is reported",
              bad_gap["status"] == "FAIL" and bad_gap["checks"]["panel_gaps"] > 0,
              f"panel_gaps={bad_gap['checks']['panel_gaps']}")

    for label, passed, detail in results:
        print(f"{'PASS' if passed else 'FAIL'}  {label}" + (f"  [{detail}]" if detail and not passed else ""))
    return 0 if all(passed for _, passed, _ in results) else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--params", help="graphics-layer parameter file")
    parser.add_argument("--root", default=os.getcwd(),
                        help="directory the parameter file's paths are relative to (default: working directory)")
    parser.add_argument("--overlay-dir", help="where the overlays live (default: the parameter file's out_dir)")
    parser.add_argument("--report", help="where the report is written (default: next to the parameter file)")
    parser.add_argument("--limit", type=int, help="only check the first N frames (smoke runs)")
    parser.add_argument("--selftest", action="store_true", help="prove the gate reports defective layers")
    args = parser.parse_args()

    if args.selftest:
        return selftest()
    if not args.params:
        parser.error("--params is required unless --selftest is given")

    params_path = os.path.abspath(args.params)
    root = os.path.abspath(args.root)
    params = glyphs.read_params(params_path)
    params["_path"] = params_path
    overlay_dir = (os.path.abspath(args.overlay_dir) if args.overlay_dir
                   else os.path.join(root, params["out_dir"]))
    report_path = (os.path.abspath(args.report) if args.report
                   else os.path.join(os.path.dirname(params_path), "mask-coverage.json"))

    report = audit(params, root, overlay_dir, limit=args.limit)
    glyphs.write_json(report_path, report)
    print(f"checks: {report['checks']}")
    print(f"{report['status']}: {report_path}")
    for failure in report["failures"]:
        print(f"  - {failure}")
    return 2 if report["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
