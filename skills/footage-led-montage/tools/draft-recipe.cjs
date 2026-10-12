#!/usr/bin/env node
'use strict';

// Draft a recipe from a new reference video.
//
// The reusable part of a recreation is a handful of numbers - the delivery canvas, how much of the
// reference becomes the body, how long the tail runs - and they used to be re-measured by hand for
// every new reference. This measures them once, writes the evidence down next to each one, and
// lists what it cannot measure, so drafting is a review of numbers instead of a re-analysis.
//
// Cut candidates come from `hypit media boundaries`, the tool the project already trusts, sampled
// at the frame rate so each candidate carries its source frame number.
//
//   node draft-recipe.cjs --reference resource/videos/<category>/<ref>.mp4 \
//       --tail resource/end.mp4 --canvas 720x1280 --out recipe-draft.json
//
// Writes only the file named by --out. Needs ffprobe and the hypit CLI.

const path = require('node:path');
const { execFileSync } = require('node:child_process');

const { sha256, probe, writeJson } = require('./lib/media.cjs');

const DEFAULT_CROSSFADE_FRAMES = 12;
const DEFAULT_THRESHOLD = 0.1;

function parseArgs(argv) {
  const args = {};
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (!item.startsWith('--')) throw new Error(`unexpected argument ${item}`);
    const key = item.slice(2).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
    const value = argv[index + 1];
    if (value === undefined || value.startsWith('--')) throw new Error(`${item} needs a value`);
    args[key] = value;
    index += 1;
  }
  return args;
}

// A script path runs through the current node; a bare name is looked up on PATH.
function hypit(bin, args) {
  const output = bin.endsWith('.mjs') || bin.endsWith('.js')
    ? execFileSync(process.execPath, [bin, ...args], { encoding: 'utf8' })
    : execFileSync(bin, args, { encoding: 'utf8' });
  return JSON.parse(output);
}

function countedFrames(file) {
  const raw = execFileSync('ffprobe', ['-v', 'error', '-select_streams', 'v:0', '-count_frames',
    '-show_entries', 'stream=nb_read_frames', '-of', 'json', file], { encoding: 'utf8' });
  const frames = Number(JSON.parse(raw).streams[0]?.nb_read_frames);
  if (!Number.isSafeInteger(frames) || frames <= 0) throw new Error(`${file}: no frame count`);
  return frames;
}

// What the reference actually is, as opposed to what the delivery will be.
function describe(file) {
  const info = probe(file);
  const video = info.streams.find((stream) => stream.codec_type === 'video');
  if (video === undefined) throw new Error(`${file}: no video stream`);
  const audio = info.streams.find((stream) => stream.codec_type === 'audio');
  const [numerator, denominator] = String(video.r_frame_rate).split('/').map(Number);
  return {
    path: path.resolve(file),
    sha256: sha256(file),
    width: video.width,
    height: video.height,
    fps: Math.round(numerator / (denominator || 1)),
    frames: countedFrames(file),
    duration_seconds: Number(info.format.duration),
    has_audio: audio !== undefined,
    audio_seconds: audio === undefined ? null : Number(audio.duration ?? info.format.duration),
  };
}

function parseCanvas(raw) {
  const match = /^(\d+)x(\d+)$/.exec(raw);
  if (match === null) throw new Error(`--canvas needs <width>x<height>, got ${raw}`);
  return { width: Number(match[1]), height: Number(match[2]) };
}

// Everything the measurements cannot decide, with the reason and what to do about it.
function confirmations({ reference, tail, strongest, bodySegments, crossfadeFrames, canvasSource }) {
  const items = [
    {
      field: 'canvas',
      why: 'The delivery canvas is a platform choice, not a property of the reference video.',
      hint: canvasSource === 'given'
        ? 'Recorded from --canvas.'
        : `Filled with the reference's native ${reference.width}x${reference.height}; pass --canvas <WxH> for the size actually delivered.`,
    },
    {
      field: 'timeline.body_frames',
      why: 'Derived from the strongest visual-change candidate, which is where the reference leaves its body.',
      hint: strongest === null
        ? 'No candidate was found; set the body length by hand.'
        : `Body ends at frame ${strongest.frame} (${strongest.at} s, score ${strongest.score}) with ${bodySegments} reference segments before it. The approved corn recipe keeps a 3-frame margin before its cut, so a value a few frames lower is expected when the next shot would otherwise bleed in.`,
    },
    {
      field: 'timeline.tail_frames',
      why: 'The tail replaces the reference ending with the shared end card.',
      hint: tail === null
        ? 'Pass --tail <video> to measure it; the value is round(tail seconds x fps).'
        : `Measured from ${tail.path}: ${tail.duration_seconds} s.`,
    },
    {
      field: 'timeline.crossfade_frames',
      why: 'A house style constant, not measured from the reference.',
      hint: `Default ${crossfadeFrames} frames (0.4 s at ${reference.fps} fps); override with --crossfade-frames.`,
    },
    {
      field: 'categories and asset_roots',
      why: 'Product category, source categories and the footage library are business decisions.',
      hint: 'Name the delivered category, the categories shots may come from, the excluded ones, and the external library root.',
    },
    {
      field: 'locked',
      why: 'Every locked asset is produced by another step and pinned by content hash.',
      hint: 'narration: the approved voice-over and its word timings (a reference transcript comes from `hypit transcribe <ref> --language zh`); music_bed: the separated backing track; tail: the end card video and the audio interval it takes; graphics: the alpha overlay from graphics/extract-overlay.py.',
    },
    {
      field: 'audio_mix and batch',
      why: 'Mix gains and the duplicate policy carry the approvals of the previous batch.',
      hint: 'Reuse the previous recipe values unless the voice or the platform changed.',
    },
    {
      field: 'id, paths and runner_sha256',
      why: 'Identity, where the plan and reports live, and which runner is allowed to execute this recipe.',
      hint: 'Set id and paths by hand; take runner_sha256 from `node render.cjs --print-runner-hash` once the runner is approved.',
    },
  ];
  return items;
}

