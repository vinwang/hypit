#!/usr/bin/env node
'use strict';

// Shared footage-led montage renderer.
//
// Reads a recipe (the values that stay fixed across a batch) plus a batch plan (per-batch shot
// selection) and renders one locked output per variant. It is a parameterised extraction of the
// renderer that produced the approved corn batch: every ffmpeg filter graph is reproduced character
// for character, so lifting it out of that batch directory cannot change a rendered byte.
//
// Usage:
//   render.cjs --recipe <recipe.json> [--output-dir <dir>] [--work-dir <dir>] [--only <ids>]
//   render.cjs --print-runner-hash

const fs = require('node:fs');
const path = require('node:path');
const { readJson, writeJson, sha256, run, probe, measure, assertHash } = require('./lib/media.cjs');

function parseArgs(argv) {
  const args = { recipe: null, outputDir: null, workDir: null, qaDir: null, reportsDir: null, only: null, printRunnerHash: false };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === '--print-runner-hash') args.printRunnerHash = true;
    else if (flag === '--recipe') args.recipe = argv[++i];
    else if (flag === '--output-dir') args.outputDir = argv[++i];
    else if (flag === '--work-dir') args.workDir = argv[++i];
    else if (flag === '--qa-dir') args.qaDir = argv[++i];
    else if (flag === '--reports-dir') args.reportsDir = argv[++i];
    else if (flag === '--only') args.only = argv[++i].split(',').map(value => value.trim()).filter(Boolean);
    else throw new Error(`Unknown argument: ${flag}`);
  }
  return args;
}

// Everything the recipe decides, resolved to absolute paths once.
function loadContext(recipePath, overrides) {
  const recipeFile = path.resolve(recipePath);
  const recipe = readJson(recipeFile);
  if (recipe.version !== '1.0') throw new Error(`Unsupported recipe version: ${recipe.version}`);
  const projectRoot = path.dirname(recipeFile);
  const resolve = target => path.resolve(projectRoot, target);

  const { width, height, fps } = recipe.canvas;
  const { body_frames: bodyFrames, tail_frames: tailFrames, crossfade_frames: crossfadeFrames } = recipe.timeline;
  const totalFrames = bodyFrames + tailFrames;
  const naming = recipe.output_naming ?? {};
  const planPath = resolve(recipe.paths.plan);

  return {
    recipe,
    recipeFile,
    projectRoot,
    width,
    height,
    fps,
    bodyFrames,
    tailFrames,
    totalFrames,
    crossfadeFrames,
    // Produced by the same arithmetic as the approved renderer. These values reach ffmpeg as
    // strings, so the division must not be rearranged.
    bodySeconds: bodyFrames / fps,
    tailSeconds: tailFrames / fps,
    totalSeconds: totalFrames / fps,
    planPath,
    scriptPath: recipe.paths.script ? resolve(recipe.paths.script) : null,
    manifestPath: recipe.paths.manifest ? resolve(recipe.paths.manifest) : null,
    workRoot: overrides.workDir ? path.resolve(overrides.workDir) : resolve(recipe.paths.work),
    outputDir: overrides.outputDir ? path.resolve(overrides.outputDir) : path.dirname(planPath),
    reportsDir: overrides.reportsDir ? path.resolve(overrides.reportsDir) : (recipe.paths.reports ? resolve(recipe.paths.reports) : null),
    qaDir: overrides.qaDir ? path.resolve(overrides.qaDir) : (recipe.paths.qa ? resolve(recipe.paths.qa) : null),
    videosRoot: resolve(recipe.asset_roots.videos),
    voice: resolve(recipe.locked.narration.path),
    voiceMeta: recipe.locked.narration.meta_path ? resolve(recipe.locked.narration.meta_path) : null,
    musicBed: resolve(recipe.locked.music_bed.path),
    referenceAudio: recipe.locked.music_bed.reference_audio_path ? resolve(recipe.locked.music_bed.reference_audio_path) : null,
    tailVideo: resolve(recipe.locked.tail.video_path),
    tailAudio: resolve(recipe.locked.tail.audio_path),
    bodyOverlay: resolve(recipe.locked.graphics.body_overlay_path),
    tailOverlay: resolve(recipe.locked.graphics.tail_overlay_path),
    sharedAudio: resolve(recipe.audio_mix.render_path),
    naming: {
      timelinePrefix: naming.timeline_prefix ?? 'timeline-',
      qaSuffix: naming.qa_suffix ?? '',
      bodyFilename: naming.body_filename ?? 'body.mp4',
      reuseTimelinePrefix: naming.reuse_timeline_prefix ?? null,
      reuseBodyFilename: naming.reuse_body_filename ?? null,
    },
  };
}

