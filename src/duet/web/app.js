'use strict';

// The duet page: a list of discussions on the left, the selected discussion on
// the right, updated live from the server's event stream.

const TOKEN = document.querySelector('meta[name="duet-token"]').content;
const NAMES = { claude: 'Claude', codex: 'Codex' };
const AGENTS = ['claude', 'codex'];

const S = {
  list: [],
  listJson: '',
  listDrawnAt: 0,
  id: null,             // the discussion on screen
  detail: null,
  detailJson: '',
  live: new Map(),      // "round:agent" -> { activity: [], text, started }
  source: null,         // EventSource for the discussion on screen
  dirty: new Set(),
  frame: 0,
  refreshTimer: 0,
  follow: false,        // keep the view scrolled to the bottom while it runs
  activityOpen: new Map(),
  mode: 'both',
  phases: new Map(),
  info: null,
  loadedAt: Date.now() / 1000,
};

const $ = (selector, root = document) => root.querySelector(selector);

// ---------------------------------------------------------------- helpers

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key in node && typeof value === 'boolean') node[key] = value;
    else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: { 'X-Duet-Token': TOKEN, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`);
  return data;
}

function toast(text, kind = 'info') {
  const node = el('div', { class: `toast ${kind}` }, text);
  $('#toasts').append(node);
  setTimeout(() => node.remove(), kind === 'error' ? 7000 : 3500);
}

async function attempt(action) {
  try {
    return await action();
  } catch (error) {
    toast(error.message, 'error');
    return undefined;
  }
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? '' : 's'}`;
}

function fmtTokens(n) {
  if (!n || n < 1000) return String(n || 0);
  return n < 10000 ? `${(n / 1000).toFixed(1)}k` : `${Math.round(n / 1000)}k`;
}

function fmtDuration(seconds) {
  const s = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

function parseTime(iso) {
  if (!iso) return null;
  const t = Date.parse(String(iso).replace(/([+-]\d\d)(\d\d)$/, '$1:$2'));
  return Number.isNaN(t) ? null : t / 1000;
}

function ago(iso) {
  const t = parseTime(iso);
  if (t == null) return '';
  const s = Date.now() / 1000 - t;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(t * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function tilde(path) {
  const home = S.info && S.info.home;
  return home && (path === home || path.startsWith(home + '/')) ? '~' + path.slice(home.length) : path;
}

function basename(path) {
  return path.split('/').filter(Boolean).pop() || path;
}

function firstLine(text) {
  const line = String(text).split('\n').find((l) => l.trim()) || '';
  return line.length > 200 ? line.slice(0, 199) + '…' : line;
}

function copy(text) {
  navigator.clipboard.writeText(text).then(() => toast('Copied.'), () => toast('Could not copy.', 'error'));
}

function prefs(update) {
  try {
    const saved = JSON.parse(localStorage.getItem('duet-new-question') || '{}');
    if (update) localStorage.setItem('duet-new-question', JSON.stringify({ ...saved, ...update }));
    return saved;
  } catch {
    return {};
  }
}

// ---------------------------------------------------------------- Markdown
// A small renderer for what agents usually write: headings, lists, tables,
// code, quotes, emphasis and links. Everything is escaped first.

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

function inline(text) {
  const codes = [];
  let s = String(text).replace(/`([^`\n]+)`/g, (_, code) => {
    codes.push(code);
    return `\u0001${codes.length - 1}\u0002`;
  });
  s = escapeHtml(s);
  s = s.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (_, label, href) =>
    /^(https?:|mailto:)/i.test(href)
      ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`
      : `${label} <span class="path">${href}</span>`);
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  s = s.replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*\w])\*(?=[^\s*])([^*\n]*?[^\s*])\*(?!\*)/g, '$1<em>$2</em>');
  s = s.replace(/~~(?=\S)([^~\n]*?\S)~~/g, '<del>$1</del>');
  return s.replace(/\u0001(\d+)\u0002/g, (_, i) => `<code>${escapeHtml(codes[+i])}</code>`);
}

