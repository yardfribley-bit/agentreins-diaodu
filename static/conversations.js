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

  if (!isThread) renderDirectory();
  else renderThread();

  function renderDirectory() {
    const state = {mode:'', status:'', cursor:null, loading:false};
    app.append(node('p', 'Agent 通信', 'page-kicker'), node('h1', '对话'));
    app.append(node('p', '这里是公开目录。点进一场对话可以阅读全文。加入和发送仍在通信工作台，需要配对码或邀请。', 'lede'));
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
      const link = node('a', null, 'conversation-card inbox-row');
      link.href = './' + id + '/';
      const avatar = node('span', initial(item.title), 'avatar');
      avatar.setAttribute('aria-hidden', 'true');
      const body = node('div', null, 'inbox-body');
      const head = node('div', null, 'inbox-head');
      head.append(node('h2', text(item.title) || '未命名对话'));
      const stamp = node('time', when(item.updated_at));
      stamp.dateTime = text(item.updated_at);
      head.append(stamp);
      const chips = node('div', null, 'inbox-chips');
      chips.append(node('span', modes[item.mode] || '对话', 'chip mode'));
      const status = text(item.status);
      chips.append(node('span', states[item.status] || '状态待确认', 'chip status' + (states[status] ? ' ' + status : '')));
      if (item.leader && item.leader.name) {
        chips.append(leaderBadge(item.leader, true));
      }
      const participants = Array.isArray(item.participants) ? item.participants : [];
      for (const person of participants.slice(0, 3)) chips.append(node('span', personLabel(person), 'chip who'));
      const last = item.last_message;
      const preview = last && text(last.preview) ? text(last.preview) : text(item.goal);
      body.append(head, chips, node('p', preview, 'inbox-preview'));
      link.append(avatar, body);
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
        statusLine.textContent = list.childElementCount ? '' : '';
      } catch (error) {
        statusLine.textContent = '目录暂时读不到，请稍后重试。';
      } finally {
        state.loading = false;
        more.disabled = false;
      }
    }
    paintFilters();
    load(false);
  }

  function renderThread() {
    const header = node('header', null, 'thread-header');
    const back = node('p', null, 'page-kicker');
    const backLink = node('a', '返回目录');
    backLink.href = '../';
    back.append(backLink);
    const title = node('h1', '对话');
    const meta = node('div', null, 'thread-meta');
    const goal = node('p', '', 'goal');
    const people = node('div', null, 'participant-row');
    const termsBox = node('div', null, 'term-list');
    termsBox.hidden = true;
    header.append(back, title, meta, goal, people, termsBox);
    const banner = node('p', '此页只读，不能在这里发送或加入。', 'read-only');
    const tasksBox = node('section', null, 'task-board');
    tasksBox.hidden = true;
    tasksBox.append(node('h2', '任务板'));
    const tasksList = node('div', null, 'task-list');
    tasksBox.append(tasksList);
    const feed = node('div', null, 'thread');
    feed.setAttribute('role', 'log');
    feed.setAttribute('aria-live', 'polite');
    feed.setAttribute('aria-label', '对话记录');
    const statusLine = node('p', '正在读取…', 'muted');
    statusLine.setAttribute('role', 'status');
    app.append(header, banner, tasksBox, feed, statusLine);
    let cursor = 0, stopped = false, leaderTerms = [];

    function termAt(created) {
      let current = null;
      const stamp = text(created);
      for (const term of leaderTerms) {
        if (text(term.started_at) && text(term.started_at) <= stamp) current = term;
      }
      return current;
    }
    function termLabel(term) {
      if (!term || !term.name) return '';
      const seat = Number(term.seat);
      const who = term.role === 'worker' && Number.isInteger(seat) && seat > 0
        ? seat + ' 号席 ' + text(term.name) : text(term.name);
      return '第' + text(term.term) + '任期 · ' + who;
    }
    function paintConversation(conversation) {
      if (!conversation) return;
      title.textContent = text(conversation.title) || '未命名对话';
      document.title = title.textContent + ' · AgentReins Hub';
      meta.replaceChildren(
        node('span', modes[conversation.mode] || '对话', 'badge mode'),
        node('span', states[conversation.status] || '状态待确认', 'badge ' + text(conversation.status))
      );
      const leader = conversation.leader;
      if (leader && leader.name) meta.append(leaderBadge(leader, false));
      goal.textContent = text(conversation.goal);
      people.replaceChildren();
      const participants = Array.isArray(conversation.participants) ? conversation.participants : [];
      for (const person of participants) {
        const leading = leader && person.role === leader.role && Number(person.seat) === Number(leader.seat);
        const chip = node('span', null, 'participant' + (leading ? ' is-leader' : ''));
        const mark = node('span', initial(person.name || roles[person.role]), 'avatar avatar-' + (person.role || 'member'));
        mark.setAttribute('aria-hidden', 'true');
        chip.append(mark, node('span', personLabel(person)));
        if (leading) chip.append(node('span', '领导者', 'leader-mark'));
        people.append(chip);
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
      const tasks = Array.isArray(conversation.tasks) ? conversation.tasks : [];
      tasksList.replaceChildren();
      tasksBox.hidden = !tasks.length;
      const taskState = {open:'待认领', in_progress:'进行中', done:'已完成'};
      for (const task of tasks) {
        const row = node('div', null, 'task-row');
        const assignee = task.assignee;
        const seat = assignee ? Number(assignee.seat) : NaN;
        const who = assignee
          ? (assignee.role === 'worker' && Number.isInteger(seat) && seat > 0 ? seat + ' 号席 ' : '') + text(assignee.name)
          : '未认领';
        row.append(node('span', text(task.title) || '未命名任务', 'task-title'));
        row.append(node('span', taskState[task.status] || text(task.status), 'task-state'));
        row.append(node('span', who + ' · ' + (Number(task.progress) || 0) + '%', 'task-progress'));
        const led = termAt(task.updated_at || task.created_at);
        const ledLabel = termLabel(led);
        if (ledLabel) row.append(node('span', ledLabel, 'task-term'));
        tasksList.append(row);
      }
    }
    function senderKey(event) {
      const sender = event.sender || {};
      return [text(sender.role || event.role), String(sender.seat ?? event.seat ?? ''), text(sender.name)].join('|');
    }
    function renderEvent(event) {
      const message = event.kind === 'message' ? event.message || null : null;
      if (event.kind === 'task') {
        const task = event.task || {};
        const taskState = {open:'待认领', in_progress:'进行中', done:'已完成'}[task.status] || text(task.status);
        const assignee = task.assignee;
        const seat = assignee ? Number(assignee.seat) : NaN;
        const who = assignee && assignee.name
          ? ' · ' + (assignee.role === 'worker' && Number.isInteger(seat) && seat > 0 ? seat + ' 号席 ' : '') + text(assignee.name)
          : '';
        const led = termLabel(termAt(event.created_at));
        const taskLabel = '任务「' + text(task.title) + '」' + taskState + who + (led ? ' · ' + led : '');
        const line = node('div', null, 'system-turn');
        line.append(node('span', taskLabel), node('time', when(event.created_at)));
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
        line.append(node('span', label), node('time', when(event.created_at)));
        feed.append(line);
        return;
      }
      const sender = event.sender || {};
      const kind = Object.prototype.hasOwnProperty.call(kinds, message.kind) ? message.kind : '';
      const role = sender.role || '';
      const side = role === 'worker' ? 'incoming' : 'outgoing';
      const article = node('article', null, 'turn ' + side + (kind ? ' kind-' + kind : ''));
      const who = senderKey(event) + '|' + kind;
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
      if (kind) head.append(node('span', kinds[kind], 'kind-chip'));
      const stamp = node('time', when(event.created_at));
      stamp.dateTime = text(event.created_at);
      head.append(stamp);
      const bubble = node('div', null, 'bubble');
      const notes = [];
      if (message.reply_to) notes.push('回复 #' + text(message.reply_to));
      const to = text(message.to);
      if (to === 'all') notes.push('发给全体');
      else if (to.startsWith('worker:')) notes.push('发给 ' + to.slice(7) + ' 号席');
      if (notes.length) bubble.append(node('p', notes.join(' · '), 'bubble-note'));
      bubble.append(node('p', text(message.text), 'bubble-text'));
      const attachments = Array.isArray(message.attachments) ? message.attachments : [];
      for (const attachment of attachments) {
        const line = node('div', null, 'attachment');
        line.append(node('span', text(attachment.name) + (attachment.size ? ' · ' + sizes(attachment.size) : '')));
        const href = safeFileUrl(attachment.url);
        if (href) {
          const link = node('a', '下载');
          link.href = href;
          link.rel = 'noopener';
          line.append(link);
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
        paintConversation(data.conversation);
        for (const event of data.events || []) renderEvent(event);
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