function verifyLockedAssets(ctx) {
  const { recipe } = ctx;
  assertHash(ctx.voice, recipe.locked.narration.sha256, 'narration');
  assertHash(ctx.musicBed, recipe.locked.music_bed.sha256, 'music bed');
  assertHash(ctx.tailVideo, recipe.locked.tail.video_sha256, 'tail video');
  assertHash(ctx.tailAudio, recipe.locked.tail.audio_sha256, 'tail audio');
  assertHash(ctx.bodyOverlay, recipe.locked.graphics.body_overlay_sha256, 'body graphics overlay');
  assertHash(ctx.tailOverlay, recipe.locked.graphics.tail_overlay_sha256, 'tail graphics overlay');
  if (ctx.referenceAudio && recipe.locked.music_bed.reference_audio_sha256) {
    assertHash(ctx.referenceAudio, recipe.locked.music_bed.reference_audio_sha256, 'reference source audio');
  }
}

// The narration's word-level timestamps must spell the confirmed text. A matching script length is
// not evidence that the right audio is in place.
function verifyNarrationText(ctx) {
  if (!ctx.voiceMeta || !fs.existsSync(ctx.voiceMeta)) return null;
  const meta = readJson(ctx.voiceMeta);
  const data = meta.data ?? meta;
  if (!data.sentences) return null;
  const spoken = data.sentences.flatMap(sentence => sentence.words || []).map(word => word.word).join('').replace(/\s/g, '');
  if (spoken !== ctx.recipe.locked.narration.text.replace(/\s/g, '')) {
    throw new Error('Narration word timestamps do not match the confirmed text in the recipe');
  }
  return data.sentences.reduce((sum, sentence) => sum + sentence.words.length, 0);
}

function verifyPlanAgainstRecipe(ctx, plan) {
  const { batch } = ctx.recipe;
  if (plan.variants.length !== batch.variant_count) {
    throw new Error(`Plan holds ${plan.variants.length} variants but the recipe asks for ${batch.variant_count}`);
  }
  if (plan.requested_variants !== undefined && plan.requested_variants !== batch.variant_count) {
    throw new Error(`Plan requests ${plan.requested_variants} variants but the recipe asks for ${batch.variant_count}`);
  }
  if (plan.status !== undefined && !['locked_pre_render', 'rendered_visual_qa_pending'].includes(plan.status)) {
    throw new Error(`Plan status "${plan.status}" is not ready to render`);
  }
  for (const variant of plan.variants) {
    if (variant.shots.length !== batch.shots_per_variant) {
      throw new Error(`${variant.variant_id}: ${variant.shots.length} shots, recipe asks for ${batch.shots_per_variant}`);
    }
  }
  if (ctx.scriptPath && plan.copy_sha256 && sha256(ctx.scriptPath) !== plan.copy_sha256) {
    throw new Error('Confirmed script changed after the shots were locked');
  }
}