const MD = {
  fence: /^\s{0,3}(`{3,}|~{3,})/,
  heading: /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/,
  hr: /^\s{0,3}([-*_])(\s*\1){2,}\s*$/,
  quote: /^\s{0,3}>\s?/,
  item: /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/,
  tableSep: /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/,
};

const indentOf = (line) => line.match(/^\s*/)[0].length;

function isBlockStart(line, next) {
  return MD.fence.test(line) || MD.heading.test(line) || MD.hr.test(line) || MD.quote.test(line)
    || MD.item.test(line) || (line.includes('|') && next !== undefined && MD.tableSep.test(next));
}

function md(source) {
  return blocks(String(source || '').replace(/\r\n?/g, '\n').split('\n'));
}

function tableCells(row) {
  return row.trim().replace(/^\|/, '').replace(/\|\s*$/, '').replace(/\\\|/g, '\u0003')
    .split('|').map((cell) => cell.trim().replace(/\u0003/g, '|'));
}

function blocks(lines) {
  let out = '';
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    let m = line.match(MD.fence);
    if (m) {
      const fence = m[1];
      const body = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence)) body.push(lines[i++]);
      i++;
      out += `<pre><code>${escapeHtml(body.join('\n'))}</code></pre>`;
      continue;
    }
    if ((m = line.match(MD.heading))) {
      const level = Math.min(m[1].length + 2, 6);
      out += `<h${level}>${inline(m[2])}</h${level}>`;
      i++;
      continue;
    }
    if (MD.hr.test(line)) { out += '<hr>'; i++; continue; }
    if (MD.quote.test(line)) {
      const body = [];
      while (i < lines.length && MD.quote.test(lines[i])) body.push(lines[i++].replace(MD.quote, ''));
      out += `<blockquote>${blocks(body)}</blockquote>`;
      continue;
    }
    if (line.includes('|') && i + 1 < lines.length && MD.tableSep.test(lines[i + 1])) {
      const head = tableCells(line);
      const aligns = tableCells(lines[i + 1]).map((c) =>
        (c.startsWith(':') && c.endsWith(':') ? 'center' : c.endsWith(':') ? 'right' : ''));
      const style = (j) => (aligns[j] ? ` style="text-align:${aligns[j]}"` : '');
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].trim() && lines[i].includes('|')) rows.push(tableCells(lines[i++]));
      out += '<div class="table"><table><thead><tr>'
        + head.map((c, j) => `<th${style(j)}>${inline(c)}</th>`).join('')
        + '</tr></thead><tbody>'
        + rows.map((r) => '<tr>' + head.map((_, j) => `<td${style(j)}>${inline(r[j] || '')}</td>`).join('') + '</tr>').join('')
        + '</tbody></table></div>';
      continue;
    }
    if (MD.item.test(line)) {
      const [html, next] = list(lines, i);
      out += html;
      i = next;
      continue;
    }
    const para = [line.trim()];
    i++;
    while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i], lines[i + 1])) para.push(lines[i++].trim());
    out += `<p>${para.map(inline).join('<br>')}</p>`;
  }
  return out;
}

function list(lines, start) {
  const first = lines[start].match(MD.item);
  const base = first[1].length;
  const ordered = /\d/.test(first[2]);
  const items = [];
  let i = start;
  while (i < lines.length) {
    const line = lines[i];
    const m = line.match(MD.item);
    if (m && m[1].length >= base && m[1].length <= base + 1) {
      if (/\d/.test(m[2]) !== ordered) break;
      items.push([m[3]]);
      i++;
      continue;
    }
    if (m && m[1].length < base) break;
    if (!line.trim()) {
      let j = i + 1;
      while (j < lines.length && !lines[j].trim()) j++;
      if (j >= lines.length) break;
      const nextItem = lines[j].match(MD.item);
      const indent = indentOf(lines[j]);
      const sibling = nextItem && indent >= base && indent <= base + 1 && /\d/.test(nextItem[2]) === ordered;
      if (sibling || indent > base + 1) { items[items.length - 1].push(''); i++; continue; }
      break;
    }
    if (indentOf(line) > base || !isBlockStart(line, lines[i + 1])) {
      items[items.length - 1].push(line);
      i++;
      continue;
    }
    break;
  }
  const tag = ordered ? 'ol' : 'ul';
  const number = parseInt(first[2], 10);
  const startAttr = ordered && number !== 1 ? ` start="${number}"` : '';
  return [`<${tag}${startAttr}>${items.map(listItem).join('')}</${tag}>`, i];
}

function listItem(lines) {
  const [first, ...rest] = lines;
  const indents = rest.filter((l) => l.trim()).map(indentOf);
  const cut = indents.length ? Math.min(...indents) : 0;
  const body = rest.map((l) => l.slice(Math.min(cut, indentOf(l))));
  const lead = [first];
  let k = 0;
  while (k < body.length && body[k].trim() && !isBlockStart(body[k], body[k + 1])) lead.push(body[k++].trim());
  let prefix = '';
  const task = lead[0].match(/^\[([ xX])\]\s+(.*)$/);
  if (task) {
    prefix = `<input type="checkbox" disabled${task[1] === ' ' ? '' : ' checked'}> `;
    lead[0] = task[2];
  }
  const remaining = body.slice(k);
  const nested = remaining.some((l) => l.trim()) ? blocks(remaining) : '';
  return `<li>${prefix}${lead.map(inline).join('<br>')}${nested}</li>`;
}

// ---------------------------------------------------------------- sidebar

async function refreshList() {
  const list = await api('GET', '/api/discussions').catch(() => null);
  if (!list) return;
  for (const d of list) {
    const before = S.phases.get(d.id);
    if (before === 'running' && d.status.phase !== 'running') notify(d);
    S.phases.set(d.id, d.status.phase);
  }
  S.list = list;
  const json = JSON.stringify(list);
  if (json !== S.listJson || Date.now() - S.listDrawnAt > 30000) {
    S.listJson = json;
    drawList();
  }
  const current = list.find((d) => d.id === S.id);
  if (current && S.detail && (current.updated_at !== S.detail.updated_at
      || current.status.label !== S.detail.status.label)) refreshSoon();
  const waiting = list.filter((d) => d.status.needs_you).length;
  document.title = waiting ? `(${waiting}) duet` : 'duet';
}

function drawList() {
  S.listDrawnAt = Date.now();
  const nav = $('#list');
  if (!S.list.length) {
    nav.replaceChildren(el('div', { class: 'empty' }, 'No discussions yet.'));
    return;
  }
  nav.replaceChildren(...S.list.map((d) => el('a', {
    class: `item${d.id === S.id ? ' active' : ''}`,
    href: `#/d/${d.id}`,
    title: d.title,
  },
  el('div', { class: 't' }, d.title),
  el('div', { class: `m phase-${d.status.phase}` }, el('span', { class: 'dot' }), el('span', { class: 'lbl' }, d.status.label)),
  el('div', { class: 'f' }, `${basename(d.cwd)} · ${ago(d.updated_at)}`))));
}

