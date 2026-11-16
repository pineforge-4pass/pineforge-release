// Pure fact-marker rendering. Callers supply facts and handle document persistence.
const integer = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
const count = (value, name) => {
  if (!Number.isSafeInteger(value) || value < 0) throw new Error(`invalid count: ${name}`);
  return value;
};

export function tokenValue(facts, token) {
  const segments = [...token.matchAll(/(?:\[([^\]]+)\]|([^.[\]]+))/g)].map((match) => match[1] ?? match[2]);
  let value = facts;
  for (const segment of segments) {
    if (!value || !Object.hasOwn(value, segment) || ['__proto__', 'prototype', 'constructor'].includes(segment)) throw new Error(`unknown fact token: ${token}`);
    value = value[segment];
  }
  return value;
}

function formatToken(value, format = 'text') {
  if (format === 'lanes' || format === 'lanes-all') {
    const allMarkets = format === 'lanes-all';
    if (allMarkets && (!value || typeof value !== 'object' || Array.isArray(value) || !Array.isArray(value.pairs)
        || !value.otherMarkets || typeof value.otherMarkets !== 'object' || Array.isArray(value.otherMarkets))) {
      throw new TypeError('lanes-all format requires a scoreboard object with pairs and otherMarkets');
    }
    const pairs = allMarkets ? value.pairs : value;
    if (!Array.isArray(pairs)) throw new Error('lanes format requires an array');
    const fields = ['graded', 'excellent', 'strong', 'belowStrong'];
    const other = allMarkets ? value.otherMarkets : null;
    if (allMarkets) {
      count(other.markets, 'otherMarkets.markets');
      count(other.lanes, 'otherMarkets.lanes');
      for (const field of fields) {
        const aggregate = count(other[field], `otherMarkets.${field}`);
        const computed = pairs.reduce((sum, lane) => sum + count(lane[field], field), 0) + aggregate;
        if (computed !== count(value[field], field) || (other.lanes === 0 && aggregate !== 0)) {
          throw new RangeError(`lanes-all total mismatch: ${field}`);
        }
      }
    }
    const header = '| Market · timeframe | Probes graded | Excellent | Strong | Below strong |\n|---|---:|---:|---:|---:|';
    const ordered = [...pairs].sort((left, right) => Number(right.hardProbes > 0) - Number(left.hardProbes > 0));
    const rows = ordered.map((lane) => `| ${lane.symbol} · ${lane.timeframe === '15' ? '15m' : lane.timeframe}${lane.hardProbes > 0 ? ' *(hard lane: zero regression allowed)*' : ''} | ${integer.format(lane.graded)} | ${integer.format(lane.excellent)} | ${integer.format(lane.strong)} | ${integer.format(lane.belowStrong)} |`);
    if (!allMarkets && !rows.length) rows.push(''); // Preserve the legacy empty table's bytes.
    if (allMarkets && other.lanes > 0) {
      rows.push(`| All other markets (${integer.format(other.markets)} markets, ${integer.format(other.lanes)} lanes) | ${fields.map((field) => integer.format(other[field])).join(' | ')} |`);
    }
    const total = fields.map((field) => `**${integer.format(allMarkets ? value[field] : pairs.reduce((sum, lane) => sum + lane[field], 0))}**`).join(' | ');
    rows.push(`| **Total** | ${total} |`);
    return `\n\n${header}\n${rows.join('\n')}\n\n`;
  }
  if (format === 'trades-badge') { count(value, 'TV trades'); return `[![Trades](https://img.shields.io/badge/TradingView%20trades%20graded-${(value / 1_000_000).toFixed(2)}M-brightgreen)](#validation-scoreboard)`; }
  if (format === 'parity-badge') {
    const label = encodeURIComponent(`${integer.format(value.excellent + value.strong)} / ${integer.format(value.graded)} excellent or strong`);
    return `[![Parity](https://img.shields.io/badge/TradingView%20parity-${label}-brightgreen)](#validation-scoreboard)`;
  }
  if (format === 'int') { count(value, 'token'); return integer.format(value); }
  if (format === 'pct') { if (typeof value !== 'number') throw new Error('pct format requires a number'); return value.toFixed(2); }
  if (format === 'short') { if (typeof value !== 'string') throw new Error('short format requires text'); return value.slice(0, 8); }
  if (format === 'code' || format === 'short-code') {
    const content = formatToken(value, format === 'short-code' ? 'short' : 'text');
    if (/[`\r\n]/.test(content)) throw new Error('code format requires single-line text without backticks');
    return `\`${content}\``;
  }
  if (format !== 'text') throw new Error(`unknown fact format: ${format}`);
  if (typeof value !== 'string' && typeof value !== 'number') throw new Error('token requires a scalar or explicit format');
  return String(value);
}

export function renderText(text, facts) {
  let markers = 0;
  const rendered = text.replace(/<!-- pf:([^\s\\|>]+)(?:(\\?\|)([a-z-]+))? -->[\s\S]*?<!-- \/pf -->/g,
    (block, token, separator, format, offset) => {
      if (block.slice(block.indexOf('-->') + 3).includes('<!-- pf:')) throw new Error('nested fact markers');
      if (text[offset - 1] === '`' || text[offset + block.length] === '`') throw new Error('backticks must be inside fact markers; use code or short-code');
      markers++;
      const tableRow = /^\s*\|/.test(text.slice(text.lastIndexOf('\n', offset - 1) + 1, offset));
      return `<!-- pf:${token}${format ? `${tableRow ? '\\|' : separator}${format}` : ''} -->${formatToken(tokenValue(facts, token), format)}<!-- /pf -->`;
    });
  if ((text.match(/<!-- pf:/g) ?? []).length !== markers || (text.match(/<!-- \/pf -->/g) ?? []).length !== markers) throw new Error('malformed fact markers');
  return { text: rendered, markers };
}

const BLOCK_TAGS = new Set(('address article aside base basefont blockquote body caption center col colgroup dd details dialog dir div dl dt '
  + 'fieldset figcaption figure footer form frame frameset h1 h2 h3 h4 h5 h6 head header hr html iframe legend li link main menu menuitem '
  + 'nav noframes ol optgroup option p param section source summary table tbody td tfoot th thead title tr track ul').split(' '));
const ATTRIBUTE = String.raw`\s+[A-Za-z_:][\w.:-]*(?:\s*=\s*(?:[^\s"'=<>\x60]+|'[^']*'|"[^"]*"))?`;
const LONE_TAG = new RegExp(String.raw`^(?:<[A-Za-z][A-Za-z0-9-]*(?:${ATTRIBUTE})*\s*\/?>|<\/[A-Za-z][A-Za-z0-9-]*\s*>)\s*$`);
function htmlBlockStart(line, paragraph) {
  const html = /^ {0,3}(<.*)$/.exec(line)?.[1];
  if (!html) return null;
  if (/^<(?:pre|script|style|textarea)(?:[\s>]|$)/i.test(html)) return /<\/(?:pre|script|style|textarea)>/i;
  if (html.startsWith('<!--')) return /-->/;
  if (html.startsWith('<?')) return /\?>/;
  if (/^<![A-Za-z]/.test(html)) return />/;
  if (html.startsWith('<![CDATA[')) return /\]\]>/;
  const tag = /^<\/?([A-Za-z][A-Za-z0-9-]*)(?:[\s>]|\/>|$)/.exec(html);
  if (tag && BLOCK_TAGS.has(tag[1].toLowerCase())) return 'blank';
  return !paragraph && LONE_TAG.test(html) ? 'blank' : null;
}
// Leading whitespace with tabs expanded to the next multiple of four columns.
function expandIndent(text) {
  return text.replace(/^[ \t]+/, (space) => {
    let columns = 0;
    for (const char of space) columns = char === '\t' ? columns + 4 - (columns % 4) : columns + 1;
    return ' '.repeat(columns);
  });
}