// Import every source into the project by content hash, then check the batch's footage reuse.
function lockAssets(ctx, plan, manifest) {
  const { categories } = ctx.recipe;
  const bookkeeping = ctx.recipe.manifest_record ?? {};
  const variantSets = {};
  const sceneUses = new Map();

  for (const variant of plan.variants) {
    if (variant.body_frames !== ctx.bodyFrames) throw new Error(`${variant.variant_id}: wrong body frame count`);
    // Each variant must draw from every source category the recipe lists: the approved batches mix
    // white and yellow corn deliberately, so a variant falling back to one category is a defect.
    if (variant.product_category !== categories.product_category ||
        !categories.source_categories.every(category => variant.source_categories.includes(category))) {
      throw new Error(`${variant.variant_id}: product or source categories do not match the recipe`);
    }
    if (new Set(variant.shots.map(shot => shot.original_sha256)).size !== variant.shots.length) {
      throw new Error(`${variant.variant_id}: a source file is repeated within this variant`);
    }
    if (variant.shots.reduce((sum, shot) => sum + shot.frames, 0) - (variant.shots.length - 1) * ctx.crossfadeFrames !== ctx.bodyFrames) {
      throw new Error(`${variant.variant_id}: shot lengths do not equal the locked body`);
    }

    const keys = [];
    for (const [index, shot] of variant.shots.entries()) {
      if (!variant.source_categories.includes(shot.category)) throw new Error(`${variant.variant_id}: shot category is not locked`);
      const uses = sceneUses.get(shot.continuous_scene_id) ?? [];
      uses.push({ variant_id: variant.variant_id, frames: shot.frames });
      sceneUses.set(shot.continuous_scene_id, uses);

      const originalPath = shot.original_path;
      if (!fs.existsSync(originalPath)) throw new Error(`Missing source: ${originalPath}`);
      if (sha256(originalPath) !== shot.original_sha256) throw new Error(`Source hash changed: ${originalPath}`);

      const key = shot.asset_key ?? `${variant.variant_id}-shot-${String(index + 1).padStart(2, '0')}`;
      const fixedPath = path.join(ctx.videosRoot, shot.category, `${ctx.recipe.snapshot_prefix ?? ''}${shot.original_sha256.slice(0, 16)}.mp4`);
      fs.mkdirSync(path.dirname(fixedPath), { recursive: true });
      // An earlier revision may have left a per-shot symlink; replace it with a real snapshot.
      const priorShotPath = shot.fixed_path;
      if (priorShotPath && priorShotPath !== fixedPath && fs.existsSync(priorShotPath) && fs.lstatSync(priorShotPath).isSymbolicLink() &&
          path.basename(priorShotPath).startsWith(`${variant.variant_id}-shot-`) && fs.realpathSync(priorShotPath) === fs.realpathSync(originalPath)) {
        fs.unlinkSync(priorShotPath);
      }
      if (!fs.existsSync(fixedPath)) fs.copyFileSync(originalPath, fixedPath, fs.constants.COPYFILE_EXCL);
      if (sha256(fixedPath) !== shot.original_sha256 || fs.lstatSync(fixedPath).isSymbolicLink()) {
        throw new Error(`Project snapshot failed its content lock: ${fixedPath}`);
      }
      if (manifest) {
        const record = {
          original_path: originalPath,
          fixed_path: fixedPath,
          sha256: shot.original_sha256,
          bytes: fs.statSync(originalPath).size,
          product_category: shot.category,
          source_kind: shot.source_kind,
          batch_id: plan.batch,
          variant_id: variant.variant_id,
          shot_index: index + 1,
          continuous_scene_id: shot.continuous_scene_id,
          source_interval_seconds: [shot.source_start_seconds, shot.source_end_seconds],
          crop: shot.crop ?? null,
        };
        const prior = manifest.items[key];
        if (prior && (prior.sha256 !== record.sha256 || (prior.fixed_path !== fixedPath && path.basename(prior.fixed_path) !== `${key}.mp4`))) {
          throw new Error(`Asset manifest lock conflict: ${key}`);
        }
        manifest.items[key] = record;
      }
      keys.push(key);
      shot.fixed_path = fixedPath;
      shot.asset_key = key;
    }
    variantSets[`${variant.variant_id}${bookkeeping.variant_set_suffix ?? ''}`] = {
      product_name: categories.product_category,
      source_categories: variant.source_categories,
      excluded_categories: categories.excluded_categories ?? [],
      asset_category: categories.asset_category,
      ...(bookkeeping.source_material_rule ? { source_material_rule: bookkeeping.source_material_rule } : {}),
      assets: keys,
    };
  }

  // Two variants sharing a continuous source scene count that scene's shorter use as duplicated.
  let maxPairwiseDuplicateRatio = 0;
  for (let left = 0; left < plan.variants.length; left++) {
    for (let right = left + 1; right < plan.variants.length; right++) {
      const leftId = plan.variants[left].variant_id;
      const rightId = plan.variants[right].variant_id;
      let duplicateFrames = 0;
      for (const uses of sceneUses.values()) {
        const leftUse = uses.find(use => use.variant_id === leftId);
        const rightUse = uses.find(use => use.variant_id === rightId);
        if (leftUse && rightUse) duplicateFrames += Math.min(leftUse.frames, rightUse.frames);
      }
      maxPairwiseDuplicateRatio = Math.max(maxPairwiseDuplicateRatio, duplicateFrames / ctx.bodyFrames);
    }
  }
  const limit = ctx.recipe.batch.duplicate_policy.max_pairwise_ratio;
  if (maxPairwiseDuplicateRatio > limit) {
    throw new Error(`Pairwise footage duplication exceeds the approved limit: ${maxPairwiseDuplicateRatio} > ${limit}`);
  }
  plan.max_pairwise_duplicate_ratio = maxPairwiseDuplicateRatio;
  if (manifest) {
    manifest.variant_asset_sets = { ...manifest.variant_asset_sets, ...variantSets };
    writeJson(ctx.manifestPath, manifest);
  }
  writeJson(ctx.planPath, plan);
  return maxPairwiseDuplicateRatio;
}

