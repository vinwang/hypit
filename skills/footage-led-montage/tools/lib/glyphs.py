#!/usr/bin/env python3
"""The pixel criterion shared by the graphics extractor and its QA gate.

The extractor decides which source pixels belong to the lettering layer; the
QA gate then proves the produced alpha covers exactly those pixels and nothing
else.  Both must use the same rule, so the rule lives here once: a pixel is
lettering when its darkest channel is at least `min_channel` and its channel
spread is at most `max_spread`, dilated by `dilation` so the layer covers the
anti-aliased edge of the glyph and not only its core.  The audit built on that
criterion lives here too, so the extractor's inline self-check and the
standalone gate cannot drift apart.

Frames and overlays are decoded through ffmpeg, never through an image
library, so the tools run with the standard library alone.

Masks are carried as one integer per row, one bit per pixel, the leftmost
pixel in the least significant bit.  Dilation is then a handful of shift-and-or
operations on big integers instead of a Python loop over every pixel.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess

# Byte value -> its eight pixels, for turning a bit row back into a gray row.
EXPAND = [bytes(255 if (value >> bit) & 1 else 0 for bit in range(8)) for value in range(256)]
# Alpha byte -> 1 when it is neither fully transparent nor fully opaque.
NONBINARY = bytes(0 if value in (0, 255) else 1 for value in range(256))


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()

DEFAULT_GLYPH = {"min_channel": 174, "max_spread": 45, "dilation": 7}
DEFAULT_PADDING = {"left": 2, "top": 2, "right": 3, "bottom": 3}
# Two lines belong to the same cluster when their baselines are this close,
# in normalised frame height.
CLUSTER_TOLERANCE = 0.02


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path):
    """One JSON object per line, as the OCR evidence is written."""
    records = []
    with open(path, "r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise SystemExit(f"{path}:{number}: {error}")
    return records


def write_json(path, value):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def read_params(path):
    """Load a graphics-layer parameter file and fill in the defaults."""
    params = read_json(path)
    params.setdefault("glyph", {})
    for key, value in DEFAULT_GLYPH.items():
        params["glyph"].setdefault(key, value)
    params.setdefault("box_padding", {})
    for key, value in DEFAULT_PADDING.items():
        params["box_padding"].setdefault(key, value)
    params.setdefault("fixed_regions", [])
    params.setdefault("panel_regions", [])
    params.setdefault("caption_band", None)
    for key in ("frames_dir", "boxes", "out_dir", "frame"):
        if key not in params:
            raise SystemExit(f"{path}: missing {key!r}")
    return params


# --- decoding -----------------------------------------------------------------

def ffmpeg(args, **kwargs):
    return subprocess.run(["ffmpeg", "-v", "error", *args], check=True, **kwargs)


def read_rgb(path, width, height):
    result = ffmpeg(["-i", path, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
    if len(result.stdout) != width * height * 3:
        raise SystemExit(f"{path}: decoded {len(result.stdout)} bytes, expected {width * height * 3}")
    return result.stdout


def read_alpha(path, width, height):
    """The alpha channel of an RGBA image, as one gray byte per pixel."""
    result = ffmpeg(["-i", path, "-vf", "alphaextract", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                    capture_output=True)
    if len(result.stdout) != width * height:
        raise SystemExit(f"{path}: decoded {len(result.stdout)} alpha bytes, expected {width * height}")
    return result.stdout


def write_overlay(rgb_source, alpha, width, height, out_path):
    """The source's colours with the extracted layer as its alpha channel."""
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    ffmpeg(["-y", "-i", rgb_source, "-f", "rawvideo", "-pix_fmt", "gray",
            "-s", f"{width}x{height}", "-i", "-",
            "-filter_complex", "[0:v]format=rgba[rgb];[1:v]format=gray[a];[rgb][a]alphamerge[out]",
            "-map", "[out]", "-frames:v", "1", out_path], input=alpha)


# --- geometry -----------------------------------------------------------------