// A marker whose <!-- is the first text of a Markdown line opens an HTML block, and GitHub
// renders that whole line as raw HTML; so does a line that starts with another comment and
// carries a marker later. Conservative: any blockquote, list or footnote prefix and any indent
// counts as line start. Exempt: fenced code, lines inside an HTML block opened on an earlier
// line, and a lanes/lanes-all block whose start or end comment stands alone on its line.
// A fence or HTML block opened in a blockquote or list item ends with that container.
export function markerLineErrors(text, file) {
  const errors = [];
  let open = null; let paragraph = false; let lanes = false; let depth = 0; let items = [];
  text.replace(/^\uFEFF/, '').split(/\r\n?|\n/).forEach((line, index) => {
    let rest = line; let quotes = 0;
    for (let quote; (quote = /^ {0,3}>[ ]?/.exec(rest)); quotes++) rest = rest.slice(quote[0].length);
    rest = expandIndent(rest);
    const blank = !rest.trim();
    const indent = rest.length - rest.trimStart().length;
    if (!blank) {
      if (quotes !== depth) { depth = quotes; items = []; }
      while (items.length && indent < items.at(-1)) items.pop();
    }
    if (open) {
      if (quotes >= open.quotes && (blank || indent >= open.column)) {
        const body = rest.slice(Math.min(open.column, indent));
        if (open.fence ? open.fence.test(body) : open.end === 'blank' ? blank : open.end.test(body)) open = null;
        return;
      }
      open = null;
    }
    if (blank) { paragraph = false; return; }
    // A list marker opens an item whose content column later lines must reach to stay inside.
    for (let item; (item = /^( {0,3})([-+*]|\d{1,9}[.)])( +|$)/.exec(rest.slice(items.at(-1) ?? 0)));) {
      const gap = item[3].length;
      items.push((items.at(-1) ?? 0) + item[1].length + item[2].length + (gap && gap <= 4 ? gap : 1));
      if (!gap) break;
    }
    const column = items.at(-1) ?? 0;
    const content = rest.slice(column);
    const leading = line.replace(/^(?:[ \t]*(?:>|[-+*](?=[ \t])|\d{1,9}[.)](?=[ \t])|\[\^[^\]]+\]:))*[ \t]*/, '');
    if (leading.startsWith('<!--') && /<!-- (?:pf:|\/pf -->)/.test(leading)) {
      const alone = /^<!-- (?:pf:|\/pf -->)/.test(leading) && !leading.slice(leading.indexOf('-->') + 3).trim();
      const exempt = alone && (/^<!-- pf:[^\s\\|>]+\\?\|lanes(?:-all)? -->/.test(leading) || (lanes && leading.startsWith('<!-- /pf -->')));
      if (!exempt) errors.push(`${file}:${index + 1}: marker starts a line: GitHub renders the line as raw HTML; keep text before it`);
    }
    for (const [marker, format] of line.matchAll(/<!-- (?:pf:[^\s\\|>]+(?:\\?\|([a-z-]+))?|\/pf) -->/g)) lanes = marker.startsWith('<!-- pf:') && (format === 'lanes' || format === 'lanes-all');
    const fence = /^( {0,3})(`{3,}|~{3,})(.*)$/.exec(content);
    if (fence && !(fence[2][0] === '`' && fence[3].includes('`'))) {
      open = { quotes, column, fence: new RegExp(`^ {0,3}${fence[2][0]}{${fence[2].length},}[ \\t]*$`) };
      paragraph = false;
      return;
    }
    const end = htmlBlockStart(content, paragraph);
    if (end) {
      paragraph = false;
      if (end === 'blank' || !end.test(content.slice(content.indexOf('<') + 2))) open = { quotes, column, end };
      return;
    }
    paragraph = !/^ {0,3}(?:#{1,6}(?:[ \t]|$)|([-*_])(?:[ \t]*\1){2,}[ \t]*$)/.test(content);
  });
  return errors;
}

// Apply the document checks before returning a complete rendering plan.
export function renderDocument(text, facts, file = 'README.md') {
  const markdown = /\.(?:md|mdx)$/.test(file);
  const refused = new Set(markdown ? markerLineErrors(text, file) : []);
  let result;
  try { result = renderText(text, facts); } catch (error) { if (!refused.size) throw error; }
  if (result && text !== result.text && markdown) {
    for (const error of markerLineErrors(result.text, file)) refused.add(error);
  }
  if (refused.size) throw new Error(`refused ${refused.size} fact marker line(s); join each onto the previous line, or start it with a word before the marker:\n${[...refused].join('\n')}`);
  return result;
}
