'use strict';

// The fugue page: a list of discussions on the left, the selected discussion on
// the right, updated live from the server's event stream.

const TOKEN = document.querySelector('meta[name="fugue-token"]').content;
const TOOLS = ['claude', 'codex'];
const NAMES = { claude: 'Claude', codex: 'Codex' };
const TOOL_LABELS = { claude: 'Claude Code', codex: 'Codex' };

const S = {
  list: [],
  listJson: '',
  listDrawnAt: 0,
  id: null,             // the discussion on screen
  detail: null,
  detailJson: '',
  live: new Map(),      // "round:member letter" -> { activity: [], text, started }
  source: null,         // EventSource for the discussion on screen
  dirty: new Set(),
  frame: 0,
  refreshTimer: 0,
  follow: false,        // keep the view scrolled to the bottom while it runs
  activityOpen: new Map(),
  roundOpen: new Map(), // "discussion:round" -> open or folded, where the user chose
  menuOpen: false,      // the ⋯ menu in the discussion's bar
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
    headers: { 'X-Fugue-Token': TOKEN, 'Content-Type': 'application/json' },
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
    const saved = JSON.parse(localStorage.getItem('fugue-new-question') || '{}');
    if (update) localStorage.setItem('fugue-new-question', JSON.stringify({ ...saved, ...update }));
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
  document.title = waiting ? `(${waiting}) fugue` : 'fugue';
  updateNavCounts();
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

// On narrow windows the list of discussions slides in from the left, opened
// by the ☰ button at the top of each page.

function navToggle() {
  return el('button', { class: 'nav-toggle btn ghost small', type: 'button', title: 'Discussions', 'aria-label': 'Show discussions', onclick: () => setNav(true) },
    '☰', el('span', { class: 'count', hidden: true }));
}

function setNav(open) {
  document.body.classList.toggle('nav-open', open);
}

// The ☰ button counts the discussions that need you.
function updateNavCounts() {
  const waiting = S.list.filter((d) => d.status.needs_you).length;
  for (const count of document.querySelectorAll('.nav-toggle .count')) {
    count.textContent = waiting;
    count.hidden = !waiting;
  }
}

// ---------------------------------------------------------------- routing

function route() {
  setNav(false);
  const m = location.hash.match(/^#\/d\/([A-Za-z0-9._-]+)/);
  if (m) openDiscussion(m[1]);
  else if (location.hash === '#/settings') showSettings();
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
  S.menuOpen = false;
}

async function openDiscussion(id) {
  if (S.id !== id) {
    leaveDiscussion();
    S.id = id;
    S.follow = false;
    $('#content').replaceChildren(el('p', { class: 'muted' }, 'Loading…'));
    drawList();
  }
  await refreshDetail();
  // The stream starts once the members are known: old events name the tool, not the letter.
  if (S.id === id && !S.source) connect(id);
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

// Which member an event is about. Events from before members name the tool instead.
function eventMember(ev) {
  if (ev.member) return ev.member;
  const legacy = S.detail && S.detail.legacy_names;
  return (legacy && legacy[ev.agent]) || ev.agent;
}

function onEvent(ev) {
  const key = `${ev.round}:${eventMember(ev)}`;
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
    const [round, letter] = key.split(':');
    patchCard(round, letter);
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
  const before = S.detail && S.detail.status.phase;
  S.detail = data;
  S.detailJson = json;
  drawDiscussion();
  // Following a discussion as it finishes takes you to where it ended.
  if (before === 'running' && S.follow && hasResult(data)) jumpTo('result');
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
    discussionBar(d),
    infoSection(d),
    el('section', { class: 'question-card' },
      el('div', { class: 'section-label' }, 'Question'),
      el('div', { class: 'md', html: md(d.question) })),
    resultSection(d),
    d.rounds.map(roundSection),
    d.rounds.length ? null : el('p', { class: 'muted' }, 'Starting…')));
  for (const r of d.rounds) for (const letter of Object.keys(r.entries)) patchCard(r.index, letter);
  drawPending(d);
  drawModes(d);
  $('#composer').hidden = false;
  updateNavCounts();
  measureBar();
  if (S.follow) scrollToBottom();
  else main.scrollTop = scroll;
  markActiveChip();
}

// The bar at the top of a discussion, which stays in view while you scroll:
// title, status, what you can do next, the ⋯ menu, and a strip of rounds to jump to.
function discussionBar(d) {
  const status = d.status;
  let label = status.label;
  if (status.phase === 'running' && d.control.paused) label += ' · will pause after this round';
  return el('div', { class: 'dbar' },
    el('div', { class: 'dbar-top' },
      navToggle(),
      el('h1', { title: `${d.title}\n(click to go to the top)`, onclick: () => { S.follow = false; $('#main').scrollTop = 0; } }, d.title),
      el('span', { class: `status phase-${status.phase}` }, el('span', { class: 'dot' }), label),
      el('span', { class: 'actions' }, actions(d)),
      moreMenu(d)),
    roundStrip(d));
}

function moreMenu(d) {
  const running = d.status.phase === 'running';
  const close = () => {
    S.menuOpen = false;
    menu.open = false;
  };
  const item = (text, onclick, extra = {}) => el('button', {
    type: 'button',
    class: `menu-item${extra.danger ? ' danger' : ''}`,
    disabled: extra.disabled,
    title: extra.title,
    onclick: () => { close(); onclick(); },
  }, text);
  const menu = el('details', { class: 'menu', open: S.menuOpen, ontoggle: () => { S.menuOpen = menu.open; } },
    el('summary', { class: 'btn ghost small', title: 'More', 'aria-label': 'More actions' }, '⋯'),
    el('div', { class: 'menu-list' },
      item('Copy transcript path', () => copy(d.transcript_path)),
      letters(d).filter((letter) => d.resume[letter]).map((letter) => item(
        `Copy resume command for ${d.members[letter].name}`, () => copy(d.resume[letter]),
        { title: "Opens this member's session in your terminal, with its memory of this discussion" })),
      item('Copy discussion id', () => copy(d.id)),
      el('div', { class: 'menu-sep' }),
      item('Move to Trash…', () => deleteDiscussion(d), {
        danger: true,
        disabled: running,
        title: running ? 'Stop it first' : 'Move this discussion to the Trash',
      })));
  return menu;
}

// What scrolls away under the bar: where it runs, who the members are, and the plan.
function infoSection(d) {
  const status = d.status;
  // A finished discussion with answers shows its objection counts in "Where it ended".
  const detail = status.detail && !(status.phase === 'finished' && hasResult(d)) ? status.detail : null;
  return el('div', { class: 'dinfo' },
    el('div', { class: 'meta' },
      el('code', { title: 'Folder the members work in' }, tilde(d.cwd)),
      letters(d).map((letter) => el('span', { class: `runs-with ${memberClass(d, letter)}` }, runsWith(d, letter))),
      el('span', {}, d.settings.web === false ? 'no web access' : 'web access for both'),
      el('span', {}, `started ${ago(d.created_at)}`)),
    detail ? el('div', { class: `status-detail phase-${status.phase}` }, detail) : null,
    planControls(d));
}

// Rounds scrolled to by the strip land just below the bar.
function measureBar() {
  const bar = $('.dbar');
  $('#main').style.setProperty('--bar-h', `${bar ? bar.offsetHeight : 0}px`);
}

// ---------------------------------------------------------------- members
// A discussion's members by letter (A, B): each has a tool, model, effort and
// extra arguments, and a name from the server such as "Claude (member A)".

function letters(d) {
  return Object.keys(d.members);
}

function otherLetter(d, letter) {
  return letters(d).find((other) => other !== letter);
}

// The other member as the card for `letter` mentions them: with the letter when the two names match.
function otherName(d, letter) {
  const other = d.members[otherLetter(d, letter)];
  return other.short_name === d.members[letter].short_name ? other.name : other.short_name;
}

// Cards take their tool's colour; when both members use the same tool, the second gets another shade.
function memberClass(d, letter) {
  const tool = d.members[letter].tool;
  return letters(d).find((other) => d.members[other].tool === tool) === letter ? tool : `${tool} alt`;
}

function runsWith(d, letter) {
  const m = d.members[letter];
  const model = m.model ? modelName(m.tool, m.model) : 'default model';
  return `${NAMES[m.tool]} (member ${letter}): ${model}, ${m.effort ? `effort ${m.effort}` : 'default effort'}`;
}

async function deleteDiscussion(d) {
  const sure = window.confirm(`Move "${d.title}" to the Trash?\n\nYou can restore it from the Trash in Finder. `
    + "The members' own Claude and Codex sessions are kept.");
  if (!sure) return;
  if (!(await attempt(() => api('DELETE', path())))) return;
  toast('Moved to the Trash.');
  leaveDiscussion();
  await refreshList();
  location.hash = S.list.length ? `#/d/${S.list[0].id}` : '#/new';
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
  // Kinds from discussions made by earlier versions of fugue.
  review: "Each reviews the other's answer",
  discuss: 'Discussion',
  final: 'Final answer',
};

// The same, in a word or two, for the strip of rounds.
const ROUND_WORDS = {
  answer: 'answers',
  comment: 'comments',
  update: 'updates',
  review: 'reviews',
  discuss: 'discussion',
  final: 'final answers',
};

const ANSWER_KINDS = new Set(['answer', 'update', 'final']);

function roundTitle(r) {
  return r.kind === 'answer' && r.index > 0 ? 'Follow-up question, independent answers' : ROUND_TITLES[r.kind];
}

// ---------------------------------------------------------------- folding rounds
// Earlier rounds fold into one line each. Open by default: the latest round,
// and while a round runs, the one before it, whose replies the members are
// working from. A finished discussion shows "Where it ended" instead, with
// every round folded. What the user opens or folds stays that way.

function roundIsOpen(d, r) {
  const key = `${d.id}:${r.index}`;
  if (S.roundOpen.has(key)) return S.roundOpen.get(key);
  if (hasResult(d)) return false;
  const last = d.rounds.length - 1;
  if (r.index === last) return true;
  const running = Object.values(d.rounds[last].entries).some((e) => e.status === 'running');
  return running && r.index === last - 1;
}

function toggleRound(r) {
  S.roundOpen.set(`${S.detail.id}:${r.index}`, !roundIsOpen(S.detail, r));
  S.follow = false;
  drawDiscussion();
}

// A member as a folded round's one line names them: the tool, or the letter when both use the same tool.
function briefName(d, letter) {
  const tool = d.members[letter].tool;
  return letters(d).every((other) => d.members[other].tool === tool) ? `member ${letter}` : NAMES[tool];
}

function roundSection(r) {
  const d = S.detail;
  const open = roundIsOpen(d, r);
  const shown = letters(d).filter((letter) => r.entries[letter]);
  return el('section', { class: `round${open ? '' : ' folded'}`, id: `round-${r.index}` },
    el('h2', {},
      el('button', { type: 'button', class: 'fold', 'aria-expanded': String(open), onclick: () => toggleRound(r) },
        el('span', { class: 'chev', 'aria-hidden': 'true' }, open ? '▾' : '▸'),
        el('span', { class: 'num' }, `Round ${r.index}`),
        roundTitle(r)),
      tally(r),
      open ? null : el('span', { class: 'brief' },
        shown.map((letter) => `${briefName(d, letter)} ${stateText(r.entries[letter])}`).join(' · '))),
    open && r.kind === 'answer' && r.index > 0
      ? el('div', { class: 'followup' }, el('div', { class: 'section-label' }, 'Your follow-up'), el('div', { class: 'md', html: md(r.question) }))
      : null,
    open ? el('div', { class: `pair${shown.length === 1 ? ' single' : ''}` }, shown.map((letter) => card(r, letter))) : null);
}

// ---------------------------------------------------------------- where it ended

// The rounds of the latest question: from the last round of independent answers on.
function currentRounds(d) {
  let start = 0;
  for (const r of d.rounds) if (r.kind === 'answer') start = r.index;
  return d.rounds.filter((r) => r.index >= start);
}

// Each member's latest finished answer to the latest question, with the round it is from.
function latestAnswers(d) {
  const latest = {};
  for (const r of currentRounds(d)) {
    if (!ANSWER_KINDS.has(r.kind)) continue;
    for (const [letter, e] of Object.entries(r.entries)) if (e.status === 'done') latest[letter] = r;
  }
  return latest;
}

function hasResult(d) {
  return d.status.phase === 'finished' && Object.keys(latestAnswers(d)).length > 0;
}

function resultSection(d) {
  if (!hasResult(d)) return null;
  const latest = latestAnswers(d);
  const shown = letters(d).filter((letter) => latest[letter]);
  const comments = currentRounds(d).filter((r) => ['comment', 'review', 'discuss'].includes(r.kind));
  const last = comments[comments.length - 1];
  const asked = currentRounds(d)[0];
  return el('section', { class: 'round result', id: 'round-result' },
    el('h2', {}, 'Where it ended', el('span', { class: 'num' }, d.status.label), last ? tally(last) : null),
    asked.index > 0
      ? el('div', { class: 'followup' }, el('div', { class: 'section-label' }, 'Your follow-up'), el('div', { class: 'md', html: md(asked.question) }))
      : null,
    el('div', { class: 'result-note muted' },
      "Each member's latest answer. The rounds that led here are folded below; open one to see how they got here."),
    el('div', { class: `pair${shown.length === 1 ? ' single' : ''}` }, shown.map((letter) => resultCard(d, letter, latest[letter]))));
}

function resultCard(d, letter, r) {
  const e = r.entries[letter];
  return el('article', { class: `agent-card ${memberClass(d, letter)} status-done` },
    el('header', {},
      el('span', { class: 'agent-name', title: runsWith(d, letter) }, d.members[letter].short_name),
      el('span', { class: 'member-letter', title: 'The letter the members know each other by' }, `member ${letter}`),
      el('span', { class: 'sub' }, `latest answer, from round ${r.index}`)),
    el('div', { class: 'reply md', html: md(e.reply) }),
    el('footer', {},
      el('span', { class: 'spacer' }),
      el('button', { class: 'link', type: 'button', onclick: () => jumpTo(r.index) }, `Go to round ${r.index}`),
      el('button', { class: 'link', type: 'button', onclick: () => copy(e.reply) }, 'Copy answer')));
}

// ---------------------------------------------------------------- jumping between rounds

function objectionCounts(r) {
  const d = S.detail;
  const done = letters(d).filter((letter) => r.entries[letter] && r.entries[letter].status === 'done');
  if (done.length < 2) return null;
  if (r.kind === 'comment') {
    if (done.every((letter) => r.entries[letter].objections === 0)) return el('span', { class: 'counts ok' }, 'agree');
    return el('span', { class: 'counts' }, done.map((letter) => `${letter} ${r.entries[letter].objections ?? '?'}`).join(' · '));
  }
  if (r.kind === 'review' || r.kind === 'discuss') {
    return el('span', { class: 'counts' }, done.map((letter) => `${letter} ${r.entries[letter].remaining ?? '?'}`).join(' · '));
  }
  return null;
}

function roundStrip(d) {
  if (!d.rounds.length) return null;
  const chips = [];
  if (hasResult(d)) {
    chips.push(el('button', { type: 'button', class: 'round-chip result', dataset: { target: 'round-result' }, onclick: () => jumpTo('result') },
      'Where it ended'));
  }
  for (const r of d.rounds) {
    const entries = Object.values(r.entries);
    const running = entries.some((e) => e.status === 'running');
    const failed = entries.some((e) => e.status === 'failed' && !['stopped', 'interrupted'].includes(e.error));
    const word = r.kind === 'answer' && r.index > 0 ? 'follow-up' : ROUND_WORDS[r.kind];
    chips.push(el('button', {
      type: 'button',
      class: `round-chip${running ? ' running' : ''}${failed ? ' failed' : ''}`,
      dataset: { target: `round-${r.index}` },
      title: `Round ${r.index}: ${roundTitle(r)}`,
      onclick: () => jumpTo(r.index),
    }, el('span', { class: 'n' }, r.index), ` ${word}`, objectionCounts(r)));
  }
  return el('nav', { class: 'round-strip', 'aria-label': 'Rounds' }, chips);
}

function jumpTo(index) {
  const d = S.detail;
  if (index !== 'result') {
    const r = d.rounds[index];
    if (!roundIsOpen(d, r)) {
      S.roundOpen.set(`${d.id}:${index}`, true);
      drawDiscussion();
    }
  }
  S.follow = false;
  const node = document.getElementById(index === 'result' ? 'round-result' : `round-${index}`);
  if (node) node.scrollIntoView({ block: 'start' });
}

// Highlights the round at the top of the view in the strip.
function markActiveChip() {
  const strip = $('.round-strip');
  if (!strip) return;
  const line = $('#main').getBoundingClientRect().top + ($('.dbar') ? $('.dbar').offsetHeight : 0) + 16;
  let current = null;
  for (const section of document.querySelectorAll('.discussion > section.round')) {
    if (section.getBoundingClientRect().top <= line) current = section.id;
  }
  for (const chip of strip.querySelectorAll('.round-chip')) {
    const on = chip.dataset.target === current;
    if (on && !chip.classList.contains('on')) chip.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    chip.classList.toggle('on', on);
  }
}

function tally(r) {
  const done = letters(S.detail).filter((letter) => r.entries[letter] && r.entries[letter].status === 'done');
  if (done.length < 2) return null;
  if (r.kind === 'comment') {
    if (done.every((letter) => r.entries[letter].objections === 0)) {
      return el('span', { class: 'tally ok' }, 'No objections on either side: they agree');
    }
    return el('span', { class: 'tally' },
      `Objections: ${done.map((letter) => `member ${letter} ${r.entries[letter].objections ?? '?'}`).join(' · ')}`);
  }
  if (r.kind === 'review' || r.kind === 'discuss') {
    return el('span', { class: 'tally' },
      `Disagreements left: ${done.map((letter) => `member ${letter} ${r.entries[letter].remaining ?? '?'}`).join(' · ')}`);
  }
  return null;
}

function subtitle(kind, letter, index) {
  const other = otherName(S.detail, letter);
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
  if (e.remaining != null) { // from earlier versions of fugue
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

function card(r, letter) {
  const d = S.detail;
  const e = r.entries[letter];
  const key = `${r.index}:${letter}`;
  const running = e.status === 'running';
  const open = S.activityOpen.has(key) ? S.activityOpen.get(key) : running;
  // Remember only what the user opens or closes; a card opens its activity
  // while the member works and closes it once the reply is in.
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
    class: `agent-card ${memberClass(d, letter)} status-${e.status}`,
    id: `card-${r.index}-${letter}`,
    dataset: { round: r.index, member: letter },
  },
  el('header', {},
    el('span', { class: 'agent-name', title: runsWith(d, letter) }, d.members[letter].short_name),
    el('span', { class: 'member-letter', title: 'The letter the members know each other by' }, `member ${letter}`),
    el('span', { class: 'sub' }, subtitle(r.kind, letter, r.index)),
    el('span', { class: 'state' }, stateText(e))),
  e.notes.length
    ? el('div', { class: 'notes' }, e.notes.map((n) => el('div', { class: 'note' },
      el('div', { class: 'label' }, n.shared ? 'Your note to both' : `Your note to member ${letter}`),
      el('div', { class: 'md', html: md(n.text) }))))
    : null,
  activity,
  body,
  el('footer', {},
    countBadge(e),
    el('span', { class: 'spacer' }),
    el('button', { class: 'link', type: 'button', onclick: () => showPrompt(r.index, letter) }, 'What fugue sent'),
    e.status === 'done' ? el('button', { class: 'link', type: 'button', onclick: () => copy(e.reply) }, 'Copy reply') : null));
}

function patchCard(round, letter) {
  const node = document.getElementById(`card-${round}-${letter}`);
  if (!node) return;
  const live = S.live.get(`${round}:${letter}`);
  const running = node.classList.contains('status-running');
  let items = live ? live.activity : [];
  // The reply is shown in full below, so don't repeat it as the last step.
  const entry = S.detail && S.detail.rounds[Number(round)] && S.detail.rounds[Number(round)].entries[letter];
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
    const { round, member } = node.dataset;
    const live = S.live.get(`${round}:${member}`);
    const entry = S.detail.rounds[Number(round)] && S.detail.rounds[Number(round)].entries[member];
    const start = (live && live.started) || parseTime(entry && entry.started_at);
    if (start) node.querySelector('header .state').textContent = `working ${fmtDuration(Date.now() / 1000 - start)}`;
  }
}