// Delivery encoding. Not a recipe value: every output owes the platform the same profile.
const ENCODE = [
  '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-pix_fmt', 'yuv420p',
  '-color_range', 'tv', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709',
];

// Cut each shot to its planned length, scale to fill the canvas, and join shots with a real
// overlapping crossfade.
function renderBody(ctx, cuts, workDir) {
  const { width, height, fps, crossfadeFrames, bodyFrames } = ctx;
  const args = ['-y', '-v', 'error'];
  for (const cut of cuts) args.push('-ss', cut.source_start_seconds.toFixed(6), '-i', cut.fixed_path);
  const graph = [];
  for (let i = 0; i < cuts.length; i++) {
    const cut = cuts[i];
    const crop = cut.crop ? `,crop=${cut.crop[2]}:${cut.crop[3]}:${cut.crop[0]}:${cut.crop[1]},scale=${width}:${height}:flags=lanczos` : '';
    graph.push(`[${i}:v]setpts=PTS-STARTPTS,fps=${fps},trim=end_frame=${cut.frames},settb=1/${fps},setpts=N,scale=${width}:${height}:force_original_aspect_ratio=increase:flags=lanczos,crop=${width}:${height}${crop},setsar=1,format=yuv420p[v${i}]`);
  }
  let previous = 'v0';
  let elapsedFrames = cuts[0].frames;
  for (let i = 1; i < cuts.length; i++) {
    const offsetFrames = elapsedFrames - crossfadeFrames;
    const next = `xf${i}`;
    graph.push(`[${previous}][v${i}]xfade=transition=fade:duration=${(crossfadeFrames / fps).toFixed(6)}:offset=${(offsetFrames / fps).toFixed(6)}[${next}]`);
    elapsedFrames += cuts[i].frames - crossfadeFrames;
    previous = next;
  }
  if (elapsedFrames !== bodyFrames) throw new Error('Body timeline does not match the locked frame count');
  graph.push(`[${previous}]trim=end_frame=${bodyFrames},settb=1/${fps},setpts=N,format=yuv420p[v]`);
  args.push('-filter_complex', graph.join(';'), '-map', '[v]', '-frames:v', String(bodyFrames), '-r', String(fps), '-an',
    ...ENCODE, path.join(workDir, ctx.naming.bodyFilename));
  run('ffmpeg', args);
}

function renderBase(ctx, workDir) {
  const { width, height, fps, tailFrames, totalFrames } = ctx;
  const graph = [
    `[0:v]settb=1/${fps},setpts=N[body]`,
    `[1:v]fps=${fps},scale=${width}:${height}:force_original_aspect_ratio=increase:flags=lanczos,crop=${width}:${height},setsar=1,trim=end_frame=${tailFrames},settb=1/${fps},setpts=N,format=yuv420p[tail]`,
    `[body][tail]concat=n=2:v=1:a=0,trim=end_frame=${totalFrames},settb=1/${fps},setpts=N[v]`,
  ].join(';');
  run('ffmpeg', ['-y', '-v', 'error', '-i', path.join(workDir, ctx.naming.bodyFilename), '-i', ctx.tailVideo,
    '-filter_complex', graph, '-map', '[v]', '-frames:v', String(totalFrames), '-r', String(fps), '-an',
    ...ENCODE, path.join(workDir, 'base-video.mp4')]);
}

