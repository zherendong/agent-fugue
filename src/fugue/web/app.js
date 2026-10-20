'use strict';

// The fugue page: a list of discussions on the left, the selected discussion on
// the right, updated live from the server's event stream.

const TOKEN = document.querySelector('meta[name="fugue-token"]').content;
const TOOLS = ['claude', 'codex', 'dsh'];
const NAMES = { claude: 'Claude', codex: 'Codex', dsh: 'DeepSeek' };
const TOOL_LABELS = { claude: 'Claude Code', codex: 'Codex', dsh: 'DeepSeek Harness' };

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
  activityOpen: new Set(), // "round:letter" of replies whose steps are shown
  historyOpen: new Set(),  // discussions whose earlier rounds are unfolded
  roundOpen: new Set(),    // "discussion:round" of earlier rounds that are unfolded
  questionOpen: new Set(), // discussions whose long question is shown in full
  menuOpen: false,         // the ⋯ panel in the discussion's header
  layout: viewPrefs().layout === 'single' ? 'single' : 'side',
  tab: null,               // the member shown when the layout is "one at a time"
  mode: 'both',
  modeChosen: false,       // whether the user picked where the note box sends
  phases: new Map(),
  drafts: new Map(),       // discussion id -> files and where the box was sending, while you are elsewhere
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
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: { 'X-Fugue-Token': TOKEN, 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    connection('down');
    throw new Error("Can't reach fugue.");
  }
  // A restarted server has a new token, which only a reloaded page knows.
  connection(response.status === 401 ? 'restarted' : null);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`);
  return data;
}

// When the server stops or restarts, the page says so instead of quietly going stale.
function connection(problem) {
  const banner = $('#banner');
  if (banner.dataset.problem === (problem || '')) return;
  banner.dataset.problem = problem || '';
  banner.hidden = !problem;
  if (problem === 'restarted') {
    banner.replaceChildren(el('span', {}, 'fugue was restarted, so this page lost touch with it.'),
      el('button', { class: 'btn small primary', type: 'button', onclick: () => location.reload() }, 'Reload'));
  } else if (problem === 'down') {
    banner.replaceChildren(el('span', {}, "Can't reach fugue. If it stopped, start it again with ", el('code', {}, 'fugue web'), '.'));
  }
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

function joinAnd(items) {
  return items.length < 3 ? items.join(' and ') : `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

function firstLine(text) {
  const line = String(text).split('\n').find((l) => l.trim()) || '';
  return line.length > 200 ? line.slice(0, 199) + '…' : line;
}

function copy(text) {
  navigator.clipboard.writeText(text).then(() => toast('Copied.'), () => toast('Could not copy.', 'error'));
}

// How the user likes to read discussions, kept in this browser.
function viewPrefs(update) {
  try {
    const saved = JSON.parse(localStorage.getItem('fugue-view') || '{}');
    if (update) localStorage.setItem('fugue-view', JSON.stringify({ ...saved, ...update }));
    return saved;
  } catch {
    return {};
  }
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
    title: `${d.title}\n${d.status.label} · ${d.no_folder ? 'no folder' : tilde(d.cwd)}`,
  },
  el('div', { class: 't' },
    d.status.needs_you || d.status.phase === 'running' ? el('span', { class: `dot phase-${d.status.phase}` }) : null,
    el('span', {}, d.title)),
  el('div', { class: 'm' }, `${shortStatus(d)} · ${ago(d.updated_at)}`))));
}