async function showPrompt(round, letter) {
  const data = await attempt(() => api('GET', `${path('prompt')}?round=${round}&member=${letter}`));
  if (!data) return;
  $('#prompt-title').textContent = `What fugue sent to ${S.detail.members[letter].name} in round ${round}`;
  $('#prompt-text').textContent = data.text;
  $('#prompt-copy').onclick = () => copy(data.text);
  $('#prompt-dialog').showModal();
}

// ---------------------------------------------------------------- notes, follow-ups, queue

function modes(d) {
  const all = {
    both: { label: 'To both members', placeholder: 'Add a note for both members…', hint: 'Goes into the next message to both members.' },
  };
  for (const letter of letters(d)) {
    const name = d.members[letter].name;
    all[letter] = { label: `To ${name}`, placeholder: `A note only ${name} will see…`, hint: `Goes into the next message to ${name} only.` };
  }
  all.ask = { label: 'Follow-up question', placeholder: 'Ask a follow-up question…', hint: 'Asked once the current round is done. Each member answers on its own first.' };
  return all;
}

function drawModes(d) {
  const all = modes(d);
  const select = $('#composer-mode');
  const wanted = JSON.stringify(Object.entries(all).map(([mode, m]) => [mode, m.label]));
  if (select.dataset.modes !== wanted) {
    select.dataset.modes = wanted;
    select.replaceChildren(...Object.entries(all).map(([mode, m]) => el('option', { value: mode }, m.label)));
  }
  setMode(all[S.mode] ? S.mode : 'both');
}