function printSummary(draft, out) {
  const { reference, tail, cuts, recipe_draft: recipe } = draft;
  const lines = [
    `reference  ${reference.width}x${reference.height} ${reference.fps} fps, ${reference.frames} frames, `
      + `${reference.duration_seconds} s${reference.has_audio ? `, audio ${reference.audio_seconds} s` : ', no audio'}`,
    `canvas     ${recipe.canvas.width}x${recipe.canvas.height} (${draft.canvas_source})`,
    `cuts       ${cuts.candidates.length} candidates (${cuts.sample_rate} samples/s, threshold ${cuts.threshold})`,
  ];
  if (cuts.body_end !== null) {
    lines.push(`body       frame ${cuts.body_end.frame} (${cuts.body_end.at} s, score ${cuts.body_end.score}), `
      + `${cuts.reference_segments} reference segments`);
  }
  lines.push(tail === null
    ? 'tail       not measured; pass --tail <video>'
    : `tail       ${path.basename(tail.path)} ${recipe.timeline.tail_frames} frames, ${tail.duration_seconds} s`);
  lines.push(`crossfade  ${recipe.timeline.crossfade_frames} frames`);
  lines.push(`wrote      ${out}`);
  lines.push(`confirm    ${draft.confirm.length} items: ${draft.confirm.map((item) => item.field).join(', ')}`);
  process.stdout.write(`${lines.join('\n')}\n`);
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.reference === undefined) throw new Error('Pass --reference <video>');
  if (args.out === undefined) throw new Error('Pass --out <recipe-draft.json>');
  const bin = args.hypit ?? 'hypit';

  const reference = describe(args.reference);
  const fps = args.fps === undefined ? reference.fps : Number(args.fps);
  const canvasSource = args.canvas === undefined ? 'reference native' : 'given';
  const canvas = args.canvas === undefined
    ? { width: reference.width, height: reference.height }
    : parseCanvas(args.canvas);
  const crossfadeFrames = args.crossfadeFrames === undefined
    ? DEFAULT_CROSSFADE_FRAMES : Number(args.crossfadeFrames);
  const rate = args.rate === undefined ? fps : Number(args.rate);
  const threshold = args.threshold === undefined ? DEFAULT_THRESHOLD : Number(args.threshold);

  const boundaries = hypit(bin, ['media', 'boundaries', reference.path,
    '--rate', String(rate), '--threshold', String(threshold), '--json']);
  const candidates = boundaries.candidates.map((candidate) => ({
    at: candidate.at,
    frame: Math.min(reference.frames, Math.round(candidate.at * fps)),
    score: candidate.score,
  }));
  const strongest = candidates.reduce(
    (best, item) => (best === null || item.score > best.score ? item : best), null);
  const bodyFrames = strongest === null ? null : strongest.frame;
  const bodySegments = bodyFrames === null
    ? null : candidates.filter((item) => item.frame < bodyFrames).length + 1;

  const tail = args.tail === undefined ? null : describe(args.tail);
  const tailFrames = tail === null ? null : Math.round(tail.duration_seconds * fps);

  const draft = {
    draft_version: '1.0',
    tool: 'draft-recipe.cjs',
    generated_at: new Date().toISOString(),
    reference,
    tail,
    cuts: {
      tool: `hypit media boundaries --rate ${rate} --threshold ${threshold}`,
      sample_rate: rate,
      threshold,
      candidates,
      body_end: strongest,
      reference_segments: bodySegments,
    },
    canvas_source: canvasSource,
    recipe_draft: {
      version: '1.0',
      id: null,
      supersedes: null,
      runner_sha256: null,
      canvas: { width: canvas.width, height: canvas.height, fps },
      timeline: { body_frames: bodyFrames, tail_frames: tailFrames, crossfade_frames: crossfadeFrames },
      categories: null,
      asset_roots: null,
      locked: null,
      audio_mix: null,
      batch: null,
      paths: null,
    },
    confirm: confirmations({ reference, tail, strongest, bodySegments, crossfadeFrames, canvasSource }),
  };

  const out = path.resolve(args.out);
  writeJson(out, draft);
  printSummary(draft, out);
}

main();