function shortStatus(d) {
  switch (d.status.phase) {
    case 'running': return 'Working';
    case 'paused':
    case 'ready': return 'Waiting for you';
    case 'stopped': return 'Stopped';
    default: return d.status.label.startsWith('Agreed') ? 'Agreed' : 'Finished';
  }
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

// What you typed in the bar belongs to the discussion it was typed in: it
// waits there while you look at another one, and survives reloading the page.
function draftKey(id) {
  return `fugue-draft:${id}`;
}

function storeDraftText() {
  if (!S.id) return;
  try {
    const text = $('#composer-text').value;
    if (text.trim()) sessionStorage.setItem(draftKey(S.id), text);
    else sessionStorage.removeItem(draftKey(S.id));
  } catch { /* the draft just isn't kept */ }
}

function saveDraft() {
  if (!S.id) return;
  storeDraftText();
  if (S.picker.files.length || S.modeChosen) S.drafts.set(S.id, { files: [...S.picker.files], mode: S.modeChosen ? S.mode : null });
  else S.drafts.delete(S.id);
  $('#composer-text').value = '';
  S.picker.set([]);
}

function restoreDraft(id) {
  let text = '';
  try { text = sessionStorage.getItem(draftKey(id)) || ''; } catch { /* nothing kept */ }
  const draft = S.drafts.get(id);
  $('#composer-text').value = text;
  S.picker.set(draft ? draft.files : []);
  if (draft && draft.mode) {
    S.mode = draft.mode;
    S.modeChosen = true;
  }
  sizeComposer();
}

function leaveDiscussion() {
  saveDraft();
  if (S.source) S.source.close();
  S.source = null;
  S.id = null;
  S.detail = null;
  S.detailJson = '';
  S.live = new Map();
  S.menuOpen = false;
  S.modeChosen = false;
}

async function openDiscussion(id) {
  if (S.id !== id) {
    leaveDiscussion();
    S.id = id;
    restoreDraft(id);
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
  // Following a discussion as it finishes takes you up to its latest answers.
  if (before === 'running' && S.follow && hasResult(data)) {
    S.follow = false;
    $('#main').scrollTop = 0;
  }
}

function scrollToBottom() {
  const main = $('#main');
  main.scrollTop = main.scrollHeight;
}

// ---------------------------------------------------------------- attached files

const MAX_ATTACH_BYTES = 50 * 1024 * 1024;

function fmtSize(n) {
  return n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
}

function fileUrl(d, name) {
  return `/api/discussions/${encodeURIComponent(d.id)}/attachments/${encodeURIComponent(name)}?token=${encodeURIComponent(TOKEN)}`;
}

function fileType(name) {
  const m = name.match(/\.([A-Za-z0-9]{1,5})$/);
  return m ? m[1].toUpperCase() : 'FILE';
}

// The files attached to a question: images as small previews, the rest by type.
// A click shows the file on the page; ⌘-click opens it in a new tab.
function attachmentList(d, records) {
  if (!records || !records.length) return null;
  return el('div', { class: 'attachments' }, records.map((a) => el('a', {
    class: `attachment k-${a.kind}`,
    href: fileUrl(d, a.name),
    target: '_blank',
    rel: 'noopener',
    title: `Show ${a.name}`,
    onclick: (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
      e.preventDefault();
      showFile(d, a);
    },
  },
  a.kind === 'image' ? el('img', { src: fileUrl(d, a.name), alt: '' }) : el('span', { class: 'type' }, fileType(a.name)),
  el('span', { class: 'name' }, a.name),
  el('span', { class: 'size' }, fmtSize(a.size)))));
}

const KIND_NAMES = { image: 'Image', pdf: 'PDF', word: 'Word file', text: 'Text file', other: 'File' };
const MAX_PREVIEW_CHARS = 400000;

// One attached file on the page, with what the members get from it.
function showFile(d, a) {
  const dialog = $('#file-dialog');
  const url = fileUrl(d, a.name);
  const textUrl = a.text ? fileUrl(d, basename(a.text)) : null;
  const facts = [KIND_NAMES[a.kind] || 'File', fmtSize(a.size), a.pages ? plural(a.pages, 'page') : null].filter(Boolean).join(' · ');
  const body = el('div', { class: 'file-body' });
  // A PDF the browser can show is shown as it is, with the text the members get one click away.
  const views = a.kind === 'pdf' && navigator.pdfViewerEnabled && textUrl ? ['file', 'text'] : null;
  const show = (view) => {
    for (const b of dialog.querySelectorAll('.file-views button')) b.classList.toggle('on', b.dataset.view === view);
    body.replaceChildren(filePreview(a, view === 'text' ? null : url, textUrl));
  };
  dialog.replaceChildren(
    el('div', { class: 'dialog-head' },
      el('h2', { title: a.name }, a.name),
      el('span', { class: 'file-facts' }, facts),
      views ? el('span', { class: 'layout-toggle file-views', role: 'group', 'aria-label': 'Show' },
        el('button', { type: 'button', dataset: { view: 'file' }, onclick: () => show('file') }, 'PDF'),
        el('button', { type: 'button', dataset: { view: 'text' }, onclick: () => show('text') }, 'Its text')) : null,
      el('a', { class: 'btn small', href: url, download: a.name }, 'Download'),
      el('button', { type: 'button', class: 'btn ghost small', onclick: () => dialog.close() }, 'Close')),
    body,
    el('div', { class: 'context-foot' }, whatMembersGet(a)));
  show('file');
  dialog.showModal();
}

function filePreview(a, url, textUrl) {
  if (a.kind === 'image' && url) return el('img', { src: url, alt: a.name });
  if (a.kind === 'pdf' && url && navigator.pdfViewerEnabled) return el('iframe', { src: url, title: a.name });
  if (a.kind === 'text' && url) return textPreview(url, /\.(md|markdown)$/i.test(a.name) ? 'markdown' : 'plain');
  if ((a.kind === 'pdf' || a.kind === 'word') && textUrl) return textPreview(textUrl, 'prose');
  const why = a.note ? `There is no text to show: ${a.note}.` : "fugue can't show this kind of file.";
  return el('p', { class: 'muted' }, `${why} Download it to open it.`);
}

// `how`: 'markdown' rendered, 'prose' (text taken out of a document) or 'plain' as it is.
function textPreview(url, how) {
  const markdown = how === 'markdown';
  const node = el('div', { class: `file-text ${how}${markdown ? ' md' : ''}` }, el('p', { class: 'muted' }, 'Loading…'));
  fetch(url).then((r) => (r.ok ? r.text() : Promise.reject(new Error(`${r.status}`)))).then((text) => {
    const cut = text.length > MAX_PREVIEW_CHARS;
    const shown = cut ? text.slice(0, MAX_PREVIEW_CHARS) : text;
    if (markdown) node.innerHTML = md(shown);
    else node.replaceChildren(el('pre', {}, shown));
    if (cut) node.append(el('p', { class: 'muted' }, `Showing the first ${MAX_PREVIEW_CHARS.toLocaleString()} characters. Download it for the rest.`));
  }, () => node.replaceChildren(el('p', { class: 'muted' }, "Couldn't load the file. Download it to open it.")));
  return node;
}

// What each member is given, said under the preview.
function whatMembersGet(a) {
  switch (a.kind) {
    case 'image': return 'The members can open this image; Codex also gets it with its message.';
    case 'pdf':
    case 'word':
      return a.text ? `The members get the ${a.kind === 'pdf' ? 'PDF' : 'Word file'} and the text fugue took out of it.`
        : `The members get the file, but no text version: ${a.note || 'it has no text'}.`;
    case 'text': return 'The members read this file as it is.';
    default: return 'The members get the file and decide how to read it.';
  }
}

function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

// Files chosen to go with a question: an "Attach files" link, dropping or pasting onto
// `target`, and a chip for each file with a × to take it off again.
function filePicker({ target, onadd } = {}) {
  const files = [];
  const input = el('input', { type: 'file', multiple: true, hidden: true });
  const chips = el('span', { class: 'file-chips' });
  const draw = () => chips.replaceChildren(...files.map((file, i) => el('span', { class: 'file-chip', title: file.name },
    el('span', { class: 'name' }, file.name),
    el('span', { class: 'size' }, fmtSize(file.size)),
    el('button', { type: 'button', 'aria-label': `Remove ${file.name}`, onclick: () => { files.splice(i, 1); draw(); } }, '×'))));
  const add = (list) => {
    if (!list || !list.length) return;
    for (const file of list) files.push(file);
    draw();
    if (onadd) onadd();
  };
  input.addEventListener('change', () => { add(input.files); input.value = ''; });
  if (target) {
    target.addEventListener('dragover', (e) => { e.preventDefault(); target.classList.add('dropping'); });
    target.addEventListener('dragleave', () => target.classList.remove('dropping'));
    target.addEventListener('drop', (e) => { e.preventDefault(); target.classList.remove('dropping'); add(e.dataTransfer.files); });
    target.addEventListener('paste', (e) => {
      if (e.clipboardData && e.clipboardData.files.length) { e.preventDefault(); add(e.clipboardData.files); }
    });
  }
  const node = el('span', { class: 'file-picker' }, chips, input);
  node.files = files;
  node.open = () => input.click();
  node.clear = () => { files.length = 0; draw(); };
  node.set = (list) => { files.splice(0, files.length, ...list); draw(); };
  node.encode = async () => {
    const total = files.reduce((sum, file) => sum + file.size, 0);
    if (total > MAX_ATTACH_BYTES) throw new Error('The attachments come to more than 50 MB.');
    return Promise.all(files.map(async (file) => ({ name: file.name, data: await readAsBase64(file) })));
  };
  return node;
}

// ---------------------------------------------------------------- one discussion
// The page reads like a document: the question as its title, one line saying
// where things stand with the one button that moves it on, then the answers.
// The rest (folder, models, plan, copy commands, delete) is in the ⋯ panel,
// and the rounds that led to the answers are folded into one line.

function drawDiscussion() {
  const d = S.detail;
  const main = $('#main');
  const scroll = main.scrollTop;
  const view = mainView(d);
  $('#content').replaceChildren(el('div', { class: 'discussion' },
    discussionHead(d),
    questionBlock(d),
    d.status.phase === 'stopped' && d.status.detail ? el('div', { class: 'problem' }, d.status.detail) : null,
    view ? view.node : el('p', { class: 'muted starting' }, 'Starting…'),
    pendingSection(d),
    historySection(d, view ? view.round : null)));
  for (const r of d.rounds) for (const letter of Object.keys(r.entries)) patchCard(r.index, letter);
  drawModes(d);
  $('#composer').hidden = false;
  updateNavCounts();
  if (S.follow) scrollToBottom();
  else main.scrollTop = scroll;
  markStuck();
}

// What the page shows under the question: the members' latest answers once it
// has finished, otherwise the round in progress or just done.
function mainView(d) {
  if (hasResult(d)) return { node: resultSection(d), round: null };
  if (!d.rounds.length) return null;
  const r = d.rounds[d.rounds.length - 1];
  return { node: currentRoundSection(d, r), round: r.index };
}

function discussionHead(d) {
  return el('header', { class: 'dhead' },
    el('div', { class: 'dhead-title' },
      navToggle(),
      el('h1', { title: d.title }, d.title),
      detailsPanel(d)),
    el('div', { class: 'dhead-status' },
      el('span', { class: `status phase-${d.status.phase}` }, el('span', { class: 'dot' }), statusText(d))));
}

// The header gets a hairline under it once the page scrolls under it.
function markStuck() {
  const head = $('.dhead');
  if (head) head.classList.toggle('stuck', $('#main').scrollTop > 4);
}

function statusText(d) {
  const s = d.status;
  if (s.phase === 'running') {
    // The round itself is named just below, so this says who is at work.
    const last = d.rounds[d.rounds.length - 1];
    const working = last ? letters(d).filter((letter) => last.entries[letter] && last.entries[letter].status === 'running') : [];
    let text = working.length === 1 ? `${memberTitle(d, working[0])} (member ${working[0]}) is working`
      : working.length === 0 ? 'Starting'
        : working.length === letters(d).length ? (working.length === 2 ? 'Both members are working' : `All ${working.length} members are working`)
          : `Members ${working.join(' and ')} are working`;
    if (d.control.paused) text += ' · will pause after this round';
    return text;
  }
  if (s.phase === 'finished' && !s.label.startsWith('Agreed')) {
    const open = openObjections(d);
    if (open) return `${s.label} · ${plural(open, 'objection')} still open`;
  }
  return s.label;
}

// Objections left in the last comment round (a council's last review) of the latest question.
function openObjections(d) {
  const comments = currentRounds(d).filter((r) => r.kind === 'comment' || (d.council && r.kind === 'review'));
  const last = comments[comments.length - 1];
  if (!last) return 0;
  return Object.values(last.entries).reduce((sum, e) => sum + (e.objections || 0), 0);
}

// What the question says beyond its title (its first line, cut at 80 characters).
function questionRest(d) {
  const question = d.question.trim();
  const lines = question.split('\n');
  const first = lines.findIndex((line) => line.trim());
  const head = first < 0 ? '' : lines[first].trim().replace(/^#+/, '').trim();
  return head === d.title.trim() ? lines.slice(first + 1).join('\n').trim() : question;
}

// The rest of the question under the title, and the files that came with it.
function questionBlock(d) {
  const question = questionRest(d);
  const files = attachmentList(d, d.attachments);
  const showText = question !== '';
  if (!showText && !files) return null;
  const long = showText && (question.length > 240 || question.split('\n').length > 4);
  const open = S.questionOpen.has(d.id);
  return el('div', { class: `question${long && !open ? ' clamped' : ''}` },
    showText ? el('div', { class: 'md', html: md(question) }) : null,
    files,
    long ? el('button', {
      class: 'link',
      type: 'button',
      onclick: () => {
        if (open) S.questionOpen.delete(d.id);
        else S.questionOpen.add(d.id);
        drawDiscussion();
      },
    }, open ? 'Show less' : 'Show the whole question') : null);
}

// The ⋯ panel: facts about the discussion, its plan, and the rarely used actions.
function detailsPanel(d) {
  const running = d.status.phase === 'running';
  const close = () => {
    S.menuOpen = false;
    panel.open = false;
  };
  const item = (text, onclick, extra = {}) => el('button', {
    type: 'button',
    class: `menu-item${extra.danger ? ' danger' : ''}`,
    disabled: extra.disabled,
    title: extra.title,
    onclick: () => { close(); onclick(); },
  }, text);
  const fact = (label, ...value) => el('div', { class: 'fact' }, el('dt', {}, label), el('dd', {}, ...value));
  const panel = el('details', { class: 'menu', open: S.menuOpen, ontoggle: () => { S.menuOpen = panel.open; } },
    el('summary', { class: 'icon-btn', title: 'Details and actions', 'aria-label': 'Details and actions' }, '⋯'),
    el('div', { class: 'menu-panel' },
      el('dl', { class: 'facts' },
        fact('Folder', d.no_folder ? 'None: the members work with the attached files only' : el('code', {}, tilde(d.cwd))),
        letters(d).map((letter) => fact(`Member ${letter}`, el('span', { class: `mdot ${memberClass(d, letter)}` }), memberLine(d, letter))),
        fact('Web', d.settings.web === false ? 'Off' : `On: ${everyone(d)} may use it`),
        fact('Started', ago(d.created_at))),
      planControls(d),
      el('div', { class: 'menu-sep' }),
      item('Copy transcript path', () => copy(d.transcript_path)),
      letters(d).filter((letter) => d.resume[letter]).map((letter) => item(
        `Copy resume command for member ${letter}`, () => copy(d.resume[letter]),
        { title: "Opens this member's session in your terminal, with its memory of this discussion" })),
      item('Copy discussion id', () => copy(d.id)),
      el('div', { class: 'menu-sep' }),
      item('Move to Trash…', () => deleteDiscussion(d), {
        danger: true,
        disabled: running,
        title: running ? 'Stop it first' : 'Move this discussion to the Trash',
      })));
  return panel;
}

// "Claude Opus 5.5 · effort high", or "Codex · GPT-6.1-Sol · default effort".
function memberLine(d, letter) {
  const m = d.members[letter];
  const model = m.model ? modelName(m.tool, m.model) : 'default model';
  const who = model.startsWith(NAMES[m.tool]) ? model : `${NAMES[m.tool]} · ${model}`;
  return `${who} · ${m.effort ? `effort ${m.effort}` : 'default effort'}`;
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

// How the page names members to you; the members themselves only know letters.

// The model and effort, e.g. "Opus 5.5 · xhigh", "GPT-6.1-Sol" or "default model".
function modelLabel(d, letter) {
  const m = d.members[letter];
  let model = m.model ? modelName(m.tool, m.model) : 'default model';
  if (model.startsWith(`${NAMES[m.tool]} `)) model = model.slice(NAMES[m.tool].length + 1);
  return m.effort ? `${model} · ${m.effort}` : model;
}

// "Claude · Opus 5.5 · xhigh"
function memberTitle(d, letter) {
  return `${NAMES[d.members[letter].tool]} · ${modelLabel(d, letter)}`;
}

// Where space is short: the tool, or the letter when both members use the same tool.
function shortWho(d, letter) {
  const tool = d.members[letter].tool;
  return letters(d).every((other) => d.members[other].tool === tool) ? `member ${letter}` : NAMES[tool];
}

function otherName(d, letter) {
  return shortWho(d, otherLetter(d, letter));
}

// Members take their tool's colour; when both use the same tool, the second gets another shade.
function memberClass(d, letter) {
  const tool = d.members[letter].tool;
  return letters(d).find((other) => d.members[other].tool === tool) === letter ? tool : `${tool} alt`;
}

function runsWith(d, letter) {
  return `Member ${letter}: ${memberLine(d, letter)}`;
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

// What one of the rounds you set is: two members comment and update, a council reviews and revises.
function roundsLabel(members) {
  return members > 2 ? 'Rounds of review and revision' : 'Rounds of comments and updates';
}

function roundsHint(members) {
  return members > 2
    ? 'After the answers and the vote, each round has two steps: the others review the shared answer, then its author '
      + 'revises it. It stops earlier once no reviewer objects.'
    : "After the answers, each round has two steps: each comments on the other's answer, then each updates their own. "
      + 'It stops earlier once neither has objections.';
}

function planControls(d) {
  const c = d.control;
  const counts = [...new Set([0, 1, 2, 3, 4, 5, 6, 8, 10, c.max_rounds])].sort((a, b) => a - b);
  return el('div', { class: 'plan' },
    el('label', { title: roundsHint(letters(d).length) },
      roundsLabel(letters(d).length),
      el('select', { onchange: (e) => control({ max_rounds: Number(e.target.value) }) },
        counts.map((n) => el('option', { value: n, selected: n === c.max_rounds }, n)))),
    el('label', {},
      el('input', { type: 'checkbox', checked: c.pause_after_each_round, onchange: (e) => control({ pause_after_each_round: e.target.checked }) }),
      'Pause after each round'));
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

const ANSWER_KINDS = new Set(['answer', 'update', 'final']);

// The member a council's vote chose to write the shared answer, as of round `index`.
function chosenBefore(d, index) {
  let chosen = null;
  for (const r of d.rounds) {
    if (r.index >= index) break;
    if (r.kind === 'answer' && !r.reopen) chosen = null;
    if (r.kind === 'vote' && r.vote) chosen = r.vote.chosen;
  }
  return chosen;
}

function roundTitle(r) {
  const d = S.detail;
  if (r.kind === 'answer' && r.reopen) return 'Everyone answers again, for a new vote';
  if (r.kind === 'answer' && r.index > 0) return 'Follow-up question, independent answers';
  if (d && d.council && r.kind !== 'answer') {
    const writer = r.kind === 'review' ? chosenBefore(d, r.index) : Object.keys(r.entries)[0];
    return {
      vote: 'Each votes for the best answer to start from',
      merge: `Member ${writer} merges the others' points into its answer`,
      review: `The others review member ${writer}'s answer`,
      revise: `Member ${writer} revises the answer`,
    }[r.kind];
  }
  return ROUND_TITLES[r.kind];
}

// ---------------------------------------------------------------- answers side by side
// Both members' replies sit side by side by default; "One at a time" shows one
// member's reply across the full width, with a tab for each member.

function setLayout(layout) {
  S.layout = layout;
  viewPrefs({ layout });
  drawDiscussion();
}

function layoutToggle() {
  return el('span', { class: 'layout-toggle', role: 'group', 'aria-label': 'Layout' },
    el('button', { type: 'button', class: S.layout === 'side' ? 'on' : '', onclick: () => setLayout('side') }, 'Side by side'),
    el('button', { type: 'button', class: S.layout === 'single' ? 'on' : '', onclick: () => setLayout('single') }, 'One at a time'));
}

function sectionHead(title, columns, ...extra) {
  return el('div', { class: 'section-head' }, el('h2', {}, title), ...extra, el('span', { class: 'spacer' }),
    columns > 1 ? layoutToggle() : null);
}

function pairOf(d, shown, make) {
  if (S.layout !== 'single' || shown.length < 2) {
    return el('div', { class: `pair${shown.length === 1 ? ' one' : ''} n${shown.length}` }, shown.map(make));
  }
  const tab = shown.includes(S.tab) ? S.tab : shown[0];
  return el('div', { class: 'pair one tabbed' },
    el('div', { class: 'tabs', role: 'tablist' }, shown.map((letter) => el('button', {
      type: 'button',
      role: 'tab',
      class: letter === tab ? 'on' : '',
      'aria-selected': String(letter === tab),
      onclick: () => { S.tab = letter; drawDiscussion(); },
    },
    el('span', { class: `mdot ${memberClass(d, letter)}` }),
    NAMES[d.members[letter].tool],
    el('span', { class: 'model' }, modelLabel(d, letter)),
    el('span', { class: 'letter' }, `member ${letter}`)))),
    make(tab));
}

function followupBlock(r) {
  const entry = youEntry('follow-up question', r.question);
  const files = attachmentList(S.detail, r.attachments);
  if (files) entry.append(files);
  return entry;
}

// The round in progress, or just done, of a discussion that hasn't finished.
function currentRoundSection(d, r) {
  return el('section', { class: 'answers', id: `round-${r.index}` },
    sectionHead(`Round ${r.index}: ${roundTitle(r)}`, Object.keys(r.entries).length, tally(r)),
    r.kind === 'answer' && r.index > 0 && !r.reopen ? followupBlock(r) : null,
    sentNotes(d, r),
    r.kind === 'vote' ? voteBoard(d, r) : null,
    pairOf(d, letters(d).filter((letter) => r.entries[letter]), (letter) => card(r, letter)));
}

// How a council's vote came out: each answer's points, and which one it starts from.
// Right after the vote, you can start from another answer instead.
function voteBoard(d, r) {
  const v = r.vote;
  if (!v) return null;
  const order = Object.keys(v.points).sort((a, b) => v.points[b] - v.points[a] || v.first[b] - v.first[a]);
  const top = Math.max(1, ...Object.values(v.points));
  const last = d.rounds[d.rounds.length - 1];
  const canPick = last.index === r.index && d.status.phase !== 'running';
  const unread = Object.entries(r.entries).filter(([, e]) => e.ballot_problem);
  return el('div', { class: 'vote-board' },
    order.map((letter) => el('div', { class: `vote-row${letter === v.chosen ? ' chosen' : ''}` },
      el('span', { class: 'vote-who' },
        el('span', { class: `mdot ${memberClass(d, letter)}` }),
        el('b', {}, NAMES[d.members[letter].tool]), ' ',
        el('span', { class: 'model' }, modelLabel(d, letter)), ' ',
        el('span', { class: 'letter' }, `member ${letter}`)),
      el('span', { class: 'vote-bar' }, el('span', { style: `width: ${Math.round((v.points[letter] / top) * 100)}%` })),
      el('span', { class: 'vote-points' }, `${plural(v.points[letter], 'point')} · ranked first ${v.first[letter]}×`),
      letter === v.chosen
        ? el('span', { class: 'vote-chosen' }, 'starting point')
        : canPick ? el('button', { type: 'button', class: 'link accent', onclick: () => pickStart(letter) }, 'Start from this one') : el('span'))),
    el('div', { class: 'vote-note' },
      `${v.valid} of ${Object.keys(r.entries).length} ballots counted · starting point chosen ${v.chosen_by}. `
      + 'A ballot gives an answer one point for every answer it ranks below it.'),
    unread.map(([letter, e]) => el('div', { class: 'vote-note warn-text' }, `Member ${letter}'s ballot wasn't counted: ${e.ballot_problem}.`)));
}

function pickStart(letter) {
  return attempt(async () => {
    await api('POST', path('editor'), { member: letter });
    refreshSoon();
  });
}

// ---------------------------------------------------------------- where it ended

// The rounds of the latest question: from the last round of independent answers on.
function currentRounds(d) {
  let start = 0;
  for (const r of d.rounds) if (r.kind === 'answer' && !r.reopen) start = r.index;
  return d.rounds.filter((r) => r.index >= start);
}

// Each member's latest finished answer to the latest question, with the round it is from.
function latestAnswers(d) {
  const latest = {};
  if (d.council) {
    // A council's result is its one shared answer, from the member who wrote it.
    const done = (r) => Object.values(r.entries).every((e) => e.status === 'done');
    const shared = [...currentRounds(d)].reverse().find((r) => (r.kind === 'merge' || r.kind === 'revise') && done(r));
    if (shared) {
      latest[Object.keys(shared.entries)[0]] = shared;
      return latest;
    }
  }
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
  const latest = latestAnswers(d);
  const shown = letters(d).filter((letter) => latest[letter]);
  const asked = currentRounds(d)[0];
  return el('section', { class: 'answers', id: 'round-result' },
    sectionHead(d.council && shown.length === 1 ? "The council's answer" : 'Their latest answers', shown.length),
    asked.index > 0 ? followupBlock(asked) : null,
    pairOf(d, shown, (letter) => resultColumn(d, letter, latest[letter])));
}

function resultColumn(d, letter, r) {
  const e = r.entries[letter];
  return el('article', { class: `reply-col ${memberClass(d, letter)} status-done` },
    columnHead(d, letter, `from round ${r.index}`, ''),
    el('div', { class: 'reply md', html: md(e.reply) }),
    el('footer', {},
      el('button', { class: 'link', type: 'button', onclick: () => showContext(r.index, letter) }, 'Inspect context'),
      el('button', { class: 'link', type: 'button', onclick: () => copy(e.reply) }, 'Copy')));
}

// ---------------------------------------------------------------- how they got here
// Every other round, folded into one line under a single "How they got here"
// (or "Earlier rounds") line; each opens to its replies side by side.

function historySection(d, current) {
  const rounds = d.rounds.filter((r) => r.index !== current);
  if (!rounds.length) return null;
  const open = S.historyOpen.has(d.id);
  return el('section', { class: `history${open ? ' open' : ''}` },
    el('button', {
      type: 'button',
      class: 'history-toggle',
      'aria-expanded': String(open),
      onclick: () => {
        if (open) S.historyOpen.delete(d.id);
        else S.historyOpen.add(d.id);
        S.follow = false;
        drawDiscussion();
      },
    },
    el('span', { class: 'chev', 'aria-hidden': 'true' }, open ? '▾' : '▸'),
    hasResult(d) ? 'How they got here' : 'Earlier rounds',
    el('span', { class: 'muted' }, ` · ${plural(rounds.length, 'round')}`)),
    open ? el('ol', { class: 'rounds' }, rounds.map((r) => historyRound(d, r))) : null);
}

function historyRound(d, r) {
  const key = `${d.id}:${r.index}`;
  const open = S.roundOpen.has(key);
  const shown = letters(d).filter((letter) => r.entries[letter]);
  return el('li', { class: `hround${open ? ' open' : ''}`, id: `round-${r.index}` },
    el('button', {
      type: 'button',
      class: 'hround-row',
      'aria-expanded': String(open),
      onclick: () => {
        if (open) S.roundOpen.delete(key);
        else S.roundOpen.add(key);
        S.follow = false;
        drawDiscussion();
      },
    },
    el('span', { class: 'num' }, r.index),
    el('span', { class: 'title' }, roundTitle(r)),
    tally(r),
    sentNotes(d, r).length ? el('span', { class: 'you-mark' }, 'with your note') : null,
    el('span', { class: 'spacer' }),
    el('span', { class: 'brief' }, shown.map((letter) => `${shortWho(d, letter)} ${stateText(r.entries[letter])}`).join(' · '))),
    open ? el('div', { class: 'hround-body' },
      r.kind === 'answer' && r.index > 0 && !r.reopen ? followupBlock(r) : null,
      sentNotes(d, r),
      pairOf(d, shown, (letter) => card(r, letter))) : null);
}

function tally(r) {
  const d = S.detail;
  const done = letters(d).filter((letter) => r.entries[letter] && r.entries[letter].status === 'done');
  if (done.length < 2 || done.length < Object.keys(r.entries).length) return null;
  if (r.kind === 'comment' || (d.council && r.kind === 'review')) {
    const restart = done.filter((letter) => r.entries[letter].starting_point === 'change').length;
    const again = restart ? ` · ${restart} of ${done.length} want a new start` : '';
    if (done.every((letter) => r.entries[letter].objections === 0)) return el('span', { class: 'tally ok' }, `no objections${again}`);
    return el('span', { class: 'tally' },
      `objections: ${done.map((letter) => `${shortWho(d, letter)} ${r.entries[letter].objections ?? '?'}`).join(', ')}${again}`);
  }
  if (r.kind === 'review' || r.kind === 'discuss') {
    return el('span', { class: 'tally' },
      `disagreements left: ${done.map((letter) => `${shortWho(d, letter)} ${r.entries[letter].remaining ?? '?'}`).join(', ')}`);
  }
  return null;
}

// ---------------------------------------------------------------- one member's reply

function subtitle(kind, letter, index) {
  if (S.detail.council) {
    const reopen = S.detail.rounds[index] && S.detail.rounds[index].reopen;
    return {
      answer: reopen ? 'its new answer' : index > 0 ? 'its own answer to the follow-up' : 'its own answer',
      vote: 'its ballot',
      merge: 'the merged answer',
      review: 'its review',
      revise: 'the revised answer',
    }[kind];
  }
  const other = otherName(S.detail, letter);
  return {
    answer: index > 0 ? 'its own answer to the follow-up' : 'its own answer',
    comment: `on ${other}'s answer`,
    update: 'updated answer',
    review: `reviewing ${other}'s answer`,
    discuss: `replying to ${other}`,
    final: 'final answer',
  }[kind];
}

function stateText(e) {
  if (e.status === 'running') return 'working';
  if (e.status === 'done') return fmtDuration(e.duration_s);
  if (e.status === 'failed') return e.error === 'stopped' || e.error === 'interrupted' ? 'stopped' : 'failed';
  return 'waiting';
}

function columnHead(d, letter, sub, state) {
  return el('header', { class: 'col-head' },
    el('span', { class: `mdot ${memberClass(d, letter)}` }),
    el('span', { class: 'who', title: runsWith(d, letter) }, NAMES[d.members[letter].tool]),
    el('span', { class: 'model' }, modelLabel(d, letter)),
    el('span', { class: 'letter' }, `member ${letter}`),
    sub ? el('span', { class: 'sub' }, sub) : null,
    el('span', { class: 'state' }, state));
}

function card(r, letter) {
  const d = S.detail;
  const e = r.entries[letter];
  const key = `${r.index}:${letter}`;
  let body;
  if (e.status === 'running') body = el('div', { class: 'live md' });
  else if (e.status === 'done') body = el('div', { class: 'reply md', html: md(e.reply) });
  else if (e.status === 'failed') {
    body = el('div', { class: 'error' },
      e.error === 'stopped' || e.error === 'interrupted' ? 'Stopped before it finished.' : `Failed: ${e.error}`);
  } else body = el('div', { class: 'waiting muted' }, 'Waiting to start.');
  return el('article', {
    class: `reply-col ${memberClass(d, letter)} status-${e.status}`,
    id: `card-${r.index}-${letter}`,
    dataset: { round: r.index, member: letter },
  },
  columnHead(d, letter, subtitle(r.kind, letter, r.index), stateText(e)),
  body,
  el('footer', {},
    el('button', { class: 'link steps-toggle', type: 'button', hidden: true, onclick: () => toggleSteps(key) }),
    el('button', { class: 'link', type: 'button', onclick: () => showContext(r.index, letter) }, 'Inspect context'),
    e.status === 'done' ? el('button', { class: 'link', type: 'button', onclick: () => copy(e.reply) }, 'Copy') : null),
  el('ol', { class: 'steps', hidden: !S.activityOpen.has(key) }));
}

// A reply's steps (files read, commands run) are hidden until asked for.
function toggleSteps(key) {
  if (S.activityOpen.has(key)) S.activityOpen.delete(key);
  else S.activityOpen.add(key);
  const [round, letter] = key.split(':');
  const node = document.getElementById(`card-${round}-${letter}`);
  if (node) node.querySelector('.steps').hidden = !S.activityOpen.has(key);
  patchCard(round, letter);
}

function patchCard(round, letter) {
  const node = document.getElementById(`card-${round}-${letter}`);
  if (!node) return;
  const live = S.live.get(`${round}:${letter}`);
  let items = live ? live.activity : [];
  // The reply is shown in full, so don't repeat it as the last step.
  const entry = S.detail && S.detail.rounds[Number(round)] && S.detail.rounds[Number(round)].entries[letter];
  const last = items[items.length - 1];
  if (entry && entry.reply && last && last.kind === 'message' && last.text === firstLine(entry.reply)) {
    items = items.slice(0, -1);
  }
  const ol = node.querySelector('.steps');
  let drawn = Number(ol.dataset.count || 0);
  if (drawn > items.length) {
    ol.replaceChildren();
    drawn = 0;
  }
  const atBottom = ol.scrollTop + ol.clientHeight >= ol.scrollHeight - 8;
  for (const item of items.slice(drawn)) ol.append(el('li', { class: `k-${item.kind}`, title: item.text }, item.text));
  ol.dataset.count = items.length;
  if (atBottom) ol.scrollTop = ol.scrollHeight;
  const toggle = node.querySelector('.steps-toggle');
  toggle.hidden = !items.length;
  toggle.textContent = ol.hidden ? `Show ${plural(items.length, 'step')}` : 'Hide steps';
  const liveNode = node.querySelector('.live');
  if (liveNode) {
    if (live && live.text) liveNode.innerHTML = md(live.text);
    else if (live && live.last === 'thinking') {
      // At high effort Claude can think for minutes before it writes or uses a tool.
      liveNode.innerHTML = `<p class="last-step">Thinking · about ${escapeHtml(fmtTokens(live.thinking))} tokens so far</p>`;
    } else {
      const step = items.length ? items[items.length - 1] : null;
      liveNode.innerHTML = step
        ? `<p class="last-step k-${escapeHtml(step.kind)}">${escapeHtml(step.text)}</p>`
        : '<p class="last-step">Starting…</p>';
    }
  }
}

function tick() {
  if (!S.detail) return;
  for (const node of document.querySelectorAll('.reply-col.status-running')) {
    const { round, member } = node.dataset;
    const live = S.live.get(`${round}:${member}`);
    const entry = S.detail.rounds[Number(round)] && S.detail.rounds[Number(round)].entries[member];
    const start = (live && live.started) || parseTime(entry && entry.started_at);
    if (start) node.querySelector('.col-head .state').textContent = `working ${fmtDuration(Date.now() / 1000 - start)}`;
  }
}

// ---------------------------------------------------------------- what a member saw
// Each member keeps one session for the whole discussion, so what it saw when
// it answered a round is every message fugue sent it up to that round, with
// its own replies in between. The inspector shows that conversation with each
// part marked by where it came from: you, fugue's instructions, the other
// member, or the member itself.

async function showContext(round, letter) {
  const data = await attempt(() => api('GET', `${path('context')}?round=${round}&member=${letter}`));
  if (!data) return;
  S.context = { round, letter, items: data.items, tab: 'round' };
  drawContext();
  $('#context-dialog').showModal();
}

function drawContext() {
  const d = S.detail;
  const { round, letter, items, tab } = S.context;
  const others = letters(d).filter((x) => x !== letter);
  const shown = tab === 'round' ? items.filter((i) => i.round === round && i.role === 'sent') : items;
  const tabButton = (name, label) => el('button', {
    type: 'button',
    role: 'tab',
    class: tab === name ? 'on' : '',
    'aria-selected': String(tab === name),
    onclick: () => { S.context.tab = name; drawContext(); },
  }, label);
  const legend = (cls, label) => el('span', {}, el('span', { class: `swatch ${cls}` }), label);
  const dialog = $('#context-dialog');
  dialog.replaceChildren(
    el('div', { class: 'dialog-head' },
      el('h2', {}, `What ${memberTitle(d, letter)} (member ${letter}) saw in round ${round}`),
      el('button', { type: 'button', class: 'btn small', onclick: () => copy(shown.map((i) => i.text).join('\n\n')) }, 'Copy'),
      el('button', { type: 'button', class: 'btn ghost small', onclick: () => dialog.close() }, 'Close')),
    el('div', { class: 'context-bar' },
      el('div', { class: 'tabs', role: 'tablist' },
        tabButton('round', "This round's message"),
        tabButton('all', `Whole conversation up to round ${round}`)),
      el('div', { class: 'legend' },
        legend('src-you', 'You'),
        legend('src-fugue', "fugue's instructions"),
        others.map((other) => legend(`src-m ${memberClass(d, other)}`, d.members[other].name)),
        legend(`src-m ${memberClass(d, letter)}`, `${d.members[letter].name}'s own replies`))),
    el('div', { class: 'context-body' }, shown.map((item) => contextItem(d, letter, item))),
    el('div', { class: 'context-foot' },
      "What fugue sent this member and what it replied, in order. Not included: its tool's own system prompt, "
      + 'CLAUDE.md or AGENTS.md, and the files it read (see its steps).'));
}

function contextItem(d, letter, item) {
  if (item.role === 'reply') {
    return contextBlock(`src-m ${memberClass(d, letter)}`, `Round ${item.round} · ${d.members[letter].name} replied`, item.text);
  }
  if (item.kind === 'answer' && item.round === 0) {
    // The first message is your question, sent exactly as you wrote it.
    return contextBlock('src-you', `Round ${item.round} · your question, sent as you wrote it`, item.text);
  }
  return el('div', { class: 'ctx-msg' },
    el('div', { class: 'ctx-label' }, `Round ${item.round} · message from fugue`),
    contextSegments(d, item.text, letter));
}

// fugue wraps what it forwards in tags, so a message splits into its sources.
// Discussions from before letters wrote from="the other member".
const FORWARDED = /<([a-z]+) from="([^"]*)" round="(\d+)">\n?([\s\S]*?)\n?<\/\1>|<question>\n?([\s\S]*?)\n?<\/question>|<user_note>\n?([\s\S]*?)\n?<\/user_note>/g;

function contextSegments(d, text, letter) {
  const out = [];
  const plain = (part) => (part.trim() ? el('div', { class: 'ctx-plain' }, part.trim()) : null);
  let last = 0;
  for (const m of text.matchAll(FORWARDED)) {
    out.push(plain(text.slice(last, m.index)));
    if (m[1]) {
      const named = m[2].match(/member ([A-Z])/);
      const sender = named && d.members[named[1]] ? named[1] : otherLetter(d, letter);
      out.push(contextBlock(`src-m ${memberClass(d, sender)}`, `${d.members[sender].name}'s ${m[1]}, round ${m[3]}`, m[4]));
    } else if (m[5] !== undefined) {
      out.push(contextBlock('src-you', 'Your follow-up question', m[5]));
    } else {
      out.push(contextBlock('src-you', 'Your note', m[6]));
    }
    last = m.index + m[0].length;
  }
  out.push(plain(text.slice(last)));
  return out;
}

// Long parts start folded to a few lines.
function contextBlock(cls, label, text) {
  const body = text.trim();
  const long = body.length > 1200 || body.split('\n').length > 16;
  const node = el('div', { class: `ctx-block ${cls}${long ? ' clamped' : ''}` },
    el('div', { class: 'ctx-label' }, label),
    el('div', { class: 'ctx-text' }, body));
  if (long) {
    const more = el('button', {
      type: 'button',
      class: 'link',
      onclick: () => {
        node.classList.toggle('clamped');
        more.textContent = node.classList.contains('clamped') ? 'Show all' : 'Show less';
      },
    }, 'Show all');
    node.append(more);
  }
  return node;
}

// ---------------------------------------------------------------- notes, follow-ups, queue

// ---------------------------------------------------------------- the action bar
// Everything you can do next is in one bar at the bottom: a box for your
// note or follow-up question, and the button that says what happens next.
// A note goes out with the next round, so "Continue" takes it along.

// "both members", or "every member" in a council.
function everyone(d) {
  return letters(d).length === 2 ? 'both members' : 'every member';
}

function modes(d) {
  const all = { both: { label: letters(d).length === 2 ? 'To both' : 'To everyone' } };
  for (const letter of letters(d)) all[letter] = { label: `To member ${letter}`, name: d.members[letter].name };
  all.ask = { label: 'Follow-up question' };
  return all;
}

function drawModes(d) {
  const all = modes(d);
  const select = $('#composer-mode');
  const wanted = JSON.stringify(Object.entries(all).map(([mode, m]) => [mode, m.label]));
  if (select.dataset.modes !== wanted) {
    select.dataset.modes = wanted;
    select.replaceChildren(...Object.entries(all).map(([mode, m]) => el('option', { value: mode, title: m.name }, m.label)));
  }
  // Until you pick, a finished discussion takes a follow-up question and any other a note to both.
  const fallback = d.status.phase === 'finished' ? 'ask' : 'both';
  setMode(S.modeChosen && all[S.mode] ? S.mode : fallback);
}

function setMode(mode) {
  const d = S.detail;
  const m = modes(d)[mode];
  const phase = d.status.phase;
  S.mode = mode;
  $('#composer-mode').value = mode;
  let placeholder;
  let hint;
  if (mode === 'ask') {
    placeholder = 'Ask a follow-up question';
    hint = phase === 'running' ? "It's asked once this round is done; each member answers on its own first."
      : 'Each member answers it on its own first.';
  } else {
    const who = mode === 'both' ? everyone(d) : m.name;
    placeholder = phase === 'finished' ? `Add a note to ${who} for one more round`
      : phase === 'running' ? `Add a note to ${who} for the next round`
        : `Add your view for the next round, to ${who} (optional)`;
    hint = `It goes into the next message fugue sends to ${who}.`;
  }
  $('#composer-text').placeholder = placeholder;
  drawBar(d);
  $('#composer-hint').textContent = `${hint} ⌘↩ presses “${S.primary ? S.primary.textContent : ''}”.`;
}

function textValue() {
  return $('#composer-text').value.trim();
}

// Saves what is in the box as a note or a follow-up question. False if that failed.
async function submitText() {
  const text = textValue();
  if (!text) return true;
  const ok = await attempt(async () => {
    if (S.mode === 'ask') {
      const files = await S.picker.encode();
      await api('POST', path('requests'), { type: 'ask', question: text, attachments: files });
    } else await api('POST', path('notes'), { text, to: S.mode });
    return true;
  });
  if (!ok) return false;
  $('#composer-text').value = '';
  storeDraftText();
  if (S.mode === 'ask') S.picker.clear();
  sizeComposer();
  return true;
}

// For the buttons that only make sense with something written.
function needText(what) {
  if (textValue()) return true;
  $('#composer-text').focus();
  $('#composer-hint').textContent = `Write ${what} first.`;
  return false;
}

function barActions(d) {
  const ask = S.mode === 'ask';
  const next = d.status.next;
  const withText = (then) => async () => {
    if (await submitText()) await then();
  };
  // Files go with a follow-up question; dropping or pasting one switches the box to it.
  const attach = button(S.picker.files.length ? `Attach (${S.picker.files.length})` : 'Attach', () => S.picker.open(), 'ghost');
  const askButton = button('Ask', async () => {
    if (needText('the question') && await submitText()) {
      follow();
      refreshSoon();
    }
  }, 'primary');
  const items = [];
  let primary;
  switch (d.status.phase) {
    case 'running':
      items.push(button(d.control.paused ? "Don't pause" : 'Pause after this round', () => control({ paused: !d.control.paused }), 'ghost'));
      items.push(button('Stop', () => attempt(async () => { await api('POST', path('stop')); refreshSoon(); }), 'ghost danger'));
      primary = button(ask ? 'Queue the question' : 'Save note', async () => {
        if (needText(ask ? 'the question' : 'the note') && await submitText()) refreshSoon();
      });
      break;
    case 'finished':
      if (ask) {
        items.push(button('One more round', () => request({ type: 'next' }), 'ghost'));
        primary = askButton;
      } else {
        primary = button('One more round', withText(() => request({ type: 'next' })), 'primary');
      }
      break;
    case 'stopped':
      primary = ask ? askButton : button('Retry', withText(() => control({ paused: false })), 'primary');
      break;
    default: // waiting for you: paused or ready
      primary = ask ? askButton
        : button(next ? `Continue: ${next}` : 'Continue', withText(() => control({ paused: false })), 'primary');
  }
  // A council that has voted can answer again and vote again, when you think it started from the wrong answer.
  const voted = currentRounds(d).some((r) => r.kind === 'vote' && r.vote);
  const last = d.rounds[d.rounds.length - 1];
  if (d.council && voted && last && last.kind !== 'vote' && ['paused', 'ready', 'finished'].includes(d.status.phase)) {
    items.unshift(button('Vote again', () => request({ type: 'revote' }), 'ghost'));
  }
  if (ask) items.unshift(attach);
  S.primary = primary;
  return [...items, primary];
}

function drawBar(d) {
  $('#composer-actions').replaceChildren(...barActions(d));
}

// The note box is one line until you click into it or it has text.
function sizeComposer() {
  const box = $('.composer-box');
  const text = $('#composer-text');
  const open = box.contains(document.activeElement) || text.value.trim() !== '';
  box.classList.toggle('open', open);
  text.style.height = '';
  if (open) text.style.height = `${Math.min(Math.max(text.scrollHeight, 66), window.innerHeight * 0.4)}px`;
}

// ---------------------------------------------------------------- your part in the discussion
// What you add, notes and follow-up questions, shows in the discussion as your
// own entries: waiting ones under the latest round, saying when they go out,
// and sent ones at the top of the round they went out with.

function youEntry(head, text, ...actions) {
  return el('div', { class: 'you' },
    el('div', { class: 'you-head' },
      el('span', { class: 'you-who' }, 'You'), el('span', {}, ` · ${head}`), el('span', { class: 'spacer' }), ...actions),
    text ? el('div', { class: 'md', html: md(text) }) : null);
}

// The notes a round's messages carried, once each: a note to both is on both members' entries.
function sentNotes(d, r) {
  const seen = new Set();
  const out = [];
  for (const letter of letters(d)) {
    for (const note of (r.entries[letter] && r.entries[letter].notes) || []) {
      if (note.shared && seen.has(note.text)) continue;
      if (note.shared) seen.add(note.text);
      out.push(youEntry(`${note.shared ? `note to ${everyone(d)}` : `note to member ${letter}`}, sent with this round`, note.text));
    }
  }
  return out;
}

function nextRoundText(d) {
  const last = d.rounds[d.rounds.length - 1];
  if (!last || d.status.phase === 'stopped') return 'the next round';
  return `round ${d.status.phase === 'running' ? last.index + 1 : d.rounds.length}`;
}

function pendingSection(d) {
  const items = [];
  const seen = new Set();
  const when = nextRoundText(d);
  for (const letter of letters(d)) {
    (d.inbox.notes[letter] || []).forEach((note, index) => {
      if (note.shared && seen.has(note.text)) return;
      if (note.shared) seen.add(note.text);
      const remove = () => api('DELETE', path('notes'), { member: letter, index });
      items.push(youEntry(`${note.shared ? `note to ${everyone(d)}` : `note to member ${letter}`}, goes out with ${when}`, note.text,
        editLink(remove, note.text, note.shared ? 'both' : letter), removeLink(remove)));
    });
  }
  for (const r of d.inbox.requests) {
    const remove = () => api('DELETE', path('requests', r.id));
    if (r.type === 'ask') {
      items.push(youEntry('follow-up question, asked once the current round is done', r.question,
        editLink(remove, r.question, 'ask'), removeLink(remove)));
    } else {
      items.push(youEntry('asked for one more round', '', removeLink(remove, 'Cancel')));
    }
  }
  return items.length ? el('div', { class: 'pending' }, items) : null;
}

function removeLink(remove, label = 'Remove') {
  return el('button', { type: 'button', class: 'link', onclick: () => attempt(async () => { await remove(); refreshSoon(); }) }, label);
}

// Takes a waiting note or question back into the box to change it.
function editLink(remove, text, mode) {
  return el('button', {
    type: 'button',
    class: 'link',
    onclick: () => attempt(async () => {
      await remove();
      S.modeChosen = true;
      setMode(mode);
      const box = $('#composer-text');
      box.value = text;
      box.focus();
      sizeComposer();
      refreshSoon();
    }),
  }, 'Edit');
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
  if (own) return `Default: ${own}`;
  const model = tool !== 'claude' && findModel(tool, modelId || (entry && entry.defaults.model));
  return model && model.default_effort ? `Default: ${model.default_effort}` : 'Default';
}

// Where a tool's own default effort comes from, for the effort menu's tooltip.
function effortSource(tool) {
  const own = S.models && S.models[tool] && S.models[tool].defaults.effort;
  if (!own) return `Default: fugue passes no effort, so ${TOOL_LABELS[tool]} uses its own.`;
  return `Default: ${own}, from your ${tool === 'claude' ? 'Claude Code settings' : `${NAMES[tool]} config`}.`;
}

function allLevels(models) {
  const levels = new Set(models.flatMap((m) => m.efforts));
  return EFFORT_ORDER.filter((level) => levels.has(level));
}

// One member's controls: the tool, then that tool's models and effort levels,
// then extra arguments. `onchange` runs whenever any of them changes.
function memberRow(index, chosen, onchange, onremove = null) {
  const row = el('div', { class: 'member-row', dataset: { index } });
  const tool = el('select', { 'aria-label': `Member ${index + 1}'s tool`, class: 'tool' },
    TOOLS.map((t) => el('option', { value: t, selected: t === chosen.tool }, TOOL_LABELS[t])));
  const picker = el('span', { class: 'picker' });
  const args = el('input', { type: 'text', class: 'args', value: chosen.args || '', spellcheck: 'false' });
  const commands = { claude: 'claude -p', codex: 'codex exec', dsh: 'dsh --profile headless' };
  // Said next to the member it concerns, so a tool you don't use never warns.
  const missing = el('div', { class: 'missing warn-text' });
  const drawPicker = (current) => {
    const t = tool.value;
    args.placeholder = `extra arguments for ${commands[t]}`;
    picker.replaceChildren(S.models ? modelPicker(t, current) : el('span', { class: 'muted' }, 'default model and effort'));
    const found = !(S.info && S.info.agents[t] === false);
    missing.hidden = found;
    missing.textContent = found ? '' : `fugue can't find the ${commands[t].split(' ')[0]} command on this Mac, so this member can't run. `
      + 'Settings shows where fugue looked.';
  };
  tool.addEventListener('change', () => { drawPicker({}); onchange(); });
  row.addEventListener('change', onchange);
  row.addEventListener('input', onchange);
  drawPicker(chosen);
  const argsLink = el('button', {
    type: 'button',
    class: 'link args-link',
    onclick: () => { args.hidden = false; argsLink.hidden = true; args.focus(); },
  }, 'Extra arguments');
  args.hidden = !args.value;
  argsLink.hidden = !args.hidden;
  row.append(
    el('span', { class: 'member-num' }, `Member ${index + 1}`),
    el('div', { class: 'member-body' }, tool, picker, argsLink, args, missing),
    onremove ? el('button', { type: 'button', class: 'link remove', title: 'Remove this member', onclick: onremove }, 'Remove') : '');
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
  const effortSelect = el('select', { class: 'effort', 'aria-label': `${NAMES[tool]} effort`, title: effortSource(tool) });
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
const MAX_MEMBERS = 4;

// Two to four member rows, with a hint under them, for the New question form and Settings.
// `onchange` gets the line-up whenever it changes.
function membersField(chosen, onchange = null) {
  const hint = el('div', { class: 'hint' });
  const warning = el('div', { class: 'hint warn-text' });
  const list = el('div', { class: 'member-list' });
  const add = el('button', { type: 'button', class: 'link accent add-member' }, '+ Add a member');
  const mode = el('div', { class: 'hint' });
  let lineup = chosen.length >= 2 ? chosen.slice(0, MAX_MEMBERS) : [{ tool: 'claude' }, { tool: 'codex' }];
  let rows = [];
  const update = () => {
    const picked = rows.map((row) => row.read());
    const same = picked.some((a, i) => picked.some((b, j) => j > i && a.tool === b.tool && (a.model || '') === (b.model || '')));
    warning.textContent = same
      ? 'Some members run the same model, so they tend to share blind spots and miss the same things. '
        + "Different models, or at least different efforts, catch more of each other's mistakes."
      : '';
    warning.hidden = !same;
    mode.textContent = picked.length === 2
      ? 'Two members answer on their own, then comment on each other\'s answer and update their own, side by side.'
      : `${picked.length} members answer on their own, then vote for the best answer to start from. Its author merges the `
        + "others' best points into it, the others review it, and it is revised until no one objects.";
    if (onchange) onchange(picked);
  };
  const draw = () => {
    rows = lineup.map((member, index) => memberRow(index, member, update, lineup.length > 2 ? () => {
      lineup = rows.map((row) => row.read()).filter((_, i) => i !== index);
      draw();
    } : null));
    list.replaceChildren(...rows);
    add.hidden = lineup.length >= MAX_MEMBERS;
    update();
  };
  add.addEventListener('click', () => {
    const used = rows.map((row) => row.read());
    lineup = [...used, { tool: TOOLS.find((t) => !used.some((m) => m.tool === t)) || 'claude' }];
    draw();
  });
  draw();
  if (S.models) {
    const codex = S.models.codex;
    const when = codex.checked_at ? ` on ${new Date(codex.checked_at * 1000).toLocaleString()}` : '';
    hint.replaceChildren(
      'Members can use the same tool. They know each other only by a letter, drawn at random for each discussion. '
      + `Default means fugue passes nothing, so the tool uses its own setting. Codex's models come from ${codex.source}${when}.`,
      el('button', {
        class: 'link',
        type: 'button',
        onclick: async () => {
          const current = rows.map((row) => row.read());
          box.replaceChildren(el('span', { class: 'muted' }, 'Asking Codex for its models…'));
          await loadModels(true);
          const fresh = membersField(current, onchange);
          box.replaceWith(fresh);
          box.read = fresh.read;
        },
      }, 'Ask Codex again'));
  } else {
    hint.textContent = "Couldn't load the model lists; members whose model you don't set use their tool's default.";
  }
  const box = el('div', { class: 'members' }, list, add, mode, warning, hint);
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
  const cwdDefault = saved.cwd || folders.recent[0] || folders.default;
  // Everything but the folder starts from the defaults on the Settings page,
  // including the members, so what the form shows is what runs.
  const rounds = defaults ? defaults.max_rounds : 2;
  const pause = defaults ? defaults.pause_after_each_round : true;
  const web = defaults ? defaults.web : true;
  let lineup = defaults ? defaults.members : [{ tool: 'claude' }, { tool: 'codex' }];
  const membersSlot = el('div', {}, el('span', { class: 'muted' }, 'Loading the model lists…'));

  // Who answers and how it runs start from Settings and stay folded into one line until you change them.
  const counts = [...new Set([0, 1, 2, 3, 4, 5, 6, 8, 10, rounds])].sort((a, b) => a - b);
  const roundsSelect = el('select', { name: 'max_rounds' }, counts.map((n) => el('option', { value: n, selected: n === rounds }, n)));
  const pauseBox = el('input', { type: 'checkbox', name: 'pause', checked: pause });
  const webBox = el('input', { type: 'checkbox', name: 'web', checked: web });
  const roundsText = el('span');
  const roundsHintNode = el('div', { class: 'hint' });
  const summary = el('span');
  // The folded line and the rounds wording follow what the form says now.
  const describe = () => {
    const names = lineup.map((m) => (m.model ? `${TOOL_LABELS[m.tool]} (${modelName(m.tool, m.model)})` : TOOL_LABELS[m.tool]));
    const n = Number(roundsSelect.value);
    roundsText.textContent = roundsLabel(lineup.length);
    roundsHintNode.textContent = roundsHint(lineup.length);
    summary.textContent = `${joinAnd(names)} · ${plural(n, 'round')} of ${lineup.length > 2 ? 'review and revision' : 'comments and updates'}`
      + ` · ${pauseBox.checked ? 'pauses along the way' : 'runs to the end'} · ${webBox.checked ? 'web on' : 'no web'}`;
  };
  for (const input of [roundsSelect, pauseBox, webBox]) input.addEventListener('change', describe);
  describe();
  const options = el('div', { class: 'more-options', hidden: true },
    el('div', { class: 'field' },
      el('div', { class: 'label' }, 'How it runs'),
      el('div', { class: 'options' },
        el('label', {}, roundsText, roundsSelect),
        el('label', {}, pauseBox, 'Pause after each round'),
        el('label', { title: "All or none. Claude: web search and fetching pages (not from this Mac's own servers). Codex and DeepSeek: their web search." },
          webBox, 'Members may use the web'),
        el('label', {}, 'Give up on a member after',
          el('input', { type: 'text', name: 'timeout', class: 'short', placeholder: String(defaults ? defaults.timeout_minutes : 30) }), 'minutes')),
      roundsHintNode),
    el('div', { class: 'field' },
      el('div', { class: 'label' }, 'Members'),
      membersSlot));
  const change = el('button', {
    class: 'link',
    type: 'button',
    onclick: () => {
      options.hidden = !options.hidden;
      summary.hidden = !options.hidden;
      change.textContent = options.hidden ? 'Change' : 'Hide options';
    },
  }, 'Change');

  const cwdInput = el('input', {
    id: 'cwd', name: 'cwd', type: 'text', list: 'folders', value: cwdDefault, spellcheck: 'false',
    placeholder: 'No folder: the members see only the question and its attachments',
    title: 'The members can read everything in this folder and run read-only commands, but not change files.',
  });
  const question = el('textarea', {
    id: 'q',
    name: 'question',
    rows: 8,
    required: true,
    'aria-label': 'Question',
    placeholder: 'What should the members answer? Each answers on its own first. Drop or paste files here to attach them.',
    onkeydown: (e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); form.requestSubmit(); } },
  });
  const picker = filePicker({ target: question });
  const form = el('form', { class: 'form new-question', onsubmit: (e) => { e.preventDefault(); start(form); } },
    el('h1', {}, navToggle(), 'New question'),
    question,
    el('div', { class: 'attach-row' },
      el('button', { type: 'button', class: 'link accent', onclick: () => picker.open() }, 'Attach files'),
      el('span', { class: 'muted' }, 'PDF, Word, images or text, up to 50 MB'),
      picker),
    el('div', { class: 'folder-row' },
      el('label', { for: 'cwd' }, 'In'),
      cwdInput,
      el('button', { type: 'button', class: 'link', title: 'Ask without a project folder', onclick: () => { cwdInput.value = ''; cwdInput.focus(); } }, 'No folder'),
      el('datalist', { id: 'folders' }, [...new Set([...folders.recent, folders.default])].map((f) => el('option', { value: f })))),
    el('div', { class: 'setup-summary' }, summary, change),
    options,
    el('div', { class: 'submit' },
      el('button', { class: 'btn primary', type: 'submit' }, 'Ask'),
      el('span', { class: 'muted' }, '⌘↩')));
  form.picker = picker;
  $('#content').replaceChildren(form);
  updateNavCounts();
  $('#main').scrollTop = 0;
  $('#q').focus();
  await loadModels();
  const members = membersField(lineup, (picked) => { lineup = picked; describe(); });
  membersSlot.replaceChildren(members);
  form.members = members;
}

async function start(form) {
  const data = Object.fromEntries(new FormData(form));
  const submit = form.querySelector('button[type="submit"]');
  submit.disabled = true;
  if (data.cwd.trim()) prefs({ cwd: data.cwd });
  const created = await attempt(async () => api('POST', '/api/discussions', {
    question: data.question,
    cwd: data.cwd.trim(),  // empty: no folder
    attachments: await form.picker.encode(),
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
  const roundsText = el('span', {}, roundsLabel(d.members.length));
  const members = membersField(d.members, (picked) => { roundsText.textContent = roundsLabel(picked.length); });
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
        el('label', {}, roundsText,
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

// Desktop notifications when a discussion stops running. The browser asks
// once; afterwards its own site settings decide.
function notificationsRow(row, redraw) {
  const label = 'Desktop notifications';
  if (!('Notification' in window)) return row('warn', label, "This browser doesn't support them.");
  if (Notification.permission === 'granted') return row('good', label, 'On: you get one when a discussion stops and needs you.');
  if (Notification.permission === 'denied') {
    return row('warn', label, "Blocked. Allow notifications for this page in your browser's site settings to turn them on.");
  }
  return row('warn', label, 'Off. ', el('button', {
    class: 'link accent',
    type: 'button',
    onclick: async () => { await Notification.requestPermission(); redraw(); },
  }, 'Turn on'), ' to get one when a discussion stops and needs you.');
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
    ...TOOLS.map(toolRow),
    row('good', 'Codex extras',
      el('div', {}, `Off whenever Codex takes part in a discussion: ${offList.join(', ')}.`),
      el('div', { class: 'muted' }, "They could change files or act in your accounts, which Codex's read-only sandbox doesn't "
        + 'stop. When you use Codex yourself, they work as usual.')),
    notificationsRow(row, () => drawStatus(box)),
    row(s.service.running ? 'good' : 'warn', 'Background service', s.service.text),
    row('good', 'Discussions', `${s.storage.discussions} in `, el('code', {}, tilde(s.storage.path)), ` (${s.storage.megabytes} MB)`),
    row('good', 'This page', `fugue ${s.version} on port ${s.port}`));
}

// ---------------------------------------------------------------- start up

function init() {
  S.picker = filePicker({
    target: $('#composer-text'),
    onadd: () => { S.modeChosen = true; setMode('ask'); },
  });
  $('#composer-files').append(S.picker);
  $('#composer-mode').addEventListener('change', (e) => {
    S.modeChosen = true;
    setMode(e.target.value);
  });
  const text = $('#composer-text');
  text.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      if (S.primary) S.primary.click();
    }
  });
  text.addEventListener('input', () => { sizeComposer(); storeDraftText(); });
  // Wait for focus to settle, so moving between the box's own controls keeps it open.
  $('.composer-box').addEventListener('focusin', () => setTimeout(sizeComposer));
  $('.composer-box').addEventListener('focusout', () => setTimeout(sizeComposer));
  $('#nav-backdrop').addEventListener('click', () => setNav(false));
  for (const dialog of document.querySelectorAll('dialog')) {
    dialog.addEventListener('click', (e) => { if (e.target === dialog) dialog.close(); });
  }
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') setNav(false);
  });
  // A click anywhere outside the ⋯ menu closes it.
  document.addEventListener('click', (e) => {
    if (!S.menuOpen || e.target.closest('.menu')) return;
    S.menuOpen = false;
    for (const menu of document.querySelectorAll('details.menu[open]')) menu.open = false;
  });
  $('#main').addEventListener('scroll', () => {
    const main = $('#main');
    S.follow = main.scrollTop + main.clientHeight >= main.scrollHeight - 80;
    markStuck();
  }, { passive: true });
  window.addEventListener('hashchange', route);
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
