'use strict';

// Thin wrappers over the external media tools and content hashing. Extracted verbatim in behaviour
// from the batch renderer that produced the approved corn outputs, so that lifting it out of a batch
// directory cannot change a rendered byte.

const fs = require('node:fs');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { performance } = require('node:perf_hooks');

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function writeJson(file, value) {
  fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`);
}

function sha256(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function sha256OfString(text) {
  return crypto.createHash('sha256').update(text).digest('hex');
}

function run(bin, args) {
  execFileSync(bin, args, { stdio: 'inherit' });
}

function probe(file) {
  return JSON.parse(execFileSync('ffprobe', [
    '-v', 'error', '-show_streams', '-show_format', '-of', 'json', file,
  ], { encoding: 'utf8' }));
}

function measure(events, name, action) {
  const started = new Date().toISOString();
  const start = performance.now();
  const result = action();
  events.push({ stage: name, started_at: started, duration_seconds: (performance.now() - start) / 1000 });
  return result;
}

// A locked asset is identified by its content, never by its path alone.
function assertHash(file, expected, label) {
  if (!fs.existsSync(file)) throw new Error(`${label}: locked asset is missing: ${file}`);
  const actual = sha256(file);
  if (actual !== expected) {
    throw new Error(`${label}: locked asset changed\n  path:     ${file}\n  expected: ${expected}\n  actual:   ${actual}`);
  }
  return actual;
}

module.exports = { readJson, writeJson, sha256, sha256OfString, run, probe, measure, assertHash };
