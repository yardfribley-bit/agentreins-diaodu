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
    const back = node('a', '全部对话');
    back.href = '../';
    const kicker = node('p', null, 'page-kicker');
    kicker.append(back);
    const title = node('h1', '正在读取…');
    const meta = node('div', null, 'thread-meta');
    const goal = node('p', '', 'goal');
    const rosterTitle = node('h2', '参与者', 'section-title');
    const people = node('div', null, 'roster');
    const termsBox = node('div', null, 'term-row');
    termsBox.hidden = true;
    const tasksTitle = node('h2', '任务', 'section-title');
    const tasksBox = node('section', null, 'task-board');
    const tasksList = node('div', null, 'task-tree');
    tasksBox.append(tasksTitle, tasksList);
    tasksBox.hidden = true;
    const talkTitle = node('h2', '对话', 'section-title');
    const feed = node('div', null, 'thread');
    feed.setAttribute('aria-live', 'polite');
    const statusLine = node('p', '正在读取…', 'read-only');
    statusLine.setAttribute('role', 'status');
    app.append(kicker, title, meta, goal, rosterTitle, people, termsBox, tasksBox, talkTitle, feed, statusLine);

    let cursor = 0;
    let stopped = false;
    let leader = null;
    let leaderTerms = [];
    const spoken = [];
    const titles = new Map();

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
        if (text(message.task_id) === taskId && message.kind === 'result') {
          found = text(message.text).replace(/\s+/g, ' ').trim();
        }
      }
      if (found.length > 96) return found.slice(0, 96) + '…';
      return found;
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
      meta.replaceChildren(
        node('span', modes[conversation.mode] || '对话', 'badge mode'),
        node('span', states[conversation.status] || '状态待确认', 'badge ' + text(conversation.status))
      );
      const participants = Array.isArray(conversation.participants) ? conversation.participants : [];
      leader = conversation.leader && conversation.leader.name ? withLive(conversation.leader, participants) : null;
      if (leader) meta.append(leaderBadge(leader, false));
      goal.textContent = text(conversation.goal);
      people.replaceChildren();
      for (const person of participants) {
        const leading = leader && person.role === leader.role && Number(person.seat) === Number(leader.seat);
        const card = node('article', null, 'roster-card' + (leading ? ' is-leader' : '') + (person.live === false ? ' is-away' : ''));
        const mark = node('span', initial(person.name || roles[person.role]), 'avatar avatar-' + (person.role || 'member'));
        mark.setAttribute('aria-hidden', 'true');
        const copy = node('div', null, 'roster-copy');
        copy.append(node('p', text(person.name) || '未命名', 'roster-name'));
        const role = node('p', null, 'roster-role');
        const dot = presenceDot(person);
        if (dot) role.append(dot);
        const seat = Number(person.seat);
        const seatText = person.role === 'worker' && Number.isInteger(seat) && seat > 0 ? ' · ' + seat + ' 号席' : '';
        const liveText = person.live === true ? '在席' : person.live === false ? '离席' : '';
        role.append(document.createTextNode((roles[person.role] || '参与者') + seatText + (liveText ? ' · ' + liveText : '')));
        copy.append(role);
        if (leading) copy.append(node('p', '现任领导者', 'leader-mark'));
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
    }
    function messageBody(value) {
      const raw = text(value);
      const lines = raw.split('\n');
      const code = raw.includes('```') || lines.some(line => line.startsWith('$ ') || line.startsWith('$'));
      const long = raw.length > 360 || lines.length > 8;
      if (!long && !code) return node('p', raw, 'bubble-text');
      const details = document.createElement('details');
      details.className = code ? 'fold code' : 'fold';
      const summary = document.createElement('summary');
      const preview = raw.replace(/\s+/g, ' ').trim().slice(0, 72);
      summary.textContent = (code ? '代码输出' : '长消息') + (preview ? ' · ' + preview : '');
      const body = document.createElement(code ? 'pre' : 'p');
      body.className = code ? 'bubble-code' : 'bubble-text';
      body.textContent = raw;
      details.append(summary, body);
      return details;
    }
    function taskLink(taskId) {
      const id = text(taskId);
      const anchor = taskAnchor(id);
      if (!anchor) return null;
      const link = node('a', titles.get(id) || '关联任务', 'task-link');
      link.href = '#' + anchor;
      return link;
    }
    function renderEvent(event) {
      const message = event.kind === 'message' ? event.message || null : null;
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
        const stamp = node('time', when(event.created_at));
        stamp.dateTime = text(event.created_at);
        line.append(stamp);
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
        const line = node('div', null, 'system-turn');
        line.append(node('span', label));
        const stamp = node('time', when(event.created_at));
        stamp.dateTime = text(event.created_at);
        line.append(stamp);
        feed.append(line);
        return;
      }
      const sender = event.sender || {};
      const kind = Object.prototype.hasOwnProperty.call(kinds, message.kind) ? message.kind : '';
      const role = sender.role || '';
      const side = role === 'worker' ? 'incoming' : 'outgoing';
      const leading = leader && sender.role === leader.role && Number(sender.seat) === Number(leader.seat);
      const article = node('article', null, 'turn ' + side + ' role-' + (roles[role] ? role : 'member')
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
      if (leading) head.append(node('span', '领导者', 'role-chip leader-chip'));
      if (kind) head.append(node('span', kinds[kind], 'kind-chip'));
      const stamp = node('time', when(event.created_at));
      stamp.dateTime = text(event.created_at);
      head.append(stamp);
      const bubble = node('div', null, 'bubble');
      const linked = taskLink(taskId);
      if (linked) bubble.append(linked);
      const notes = [];
      if (message.reply_to) notes.push('回复 #' + text(message.reply_to));
      const to = text(message.to);
      if (to === 'all') notes.push('发给全体');
      else if (to.startsWith('worker:')) notes.push('发给 ' + to.slice(7) + ' 号席');
      if (notes.length) bubble.append(node('p', notes.join(' · '), 'bubble-note'));
      bubble.append(messageBody(message.text));
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
          statusLine.textContent = '这场对话不在目录里。';
          return;
        }
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const data = await response.json();
        const incoming = Array.isArray(data.events) ? data.events : [];
        for (const event of incoming) {
          if (event.kind === 'message' && event.message) spoken.push(event);
        }
        paintConversation(data.conversation);
        for (const event of incoming) renderEvent(event);
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
