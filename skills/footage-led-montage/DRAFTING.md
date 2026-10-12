# 起草新配方

配方（`projects/<project>/recipe.json`）把一次复刻的全部生产参数固定下来，让下一批只改「选哪些镜头、出几条」。本文档是从一部新参考片走到配方草案的路径。

## 哪些能自动测，哪些不能

| 参数 | 来源 |
| --- | --- |
| 参考片画幅、帧率、帧数、时长、音频 | `hypit media probe` + ffprobe `-count_frames` |
| 参考片切点候选 | `hypit media boundaries`（按交付帧率采样，候选带源帧号） |
| `timeline.tail_frames` | `round(尾板时长 × 交付帧率)`，精确可推 |
| `timeline.crossfade_frames` | 默认 12 帧（0.4 s），风格常量 |
| `canvas` | **交付规格，不是参考片属性。** 同一部参考片在 `corn-260923-clone-4` 按原生 1080×1920 交付、在 `corn-260923-10-recreation` 按 720×1280 交付 —— 必须由人给定 |
| `timeline.body_frames` | 最强切点候选只是建议值。已批准的玉米配方在切点前留了 3 帧余量（用 955，切点在 958），余量不可推导，必须人工确认 |
| 品类、素材路径、锁定资产、混音、条数、命名 | 业务决策，脚本只把它们列进 `confirm` 清单 |

## 步骤

### 1. 取片

```bash
node bin/hypit.mjs media fetch <url> --to resource/videos/<品类>/reference.mp4
```

### 2. 生成草案

```bash
node skills/footage-led-montage/tools/draft-recipe.cjs \
  --reference resource/videos/<品类>/reference.mp4 \
  --tail resource/end.mp4 --canvas 720x1280 \
  --out projects/<项目>/recipe-draft.json
```

产出三块：测量事实（`reference`/`tail`/`cuts`）、`recipe_draft` 骨架（能算的已填，业务项为 `null`）、`confirm` 清单（每项写明为什么测不出、下一步做什么）。`--canvas` 省略时填参考片原生尺寸并标注 `canvas_source: "reference native"`。

### 3. 图形层（参考片自带文字时才需要）

参考片的促销卡、CTA、说明、水印是可以整层复用的资产，不必重建字体排版 —— 重建正是过去反复调试的地方。

```bash
# 取帧
node bin/hypit.mjs media frames <ref> --every-frame --to work/frames
# OCR：macOS Vision，逐图输出 {file, lines:[{text, confidence, box}]}，box 为底原点归一化
cp projects/corn-fruit-reference-20261010/tools/read_text.swift tools/
swift tools/read_text.swift work/frames/*.png > references/evidence/source-text.jsonl
# 抠层（overlay-params.json 见 projects/corn-fruit-reference-20261010/tools/overlay-params.json 的字段形状）
python3 skills/footage-led-montage/tools/graphics/extract-overlay.py \
  --params tools/overlay-params.json --root . --mov work/fixed-visual-overlay.mov --mov-fps 30
# 像素级复核：暴露率必须为 0
python3 skills/footage-led-montage/tools/qa/mask-coverage.py --params tools/overlay-params.json --root .
```

`caption_band: null` 让字幕带由 OCR 聚类自动推导（换参考片不必改常量）；`fixed_regions`/`panel_regions` 是参考片自己的 UI 区域，逐片填写。两个工具共用 `tools/lib/glyphs.py` 的同一判据，先跑各自的 `--selftest`。

### 4. 旁白（可选、付费）

```bash
node bin/hypit.mjs transcribe <ref> --language zh --to references/reference-transcript.json
```

它经 Runtime Profile 走 whisperx Endpoint，运行前会报出 Endpoint 与价格页。**参考片没有转写也能复刻**：玉米项目用的是重新合成的 Doubao A 旁白，参考片音频只用于核对节奏。需要词级时间戳时它是唯一来源，但不要让草案流程默认调用。

### 5. 补齐并确认

按 `confirm` 清单把 `recipe_draft` 的 `null` 块填成 `projects/<项目>/recipe.json`，然后：

```bash
npx --yes ajv-cli@5 validate -s skills/footage-led-montage/recipe.schema.json -d projects/<项目>/recipe.json
node skills/footage-led-montage/tools/render.cjs --print-runner-hash   # 填进 recipe.runner_sha256
```

`runner_sha256` 是双锁的一半：runner 哈希不符时拒绝执行，每次运行再把 `recipe_sha256` 与 `runner_sha256` 一起记进计时报告。

### 6. 出片

```bash
node skills/footage-led-montage/tools/render.cjs --recipe projects/<项目>/recipe.json
```

## 玉米范例（2026-10-10 已批准配方）

`draft-recipe.cjs` 对 `projects/corn-260923-10-recreation/resource/videos/玉米/260923-reference.mp4` 跑出的草案：

| 字段 | 草案 | `recipe.json` | 结论 |
| --- | --- | --- | --- |
| `canvas` | 720×1280 @30 | 720×1280 @30 | 一致（由 `--canvas` 给定） |
| `timeline.tail_frames` | 318 | 318 | 精确一致（`round(10.588005 × 30)`） |
| `timeline.crossfade_frames` | 12 | 12 | 一致 |
| `timeline.body_frames` | 958 | 955 | 差 3 帧：草案给最强切点，配方留了 0.1 s 余量 |

参考片 1080×1920 / 30fps / 1252 帧 / 41.749 s；尾板 `resource/end.mp4` 本身是 720×1280 / 50fps / 529 帧 / 10.588 s —— `tail_frames` 按**交付**帧率算，不是尾板自己的帧率。

## 不要做的事

- 不要把参考片原生画布当交付画布（除非确实按原生交付）。
- 不要把最强切点当 `body_frames` 直接锁定；确认切点之后的内容确实由尾板替代，再决定留几帧余量。
- 不要为了凑 `tail_frames` 去改尾板资产；尾板是锁定资产，帧数由它的时长决定。
- 不要默认调用 `hypit transcribe`（付费），也不要因为参考片有旁白就以为必须转写。