def box_rect(box, frame, padding):
    """An OCR box as a half-open pixel rectangle.

    OCR boxes are normalised with the origin at the bottom left: `box[1]` is the
    bottom edge measured from the bottom, so the top edge in image coordinates
    is `1 - box[1] - box[3]`.  The padding is asymmetric on purpose - the
    approved layer reaches further right and down than left and up.
    """
    width, height = frame["width"], frame["height"]
    x, y, box_width, box_height = box
    return (
        max(0, int(x * width) - padding["left"]),
        max(0, int((1 - y - box_height) * height) - padding["top"]),
        min(width, int((x + box_width) * width) + padding["right"]),
        min(height, int((1 - y) * height) + padding["bottom"]),
    )


def inside(rect, outer):
    return (rect[0] >= outer[0] and rect[1] >= outer[1]
            and rect[2] <= outer[2] and rect[3] <= outer[3])


def grow(rect, radius, width, height):
    return (max(0, rect[0] - radius), max(0, rect[1] - radius),
            min(width, rect[2] + radius), min(height, rect[3] + radius))


# --- the glyph criterion ------------------------------------------------------

def glyph_rows(rgb, frame_width, rect, glyph):
    """Rows of bit masks for the lettering inside `rect`, before dilation."""
    x1, y1, x2, y2 = rect
    width = x2 - x1
    minimum = glyph["min_channel"]
    spread = glyph["max_spread"]
    rows = []
    for y in range(y1, y2):
        base = (y * frame_width + x1) * 3
        row = rgb[base:base + width * 3]
        bits = 0
        for index, (red, green, blue) in enumerate(zip(row[0::3], row[1::3], row[2::3])):
            low = red if red < green else green
            if blue < low:
                low = blue
            high = red if red > green else blue
            if green > high:
                high = green
            if low >= minimum and high - low <= spread:
                bits |= 1 << index
        rows.append(bits)
    return rows


def dilate_rows(rows, dilation):
    """A square max filter, the equivalent of PIL's MaxFilter over the region."""
    radius = dilation // 2
    if radius == 0:
        return list(rows)
    horizontal = []
    for bits in rows:
        spread = bits
        for shift in range(1, radius + 1):
            spread |= (bits << shift) | (bits >> shift)
        horizontal.append(spread)
    dilated = []
    for index in range(len(rows)):
        spread = 0
        for offset in range(max(0, index - radius), min(len(rows), index + radius + 1)):
            spread |= horizontal[offset]
        dilated.append(spread)
    return dilated