function setMode(mode) {
  const m = modes(S.detail)[mode];
  S.mode = mode;
  $('#composer-mode').value = mode;
  $('#composer-text').placeholder = m.placeholder;
  $('#composer-hint').textContent = `${m.hint} ⌘↩ sends.`;
}

// The note box is one line until you click into it or it has text.
function sizeComposer() {
  const box = $('.composer-box');
  const text = $('#composer-text');
  const open = box.contains(document.activeElement) || text.value.trim() !== '';
  box.classList.toggle('open', open);
  text.style.height = '';
  if (open) text.style.height = `${Math.min(Math.max(text.scrollHeight, 66), window.innerHeight * 0.4)}px`;
  measureBar();
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
  sizeComposer();
  if (S.mode === 'ask') follow();
  toast(S.mode === 'ask' ? 'Follow-up question queued.' : 'Note saved for the next round.');
  refreshSoon();
}

function drawPending(d) {
  const chips = [];
  const seen = new Set();
  for (const letter of letters(d)) {
    (d.inbox.notes[letter] || []).forEach((note, index) => {
      if (note.shared && seen.has(note.text)) return;
      if (note.shared) seen.add(note.text);
      chips.push(el('span', { class: 'chip', title: note.text },
        el('span', { class: 'text' }, `${note.shared ? 'Note to both' : `Note to member ${letter}`}: ${note.text}`),
        el('button', {
          type: 'button',
          title: 'Withdraw this note',
          onclick: () => attempt(async () => { await api('DELETE', path('notes'), { member: letter, index }); refreshSoon(); }),
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
// list is built into fugue. "Default" means fugue passes nothing, so each tool
// uses its own settings.

const EFFORT_ORDER = ['minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'];
const CLAUDE_ALIASES = { fable: 'claude-fable-5-1', opus: 'claude-opus-5-5', sonnet: 'claude-sonnet-5-5', haiku: 'claude-haiku-5-5' };

async function loadModels(refresh = false) {
  if (S.models && !refresh) return S.models;
  const models = await api('GET', `/api/models${refresh ? '?refresh=1' : ''}`).catch(() => null);
  if (models) S.models = models;
  return S.models;
}

function findModel(tool, id) {
  const entry = S.models && S.models[tool];
  if (!entry || !id) return null;
  let base = String(id).trim().replace(/\[[^\]]*\]$/, '');
  if (tool === 'claude') base = CLAUDE_ALIASES[base] || base;
  return entry.models.find((m) => m.id === base) || null;
}

function modelName(tool, id) {
  const model = findModel(tool, id);
  if (!model) return id;
  const suffix = String(id).match(/\[[^\]]*\]$/);
  return suffix ? `${model.name} ${suffix[0]}` : model.name;
}

function defaultModelLabel(tool) {
  const own = S.models && S.models[tool].defaults.model;
  return own ? `Default: ${modelName(tool, own)}` : `Default: ${NAMES[tool]}'s own choice`;
}

function defaultEffortLabel(tool, modelId) {
  const entry = S.models && S.models[tool];
  const own = entry && entry.defaults.effort;
  if (tool === 'codex') {
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

// One member's controls: the tool, then that tool's models and effort levels,
// then extra arguments. `onchange` runs whenever any of them changes.
function memberRow(index, chosen, onchange) {
  const row = el('div', { class: 'member-row', dataset: { index } });
  const tool = el('select', { 'aria-label': `Member ${index + 1}'s tool`, class: 'tool' },
    TOOLS.map((t) => el('option', { value: t, selected: t === chosen.tool }, TOOL_LABELS[t])));
  const picker = el('span', { class: 'picker' });
  const args = el('input', { type: 'text', class: 'args', value: chosen.args || '', spellcheck: 'false' });
  const drawPicker = (current) => {
    const t = tool.value;
    args.placeholder = `extra arguments for ${t === 'claude' ? 'claude -p' : 'codex exec'}, e.g. --add-dir ../shared`;
    picker.replaceChildren(S.models ? modelPicker(t, current) : el('span', { class: 'muted' }, 'default model and effort'));
  };
  tool.addEventListener('change', () => { drawPicker({}); onchange(); });
  row.addEventListener('change', onchange);
  row.addEventListener('input', onchange);
  drawPicker(chosen);
  row.append(el('span', { class: 'member-num' }, `Member ${index + 1}`), el('div', { class: 'member-body' }, tool, picker, args));
  row.read = () => {
    const modelChoice = row.querySelector('select.model');
    const model = !modelChoice ? chosen.model || ''
      : modelChoice.value === '__other' ? row.querySelector('input.model-other').value.trim() : modelChoice.value;
    const effortChoice = row.querySelector('select.effort');
    return { tool: tool.value, model, effort: effortChoice ? effortChoice.value : chosen.effort || '', args: args.value.trim() };
  };
  return row;
}

function modelPicker(tool, chosen) {
  const entry = S.models[tool];
  const models = entry.models;
  const chosenModel = (chosen.tool === undefined || chosen.tool === tool) ? chosen.model || '' : '';
  const listed = !chosenModel || models.some((m) => m.id === chosenModel);
  const modelSelect = el('select', { class: 'model', 'aria-label': `${NAMES[tool]} model` },
    el('option', { value: '' }, defaultModelLabel(tool)),
    models.map((m) => el('option', { value: m.id, selected: m.id === chosenModel, title: m.description || m.id }, m.name)),
    el('option', { value: '__other', selected: !listed }, 'Other model id…'));
  const other = el('input', {
    type: 'text',
    class: 'model-other',
    value: listed ? '' : chosenModel,
    placeholder: 'model id',
    spellcheck: 'false',
    hidden: listed,
  });
  const effortSelect = el('select', { class: 'effort', 'aria-label': `${NAMES[tool]} effort` });
  let wanted = chosen.effort || '';
  const fill = () => {
    other.hidden = modelSelect.value !== '__other';
    const id = modelSelect.value === '__other' ? other.value : (modelSelect.value || entry.defaults.model);
    const model = findModel(tool, id);
    // Only offer the levels this model accepts; keep the choice when it still fits.
    const levels = model ? model.efforts : allLevels(models);
    effortSelect.replaceChildren(
      el('option', { value: '' }, defaultEffortLabel(tool, id)),
      ...levels.map((level) => el('option', { value: level }, level)));
    effortSelect.value = levels.includes(wanted) ? wanted : '';
  };
  modelSelect.addEventListener('change', fill);
  other.addEventListener('input', fill);
  effortSelect.addEventListener('change', () => { wanted = effortSelect.value; });
  fill();
  return el('span', { class: 'model-row' }, modelSelect, other, el('span', { class: 'muted' }, 'effort'), effortSelect);
}

// The two member rows, with a hint under them, for the New question form and Settings.
function membersField(chosen) {
  const hint = el('div', { class: 'hint' });
  const warning = el('div', { class: 'hint warn-text' });
  const rows = [];
  const update = () => {
    const [a, b] = rows.map((row) => row.read());
    const sameModel = a.tool === b.tool && (a.model || '') === (b.model || '');
    warning.textContent = sameModel
      ? 'Both members run the same model, so they tend to share blind spots and miss the same things. '
        + 'Different models, or at least different efforts, catch more of each other\'s mistakes.'
      : '';
    warning.hidden = !sameModel;
  };
  const lineup = chosen.length === 2 ? chosen : [{ tool: 'claude' }, { tool: 'codex' }];
  lineup.forEach((member, index) => rows.push(memberRow(index, member, update)));
  update();
  if (S.models) {
    const codex = S.models.codex;
    const when = codex.checked_at ? ` on ${new Date(codex.checked_at * 1000).toLocaleString()}` : '';
    hint.replaceChildren(
      'Each member is Claude Code or Codex; both can be the same tool. They know each other only as member A and B, '
      + 'and the letters are drawn at random when the discussion starts. '
      + `Default means fugue passes nothing, so the tool uses its own settings. Codex's models come from ${codex.source}${when}; `
      + "Claude's list is built into fugue. ",
      el('button', {
        class: 'link',
        type: 'button',
        onclick: async () => {
          const current = rows.map((row) => row.read());
          box.replaceChildren(el('span', { class: 'muted' }, 'Asking Codex for its models…'));
          await loadModels(true);
          box.replaceWith(membersField(current));
        },
      }, 'Ask Codex again'));
  } else {
    hint.textContent = "Couldn't load the model lists; members whose model you don't set use their tool's default.";
  }
  const box = el('div', { class: 'members' }, rows, warning, hint);
  box.read = () => rows.map((row) => row.read());
  return box;
}

// ---------------------------------------------------------------- new question

async function showNewQuestion() {
  leaveDiscussion();
  $('#composer').hidden = true;
  drawList();
  const [folders, settings] = await Promise.all([
    api('GET', '/api/folders').catch(() => ({ default: '', recent: [] })),
    api('GET', '/api/settings').catch(() => null),
  ]);
  const defaults = settings ? settings.defaults : null;
  const saved = prefs();
  const missing = TOOLS.filter((t) => S.info && !S.info.agents[t]);
  const cwdDefault = saved.cwd || folders.recent[0] || folders.default;
  // Everything but the folder starts from the defaults on the Settings page,
  // including the members, so what the form shows is what runs.
  const rounds = defaults ? defaults.max_rounds : 2;
  const membersSlot = el('div', {}, el('span', { class: 'muted' }, 'Loading the model lists…'));

  const form = el('form', { class: 'form', onsubmit: (e) => { e.preventDefault(); start(form); } },
    el('h1', {}, navToggle(), 'New question'),
    missing.length
      ? el('div', { class: 'warning' }, `The server can't find the ${missing.join(' and ')} command on its PATH, so members can't use it.`)
      : null,
    el('div', { class: 'field' },
      el('label', { for: 'q' }, 'Question'),
      el('textarea', {
        id: 'q',
        name: 'question',
        rows: 9,
        required: true,
        placeholder: 'Both members get exactly this, and each answers on its own first.',
        onkeydown: (e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); form.requestSubmit(); } },
      })),
    el('div', { class: 'field' },
      el('label', { for: 'cwd' }, 'Folder the members work in'),
      el('input', { id: 'cwd', name: 'cwd', type: 'text', list: 'folders', value: cwdDefault, required: true, spellcheck: 'false' }),
      el('datalist', { id: 'folders' }, [...new Set([...folders.recent, folders.default])].map((f) => el('option', { value: f }))),
      el('div', { class: 'hint' }, 'They can read everything in it and run read-only commands, but not change files.')),
    el('div', { class: 'field' },
      el('div', { class: 'label' }, 'How it runs'),
      el('div', { class: 'options' },
        el('label', {}, 'Rounds of comments and updates',
          el('select', { name: 'max_rounds' }, [0, 1, 2, 3, 4, 5, 6, 8, 10].map((n) => el('option', { value: n, selected: n === rounds }, n)))),
        el('label', {}, el('input', { type: 'checkbox', name: 'pause', checked: defaults ? defaults.pause_after_each_round : true }), 'Pause after each round'),
        el('label', { title: "Both or neither. Claude: web search and fetching pages (not from this Mac's own servers). Codex: its web search." },
          el('input', { type: 'checkbox', name: 'web', checked: defaults ? defaults.web : true }), 'Members may use the web')),
      el('div', { class: 'hint' },
        "After the answers, each round has two steps: each comments on the other's answer, then each updates their own. "
        + 'It stops earlier once neither has objections. With pausing on, you move it on yourself after every round. '
        + 'You can change all of this while it runs.')),
    el('div', { class: 'field' },
      el('div', { class: 'label' }, 'Members'),
      membersSlot),
    el('details', {},
      el('summary', {}, 'Time limit'),
      el('div', { class: 'grid' },
        el('label', {}, 'Give up on a member after (minutes)',
          el('input', { type: 'text', name: 'timeout', placeholder: String(defaults ? defaults.timeout_minutes : 30) })))),
    el('div', { class: 'submit' },
      el('button', { class: 'btn primary', type: 'submit' }, 'Ask both'),
      el('span', { class: 'muted' }, '⌘↩')));
  $('#content').replaceChildren(form);
  updateNavCounts();
  $('#main').scrollTop = 0;
  $('#q').focus();
  await loadModels();
  const members = membersField(defaults ? defaults.members : []);
  membersSlot.replaceChildren(members);
  form.members = members;
}

async function start(form) {
  const data = Object.fromEntries(new FormData(form));
  const submit = form.querySelector('button[type="submit"]');
  submit.disabled = true;
  prefs({ cwd: data.cwd });
  const created = await attempt(() => api('POST', '/api/discussions', {
    question: data.question,
    cwd: data.cwd,
    max_rounds: Number(data.max_rounds),
    pause_after_each_round: data.pause === 'on',
    web: data.web === 'on',
    members: form.members ? form.members.read() : null,
    timeout_minutes: data.timeout ? Number(data.timeout) : null,
  }));
  submit.disabled = false;
  if (!created) return;
  await refreshList();
  location.hash = `#/d/${created.id}`;
}

// ---------------------------------------------------------------- settings

async function showSettings() {
  leaveDiscussion();
  $('#composer').hidden = true;
  drawList();
  $('#content').replaceChildren(el('p', { class: 'muted' }, 'Loading…'));
  const [settings] = await Promise.all([api('GET', '/api/settings').catch((error) => ({ error: error.message })), loadModels()]);
  if (settings.error) {
    $('#content').replaceChildren(el('p', { class: 'muted' }, settings.error));
    return;
  }
  const d = settings.defaults;
  const members = membersField(d.members);
  const statusBox = el('div', { class: 'status-box' });
  const counts = [...new Set([0, 1, 2, 3, 4, 5, 6, 8, 10, d.max_rounds])].sort((a, b) => a - b);
  const form = el('form', { class: 'form settings', onsubmit: (e) => { e.preventDefault(); saveSettings(form); } },
    el('h1', {}, navToggle(), 'Settings'),
    el('h2', {}, 'Defaults for new discussions'),
    el('div', { class: 'hint' }, 'The New question form and ', el('code', {}, 'fugue start'),
      ' both start from these. They are saved to ', el('code', {}, tilde(settings.path)),
      '; saving rewrites that file, so comments in it are not kept.'),
    el('div', { class: 'field' },
      el('div', { class: 'options' },
        el('label', {}, 'Rounds of comments and updates',
          el('select', { name: 'max_rounds' }, counts.map((n) => el('option', { value: n, selected: n === d.max_rounds }, n)))),
        el('label', {}, el('input', { type: 'checkbox', name: 'pause', checked: d.pause_after_each_round }), 'Pause after each round'),
        el('label', {}, el('input', { type: 'checkbox', name: 'web', checked: d.web }), 'Members may use the web'),
        el('label', {}, 'Give up on a member after',
          el('input', { type: 'text', name: 'timeout', value: d.timeout_minutes, class: 'short' }), 'minutes'))),
    el('div', { class: 'field' }, el('div', { class: 'label' }, 'Members'), members),
    el('div', { class: 'submit' }, el('button', { class: 'btn primary', type: 'submit' }, 'Save defaults')),
    el('h2', {}, 'Status'),
    statusBox,
    el('div', { class: 'submit' }, el('button', { class: 'btn small', type: 'button', onclick: () => drawStatus(statusBox) }, 'Check again')));
  form.members = members;
  $('#content').replaceChildren(form);
  updateNavCounts();
  $('#main').scrollTop = 0;
  drawStatus(statusBox);
}

async function saveSettings(form) {
  const data = Object.fromEntries(new FormData(form));
  const body = {
    max_rounds: Number(data.max_rounds),
    pause_after_each_round: data.pause === 'on',
    web: data.web === 'on',
    timeout_minutes: Number(data.timeout),
    members: form.members.read(),
  };
  if (await attempt(() => api('POST', '/api/settings', body))) toast('Defaults saved.');
}

async function drawStatus(box) {
  box.replaceChildren(el('p', { class: 'muted' }, 'Checking…'));
  const s = await api('GET', '/api/status').catch((error) => ({ error: error.message }));
  if (s.error) {
    box.replaceChildren(el('p', { class: 'muted' }, s.error));
    return;
  }
  const dot = (state) => el('span', { class: `dot phase-${{ good: 'finished', warn: 'paused', bad: 'stopped' }[state]}` });
  const row = (state, label, ...value) => el('div', { class: 'status-line' }, dot(state), el('span', { class: 'status-label' }, label), el('span', {}, ...value));
  const toolRow = (t) => {
    const x = s.agents[t];
    if (!x.found) return row('bad', TOOL_LABELS[t], x.problem);
    return row(x.logged_in ? 'good' : 'bad', TOOL_LABELS[t], `${x.version} · ${x.login} · `, el('code', {}, tilde(x.path)));
  };
  const off = s.codex_switched_off;
  const short = (name) => name.split('@')[0];
  const offList = ['the apps connector'];
  if (off.plugins.length) offList.push(`${plural(off.plugins.length, 'plugin')} (${off.plugins.map(short).join(', ')})`);
  if (off.mcp_servers.length) offList.push(`the ${off.mcp_servers.join(', ')} ${off.mcp_servers.length === 1 ? 'server' : 'servers'}`);
  box.replaceChildren(
    TOOLS.map(toolRow),
    row('good', 'Codex extras',
      el('div', {}, `Off whenever Codex takes part in a discussion: ${offList.join(', ')}.`),
      el('div', { class: 'muted' }, "They could change files or act in your accounts, which Codex's read-only sandbox doesn't "
        + 'stop. When you use Codex yourself, they work as usual.')),
    row(s.service.running ? 'good' : 'warn', 'Background service', s.service.text),
    row('good', 'Discussions', `${s.storage.discussions} in `, el('code', {}, tilde(s.storage.path)), ` (${s.storage.megabytes} MB)`),
    row('good', 'This page', `fugue ${s.version} on port ${s.port}`));
}

// ---------------------------------------------------------------- start up

function init() {
  $('#composer-send').addEventListener('click', send);
  $('#composer-mode').addEventListener('change', (e) => setMode(e.target.value));
  const text = $('#composer-text');
  text.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      send();
    }
  });
  text.addEventListener('input', sizeComposer);
  // Wait for focus to settle, so moving between the box's own controls keeps it open.
  $('.composer-box').addEventListener('focusin', () => setTimeout(sizeComposer));
  $('.composer-box').addEventListener('focusout', () => setTimeout(sizeComposer));
  $('#prompt-close').addEventListener('click', () => $('#prompt-dialog').close());
  $('#nav-backdrop').addEventListener('click', () => setNav(false));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') setNav(false);
  });
  // A click anywhere outside the ⋯ menu closes it.
  document.addEventListener('click', (e) => {
    if (!S.menuOpen || e.target.closest('.menu')) return;
    S.menuOpen = false;
    for (const menu of document.querySelectorAll('details.menu[open]')) menu.open = false;
  });
  window.addEventListener('resize', measureBar);
  $('#main').addEventListener('scroll', () => {
    const main = $('#main');
    S.follow = main.scrollTop + main.clientHeight >= main.scrollHeight - 80;
    markActiveChip();
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