// The graphics layers are pre-rendered alpha movies: the body layer covers the body, the tail layer
// takes over at the tail boundary. No text is drawn here.
function applyGraphics(ctx, workDir) {
  const seconds = ctx.bodySeconds.toFixed(6);
  const graph = [
    '[0:v]setpts=PTS-STARTPTS[base]',
    '[1:v]setpts=PTS-STARTPTS,format=rgba[bodygraphics]',
    '[2:v]setpts=PTS-STARTPTS,format=rgba[tailgraphics]',
    `[base][bodygraphics]overlay=0:0:enable='lt(t,${seconds})':format=auto[body]`,
    `[body][tailgraphics]overlay=0:0:enable='gte(t,${seconds})':format=auto,format=yuv420p,setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709[vout]`,
  ].join(';');
  run('ffmpeg', ['-y', '-v', 'error', '-i', path.join(workDir, 'base-video.mp4'), '-i', ctx.bodyOverlay, '-i', ctx.tailOverlay,
    '-filter_complex', graph, '-map', '[vout]', '-frames:v', String(ctx.totalFrames), '-r', String(ctx.fps), '-an',
    ...ENCODE, '-movflags', '+faststart', path.join(workDir, 'composited.mp4')]);
}

// One shared audio render for the whole batch: music bed under the narration across the body, then
// the locked tail audio.
function renderSharedAudio(ctx) {
  const { bodySeconds, tailSeconds } = ctx;
  const mix = ctx.recipe.audio_mix;
  const rate = mix.sample_rate ?? 48000;
  // Three decimals, so the approved 12 dB stays volume=3.981 rather than its full expansion.
  const voiceVolume = Math.pow(10, mix.voice_gain_db / 20).toFixed(3);
  const resample = `aresample=${rate},aformat=sample_rates=${rate}:channel_layouts=stereo`;
  const graph = [
    `[0:a]${resample},atrim=duration=` + bodySeconds + ',asetpts=PTS-STARTPTS[music]',
    `[1:a]${resample},atrim=duration=` + bodySeconds + `,asetpts=PTS-STARTPTS,volume=${voiceVolume}[voice]`,
    '[music][voice]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,atrim=duration=' + bodySeconds + `,alimiter=limit=${mix.limiter_limit}:attack=5:release=50:level=false,asetpts=PTS-STARTPTS[body]`,
    `[2:a]${resample},atrim=start=` + bodySeconds + ':duration=' + tailSeconds + ',asetpts=PTS-STARTPTS[tail]',
    '[body][tail]concat=n=2:v=0:a=1[a]',
  ].join(';');
  run('ffmpeg', ['-y', '-v', 'error', '-i', ctx.musicBed, '-i', ctx.voice, '-i', ctx.tailAudio, '-filter_complex', graph,
    '-map', '[a]', '-c:a', 'aac', '-b:a', mix.bitrate ?? '192k', '-ar', String(rate), '-movflags', '+faststart', ctx.sharedAudio]);
}

function makeTimeline(ctx, plan, variant, outputPath, wordCount) {
  const { fps, crossfadeFrames } = ctx;
  const relative = target => path.relative(ctx.projectRoot, target);
  let elapsedFrames = 0;
  const cuts = variant.shots.map((shot, index) => {
    const startFrame = index === 0 ? 0 : elapsedFrames - crossfadeFrames;
    const endFrame = startFrame + shot.frames;
    elapsedFrames = endFrame;
    return {
      source: relative(shot.fixed_path),
      asset_key: shot.asset_key,
      source_sha256: sha256(shot.fixed_path),
      original_path: shot.original_path,
      original_sha256: shot.original_sha256,
      source_kind: shot.source_kind,
      product_category: shot.category,
      continuous_scene_id: shot.continuous_scene_id,
      crop: shot.crop ?? null,
      source_interval_seconds: [shot.source_start_seconds, shot.source_end_seconds],
      source_frames: shot.source_frames,
      role: shot.role ?? 'regular',
      output_interval_seconds: [startFrame / fps, endFrame / fps],
      output_start_frame: startFrame,
      output_end_frame: endFrame,
      continuous_scene_internal_cuts: 0,
      transition_out: index < variant.shots.length - 1 ? { type: 'fade', overlap_frames: crossfadeFrames } : null,
    };
  });
  return {
    variant_id: variant.variant_id,
    batch_id: plan.batch,
    recipe_id: ctx.recipe.id,
    fixed_visual_profile_id: ctx.recipe.fixed_visual_profile_id ?? null,
    locked_assets_id: ctx.recipe.locked_assets_id ?? null,
    product_category: ctx.recipe.categories.product_category,
    product_category_strategy: plan.product_category_strategy ?? null,
    source_categories: [...new Set(variant.shots.map(shot => shot.category))],
    asset_category: ctx.recipe.categories.asset_category,
    copy_sha256: ctx.scriptPath ? sha256(ctx.scriptPath) : null,
    narration: {
      path: relative(ctx.voice),
      sha256: sha256(ctx.voice),
      duration_seconds: Number(probe(ctx.voice).format.duration),
      timestamp_metadata: ctx.voiceMeta ? relative(ctx.voiceMeta) : null,
      word_timestamp_count: wordCount,
      voiceover_text: ctx.recipe.locked.narration.text,
      locked_render_path: relative(ctx.sharedAudio),
      audio_sha256: sha256(ctx.sharedAudio),
      voice_gain_db: ctx.recipe.audio_mix.voice_gain_db,
      music_bed_source: relative(ctx.musicBed),
      tail_audio_source: relative(ctx.tailAudio),
    },
    body_frames: ctx.bodyFrames,
    body_seconds: ctx.bodySeconds,
    tail_video: { path: relative(ctx.tailVideo), sha256: sha256(ctx.tailVideo), output_frames: ctx.tailFrames },
    total_frames: ctx.totalFrames,
    total_seconds: ctx.totalSeconds,
    crossfade_frames: crossfadeFrames,
    cuts,
    duplicate_policy: ctx.recipe.batch.duplicate_policy,
    output_path: relative(outputPath),
    output_sha256: sha256(outputPath),
    output_bytes: fs.statSync(outputPath).size,
  };
}