function notify(d) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  if (!document.hidden && d.id === S.id) return;
  const n = new Notification(d.title, { body: d.status.label, tag: d.id });
  n.onclick = () => {
    window.focus();
    location.hash = `#/d/${d.id}`;
    n.close();
  };
}

function setUpNotifications() {
  const button = $('#notify');
  if (!('Notification' in window) || Notification.permission !== 'default') return;
  button.hidden = false;
  button.addEventListener('click', async () => {
    await Notification.requestPermission();
    button.hidden = Notification.permission === 'default';
  });
}

// ---------------------------------------------------------------- routing

function route() {
  const m = location.hash.match(/^#\/d\/([A-Za-z0-9._-]+)/);
  if (m) openDiscussion(m[1]);
  else if (location.hash === '#/new' || !S.list.length) showNewQuestion();
  else location.hash = `#/d/${S.list[0].id}`;
}

function leaveDiscussion() {
  if (S.source) S.source.close();
  S.source = null;
  S.id = null;
  S.detail = null;
  S.detailJson = '';
  S.live = new Map();
}

async function openDiscussion(id) {
  if (S.id !== id) {
    leaveDiscussion();
    S.id = id;
    S.follow = false;
    $('#content').replaceChildren(el('p', { class: 'muted' }, 'Loading…'));
    drawList();
    connect(id);
  }
  await refreshDetail();
  if (S.detail && S.detail.status.phase === 'running') {
    S.follow = true;
    scrollToBottom();
  } else {
    $('#main').scrollTop = 0;
  }
}

// ---------------------------------------------------------------- live events

function connect(id) {
  const source = new EventSource(`/api/discussions/${encodeURIComponent(id)}/events?after=0&token=${encodeURIComponent(TOKEN)}`);
  source.onmessage = (message) => {
    if (S.source !== source) return;
    onEvent(JSON.parse(message.data));
  };
  S.source = source;
}

function liveEntry(key) {
  if (!S.live.has(key)) S.live.set(key, { activity: [], text: '', started: null, thinking: 0, last: 'start' });
  return S.live.get(key);
}

function onEvent(ev) {
  const key = `${ev.round}:${ev.agent}`;
  switch (ev.type) {
    case 'agent_started':
      S.live.set(key, { activity: [], text: '', started: ev.t, thinking: 0, last: 'start' });
      markDirty(key);
      refreshSoon();
      break;
    case 'activity':
      liveEntry(key).activity.push({ kind: ev.kind, text: ev.text });
      liveEntry(key).last = 'activity';
      markDirty(key);
      break;
    case 'progress':
      liveEntry(key).thinking = ev.thinking_tokens;
      liveEntry(key).last = 'thinking';
      markDirty(key);
      break;
    case 'message': {
      const entry = liveEntry(key);
      entry.activity.push({ kind: 'message', text: firstLine(ev.text) });
      entry.text = '';
      entry.last = 'activity';
      markDirty(key);
      break;
    }
    case 'text_delta':
      liveEntry(key).text += ev.text;
      markDirty(key);
      break;
    case 'notice':
      if (ev.t > S.loadedAt) toast(ev.text, ev.level === 'error' ? 'error' : 'info');
      break;
    default: // round_started, round_finished, agent_finished, runner
      refreshSoon();
  }
}

function markDirty(key) {
  S.dirty.add(key);
  // A timer rather than requestAnimationFrame: animation frames can stop firing
  // (in embedded browser panes, background tabs), which froze the live view.
  if (!S.frame) S.frame = setTimeout(flushDirty, 100);
}

function flushDirty() {
  S.frame = 0;
  for (const key of S.dirty) {
    const [round, agent] = key.split(':');
    patchCard(round, agent);
  }
  S.dirty.clear();
  if (S.follow) scrollToBottom();
}

function refreshSoon() {
  clearTimeout(S.refreshTimer);
  S.refreshTimer = setTimeout(refreshDetail, 150);
}

async function refreshDetail() {
  const id = S.id;
  if (!id) return;
  let data;
  try {
    data = await api('GET', `/api/discussions/${encodeURIComponent(id)}`);
  } catch (error) {
    if (!S.detail) $('#content').replaceChildren(el('p', { class: 'muted' }, error.message));
    return;
  }
  if (S.id !== id) return;
  const json = JSON.stringify(data);
  if (json === S.detailJson) return;
  S.detail = data;
  S.detailJson = json;
  drawDiscussion();
}

function scrollToBottom() {
  const main = $('#main');
  main.scrollTop = main.scrollHeight;
}

// ---------------------------------------------------------------- one discussion

function drawDiscussion() {
  const d = S.detail;
  const main = $('#main');
  const scroll = main.scrollTop;
  $('#content').replaceChildren(el('div', { class: 'discussion' },
    header(d),
    el('section', { class: 'question-card' },
      el('div', { class: 'section-label' }, 'Question'),
      el('div', { class: 'md', html: md(d.question) })),
    d.rounds.map(roundSection),
    d.rounds.length ? null : el('p', { class: 'muted' }, 'Starting…')));
  for (const r of d.rounds) for (const agent of Object.keys(r.entries)) patchCard(r.index, agent);
  drawPending(d);
  $('#composer').hidden = false;
  if (S.follow) scrollToBottom();
  else main.scrollTop = scroll;
}

function header(d) {
  const status = d.status;
  let label = status.label;
  if (status.phase === 'running' && d.control.paused) label += ' · will pause after this round';
  return el('header', { class: 'dhead' },
    el('h1', {}, d.title),
    el('div', { class: 'meta' },
      el('code', { title: 'Folder the agents work in' }, tilde(d.cwd)),
      AGENTS.map((agent) => el('span', { class: `runs-with ${agent}` }, runsWith(d, agent))),
      el('span', {}, d.settings.web === false ? 'no web access' : 'web access for both'),
      el('span', {}, `started ${ago(d.created_at)}`),
      el('span', {}, d.id),
      el('button', { class: 'link', onclick: () => copy(d.transcript_path) }, 'Copy transcript path'),
      resumeLinks(d)),
    el('div', { class: 'status-row' },
      el('span', { class: `status phase-${status.phase}` }, el('span', { class: 'dot' }), label),
      el('span', { class: 'actions' }, actions(d))),
    status.detail ? el('div', { class: `status-detail phase-${status.phase}` }, status.detail) : null,
    planControls(d));
}

function runsWith(d, agent) {
  const chosen = d.settings[agent] || {};
  const model = chosen.model ? modelName(agent, chosen.model) : 'default model';
  const letter = d.labels && d.labels[agent] ? ` (member ${d.labels[agent]})` : '';
  return `${NAMES[agent]}${letter}: ${model}, ${chosen.effort ? `effort ${chosen.effort}` : 'default effort'}`;
}

function resumeLinks(d) {
  const quote = (path) => `'${path.replace(/'/g, "'\\''")}'`;
  const links = [];
  if (d.sessions.claude) {
    links.push(el('button', {
      class: 'link',
      title: 'Copies a command that opens this Claude session in your terminal',
      onclick: () => copy(`cd ${quote(d.cwd)} && claude --resume ${d.sessions.claude}`),
    }, 'Copy Claude resume command'));
  }
  if (d.sessions.codex) {
    links.push(el('button', {
      class: 'link',
      title: 'Copies a command that opens this Codex session in your terminal',
      onclick: () => copy(`cd ${quote(d.cwd)} && codex resume ${d.sessions.codex}`),
    }, 'Copy Codex resume command'));
  }
  return links;
}

function button(text, onclick, kind = '') {
  return el('button', { class: `btn small ${kind}`, type: 'button', onclick }, text);
}

function path(...parts) {
  return `/api/discussions/${encodeURIComponent(S.id)}${parts.length ? '/' + parts.join('/') : ''}`;
}

// Moving the discussion forward also follows it down the page to the new round.
function follow() {
  S.follow = true;
  scrollToBottom();
}

const control = (changes) => attempt(async () => {
  await api('POST', path('control'), changes);
  if (changes.paused === false) follow();
  refreshSoon();
});
const request = (body) => attempt(async () => {
  await api('POST', path('requests'), body);
  follow();
  refreshSoon();
});

function actions(d) {
  const next = d.status.next;
  switch (d.status.phase) {
    case 'running':
      return [
        d.control.paused
          ? button("Don't pause", () => control({ paused: false }), 'ghost')
          : button('Pause after this round', () => control({ paused: true }), 'ghost'),
        button('Stop now', () => attempt(async () => { await api('POST', path('stop')); refreshSoon(); }), 'danger'),
      ];
    case 'stopped':
      return [button('Retry and continue', () => control({ paused: false }), 'primary')];
    case 'ready':
    case 'paused':
      // Continuing runs exactly the next round when "pause after each round" is on.
      return [button(next ? `Next: ${next}` : 'Continue', () => control({ paused: false }), 'primary')];
    case 'finished':
      return [button('One more round', () => request({ type: 'next' }))];
    default:
      return [];
  }
}

function planControls(d) {
  const c = d.control;
  const counts = [...new Set([0, 1, 2, 3, 4, 5, 6, 8, 10, c.max_rounds])].sort((a, b) => a - b);
  const rounds = el('select', { onchange: (e) => control({ max_rounds: Number(e.target.value) }) },
    counts.map((n) => el('option', { value: n, selected: n === c.max_rounds }, n)));
  const pause = el('input', {
    type: 'checkbox',
    checked: c.pause_after_each_round,
    onchange: (e) => control({ pause_after_each_round: e.target.checked }),
  });
  return el('div', { class: 'plan' },
    el('label', { title: 'Each round: everyone comments on the others\' answers, then everyone updates their own. It stops earlier once nobody has objections.' },
      'Rounds of comments and updates', rounds),
    el('label', {}, pause, 'Pause after each round'));
}

const ROUND_TITLES = {
  answer: 'Independent answers',
  comment: "Each comments on the other's answer",
  update: 'Each updates their answer',
  // Kinds from discussions made by earlier versions of duet.
  review: "Each reviews the other's answer",
  discuss: 'Discussion',
  final: 'Final answer',
};

function roundSection(r) {
  const agents = AGENTS.filter((a) => r.entries[a]);
  const title = r.kind === 'answer' && r.index > 0 ? 'Follow-up question, independent answers' : ROUND_TITLES[r.kind];
  return el('section', { class: 'round', id: `round-${r.index}` },
    el('h2', {}, el('span', { class: 'num' }, `Round ${r.index}`), title, tally(r)),
    r.kind === 'answer' && r.index > 0
      ? el('div', { class: 'followup' }, el('div', { class: 'section-label' }, 'Your follow-up'), el('div', { class: 'md', html: md(r.question) }))
      : null,
    el('div', { class: `pair${agents.length === 1 ? ' single' : ''}` }, agents.map((a) => card(r, a))));
}

function tally(r) {
  const done = AGENTS.filter((a) => r.entries[a] && r.entries[a].status === 'done');
  if (done.length < 2) return null;
  if (r.kind === 'comment') {
    if (done.every((a) => r.entries[a].objections === 0)) {
      return el('span', { class: 'tally ok' }, 'No objections on either side: they agree');
    }
    return el('span', { class: 'tally' },
      `Objections: ${done.map((a) => `${NAMES[a]} ${r.entries[a].objections ?? '?'}`).join(' · ')}`);
  }
  if (r.kind === 'review' || r.kind === 'discuss') {
    return el('span', { class: 'tally' },
      `Disagreements left: ${done.map((a) => `${NAMES[a]} ${r.entries[a].remaining ?? '?'}`).join(' · ')}`);
  }
  return null;
}

function subtitle(kind, agent, index) {
  const other = NAMES[agent === 'claude' ? 'codex' : 'claude'];
  return {
    answer: index > 0 ? 'its own answer to the follow-up' : 'its own answer',
    comment: `commenting on ${other}'s answer`,
    update: 'its updated answer',
    review: `reviewing ${other}'s answer`,
    discuss: `replying to ${other}`,
    final: 'final answer',
  }[kind];
}

function countBadge(e) {
  if (e.objections != null) {
    return el('span', { class: `badge${e.objections === 0 ? ' ok' : ''}` },
      e.objections === 0 ? 'No objections' : plural(e.objections, 'objection'));
  }
  if (e.remaining != null) { // from earlier versions of duet
    return el('span', { class: `badge${e.remaining === 0 ? ' ok' : ''}` },
      e.remaining === 0 ? 'No disagreements left' : `${plural(e.remaining, 'disagreement')} left`);
  }
  return null;
}

function stateText(e) {
  if (e.status === 'running') return 'working';
  if (e.status === 'done') return fmtDuration(e.duration_s);
  if (e.status === 'failed') return e.error === 'stopped' || e.error === 'interrupted' ? 'stopped' : 'failed';
  return 'waiting';
}

function card(r, agent) {
  const e = r.entries[agent];
  const key = `${r.index}:${agent}`;
  const running = e.status === 'running';
  const open = S.activityOpen.has(key) ? S.activityOpen.get(key) : running;
  // Remember only what the user opens or closes; a card opens its activity
  // while the agent works and closes it once the reply is in.
  const activity = el('details', { class: 'activity', open },
    el('summary', { onclick: () => S.activityOpen.set(key, !activity.open) }, 'Activity'), el('ol'));
  let body = null;
  if (running) body = el('div', { class: 'live md' });
  else if (e.status === 'done') body = el('div', { class: 'reply md', html: md(e.reply) });
  else if (e.status === 'failed') {
    body = el('div', { class: 'error' },
      e.error === 'stopped' || e.error === 'interrupted' ? 'Stopped before it finished.' : `Failed: ${e.error}`);
  } else body = el('div', { class: 'waiting muted' }, 'Waiting to start.');
  return el('article', {
    class: `agent-card ${agent} status-${e.status}`,
    id: `card-${r.index}-${agent}`,
    dataset: { round: r.index, agent },
  },
  el('header', {},
    el('span', { class: 'agent-name' }, NAMES[agent]),
    S.detail && S.detail.labels ? el('span', { class: 'member-letter', title: 'The letter the members know each other by' }, `member ${S.detail.labels[agent]}`) : null,
    el('span', { class: 'sub' }, subtitle(r.kind, agent, r.index)),
    el('span', { class: 'state' }, stateText(e))),
  e.notes.length
    ? el('div', { class: 'notes' }, e.notes.map((n) => el('div', { class: 'note' },
      el('div', { class: 'label' }, n.shared ? 'Your note to both' : `Your note to ${NAMES[agent]}`),
      el('div', { class: 'md', html: md(n.text) }))))
    : null,
  activity,
  body,
  el('footer', {},
    countBadge(e),
    el('span', { class: 'spacer' }),
    el('button', { class: 'link', type: 'button', onclick: () => showPrompt(r.index, agent) }, 'What duet sent'),
    e.status === 'done' ? el('button', { class: 'link', type: 'button', onclick: () => copy(e.reply) }, 'Copy reply') : null));
}

function patchCard(round, agent) {
  const node = document.getElementById(`card-${round}-${agent}`);
  if (!node) return;
  const live = S.live.get(`${round}:${agent}`);
  const running = node.classList.contains('status-running');
  let items = live ? live.activity : [];
  // The reply is shown in full below, so don't repeat it as the last step.
  const entry = S.detail && S.detail.rounds[Number(round)] && S.detail.rounds[Number(round)].entries[agent];
  const last = items[items.length - 1];
  if (entry && entry.reply && last && last.kind === 'message' && last.text === firstLine(entry.reply)) {
    items = items.slice(0, -1);
  }
  const details = node.querySelector('.activity');
  const ol = details.querySelector('ol');
  let drawn = Number(ol.dataset.count || 0);
  if (drawn > items.length) {
    ol.replaceChildren();
    drawn = 0;
  }
  const atBottom = ol.scrollTop + ol.clientHeight >= ol.scrollHeight - 8;
  for (const item of items.slice(drawn)) ol.append(el('li', { class: `k-${item.kind}`, title: item.text }, item.text));
  ol.dataset.count = items.length;
  if (atBottom) ol.scrollTop = ol.scrollHeight;
  details.querySelector('summary').textContent = items.length ? `Activity · ${plural(items.length, 'step')}` : 'Activity';
  details.hidden = !items.length && !running;
  const liveNode = node.querySelector('.live');
  if (liveNode) {
    if (live && live.text) liveNode.innerHTML = md(live.text);
    else if (live && live.last === 'thinking') {
      // At high effort Claude can think for minutes before it writes or uses a tool.
      liveNode.innerHTML = `<p class="last-step"><span>Thinking:</span> about ${escapeHtml(fmtTokens(live.thinking))} tokens so far</p>`;
    } else {
      const last = items.length ? items[items.length - 1] : null;
      liveNode.innerHTML = last
        ? `<p class="last-step k-${escapeHtml(last.kind)}"><span>Last step:</span> ${escapeHtml(last.text)}</p>`
        : '<p class="last-step">Starting…</p>';
    }
  }
}

function tick() {
  if (!S.detail) return;
  for (const node of document.querySelectorAll('.agent-card.status-running')) {
    const { round, agent } = node.dataset;
    const live = S.live.get(`${round}:${agent}`);
    const entry = S.detail.rounds[Number(round)] && S.detail.rounds[Number(round)].entries[agent];
    const start = (live && live.started) || parseTime(entry && entry.started_at);
    if (start) node.querySelector('header .state').textContent = `working ${fmtDuration(Date.now() / 1000 - start)}`;
  }
}

async function showPrompt(round, agent) {
  const data = await attempt(() => api('GET', `${path('prompt')}?round=${round}&agent=${agent}`));
  if (!data) return;
  $('#prompt-title').textContent = `What duet sent to ${NAMES[agent]} in round ${round}`;
  $('#prompt-text').textContent = data.text;
  $('#prompt-copy').onclick = () => copy(data.text);
  $('#prompt-dialog').showModal();
}

// ---------------------------------------------------------------- notes, follow-ups, queue

const MODES = {
  both: { placeholder: 'Add your view, a correction, or something to check…', hint: 'Goes into the next message to both agents.' },
  claude: { placeholder: 'Something only Claude should hear…', hint: 'Goes into the next message to Claude only.' },
  codex: { placeholder: 'Something only Codex should hear…', hint: 'Goes into the next message to Codex only.' },
  ask: { placeholder: 'Ask a follow-up question…', hint: 'Asked once the current round is done. Each agent answers on its own first.' },
};

function setMode(mode) {
  S.mode = mode;
  for (const b of document.querySelectorAll('.modes button')) b.classList.toggle('on', b.dataset.mode === mode);
  $('#composer-text').placeholder = MODES[mode].placeholder;
  $('#composer-hint').textContent = `${MODES[mode].hint} ⌘↩ sends.`;
}

async function send() {
  const box = $('#composer-text');
  const text = box.value.trim();
  if (!text || !S.id) return;
  const ok = await attempt(async () => {
    if (S.mode === 'ask') await api('POST', path('requests'), { type: 'ask', question: text });
    else await api('POST', path('notes'), { text, to: S.mode });
    return true;
  });
  if (!ok) return;
  box.value = '';
  if (S.mode === 'ask') follow();
  toast(S.mode === 'ask' ? 'Follow-up question queued.' : 'Note saved for the next round.');
  refreshSoon();
}

function drawPending(d) {
  const chips = [];
  const seen = new Set();
  for (const agent of AGENTS) {
    (d.inbox.notes[agent] || []).forEach((note, index) => {
      if (note.shared && seen.has(note.text)) return;
      if (note.shared) seen.add(note.text);
      chips.push(el('span', { class: 'chip', title: note.text },
        el('span', { class: 'text' }, `${note.shared ? 'Note to both' : `Note to ${NAMES[agent]}`}: ${note.text}`),
        el('button', {
          type: 'button',
          title: 'Withdraw this note',
          onclick: () => attempt(async () => { await api('DELETE', path('notes'), { agent, index }); refreshSoon(); }),
        }, '×')));
    });
  }
  const describe = (r) => (r.type === 'ask' ? `Follow-up: ${r.question}`
    : 'One more round');
  for (const r of d.inbox.requests) {
    chips.push(el('span', { class: 'chip', title: describe(r) },
      el('span', { class: 'text' }, `Queued: ${describe(r)}`),
      el('button', {
        type: 'button',
        title: 'Cancel',
        onclick: () => attempt(async () => { await api('DELETE', path('requests', r.id)); refreshSoon(); }),
      }, '×')));
  }
  $('#pending').replaceChildren(...chips);
}

// ---------------------------------------------------------------- model pickers
// Codex's models come from `codex debug models` through the server; Claude's
// list is built into duet. "Default" means duet passes nothing, so each tool
// uses its own settings.

const EFFORT_ORDER = ['minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'];
const CLAUDE_ALIASES = { fable: 'claude-fable-5-1', opus: 'claude-opus-5-5', sonnet: 'claude-sonnet-5-5', haiku: 'claude-haiku-5-5' };

async function loadModels(refresh = false) {
  if (S.models && !refresh) return S.models;
  const models = await api('GET', `/api/models${refresh ? '?refresh=1' : ''}`).catch(() => null);
  if (models) S.models = models;
  return S.models;
}

function findModel(agent, id) {
  const entry = S.models && S.models[agent];
  if (!entry || !id) return null;
  let base = String(id).trim().replace(/\[[^\]]*\]$/, '');
  if (agent === 'claude') base = CLAUDE_ALIASES[base] || base;
  return entry.models.find((m) => m.id === base) || null;
}

