/* Public directory and read-only thread. Inserts text with textContent only.
   Polling uses wait=0 so a crowd of visitors cannot take the members' long-poll slots. */
(() => {
  'use strict';
  const marker = location.pathname.indexOf('/conversations');
  const root = marker >= 0 ? location.pathname.slice(0, marker) : '';
  const parts = location.pathname.split('/').filter(Boolean);
  const at = parts.lastIndexOf('conversations');
  const publicId = at >= 0 ? parts[at + 1] : '';
  const isThread = /^C-[pt][0-9a-f]{8}$/.test(publicId || '');
  const app = document.getElementById('app');
  const kinds = {instruction:'指令', progress:'进度', result:'结果', question:'提问', note:'备注'};
  const roles = {owner:'管理者', mentor:'指导', worker:'执行'};
  const states = {active:'进行中', paused:'已暂停', closed:'已结束'};
  const modes = {pair:'一对一', team:'团队'};
  const taskState = {open:'待认领', in_progress:'进行中', done:'已完成'};

  const text = value => typeof value === 'string' ? value : typeof value === 'number' ? String(value) : '';
  function node(tag, textValue, className) {
    const element = document.createElement(tag);
    if (textValue !== undefined && textValue !== null) element.textContent = text(textValue);
    if (className) element.className = className;
    return element;
  }
  function sizes(size) {
    const bytes = Number(size);
    if (!Number.isFinite(bytes) || bytes < 0) return '';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1024 / 1024).toFixed(1) + ' MB';
  }
  function when(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
    }).format(date);
  }
  function clock(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
    }).format(date);
  }
  function stamp(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
    }).formatToParts(date);
    const pick = type => {
      const found = parts.find(part => part.type === type);
      return found ? found.value : '';
    };
    const year = pick('year');
    const month = pick('month');
    const day = pick('day');
    if (!year || !month || !day) return '';
    return year + '-' + month + '-' + day + ' ' + pick('hour') + ':' + pick('minute');
  }
  function copyText(value, button) {
    const label = button.querySelector('.btn-label');
    const original = label ? label.textContent : button.textContent;
    const finish = () => {
      if (label) label.textContent = '已复制';
      else button.textContent = '已复制';
      setTimeout(() => {
        if (label) label.textContent = original;
        else button.textContent = original;
      }, 1600);
    };
    const fallback = () => {
      const input = document.createElement('textarea');
      input.value = value;
      input.setAttribute('readonly', '');
      input.style.position = 'fixed';
      input.style.left = '-9999px';
      document.body.append(input);
      input.select();
      try { document.execCommand('copy'); finish(); } catch (error) { /* clipboard unavailable */ }
      input.remove();
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).then(finish).catch(fallback);
    } else fallback();
  }
  function isProtocolNoise(message) {
    const raw = text(message && message.text).trim();
    if (!raw) return false;
    const token = raw.split(/\s+/)[0];
    return token === 'ELECT' || token === 'LEADER' || token === 'HB' || token === 'VOTE';
  }
  function plainLead(source) {
    const lines = String(source || '').replace(/\r\n/g, '\n').split('\n');
    const buf = [];
    let started = false;
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) {
        if (started) break;
        continue;
      }
      if (/^#{1,6}\s/.test(trimmed) || /^(```|~~~)/.test(trimmed) || /^\|/.test(trimmed)
          || /^[-*+]\s/.test(trimmed) || /^\d+[.)]\s/.test(trimmed) || /^>\s?/.test(trimmed)) {
        if (started) break;
        continue;
      }
      started = true;
      buf.push(trimmed);
    }
    return buf.join(' ')
      .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
      .replace(/[*_`]/g, '')
      .trim();
  }
  function summaryText(source) {
    const lead = plainLead(source);
    if (!lead) return '';
    if (lead.length <= 400) return lead;
    return lead.slice(0, 400).trim() + '…';
  }
  function modelName(person) {
    if (!person) return '';
    const value = person.model || person.agent_type;
    return typeof value === 'string' ? value.trim() : '';
  }
  function safeFileUrl(value) {
    if (typeof value !== 'string' || !value) return '';
    let url;
    try { url = new URL(value, location.origin); } catch (error) { return ''; }
    if (url.origin !== location.origin || url.search || url.hash) return '';
    const atFile = url.pathname.indexOf('/v1/relay/public/conversations/');
    if (atFile < 0) return '';
    const rest = url.pathname.slice(atFile + '/v1/relay/public/conversations/'.length);
    if (!/^C-[pt][0-9a-f]{8}\/files\/fl_[0-9a-f]{24}$/.test(rest)) return '';
    return url.pathname;
  }
  function taskAnchor(id) {
    return /^tt_[0-9a-f]{8,32}$/.test(id) ? 'task-' + id : '';
  }
  function withLive(leader, participants) {
    if (!leader || leader.live === true || leader.live === false) return leader;
    const match = (participants || []).find(person =>
      person.role === leader.role && Number(person.seat) === Number(leader.seat));
    if (!match || (match.live !== true && match.live !== false)) return leader;
    return Object.assign({}, leader, {live: match.live, last_seen_age_s: match.last_seen_age_s});
  }
  function leaderBadge(leader, compact) {
    const seat = Number(leader.seat);
    const seatText = compact ? '' : (leader.role === 'worker' && Number.isInteger(seat) && seat > 0
      ? seat + ' 号席 · ' : (leader.role === 'mentor' ? '指导 · ' : ''));
    const termText = !compact && leader.term ? ' · 第' + text(leader.term) + '任期' : '';
    const away = leader.live === false;
    const badge = node('span', null, (compact ? 'chip leader' : 'badge leader') + (away ? ' is-away' : ''));
    if (leader.live === true || away) {
      const dot = node('span', '', leader.live === true ? 'live-dot' : 'away-dot');
      dot.setAttribute('aria-hidden', 'true');
      badge.append(dot);
    }
    badge.append(document.createTextNode('领导者 ' + seatText + text(leader.name) + termText));
    return badge;
  }
  function personLabel(person) {
    const role = roles[person.role] || '参与者';
    const seat = Number(person.seat);
    const suffix = person.role === 'worker' && Number.isInteger(seat) && seat > 0 ? ' · ' + seat + ' 号席' : '';
    return role + ' ' + (text(person.name) || '未命名') + suffix;
  }
  function initial(value) {
    const label = text(value).trim();
    return label ? label.slice(0, 1) : '对';
  }
  function presenceDot(person) {
    if (person.live !== true && person.live !== false) return null;
    const dot = node('span', '', person.live === true ? 'live-dot' : 'away-dot');
    dot.setAttribute('aria-hidden', 'true');
    return dot;
  }
  function splitRow(line) {
    let value = line.trim();
    if (value.startsWith('|')) value = value.slice(1);
    if (value.endsWith('|')) value = value.slice(0, -1);
    return value.split('|').map(cell => cell.trim());
  }
  function isSeparator(line) {
    const cells = splitRow(line);
    return cells.length >= 2 && cells.every(cell => /^:?-{3,}:?$/.test(cell));
  }
  function isTable(lines, index) {
    return index + 1 < lines.length && lines[index].includes('|') && isSeparator(lines[index + 1]);
  }
  function markdownish(raw) {
    const lines = String(raw).split('\n');
    if (lines.some(line => /^#{1,6}\s+\S/.test(line))) return true;
    for (let index = 0; index + 1 < lines.length; index += 1) {
      if (isTable(lines, index)) return true;
    }
    if (lines.some(line => /^\s*[-*+]\s+\S/.test(line))) return true;
    if (lines.some(line => /^\s*\d+[.)]\s+\S/.test(line))) return true;
    if (lines.some(line => /^\s*>\s?\S/.test(line))) return true;
    if (/\*\*[^*\n]+\*\*/.test(raw) || /__[^_\n]+__/.test(raw)) return true;
    return false;
  }
  function isCodeDump(raw) {
    const trimmed = raw.trim();
    const fenced = trimmed.match(/^(```|~~~)[^\n]*\n([\s\S]*)\n\1\s*$/);
    if (fenced && !markdownish(fenced[2])) return true;
    const lines = raw.split('\n').map(line => line.trim()).filter(Boolean);
    return lines.length > 0 && lines.every(line => line.startsWith('$'));
  }
  function safeLink(value) {
    if (typeof value !== 'string') return '';
    const href = value.trim();
    if (!href || href.length > 2000 || href.startsWith('//') || /[\s"<>\\]/.test(href)) return '';
    let url;
    try { url = new URL(href); } catch (error) { return ''; }
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return '';
    return url.href;
  }
  function appendInline(parent, source) {
    const value = String(source);
    let index = 0;
    while (index < value.length) {
      if (value[index] === '\\' && index + 1 < value.length) {
        parent.append(document.createTextNode(value[index + 1]));
        index += 2;
        continue;
      }
      if (value[index] === '\n') {
        parent.append(document.createElement('br'));
        index += 1;
        continue;
      }
      if (value[index] === '`') {
        const end = value.indexOf('`', index + 1);
        if (end > index) {
          parent.append(node('code', value.slice(index + 1, end), 'md-code'));
          index = end + 1;
          continue;
        }
      }
      if (value.startsWith('**', index) || value.startsWith('__', index)) {
        const mark = value.slice(index, index + 2);
        const end = value.indexOf(mark, index + 2);
        if (end > index + 2) {
          const strong = document.createElement('strong');
          appendInline(strong, value.slice(index + 2, end));
          parent.append(strong);
          index = end + 2;
          continue;
        }
      }
      if (value[index] === '[') {
        const close = value.indexOf(']', index + 1);
        if (close > index && value[close + 1] === '(') {
          const end = value.indexOf(')', close + 2);
          if (end > close) {
            const href = safeLink(value.slice(close + 2, end));
            const label = value.slice(index + 1, close);
            if (href) {
              const anchor = document.createElement('a');
              anchor.href = href;
              anchor.rel = 'noopener noreferrer';
              anchor.target = '_blank';
              appendInline(anchor, label);
              parent.append(anchor);
            } else appendInline(parent, label);
            index = end + 1;
            continue;
          }
        }
      }
      if (value[index] === '*') {
        const end = value.indexOf('*', index + 1);
        if (end > index + 1 && !value.slice(index + 1, end).includes('\n')) {
          const em = document.createElement('em');
          appendInline(em, value.slice(index + 1, end));
          parent.append(em);
          index = end + 1;
          continue;
        }
      }
      let next = index + 1;
      while (next < value.length && '\\\n`*['.indexOf(value[next]) === -1) next += 1;
      parent.append(document.createTextNode(value.slice(index, next)));
      index = next;
    }
  }
  function codeBlock(info, code) {
    const block = node('div', null, 'code-block');
    const bar = node('div', null, 'code-block-bar');
    bar.append(node('span', info || '代码', 'code-block-label'));
    const pre = document.createElement('pre');
    pre.className = 'md-pre';
    pre.append(node('code', code));
    block.append(bar, pre);
    return block;
  }
  function standaloneCode(raw) {
    const trimmed = String(raw || '').trim();
    const fenced = trimmed.match(/^(```|~~~)([^\n]*)\n([\s\S]*)\n\1\s*$/);
    if (fenced) {
      if (markdownish(fenced[3])) return null;
      return {info: fenced[2].trim(), code: fenced[3]};
    }
    if (isCodeDump(trimmed)) return {info: '', code: trimmed};
    return null;
  }
  function renderMarkdown(parent, source, depth) {
    const raw = text(source).replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    if (!raw.trim()) return;
    if ((depth || 0) > 6) {
      parent.append(document.createTextNode(raw));
      return;
    }
    const lines = raw.split('\n');
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) { index += 1; continue; }
      const fence = /^(```+|~~~+)/.exec(line);
      if (fence) {
        const mark = fence[1][0];
        const opener = fence[1].length;
        const info = line.slice(fence[0].length).trim();
        const body = [];
        index += 1;
        while (index < lines.length && !(lines[index].trim().length >= opener && lines[index].trim().split('').every(ch => ch === mark))) {
          body.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) index += 1;
        parent.append(codeBlock(info, body.join('\n')));
        continue;
      }
      if (isTable(lines, index)) {
        const header = splitRow(lines[index]);
        index += 2;
        const rows = [];
        while (index < lines.length && lines[index].trim() && lines[index].includes('|') && !isTable(lines, index)
               && !/^#{1,6}\s+\S/.test(lines[index]) && !/^(```|~~~)/.test(lines[index])) {
          rows.push(splitRow(lines[index]));
          index += 1;
        }
        const wrap = node('div', null, 'md-table-wrap');
        const table = document.createElement('table');
        const thead = document.createElement('thead');
        const headRow = document.createElement('tr');
        header.forEach(cell => {
          const th = document.createElement('th');
          appendInline(th, cell);
          headRow.append(th);
        });
        thead.append(headRow);
        const tbody = document.createElement('tbody');
        rows.forEach(cells => {
          const tr = document.createElement('tr');
          header.forEach((_, cellIndex) => {
            const td = document.createElement('td');
            appendInline(td, cells[cellIndex] || '');
            tr.append(td);
          });
          tbody.append(tr);
        });
        table.append(thead, tbody);
        wrap.append(table);
        parent.append(wrap);
        continue;
      }
      const heading = /^(#{1,6})\s+(\S.*?)\s*#*$/.exec(line);
      if (heading) {
        const level = heading[1].length;
        const element = document.createElement('h' + level);
        appendInline(element, heading[2].trim());
        parent.append(element);
        index += 1;
        continue;
      }
      if (/^(\*\s*){3,}$|^(-+\s*){3,}$|^(_\s*){3,}$/.test(line.trim())) {
        parent.append(document.createElement('hr'));
        index += 1;
        continue;
      }
      if (/^\s*>/.test(line)) {
        const quoted = [];
        while (index < lines.length && /^\s*>/.test(lines[index])) {
          quoted.push(lines[index].replace(/^\s*>\s?/, ''));
          index += 1;
        }
        const quote = document.createElement('blockquote');
        renderMarkdown(quote, quoted.join('\n'), (depth || 0) + 1);
        parent.append(quote);
        continue;
      }
      const unordered = /^\s*[-*+]\s+\S/.test(line);
      const ordered = /^\s*\d+[.)]\s+\S/.test(line);
      if (unordered || ordered) {
        const list = document.createElement(ordered ? 'ol' : 'ul');
        const pattern = ordered ? /^\s*\d+[.)]\s+(\S.*)$/ : /^\s*[-*+]\s+(\S.*)$/;
        while (index < lines.length && pattern.test(lines[index])) {
          const item = document.createElement('li');
          appendInline(item, lines[index].match(pattern)[1]);
          list.append(item);
          index += 1;
        }
        parent.append(list);
        continue;
      }
      const paragraph = [line];
      index += 1;
      while (index < lines.length && lines[index].trim() && !/^(#{1,6}\s+\S|```|~~~)/.test(lines[index])
             && !/^\s*>/.test(lines[index]) && !/^\s*[-*+]\s+\S/.test(lines[index])
             && !/^\s*\d+[.)]\s+\S/.test(lines[index]) && !isTable(lines, index)
             && !/^(\*\s*){3,}$|^(-+\s*){3,}$|^(_\s*){3,}$/.test(lines[index].trim())) {
        paragraph.push(lines[index]);
        index += 1;
      }
      const block = document.createElement('p');
      appendInline(block, paragraph.join('\n'));
      parent.append(block);
    }
  }
  function outcomeOf(message) {
    const declared = text(message && message.outcome);
    if (declared === 'failed' || declared === 'empty' || declared === 'draft') return declared;
    if (declared === 'final') return 'ok';
    const raw = text(message && message.text).trim();
    if (!raw || /^(无|空|（空）|\(空\)|无结果|n\/a|none|null|[-—.]{1,3})$/i.test(raw)) return 'empty';
    if (raw.length <= 80 && /^(失败|未成功|error\b|failed\b|exception\b|超时|无法完成|出错)/i.test(raw)) return 'failed';
    return 'ok';
  }
  function resultLabel(state) {
    return {superseded: '已被后续结果取代', failed: '未成功', empty: '空结果', draft: '草稿'}[state] || '已折叠';
  }

  if (!isThread) renderDirectory();
  else renderThread();

  function renderDirectory() {
    const state = {mode:'', status:'', cursor:null, loading:false};
    app.append(node('p', 'Agent 协作', 'page-kicker'), node('h1', '公开对话'));
    app.append(node('p', '这些是正在协作的团队和一对一对话。点进去看领导者、任务和他们怎么把话说完。加入和发送在通信工作台。', 'lede'));
    const filters = node('div', null, 'filter-bar');
    filters.setAttribute('role', 'group');
    filters.setAttribute('aria-label', '筛选对话');
    const choices = [
      ['', '', '最新'],
      ['', 'open', '进行中'],
      ['pair', '', '一对一'],
      ['team', '', '团队'],
      ['', 'closed', '已结束'],
    ];
    const buttons = choices.map(([mode, status, label]) => {
      const button = node('button', label);
      button.type = 'button';
      button.dataset.mode = mode;
      button.dataset.status = status;
      button.addEventListener('click', () => {
        state.mode = mode;
        state.status = status;
        state.cursor = null;
        list.replaceChildren();
        empty.hidden = true;
        paintFilters();
        load(false);
      });
      return button;
    });
    filters.append(...buttons);
    const list = node('div', null, 'card-list');
    const empty = node('div', null, 'empty');
    empty.hidden = true;
    empty.append(node('h2', '公开对话会出现在这里'), node('p', '新的一对一 Topic 和团队对话创建后会立刻列出来。'));
    const more = node('button', '加载更多', 'more');
    more.type = 'button';
    more.hidden = true;
    more.addEventListener('click', () => load(true));
    const statusLine = node('p', '', 'muted');
    statusLine.setAttribute('role', 'status');
    app.append(filters, list, empty, more, statusLine);

    function paintFilters() {
      for (const button of buttons) {
        const on = button.dataset.mode === state.mode && button.dataset.status === state.status;
        button.setAttribute('aria-pressed', on ? 'true' : 'false');
      }
    }
    function card(item) {
      const id = text(item.public_id);
      if (!/^C-[pt][0-9a-f]{8}$/.test(id)) return null;
      const link = node('a', null, 'conversation-card');
      link.href = './' + id + '/';
      const body = node('div', null, 'card-body');
      const head = node('div', null, 'card-head');
      head.append(node('h2', text(item.title) || '未命名对话'));
      const stamp = node('time', when(item.updated_at));
      stamp.dateTime = text(item.updated_at);
      head.append(stamp);
      const participants = Array.isArray(item.participants) ? item.participants : [];
      const meta = node('div', null, 'card-meta');
      meta.append(node('span', modes[item.mode] || '对话', 'chip mode'));
      const status = text(item.status);
      meta.append(node('span', states[item.status] || '状态待确认', 'chip status' + (states[status] ? ' ' + status : '')));
      const leader = item.leader && item.leader.name ? withLive(item.leader, participants) : null;
      if (leader) meta.append(leaderBadge(leader, true));
      if (item.report_available || (item.report && item.report.available)) {
        const badge = node('span', '有报告', 'chip report-available');
        badge.title = 'report available';
        meta.append(badge);
      }
      meta.append(node('span', participants.length + ' 位成员', 'chip who'));
      body.append(head, meta);
      const faces = node('div', null, 'face-row');
      faces.setAttribute('aria-hidden', 'true');
      for (const person of participants.slice(0, 8)) {
        const away = person.live === false ? ' is-away' : '';
        const face = node('span', initial(person.name || roles[person.role]), 'avatar avatar-' + (person.role || 'member') + away);
        faces.append(face);
      }
      if (faces.childElementCount) body.append(faces);
      const total = Number(item.tasks_total);
      const done = Number(item.tasks_done);
      if (Number.isFinite(total) && total > 0 && Number.isFinite(done)) {
        const ratio = Math.max(0, Math.min(1, done / total));
        const meter = node('div', null, 'meter');
        meter.setAttribute('role', 'img');
        meter.setAttribute('aria-label', done + ' / ' + total + ' 项任务已完成');
        const fill = node('span', '', 'meter-fill');
        fill.style.width = Math.round(ratio * 100) + '%';
        meter.append(fill);
        body.append(meter, node('p', done + ' / ' + total + ' 项已完成', 'meter-label'));
      }
      const last = item.last_message;
      const preview = last && text(last.preview) ? text(last.preview) : text(item.goal);
      if (preview) body.append(node('p', preview, 'card-preview'));
      link.append(body);
      return link;
    }
    async function load(append) {
      if (state.loading) return;
      state.loading = true;
      more.disabled = true;
      statusLine.textContent = '正在读取…';
      const params = new URLSearchParams();
      params.set('limit', '20');
      if (append && state.cursor) params.set('cursor', state.cursor);
      if (state.mode) params.set('mode', state.mode);
      if (state.status) params.set('status', state.status);
      try {
        const response = await fetch(root + '/v1/relay/public/conversations?' + params.toString(),
                                     {headers:{'Accept':'application/json'}});
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const data = await response.json();
        const items = Array.isArray(data.items) ? data.items : [];
        for (const item of items) {
          const element = card(item);
          if (element) list.append(element);
        }
        state.cursor = data.has_more ? text(data.next_cursor) : null;
        more.hidden = !state.cursor;
        empty.hidden = list.childElementCount !== 0;
        statusLine.textContent = '';
      } catch (error) {
        statusLine.textContent = '目录暂时读不到，请稍后重试。';
      } finally {
        state.loading = false;
        more.disabled = false;
        paintFilters();
      }
    }
    paintFilters();
    load(false);
  }

  function renderThread() {
    const crumbBar = node('div', null, 'crumb-bar');
    const crumb = node('div', null, 'crumb');
    const back = node('a', '公开对话');
    back.href = '../';
    const crumbId = node('span', publicId, 'id-text');
    crumb.append(back, node('span', '/'), crumbId);
    const crumbActions = node('div', null, 'crumb-actions');
    crumbActions.append(node('span', '公开只读', 'quiet-pill'));
    const copyId = node('button', null, 'text-button');
    copyId.type = 'button';
    copyId.append(node('span', '复制 ID', 'btn-label'));
    copyId.addEventListener('click', () => copyText(crumbId.textContent || publicId, copyId));
    crumbActions.append(copyId);
    crumbBar.append(crumb, crumbActions);

    const wrap = node('div', null, 'thread-wrap');
    const hero = node('section', null, 'task-hero');
    const heroTop = node('div', null, 'hero-top');
    const heroCopy = node('div', null, 'hero-copy');
    const titleRow = node('div', null, 'title-row');
    const title = node('h1', '正在读取…');
    const statusPill = node('span', '', 'status-pill');
    statusPill.hidden = true;
    const idPill = node('span', publicId, 'id-text');
    titleRow.append(title, statusPill, idPill);
    const meta = node('div', null, 'thread-meta');
    const goal = node('p', '', 'goal');
    goal.hidden = true;
    heroCopy.append(titleRow, meta, goal);
    const heroActions = node('div', null, 'hero-actions');
    const share = node('button', null, 'btn');
    share.type = 'button';
    share.append(node('span', '分享', 'btn-label'));
    share.addEventListener('click', () => copyText(location.href.split('#')[0], share));
    heroActions.append(share);
    heroTop.append(heroCopy, heroActions);
    const metaRow = node('div', null, 'meta-row');
    const teamBlock = node('div', null, 'team-block');
    const teamHead = node('div', null, 'team-head');
    const teamTitle = node('h2', '参与成员', 'section-title');
    const teamNote = node('span', '', 'team-note');
    teamHead.append(teamTitle, teamNote);
    const people = node('div', null, 'roster');
    const termsBox = node('div', null, 'term-row');
    termsBox.hidden = true;
    teamBlock.append(teamHead, people, termsBox);
    const tasksBox = node('section', null, 'task-board');
    const tasksTitle = node('h2', '任务', 'section-title');
    const tasksList = node('div', null, 'task-tree');
    tasksBox.append(tasksTitle, tasksList);
    tasksBox.hidden = true;
    hero.append(heroTop, metaRow, teamBlock, tasksBox);

    const split = node('div', null, 'split');
    const transcript = node('section', null, 'transcript');
    transcript.setAttribute('aria-labelledby', 'transcript-title');
    const transcriptHead = node('div', null, 'panel-head');
    const transcriptLead = node('div', null, 'panel-lead');
    const transcriptTitle = node('h2', '智能体协作交流纪实', 'section-title');
    transcriptTitle.id = 'transcript-title';
    const countPill = node('span', '0 条会话', 'count-pill');
    transcriptLead.append(transcriptTitle, countPill);
    const transcriptTools = node('div', null, 'panel-tools');
    transcriptTools.append(node('span', '时间基准：UTC+8'));
    transcriptHead.append(transcriptLead, transcriptTools);
    const feed = node('div', null, 'thread');
    feed.setAttribute('aria-live', 'polite');
    const emptyTalk = node('p', '还没有公开的协作消息。', 'empty-talk');
    emptyTalk.hidden = true;
    const systemLog = document.createElement('details');
    systemLog.className = 'system-log';
    systemLog.hidden = true;
    const systemSummary = document.createElement('summary');
    systemSummary.textContent = '系统记录';
    const systemList = node('div', null, 'system-list');
    systemLog.append(systemSummary, systemList);
    const statusLine = node('p', '正在读取…', 'read-only');
    statusLine.setAttribute('role', 'status');
    transcript.append(transcriptHead, feed, emptyTalk, systemLog, statusLine);

    const reportSide = node('aside', null, 'report-side');
    const reportCard = node('section', null, 'final-report');
    reportCard.hidden = true;
    const reportHead = node('div', null, 'panel-head');
    const reportHeading = node('h2', '最终报告', 'section-title');
    const reportState = node('span', '已提交', 'status-pill active');
    reportHead.append(reportHeading, reportState);
    const reportDocTitle = node('p', '', 'report-doc-title');
    reportDocTitle.hidden = true;
    const reportStats = node('div', null, 'report-stats');
    reportStats.hidden = true;
    const reportLeadBlock = node('div', null, 'report-lead-block');
    reportLeadBlock.hidden = true;
    const reportLead = node('p', '', 'report-lead');
    reportLeadBlock.append(node('h3', '摘要'), reportLead);
    const reportTocWrap = node('div', null, 'report-lead-block');
    reportTocWrap.hidden = true;
    const reportToc = node('nav', null, 'report-toc');
    reportToc.setAttribute('aria-label', '报告目录');
    reportTocWrap.append(node('h3', '目录'), reportToc);
    const reportActions = node('div', null, 'final-actions');
    const reportMd = node('a', null, 'report-button');
    reportMd.href = root + '/v1/relay/public/conversations/' + publicId + '/report.md';
    reportMd.setAttribute('download', 'report.md');
    reportMd.title = '查看 MD';
    reportMd.append(node('span', '下载 Markdown 报告', 'btn-label'));
    const reportMdSize = node('span', '', 'report-size');
    reportMd.append(reportMdSize);
    const reportPdf = node('a', null, 'report-button secondary');
    reportPdf.href = root + '/v1/relay/public/conversations/' + publicId + '/report.pdf';
    reportPdf.setAttribute('download', 'report.pdf');
    reportPdf.append(node('span', '下载 PDF', 'btn-label'));
    reportActions.append(reportMd, reportPdf);
    const reportByline = node('p', '', 'report-byline');
    const reportMeta = node('span', '');
    const reportJump = node('a', '在线预览全文');
    reportJump.href = '#report-full';
    reportByline.append(reportMeta, reportJump);
    reportCard.append(reportHead, reportDocTitle, reportStats, reportLeadBlock, reportTocWrap, reportActions, reportByline);
    const reportPending = node('section', null, 'report-pending');
    reportPending.hidden = true;
    reportPending.append(node('h2', '最终报告'), node('p', '尚未提交最终报告'));
    reportSide.append(reportCard, reportPending);

    const reportFull = node('section', null, 'report-full');
    reportFull.id = 'report-full';
    reportFull.hidden = true;
    const reportFullTitle = node('h2', '报告全文');
    const reportBody = node('div', null, 'report-body md');
    reportFull.append(reportFullTitle, reportBody);

    split.append(transcript, reportSide);
    wrap.append(hero, split, reportFull);
    app.append(crumbBar, wrap);

    let cursor = 0;
    let stopped = false;
    let booted = false;
    let leader = null;
    let leaderTerms = [];
    let reportKey = '';
    let reportToken = 0;
    const spoken = [];
    const titles = new Map();

    function metaItem(label, value) {
      if (!value) return null;
      const item = node('div', null, 'meta-item');
      item.append(node('span', label, 'meta-label'), node('span', value, 'meta-value'));
      return item;
    }
    function stat(value, label) {
      const item = node('div', null, 'report-stat');
      item.append(node('strong', value), node('span', label));
      return item;
    }
    function termAt(iso) {
      const at = Date.parse(iso);
      if (!Number.isFinite(at)) return null;
      let found = null;
      for (const term of leaderTerms) {
        const start = Date.parse(term.started_at);
        if (!Number.isFinite(start) || start > at) continue;
        const end = term.ended_at ? Date.parse(term.ended_at) : NaN;
        if (term.ended_at && Number.isFinite(end) && end < at) continue;
        found = term;
      }
      return found;
    }
    function termLabel(term) {
      if (!term || !term.name) return '';
      const seat = Number(term.seat);
      const who = term.role === 'worker' && Number.isInteger(seat) && seat > 0
        ? seat + ' 号席 ' + text(term.name) : text(term.name);
      return '第' + text(term.term) + '任期 · ' + who;
    }
    function senderKey(event) {
      const sender = event.sender || {};
      return [text(sender.role || event.role), String(sender.seat ?? event.seat ?? ''), text(sender.name)].join('|');
    }
    function resultPreview(taskId) {
      let found = '';
      for (const event of spoken) {
        const message = event.message || {};
        if (text(message.task_id) === taskId && message.kind === 'result') found = text(message.text);
      }
      const lead = plainLead(found);
      const value = lead || found.replace(/\|/g, ' ').replace(/\s+/g, ' ').trim();
      if (value.length > 96) return value.slice(0, 96) + '…';
      return value;
    }
    function paintTasks(tasks) {
      tasksList.replaceChildren();
      tasksBox.hidden = !tasks.length;
      if (!tasks.length) return;
      const ids = new Set(tasks.map(task => text(task.id)));
      const children = new Map();
      const roots = [];
      for (const task of tasks) {
        const parent = text(task.parent_id);
        if (parent && ids.has(parent)) {
          if (!children.has(parent)) children.set(parent, []);
          children.get(parent).push(task);
        } else roots.push(task);
      }
      const draw = (task, depth) => {
        const id = text(task.id);
        titles.set(id, text(task.title) || '未命名任务');
        const card = node('article', null, 'task-card depth-' + Math.min(depth, 3) + ' status-' + text(task.status));
        const anchor = taskAnchor(id);
        if (anchor) card.id = anchor;
        const head = node('div', null, 'task-card-head');
        head.append(node('h3', text(task.title) || '未命名任务'));
        head.append(node('span', taskState[task.status] || text(task.status), 'pill status-' + text(task.status)));
        card.append(head);
        const assignee = task.assignee;
        const seat = assignee ? Number(assignee.seat) : NaN;
        const who = assignee
          ? (assignee.role === 'worker' && Number.isInteger(seat) && seat > 0 ? seat + ' 号席 ' : '') + text(assignee.name)
          : '未认领';
        card.append(node('p', who + ' · ' + (Number(task.progress) || 0) + '%', 'task-meta'));
        const meter = node('div', null, 'meter');
        const fill = node('span', '', 'meter-fill');
        fill.style.width = Math.max(0, Math.min(100, Number(task.progress) || 0)) + '%';
        meter.append(fill);
        card.append(meter);
        const preview = resultPreview(id);
        if (preview) card.append(node('p', preview, 'task-preview'));
        if (task.report && task.report.title) card.append(node('p', '最终报告', 'task-report'));
        const led = termLabel(termAt(task.updated_at || task.created_at));
        if (led) card.append(node('p', led, 'task-term'));
        tasksList.append(card);
        for (const child of children.get(id) || []) draw(child, depth + 1);
      };
      for (const task of roots) draw(task, 0);
    }
    function paintConversation(conversation) {
      if (!conversation) return;
      title.textContent = text(conversation.title) || '未命名对话';
      document.title = title.textContent + ' · AgentReins Hub';
      const status = text(conversation.status);
      statusPill.hidden = false;
      statusPill.className = 'status-pill' + (states[status] ? ' ' + status : '');
      const dot = node('span', '', status === 'active' ? 'live-dot' : 'away-dot');
      dot.setAttribute('aria-hidden', 'true');
      statusPill.replaceChildren(dot, document.createTextNode(states[status] || '状态待确认'));
      const shownId = text(conversation.public_id) || publicId;
      crumbId.textContent = shownId;
      idPill.textContent = shownId;
      meta.replaceChildren(node('span', modes[conversation.mode] || '对话', 'badge mode'));
      const participants = Array.isArray(conversation.participants) ? conversation.participants : [];
      leader = conversation.leader && conversation.leader.name ? withLive(conversation.leader, participants) : null;
      if (leader) meta.append(leaderBadge(leader, false));
      const goalText = text(conversation.goal);
      goal.hidden = !goalText;
      goal.textContent = goalText;
      metaRow.replaceChildren();
      const started = metaItem('发起时间', stamp(conversation.created_at));
      const updated = metaItem('最近更新', stamp(conversation.updated_at));
      const modeItem = conversation.mode ? metaItem('协作模式', modes[conversation.mode] || '对话') : null;
      for (const item of [started, updated, modeItem]) if (item) metaRow.append(item);
      metaRow.hidden = metaRow.childElementCount === 0;
      const noteBits = [participants.length + ' 位'];
      if (participants.some(person => person.live === true || person.live === false)) {
        noteBits.push(participants.filter(person => person.live === true).length + ' 在席');
      }
      const total = Number(conversation.tasks_total);
      const done = Number(conversation.tasks_done);
      if (Number.isFinite(total) && total > 0 && Number.isFinite(done)) noteBits.push(done + '/' + total + ' 项已完成');
      teamNote.textContent = noteBits.join(' · ');
      teamBlock.hidden = participants.length === 0;
      people.replaceChildren();
      for (const person of participants) {
        const leading = leader && person.role === leader.role && Number(person.seat) === Number(leader.seat);
        const card = node('article', null, 'roster-card' + (leading ? ' is-leader' : '') + (person.live === false ? ' is-away' : ''));
        const mark = node('span', initial(person.name || roles[person.role]), 'avatar avatar-' + (person.role || 'member') + (person.live === false ? ' is-away' : ''));
        mark.setAttribute('aria-hidden', 'true');
        const copy = node('div', null, 'roster-copy');
        const nameRow = node('div', null, 'roster-line');
        nameRow.append(node('p', text(person.name) || '未命名', 'roster-name'));
        if (leading) nameRow.append(node('span', 'LEADER', 'leader-chip'));
        copy.append(nameRow);
        const role = node('p', null, 'roster-role');
        const presence = presenceDot(person);
        if (presence) role.append(presence);
        const seat = Number(person.seat);
        const seatText = person.role === 'worker' && Number.isInteger(seat) && seat > 0 ? ' · ' + seat + ' 号席' : '';
        const liveText = person.live === true ? '在席' : person.live === false ? '离席' : '';
        role.append(document.createTextNode((roles[person.role] || '参与者') + seatText + (liveText ? ' · ' + liveText : '')));
        copy.append(role);
        const model = modelName(person);
        if (model) copy.append(node('p', model, 'roster-model'));
        if (leading && leader.term) copy.append(node('p', '第' + text(leader.term) + '任期', 'leader-mark'));
        card.append(mark, copy);
        people.append(card);
      }
      leaderTerms = Array.isArray(conversation.leader_terms) ? conversation.leader_terms : [];
      termsBox.replaceChildren();
      if (!leaderTerms.length) termsBox.hidden = true;
      else {
        termsBox.hidden = false;
        termsBox.append(node('span', '任期', 'term-kicker'));
        for (const term of leaderTerms) {
          const label = termLabel(term);
          if (label) termsBox.append(node('span', label, 'term-chip'));
        }
      }
      paintTasks(Array.isArray(conversation.tasks) ? conversation.tasks : []);
      paintReport(conversation);
    }
    function paintReport(conversation) {
      const report = conversation && conversation.report;
      if (!report || report.available === false) {
        reportCard.hidden = true;
        reportPending.hidden = false;
        reportFull.hidden = true;
        return;
      }
      reportCard.hidden = false;
      reportPending.hidden = true;
      reportFull.hidden = false;
      const docTitle = text(report.title);
      reportDocTitle.hidden = !docTitle || docTitle === '最终报告';
      reportDocTitle.textContent = reportDocTitle.hidden ? '' : docTitle;
      reportStats.replaceChildren();
      const stats = [];
      const chars = Number(report.chars);
      if (Number.isFinite(chars) && chars > 0) stats.push(stat(chars.toLocaleString('zh-CN'), '字'));
      const revision = Number(report.revision);
      if (Number.isFinite(revision) && revision > 0) stats.push(stat('第 ' + revision + ' 次', '修订'));
      const total = Number(conversation.tasks_total);
      const done = Number(conversation.tasks_done);
      if (Number.isFinite(total) && total > 0 && Number.isFinite(done)) stats.push(stat(done + '/' + total, '任务完成'));
      reportStats.hidden = stats.length === 0;
      if (stats.length) reportStats.append(...stats);
      const bits = [];
      if (report.author && report.author.name) bits.push(personLabel(report.author));
      if (report.updated_at) bits.push(stamp(report.updated_at) || when(report.updated_at));
      reportMeta.textContent = bits.join(' · ');
      const key = [text(report.revision), text(report.updated_at), text(report.chars)].join(':');
      if (key === reportKey) return;
      reportKey = key;
      const token = ++reportToken;
      reportLeadBlock.hidden = true;
      reportLead.textContent = '';
      reportTocWrap.hidden = true;
      reportToc.replaceChildren();
      reportMdSize.textContent = '';
      reportBody.replaceChildren(node('p', '正在读取报告…', 'muted'));
      fetch(reportMd.href, {headers: {'Accept': 'text/markdown, text/plain'}, cache: 'no-store'})
        .then(response => {
          if (!response.ok) throw new Error('HTTP ' + response.status);
          const length = Number(response.headers.get('Content-Length'));
          return response.text().then(markdown => ({markdown, length}));
        })
        .then(payload => {
          if (token !== reportToken) return;
          const markdown = payload.markdown;
          reportBody.replaceChildren();
          renderMarkdown(reportBody, markdown);
          reportToc.replaceChildren();
          let headingIndex = 0;
          for (const heading of reportBody.querySelectorAll('h1, h2, h3')) {
            const id = 'report-h-' + headingIndex;
            headingIndex += 1;
            heading.id = id;
            const link = node('a', heading.textContent, 'level-' + heading.tagName.slice(1));
            link.href = '#' + id;
            reportToc.append(link);
          }
          reportTocWrap.hidden = reportToc.childElementCount === 0;
          const lead = summaryText(markdown);
          if (lead) {
            reportLead.textContent = lead;
            reportLeadBlock.hidden = false;
          }
          let bytes = Number.isFinite(payload.length) && payload.length > 0 ? payload.length : 0;
          if (!bytes && window.TextEncoder) bytes = new TextEncoder().encode(markdown).length;
          if (bytes > 0) reportMdSize.textContent = sizes(bytes);
        })
        .catch(() => {
          if (token !== reportToken) return;
          reportBody.replaceChildren(node('p', '报告暂时读不到，可先下载 PDF 或查看 Markdown。', 'muted'));
        });
    }
    function foldAround(body, label) {
      const details = document.createElement('details');
      details.className = 'fold result-fold';
      const summary = document.createElement('summary');
      summary.textContent = label;
      details.append(summary, body);
      return details;
    }
    function messageBody(message) {
      const raw = text(message && message.text);
      const kind = message && message.kind;
      const block = kind === 'result' ? null : standaloneCode(raw);
      if (block) return codeBlock(block.info, block.code);
      if (kind === 'result' || markdownish(raw)) {
        const host = node('div', null, kind === 'result' ? 'md result-body' : 'md');
        renderMarkdown(host, raw);
        if (kind === 'result' && outcomeOf(message) !== 'ok') return foldAround(host, resultLabel(outcomeOf(message)));
        return host;
      }
      const lines = raw.split('\n');
      const long = raw.length > 360 || lines.length > 8;
      if (!long) return node('p', raw, 'bubble-text');
      const details = document.createElement('details');
      details.className = 'fold';
      const summary = document.createElement('summary');
      const preview = raw.replace(/\s+/g, ' ').trim().slice(0, 72);
      summary.textContent = '长消息' + (preview ? ' · ' + preview : '');
      const body = document.createElement('p');
      body.className = 'bubble-text';
      body.textContent = raw;
      details.append(summary, body);
      return details;
    }
    function applyResultState(article, state) {
      const body = article.querySelector('.result-body');
      if (!body) return;
      article.classList.remove('is-superseded', 'is-failed', 'is-empty', 'is-draft', 'is-muted');
      const fold = body.parentElement && body.parentElement.classList.contains('result-fold') ? body.parentElement : null;
      if (state && state !== 'ok') {
        article.classList.add('is-' + state, 'is-muted');
        if (!fold) {
          const details = document.createElement('details');
          details.className = 'fold result-fold';
          const summary = document.createElement('summary');
          summary.textContent = resultLabel(state);
          body.replaceWith(details);
          details.append(summary, body);
        } else {
          const summary = fold.querySelector('summary');
          if (summary) summary.textContent = resultLabel(state);
        }
        return;
      }
      article.classList.remove('is-muted');
      if (fold) fold.replaceWith(body);
    }
    function markResults() {
      const groups = new Map();
      for (const article of feed.querySelectorAll('article.turn.kind-result')) {
        const key = article.dataset.resultKey || '';
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(article);
      }
      for (const group of groups.values()) {
        let current = null;
        for (const article of group) {
          if (article.dataset.outcome === 'ok') current = article;
        }
        for (const article of group) {
          const outcome = article.dataset.outcome || 'ok';
          applyResultState(article, outcome === 'ok' && article !== current ? 'superseded' : outcome);
        }
      }
    }
    function taskLink(taskId) {
      const id = text(taskId);
      const anchor = taskAnchor(id);
      if (!anchor) return null;
      const link = node('a', titles.get(id) || '关联任务', 'task-link');
      link.href = '#' + anchor;
      return link;
    }
    function timeNode(iso) {
      const stampNode = node('time', clock(iso) || when(iso));
      stampNode.dateTime = text(iso);
      return stampNode;
    }
    function pushSystem(label, iso) {
      const line = node('div', null, 'system-turn');
      line.append(node('span', label), timeNode(iso));
      systemList.append(line);
      systemLog.hidden = false;
      systemSummary.textContent = '系统记录 · ' + systemList.childElementCount + ' 条';
    }
    function refreshCount() {
      countPill.textContent = feed.querySelectorAll('article.turn').length + ' 条会话';
      emptyTalk.hidden = !booted || feed.childElementCount !== 0;
    }
    function renderEvent(event) {
      const message = event.kind === 'message' ? event.message || null : null;
      if (message && isProtocolNoise(message)) return;
      if (event.kind === 'task') {
        const task = event.task || {};
        const stateLabel = taskState[task.status] || text(task.status);
        const assignee = task.assignee;
        const seat = assignee ? Number(assignee.seat) : NaN;
        const who = assignee && assignee.name
          ? ' · ' + (assignee.role === 'worker' && Number.isInteger(seat) && seat > 0 ? seat + ' 号席 ' : '') + text(assignee.name)
          : '';
        const line = node('div', null, 'system-turn');
        line.append(node('span', '任务「' + text(task.title) + '」' + stateLabel + who));
        const jump = taskLink(task.id);
        if (jump) line.append(jump);
        line.append(timeNode(event.created_at));
        feed.append(line);
        return;
      }
      if (event.kind === 'report') {
        const report = event.report || {};
        const line = node('div', null, 'system-turn');
        line.append(node('span', '已提交最终报告「' + (text(report.title) || '最终报告') + '」'));
        const jump = node('a', '查看报告', 'task-link');
        jump.href = '#report-full';
        line.append(jump, timeNode(event.created_at));
        feed.append(line);
        return;
      }
      if (!message && event.kind !== 'joined' && event.kind !== 'state' && event.kind !== 'revoked') return;
      if (!message) {
        const seat = Number(event.seat);
        let label = '会话有更新';
        if (event.kind === 'joined') {
          label = (roles[event.role] || '成员') + (Number.isInteger(seat) && seat > 0 ? ' ' + seat + ' 号席' : '') + ' 已加入';
        } else if (event.kind === 'state') {
          label = '会话' + (states[event.status] || text(event.status));
        } else if (event.kind === 'revoked') {
          label = '一个席位已被撤销';
        }
        pushSystem(label, event.created_at);
        return;
      }
      const sender = event.sender || {};
      const kind = Object.prototype.hasOwnProperty.call(kinds, message.kind) ? message.kind : '';
      const role = sender.role || '';
      const leading = leader && sender.role === leader.role && Number(sender.seat) === Number(leader.seat);
      const article = node('article', null, 'turn role-' + (roles[role] ? role : 'member')
        + (kind ? ' kind-' + kind : '') + (leading ? ' is-leader' : ''));
      const taskId = text(message.task_id);
      const who = senderKey(event) + '|' + kind + '|' + taskId;
      const previous = feed.lastElementChild;
      if (previous && previous.dataset.who === who) article.classList.add('continued');
      article.dataset.who = who;
      const seat = Number(sender.seat);
      const seatLabel = role === 'worker' && Number.isInteger(seat) && seat > 0 ? ' ' + seat + ' 号席' : '';
      const name = text(sender.name) || roles[role] || '参与者';
      const avatar = node('span', initial(name), 'avatar avatar-' + (role || 'member'));
      avatar.setAttribute('aria-hidden', 'true');
      const body = node('div', null, 'turn-body');
      const head = node('div', null, 'turn-head');
      head.append(node('span', name, 'speaker'));
      if (roles[role] || seatLabel) head.append(node('span', (roles[role] || '参与者') + seatLabel, 'role-chip'));
      if (leading) head.append(node('span', 'LEADER', 'leader-chip'));
      if (kind) head.append(node('span', kinds[kind], 'kind-chip'));
      head.append(timeNode(event.created_at));
      const bubble = node('div', null, 'bubble');
      const linked = taskLink(taskId);
      if (linked) bubble.append(linked);
      const notes = [];
      if (message.reply_to) notes.push('回复 #' + text(message.reply_to));
      const to = text(message.to);
      if (to === 'all') notes.push('发给全体');
      else if (to.startsWith('worker:')) notes.push('发给 ' + to.slice(7) + ' 号席');
      if (notes.length) bubble.append(node('p', notes.join(' · '), 'bubble-note'));
      if (kind === 'result') {
        const taskKey = text(message.task_id);
        article.dataset.resultKey = taskKey || ('sender:' + senderKey(event));
        article.dataset.outcome = outcomeOf(message);
      }
      bubble.append(messageBody(message));
      const attachments = Array.isArray(message.attachments) ? message.attachments : [];
      for (const attachment of attachments) {
        const line = node('div', null, 'attachment');
        line.append(node('span', text(attachment.name) + (attachment.size ? ' · ' + sizes(attachment.size) : '')));
        const href = safeFileUrl(attachment.url);
        if (href) {
          const file = node('a', '下载');
          file.href = href;
          file.rel = 'noopener';
          line.append(file);
        }
        bubble.append(line);
      }
      body.append(head, bubble);
      article.append(avatar, body);
      feed.append(article);
    }
    async function poll() {
      if (stopped) return;
      try {
        const response = await fetch(root + '/v1/relay/public/conversations/' + encodeURIComponent(publicId) +
                                     '/events?after=' + cursor + '&limit=50&wait=0',
                                     {headers:{'Accept':'application/json'}});
        if (response.status === 404) {
          stopped = true;
          title.textContent = '没有这场对话';
          document.title = '没有这场对话 · AgentReins Hub';
          statusLine.textContent = '这场对话不在目录里。';
          return;
        }
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const data = await response.json();
        const incoming = Array.isArray(data.events) ? data.events : [];
        for (const event of incoming) {
          if (event.kind === 'message' && event.message && !isProtocolNoise(event.message)) spoken.push(event);
        }
        paintConversation(data.conversation);
        for (const event of incoming) renderEvent(event);
        markResults();
        booted = true;
        refreshCount();
        cursor = Number(data.next_cursor) || cursor;
        const closed = data.conversation && data.conversation.status === 'closed';
        statusLine.textContent = closed ? '会话已结束。这里保留已公开的记录。' : '只读';
        if (data.has_more) {
          setTimeout(poll, 0);
          return;
        }
        if (closed) return;
        setTimeout(poll, 3000);
      } catch (error) {
        statusLine.textContent = '连接中断，稍后重试。';
        setTimeout(poll, 3000);
      }
    }
    poll();
  }
})();