def rows_to_bytes(rows, width):
    """Bit rows back to one gray byte per pixel, truncated to the region width.

    Dilation can set bits past the last pixel of a row; the criterion clips the
    window there, so those bits are dropped.  The byte count is sized to hold
    the value rather than to hold the width, or a wide row would overflow, and
    never falls below the width, or a narrow row would come back short.
    """
    minimum = (width + 7) // 8
    out = []
    for bits in rows:
        byte_count = max(minimum, (bits.bit_length() + 7) // 8)
        out.append(b"".join(EXPAND[byte] for byte in bits.to_bytes(byte_count, "little"))[:width])
    return b"".join(out)


def count_bits(rows):
    return sum(bin(bits).count("1") for bits in rows)


def set_bits(bits):
    """The indices of the set bits of one row, lowest first."""
    while bits:
        lowest = bits & -bits
        yield lowest.bit_length() - 1
        bits ^= lowest


def layer_rows(rgb, frame_width, rect, glyph):
    return dilate_rows(glyph_rows(rgb, frame_width, rect, glyph), glyph["dilation"])


# --- compositing --------------------------------------------------------------

def paste_rows(mask, rows, width, rect):
    """Copy a region's rows into the full-frame mask, replacing what was there.

    Replacing, not merging, is what the approved layer does: a later region
    painted over an earlier one clears it.  Region order therefore matters and
    is preserved by the caller.
    """
    x1, y1, x2, y2 = rect
    region_width = x2 - x1
    for offset, y in enumerate(range(y1, y2)):
        start = y * width + x1
        mask[start:start + region_width] = rows_to_bytes([rows[offset]], region_width)


def fill_rect(mask, width, rect):
    x1, y1, x2, y2 = rect
    band = b"\xff" * (x2 - x1)
    for y in range(y1, y2):
        mask[y * width + x1:y * width + x2] = band


def empty_mask(width, height):
    return bytearray(width * height)


def compose_mask(source_rgb, frame, regions, glyph):
    """The layer the criterion defines, for one frame.

    Glyph regions are painted in order and each replaces what was under it, so
    a later caption box clears an earlier region where they overlap; the panels
    are painted last and solid.  Region order is therefore part of the layer's
    definition, not an implementation detail.
    """
    width, height = frame["width"], frame["height"]
    mask = empty_mask(width, height)
    for rect in regions["fixed"] + regions["caption"]:
        paste_rows(mask, layer_rows(source_rgb, width, rect, glyph), width, rect)
    for rect in regions["panel"]:
        fill_rect(mask, width, rect)
    return mask


def frame_regions(frame, captions, name, fixed_regions, panel_regions, padding):
    """The rectangles one frame's layer is allowed to touch."""
    return {
        "fixed": list(fixed_regions),
        "caption": [box_rect(line["box"], frame, padding) for line in captions[name]],
        "panel": list(panel_regions),
    }


# --- auditing -----------------------------------------------------------------

def row_intervals(rects, y, width):
    """The x spans of the rectangles covering row `y`, merged where they overlap."""
    spans = sorted((max(0, rect[0]), min(width, rect[2])) for rect in rects if rect[1] <= y < rect[3])
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def leaked_pixels(alpha, width, height, rects):
    """Opaque pixels outside every declared rectangle.

    Counted row by row over the complement of the merged spans, so overlapping
    rectangles are never counted twice and the frame is never materialised as a
    second full-size mask.
    """
    leaked = 0
    for y in range(height):
        row = alpha[y * width:(y + 1) * width]
        cursor = 0
        for start, end in row_intervals(rects, y, width):
            leaked += row[cursor:start].count(255)
            cursor = end
        leaked += row[cursor:].count(255)
    return leaked


def audit_frame(source_rgb, alpha, frame, regions, glyph):
    """Coverage findings for one frame's layer.

    Each finding is a way the layer could be wrong while still looking right:
    exposure is lettering the layer was meant to hide but left visible,
    leakage is opacity where no region declares any, and a panel gap or a
    half-transparent pixel means the layer is not the binary shape it claims.

    The layer is a dilation, so a glyph region legitimately reaches `radius`
    beyond its own rectangle; opacity past that cannot be explained by the
    criterion and is what leakage measures.
    """
    width, height = frame["width"], frame["height"]
    radius = glyph["dilation"] // 2
    allowed = [grow(rect, radius, width, height) for rect in regions["fixed"] + regions["caption"]]
    allowed += list(regions["panel"])
    findings = {
        "exposed_pixels": 0,
        "exposed_rects": [],
        "leaked_pixels": leaked_pixels(alpha, width, height, allowed),
        "nonbinary_pixels": alpha.translate(NONBINARY).count(1),
        "panel_gaps": 0,
    }
    for rect in regions["fixed"] + regions["caption"]:
        exposed = 0
        for offset, bits in enumerate(glyph_rows(source_rgb, width, rect, glyph)):
            if not bits:
                continue
            base = (rect[1] + offset) * width + rect[0]
            for index in set_bits(bits):
                if alpha[base + index] != 255:
                    exposed += 1
        if exposed:
            findings["exposed_pixels"] += exposed
            if len(findings["exposed_rects"]) < 8:
                findings["exposed_rects"].append({"rect": list(rect), "pixels": exposed})
    for x1, y1, x2, y2 in regions["panel"]:
        for y in range(y1, y2):
            findings["panel_gaps"] += (x2 - x1) - alpha[y * width + x1:y * width + x2].count(255)
    return findings


def audit_failures(totals):
    failures = []
    if totals["exposed_pixels"]:
        failures.append(f"{totals['exposed_pixels']} lettering pixels are still visible through the layer")
    if totals["leaked_pixels"]:
        failures.append(f"{totals['leaked_pixels']} opaque pixels lie outside every declared region")
    if totals["nonbinary_pixels"]:
        failures.append(f"{totals['nonbinary_pixels']} alpha pixels are neither 0 nor 255")
    if totals["panel_gaps"]:
        failures.append(f"{totals['panel_gaps']} pixels of a declared panel are not covered")
    return failures


# --- caption band derivation --------------------------------------------------

def caption_clusters(records, frame, padding):
    """Group the OCR lines by where they sit vertically, and describe each group.

    Burned-in captions are the largest type on screen once the declared UI
    rectangles are set aside, which is how the band is derived: no hardcoded
    fraction survives a change of reference video, but the choice is reported
    with every alternative so a wrong pick is visible.
    """
    lines = [(record["file"], line) for record in records for line in record["lines"]]
    positions = sorted({round(line["box"][1], 4) for _, line in lines})
    groups = []
    for position in positions:
        if groups and position - groups[-1][-1] <= CLUSTER_TOLERANCE:
            groups[-1].append(position)
        else:
            groups.append([position])

    def group_index(position):
        for index, group in enumerate(groups):
            if group[0] - 1e-9 <= position <= group[-1] + 1e-9:
                return index
        return None

    buckets = {}
    for name, line in lines:
        buckets.setdefault(group_index(round(line["box"][1], 4)), []).append((name, line))

    clusters = []
    for index in sorted(buckets):
        items = buckets[index]
        heights = sorted(line["box"][3] for _, line in items)
        rects = [box_rect(line["box"], frame, padding) for _, line in items]
        ui_lines = sum(1 for rect in rects
                       if any(inside(rect, outer) for outer in frame["_ui_rects"]))
        clusters.append({
            "index": index,
            "band": [min(line["box"][1] for _, line in items), max(line["box"][1] for _, line in items)],
            "lines": len(items),
            "frames": len({name for name, _ in items}),
            "median_height": heights[len(heights) // 2],
            "median_width": sorted(line["box"][2] for _, line in items)[len(items) // 2],
            "median_centre_x": sorted(line["box"][0] + line["box"][2] / 2 for _, line in items)[len(items) // 2],
            "inside_declared_ui": ui_lines == len(items),
            "sample": sorted({line["text"] for _, line in items}, key=len)[-1][:24],
        })
    return clusters


def derive_caption_band(records, frame, padding, fixed_regions, panel_regions):
    frame = dict(frame, _ui_rects=list(fixed_regions) + list(panel_regions))
    clusters = caption_clusters(records, frame, padding)
    candidates = [cluster for cluster in clusters if not cluster["inside_declared_ui"]]
    if not candidates:
        raise SystemExit("every OCR cluster sits inside a declared UI rectangle: "
                         "declare the caption band explicitly")
    chosen = max(candidates, key=lambda cluster: (cluster["median_height"], cluster["lines"]))
    return chosen["band"], clusters, chosen


def caption_lines(records, band):
    """Per frame, the caption lines, with OCR misses filled from the nearest frame.

    A compressed frame can lose its caption entirely; its neighbours still
    locate the original ink, and leaving the layer transparent there would show
    the source video's own subtitle through.
    """
    captions = {}
    for record in records:
        name = os.path.basename(record["file"])
        captions[name] = [line for line in record["lines"] if band[0] <= line["box"][1] <= band[1]]
    recognized = [(int(os.path.splitext(name)[0]), lines) for name, lines in captions.items() if lines]
    if not recognized:
        raise SystemExit("no frame carries a caption inside the derived band")
    filled = 0
    for name, lines in captions.items():
        if not lines:
            index = int(os.path.splitext(name)[0])
            captions[name] = min(recognized, key=lambda item: abs(item[0] - index))[1]
            filled += 1
    return captions, filled


def frame_files(frames_dir):
    names = sorted(name for name in os.listdir(frames_dir) if name.lower().endswith(".png"))
    if not names:
        raise SystemExit(f"{frames_dir}: no PNG frames")
    return names