function assertTechnicalProfile(ctx, info, label) {
  const video = info.streams.find(stream => stream.codec_type === 'video');
  const audio = info.streams.find(stream => stream.codec_type === 'audio');
  if (!video || video.codec_name !== 'h264' || video.width !== ctx.width || video.height !== ctx.height ||
      video.pix_fmt !== 'yuv420p' || video.r_frame_rate !== `${ctx.fps}/1` || Number(video.nb_frames) !== ctx.totalFrames ||
      !audio || audio.codec_name !== 'aac' || Math.abs(Number(info.format.duration) - ctx.totalSeconds) > 0.08) {
    throw new Error(`${label}: output failed technical profile validation`);
  }
  return { video, audio };
}

// Lock the mixed audio in the manifest, so a later batch cannot silently point at a different mix.
function lockSharedAudio(ctx, manifest) {
  const key = ctx.recipe.manifest_record?.shared_audio_key;
  if (!manifest || !key) return;
  const hash = sha256(ctx.sharedAudio);
  const prior = manifest.items[key];
  if (prior && (prior.fixed_path !== ctx.sharedAudio || prior.sha256 !== hash)) throw new Error('Locked shared audio changed');
  manifest.items[key] ??= {
    original_path: ctx.voice,
    fixed_path: ctx.sharedAudio,
    sha256: hash,
    bytes: fs.statSync(ctx.sharedAudio).size,
    voiceover_sha256: sha256(ctx.voice),
    separated_music_path: ctx.musicBed,
    separated_music_sha256: sha256(ctx.musicBed),
    tail_audio_path: ctx.tailAudio,
    tail_audio_sha256: sha256(ctx.tailAudio),
  };
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const runnerHash = sha256(__filename);
  if (args.printRunnerHash) {
    process.stdout.write(`${runnerHash}\n`);
    return;
  }
  if (!args.recipe) throw new Error('Pass --recipe <recipe.json>');

  const ctx = loadContext(args.recipe, args);
  const { recipe } = ctx;

  // Double lock: the recipe pins the runner allowed to execute it, and each run records the recipe
  // it ran. A null runner_sha256 means the lock has not been established yet.
  if (recipe.runner_sha256 && recipe.runner_sha256 !== runnerHash) {
    throw new Error(`Runner differs from the one approved by recipe ${recipe.id}\n  expected: ${recipe.runner_sha256}\n  actual:   ${runnerHash}\n  If the change is intended, update runner_sha256 and add a decision_log entry.`);
  }
  const recipeHash = sha256(ctx.recipeFile);

  const plan = readJson(ctx.planPath);
  const manifest = ctx.manifestPath && fs.existsSync(ctx.manifestPath) ? readJson(ctx.manifestPath) : null;

  verifyPlanAgainstRecipe(ctx, plan);
  verifyLockedAssets(ctx);
  if (manifest?.items?.tail && manifest.items.tail.sha256 !== recipe.locked.tail.video_sha256) {
    throw new Error('Asset manifest records a different tail video than the recipe locks');
  }
  const wordCount = verifyNarrationText(ctx);

  fs.mkdirSync(ctx.workRoot, { recursive: true });
  fs.mkdirSync(ctx.outputDir, { recursive: true });

  const startedAt = new Date().toISOString();
  const timingPath = ctx.reportsDir ? path.join(ctx.reportsDir, `process-timing${ctx.naming.qaSuffix}.json`) : null;
  const priorTiming = timingPath && fs.existsSync(timingPath) ? readJson(timingPath) : null;
  const report = priorTiming?.batch_id === plan.batch
    ? { ...priorTiming, status: 'running', resumed_at: startedAt }
    : { batch_id: plan.batch, started_at: startedAt, status: 'running', variants: [] };
  report.recipe_id = recipe.id;
  report.recipe_sha256 = recipeHash;
  report.runner_sha256 = runnerHash;
  report.stages ??= [];
  const writeTiming = extra => {
    if (!timingPath) return;
    fs.mkdirSync(ctx.reportsDir, { recursive: true });
    writeJson(timingPath, { ...report, ...extra });
  };

  if (!fs.existsSync(ctx.sharedAudio)) measure(report.stages, 'render_shared_audio', () => renderSharedAudio(ctx));
  const expectedAudioSeconds = ctx.bodySeconds + Number(probe(ctx.tailVideo).format.duration);
  if (Math.abs(Number(probe(ctx.sharedAudio).format.duration) - expectedAudioSeconds) > 0.08) {
    throw new Error('Shared audio does not match the body and locked tail lengths');
  }
  lockSharedAudio(ctx, manifest);

  measure(report.stages, 'asset_lock', () => lockAssets(ctx, plan, manifest));
  measure(report.stages, 'verify_locked_graphics', () => {
    for (const [label, overlay] of [['Body', ctx.bodyOverlay], ['Tail', ctx.tailOverlay]]) {
      if (Number(probe(overlay).streams[0]?.nb_frames) !== ctx.totalFrames) {
        throw new Error(`${label} graphics overlay has the wrong frame count`);
      }
    }
  });

  const selected = args.only
    ? plan.variants.filter(variant => args.only.includes(variant.variant_id) || args.only.includes(variant.file))
    : plan.variants;
  if (args.only && selected.length === 0) throw new Error(`No variant matched --only ${args.only.join(',')}`);

  for (const variant of selected) {
    const index = plan.variants.indexOf(variant);
    const events = [];
    const workDir = path.join(ctx.workRoot, variant.variant_id);
    const outputPath = path.join(ctx.outputDir, variant.file);
    const ordinal = String(index + 1).padStart(2, '0');
    const timelinePath = path.join(ctx.outputDir, `${ctx.naming.timelinePrefix}${ordinal}.json`);
    fs.mkdirSync(workDir, { recursive: true });

    // Resume: an output that still matches its recorded identity is left alone.
    if (fs.existsSync(outputPath) && fs.existsSync(timelinePath)) {
      const priorTimeline = readJson(timelinePath);
      if (priorTimeline.output_sha256 !== sha256(outputPath)) throw new Error(`Existing output changed since it was locked: ${variant.file}`);
      assertTechnicalProfile(ctx, probe(outputPath), variant.file);
      if (!report.variants.some(item => item.variant_id === variant.variant_id)) {
        report.variants.push({ variant_id: variant.variant_id, file: variant.file, output_sha256: priorTimeline.output_sha256, stages: [{ stage: 'reuse_complete_output' }] });
      }
      console.log(`Already complete ${index + 1}/${plan.variants.length}: ${outputPath}`);
      continue;
    }

    const bodyPath = path.join(workDir, ctx.naming.bodyFilename);
    if (fs.existsSync(bodyPath)) {
      const bodyVideo = probe(bodyPath).streams.find(stream => stream.codec_type === 'video');
      if (!bodyVideo || bodyVideo.width !== ctx.width || bodyVideo.height !== ctx.height || Number(bodyVideo.nb_frames) !== ctx.bodyFrames) {
        throw new Error(`${variant.file}: cached body failed validation`);
      }
      events.push({ stage: 'reuse_cached_body' });
    } else {
      // An earlier revision's body can be reused only when every shot's source identity and framing
      // are unchanged. Matching frame counts would not prove that.
      const reusePrefix = ctx.naming.reuseTimelinePrefix;
      const priorBodyPath = ctx.naming.reuseBodyFilename ? path.join(workDir, ctx.naming.reuseBodyFilename) : null;
      const priorTimelinePath = reusePrefix ? path.join(ctx.outputDir, `${reusePrefix}${ordinal}.json`) : null;
      const priorTimeline = priorTimelinePath && fs.existsSync(priorTimelinePath) ? readJson(priorTimelinePath) : null;
      const sameCuts = priorTimeline?.cuts?.length === variant.shots.length && variant.shots.every((shot, shotIndex) => {
        const prior = priorTimeline.cuts[shotIndex];
        return prior.original_sha256 === shot.original_sha256 &&
          prior.source_interval_seconds[0] === shot.source_start_seconds &&
          prior.source_interval_seconds[1] === shot.source_end_seconds &&
          JSON.stringify(prior.crop) === JSON.stringify(shot.crop ?? null);
      });
      if (sameCuts && priorBodyPath && fs.existsSync(priorBodyPath)) {
        fs.copyFileSync(priorBodyPath, bodyPath, fs.constants.COPYFILE_EXCL);
        events.push({ stage: 'reuse_unchanged_body' });
      } else {
        measure(events, 'render_body', () => renderBody(ctx, variant.shots, workDir));
      }
    }

    measure(events, 'compose_tail', () => renderBase(ctx, workDir));
    measure(events, 'apply_graphics', () => applyGraphics(ctx, workDir));
    measure(events, 'mux_locked_audio', () => run('ffmpeg', ['-y', '-v', 'error', '-i', path.join(workDir, 'composited.mp4'), '-i', ctx.sharedAudio,
      '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'copy', '-t', ctx.totalSeconds.toFixed(6), '-movflags', '+faststart', outputPath]));

    const finalProbe = measure(events, 'probe_output', () => probe(outputPath));
    const { video, audio } = assertTechnicalProfile(ctx, finalProbe, variant.file);
    const timeline = makeTimeline(ctx, plan, variant, outputPath, wordCount);
    writeJson(timelinePath, timeline);

    if (ctx.qaDir) {
      fs.mkdirSync(ctx.qaDir, { recursive: true });
      writeJson(path.join(ctx.qaDir, `${variant.variant_id}${ctx.naming.qaSuffix}.json`), {
        variant_id: variant.variant_id,
        recipe_id: recipe.id,
        status: 'technical_pass_visual_review_pending',
        technical: {
          width: video.width, height: video.height, fps: video.r_frame_rate, frames: Number(video.nb_frames),
          duration_seconds: Number(finalProbe.format.duration), video_codec: video.codec_name,
          pixel_format: video.pix_fmt, audio_codec: audio.codec_name,
        },
        locked: {
          narration_sha256: recipe.locked.narration.sha256,
          shared_audio_sha256: sha256(ctx.sharedAudio),
          tail_video_sha256: recipe.locked.tail.video_sha256,
          tail_audio_sha256: recipe.locked.tail.audio_sha256,
          body_graphics_sha256: recipe.locked.graphics.body_overlay_sha256,
          tail_graphics_sha256: recipe.locked.graphics.tail_overlay_sha256,
        },
        output_sha256: timeline.output_sha256,
        checks_pending: ['visual relevance', 'transition and black-frame review', 'pairwise duplication review', 'source text review'],
      });
    }

    report.variants.push({ variant_id: variant.variant_id, file: variant.file, output_sha256: timeline.output_sha256, stages: events });
    writeTiming({ completed_variants: report.variants.length });
    console.log(`Rendered ${index + 1}/${plan.variants.length}: ${outputPath} (${timeline.output_sha256})`);
  }

  report.status = 'renders_complete_visual_qa_pending';
  report.completed_at = new Date().toISOString();
  writeTiming();
  if (plan.status !== undefined && !args.only) {
    plan.status = 'rendered_visual_qa_pending';
    writeJson(ctx.planPath, plan);
  }
  console.log(`Batch render complete: ${report.variants.length} videos, ${ctx.totalSeconds}s each.`);
}

try {
  main();
} catch (error) {
  console.error(error.message ?? error);
  process.exit(1);
}