function modelName(agent, id) {
  const model = findModel(agent, id);
  if (!model) return id;
  const suffix = String(id).match(/\[[^\]]*\]$/);
  return suffix ? `${model.name} ${suffix[0]}` : model.name;
}

function defaultModelLabel(agent) {
  const own = S.models && S.models[agent].defaults.model;
  return own ? `Default: ${modelName(agent, own)}` : `Default: ${NAMES[agent]}'s own choice`;
}

function defaultEffortLabel(agent, modelId) {
  const entry = S.models && S.models[agent];
  const own = entry && entry.defaults.effort;
  if (agent === 'codex') {
    if (own) return `Default: ${own}, from your Codex config`;
    const model = findModel('codex', modelId || (entry && entry.defaults.model));
    return model && model.default_effort ? `Default: ${model.default_effort}` : 'Default';
  }
  return own ? `Default: ${own}, from your Claude Code settings` : "Default: Claude Code's own";
}

function allLevels(models) {
  const levels = new Set(models.flatMap((m) => m.efforts));
  return EFFORT_ORDER.filter((level) => levels.has(level));
}

function modelPicker(agent, chosen) {
  const entry = S.models[agent];
  const models = entry.models;
  const chosenModel = chosen.model || '';
  const listed = !chosenModel || models.some((m) => m.id === chosenModel);
  const modelSelect = el('select', { name: `${agent}_model_choice`, 'aria-label': `${NAMES[agent]} model` },
    el('option', { value: '' }, defaultModelLabel(agent)),
    models.map((m) => el('option', { value: m.id, selected: m.id === chosenModel, title: m.description || m.id }, m.name)),
    el('option', { value: '__other', selected: !listed }, 'Other model id…'));
  const other = el('input', {
    type: 'text',
    name: `${agent}_model_other`,
    value: listed ? '' : chosenModel,
    placeholder: 'model id',
    spellcheck: 'false',
    hidden: listed,
  });
  const effortSelect = el('select', { name: `${agent}_effort`, 'aria-label': `${NAMES[agent]} effort` });
  let wanted = chosen.effort || '';
  const fill = () => {
    other.hidden = modelSelect.value !== '__other';
    const id = modelSelect.value === '__other' ? other.value : (modelSelect.value || entry.defaults.model);
    const model = findModel(agent, id);
    // Only offer the levels this model accepts; keep the choice when it still fits.
    const levels = model ? model.efforts : allLevels(models);
    effortSelect.replaceChildren(
      el('option', { value: '' }, defaultEffortLabel(agent, id)),
      ...levels.map((level) => el('option', { value: level }, level)));
    effortSelect.value = levels.includes(wanted) ? wanted : '';
  };
  modelSelect.addEventListener('change', fill);
  other.addEventListener('input', fill);
  effortSelect.addEventListener('change', () => { wanted = effortSelect.value; });
  fill();
  return el('div', { class: `model-row ${agent}` },
    el('span', { class: 'agent-name' }, NAMES[agent]),
    modelSelect,
    other,
    el('span', { class: 'muted' }, 'effort'),
    effortSelect);
}

