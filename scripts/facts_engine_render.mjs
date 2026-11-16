// Stdin/stdout bridge to the existing pure README renderer. Requires Node 20.
import { renderDocument } from './facts_markdown.mjs';

const LIMIT = 2 * 1024 * 1024;
const fail = () => { throw new Error('invalid engine README render request'); };
const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
function utf8(text) {
  if (typeof text !== 'string') fail();
  const bytes = Buffer.from(text, 'utf8');
  if (bytes.length > LIMIT || bytes.toString('utf8') !== text) fail();
  return bytes;
}

function jsonTypes(value, depth = 0) {
  if (depth > 64) fail();
  if (typeof value === 'string') { utf8(value); return; }
  if (value === null || typeof value === 'boolean') return;
  if (typeof value === 'number') { if (!Number.isFinite(value)) fail(); return; }
  if (!object(value) && !Array.isArray(value)) fail();
  for (const [key, child] of Object.entries(value)) {
    utf8(key);
    jsonTypes(child, depth + 1);
  }
}

try {
  if (process.versions.node.split('.')[0] !== '20') fail();
  const chunks = [];
  let length = 0;
  for await (const chunk of process.stdin) {
    length += chunk.length;
    if (length > LIMIT) fail();
    chunks.push(chunk);
  }
  const raw = Buffer.concat(chunks);
  const input = JSON.parse(new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(raw));
  if (!object(input) || Object.keys(input).sort().join(',') !== 'facts,text' || !object(input.facts)) fail();
  utf8(input.text);
  jsonTypes(input.facts);
  const result = renderDocument(input.text, input.facts, 'README.md');
  if (!object(result) || Object.keys(result).sort().join(',') !== 'markers,text'
      || !Number.isSafeInteger(result.markers) || result.markers < 1) fail();
  utf8(result.text);
  const output = utf8(JSON.stringify({ text: result.text, markers: result.markers }));
  process.stdout.write(output);
} catch {
  process.stderr.write('engine-readme-render: invalid input or render failure\n');
  process.exitCode = 2;
}