function pickedModels(form) {
  const data = new FormData(form);
  const picked = {};
  for (const agent of AGENTS) {
    const choice = data.get(`${agent}_model_choice`);
    if (choice === null) continue; // the pickers haven't loaded
    const model = choice === '__other' ? String(data.get(`${agent}_model_other`) || '').trim() : choice;
    picked[agent] = { model, effort: data.get(`${agent}_effort`) || '' };
  }
  return picked;
}

function modelsHint() {
  const codex = S.models.codex;
  const when = codex.checked_at ? ` on ${new Date(codex.checked_at * 1000).toLocaleString()}` : '';
  return `Codex's list comes from ${codex.source}${when}; Claude's is built into duet. `
    + 'Default means duet passes nothing, so each tool uses its own settings.';
}

// ---------------------------------------------------------------- new question

async function showNewQuestion() {
  leaveDiscussion();
  $('#composer').hidden = true;
  drawList();
  const folders = await api('GET', '/api/folders').catch(() => ({ default: '', recent: [] }));
  const saved = prefs();
  const missing = AGENTS.filter((a) => S.info && !S.info.agents[a]);
  const cwdDefault = saved.cwd || folders.recent[0] || folders.default;
  // Saved under new names: the old "max_rounds" and "pause" meant review rounds and
  // a default of not pausing, from before the council.
  const rounds = saved.rounds_of_comments ?? 2;
  const pickers = el('div', { class: 'models' }, el('span', { class: 'muted' }, 'Loading the model lists…'));
  const pickersHint = el('div', { class: 'hint' });
  const drawPickers = (chosen) => {
    if (!S.models) {
      pickers.replaceChildren(el('span', { class: 'muted' }, "Couldn't load the model lists; each agent will use its default."));
      return;
    }
    pickers.replaceChildren(...AGENTS.map((a) => modelPicker(a, chosen[a] || {})));
    pickersHint.replaceChildren(modelsHint(), el('button', {
      class: 'link',
      type: 'button',
      onclick: async () => {
        const current = pickedModels(form);
        pickers.replaceChildren(el('span', { class: 'muted' }, 'Asking Codex for its models…'));
        await loadModels(true);
        drawPickers(current);
      },
    }, 'Ask Codex again'));
  };

  const form = el('form', { class: 'form', onsubmit: (e) => { e.preventDefault(); start(form); } },
    el('h1', {}, 'New question'),
    missing.length
      ? el('div', { class: 'warning' }, `The server can't find the ${missing.join(' and ')} command on its PATH.`)
      : null,
    el('div', { class: 'field' },
      el('label', { for: 'q' }, 'Question'),
      el('textarea', {
        id: 'q',
        name: 'question',
        rows: 9,
        required: true,
        placeholder: 'Both agents get exactly this, and each answers on its own first.',
        onkeydown: (e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); form.requestSubmit(); } },
      })),
    el('div', { class: 'field' },
      el('label', { for: 'cwd' }, 'Folder the agents work in'),
      el('input', { id: 'cwd', name: 'cwd', type: 'text', list: 'folders', value: cwdDefault, required: true, spellcheck: 'false' }),
      el('datalist', { id: 'folders' }, [...new Set([...folders.recent, folders.default])].map((f) => el('option', { value: f }))),
      el('div', { class: 'hint' }, 'They can read everything in it and run read-only commands, but not change files.')),
    el('div', { class: 'field' },
      el('div', { class: 'label' }, 'How it runs'),
      el('div', { class: 'options' },
        el('label', {}, 'Rounds of comments and updates',
          el('select', { name: 'max_rounds' }, [0, 1, 2, 3, 4, 5, 6, 8, 10].map((n) => el('option', { value: n, selected: n === rounds }, n)))),
        el('label', {}, el('input', { type: 'checkbox', name: 'pause', checked: saved.pause_after_each_round ?? true }), 'Pause after each round'),
        el('label', { title: "Both or neither. Claude: web search and fetching pages (not from this Mac's own servers). Codex: its web search." },
          el('input', { type: 'checkbox', name: 'web', checked: saved.web ?? true }), 'Members may use the web')),
      el('div', { class: 'hint' },
        "After the answers, each round has two steps: each comments on the other's answer, then each updates their own. "
        + 'It stops earlier once neither has objections. With pausing on, you move it on yourself after every round. '
        + 'You can change all of this while it runs.')),
    el('div', { class: 'field' },
      el('div', { class: 'label' }, 'Models'),
      pickers,
      pickersHint),
    el('details', {},
      el('summary', {}, 'Extra arguments and time limit'),
      el('div', { class: 'grid' },
        el('label', {}, 'Extra arguments for claude -p', el('input', { type: 'text', name: 'claude_args', value: saved.claude_args || '', placeholder: '--add-dir ../shared' })),
        el('label', {}, 'Extra arguments for codex exec', el('input', { type: 'text', name: 'codex_args', value: saved.codex_args || '', placeholder: '-c web_search="disabled"' })),
        el('label', {}, 'Give up on an agent after (minutes)', el('input', { type: 'text', name: 'timeout', value: saved.timeout || '', placeholder: '30' })))),
    el('div', { class: 'submit' },
      el('button', { class: 'btn primary', type: 'submit' }, 'Ask both'),
      el('span', { class: 'muted' }, '⌘↩')));
  $('#content').replaceChildren(form);
  $('#main').scrollTop = 0;
  $('#q').focus();
  await loadModels();
  drawPickers({
    claude: { model: saved.claude_model, effort: saved.claude_effort },
    codex: { model: saved.codex_model, effort: saved.codex_effort },
  });
}

async function start(form) {
  const data = Object.fromEntries(new FormData(form));
  const submit = form.querySelector('button[type="submit"]');
  submit.disabled = true;
  const picked = pickedModels(form);
  const pick = (agent, key) => (picked[agent] ? picked[agent][key] : undefined);
  prefs({
    cwd: data.cwd,
    rounds_of_comments: Number(data.max_rounds),
    pause_after_each_round: data.pause === 'on',
    web: data.web === 'on',
    claude_model: pick('claude', 'model'),
    claude_effort: pick('claude', 'effort'),
    codex_model: pick('codex', 'model'),
    codex_effort: pick('codex', 'effort'),
    claude_args: data.claude_args,
    codex_args: data.codex_args,
    timeout: data.timeout,
  });
  const created = await attempt(() => api('POST', '/api/discussions', {
    question: data.question,
    cwd: data.cwd,
    max_rounds: Number(data.max_rounds),
    pause_after_each_round: data.pause === 'on',
    web: data.web === 'on',
    claude_model: pick('claude', 'model'),
    claude_effort: pick('claude', 'effort'),
    codex_model: pick('codex', 'model'),
    codex_effort: pick('codex', 'effort'),
    claude_args: data.claude_args,
    codex_args: data.codex_args,
    timeout_minutes: data.timeout ? Number(data.timeout) : null,
  }));
  submit.disabled = false;
  if (!created) return;
  await refreshList();
  location.hash = `#/d/${created.id}`;
}

// ---------------------------------------------------------------- start up

function init() {
  for (const b of document.querySelectorAll('.modes button')) b.addEventListener('click', () => setMode(b.dataset.mode));
  setMode('both');
  $('#composer-send').addEventListener('click', send);
  $('#composer-text').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      send();
    }
  });
  $('#prompt-close').addEventListener('click', () => $('#prompt-dialog').close());
  $('#main').addEventListener('scroll', () => {
    const main = $('#main');
    S.follow = main.scrollTop + main.clientHeight >= main.scrollHeight - 80;
  }, { passive: true });
  window.addEventListener('hashchange', route);
  setUpNotifications();
  setInterval(tick, 1000);
  setInterval(refreshList, 2000);
  setInterval(() => { if (S.id && !document.hidden) refreshDetail(); }, 5000);
  loadModels().then(() => { if (S.detail) drawDiscussion(); });
  api('GET', '/api/info').then((info) => { S.info = info; }).catch(() => {}).finally(async () => {
    await refreshList();
    route();
  });
}

init();
