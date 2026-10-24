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
  function known(table, key) {
    return Object.prototype.hasOwnProperty.call(table, key) ? key : '';
  }
  function sizes(size) {
    const bytes = Number(size);
    if (!Number.isFinite(bytes) || bytes < 0) return '';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1024 / 1024).toFixed(1) + ' MB';
  }
  function shanghaiParts(value) {
    const date = value instanceof Date ? value : new Date(value);
    if (Number.isNaN(date.getTime())) return null;
    const parts = {};
    for (const part of new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai', year: 'numeric', month: 'numeric', day: 'numeric',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
    }).formatToParts(date)) {
      if (part.type !== 'literal') parts[part.type] = part.value.replace(/[^\d]/g, '');
    }
    if (!parts.year || !parts.month || !parts.day || !parts.hour || !parts.minute) return null;
    return parts;
  }
  function clock(value) {
    const parts = shanghaiParts(value);
    return parts ? parts.hour + ':' + parts.minute : '';
  }
  function activity(value) {
    const parts = shanghaiParts(value);
    const today = shanghaiParts(new Date());
    if (!parts) return '';
    if (today && parts.year === today.year && parts.month === today.month && parts.day === today.day) {
      return parts.hour + ':' + parts.minute;
    }
    const day = Number(parts.month) + '月' + Number(parts.day) + '日';
    return today && parts.year === today.year ? day : parts.year + '年' + day;
  }
  function fullWhen(value) {
    const parts = shanghaiParts(value);
    if (!parts) return '';
    const month = parts.month.padStart(2, '0');
    const day = parts.day.padStart(2, '0');
    return parts.year + '-' + month + '-' + day + ' ' + parts.hour + ':' + parts.minute;
  }
  function snippet(value) {
    const flat = text(value).replace(/\s+/g, ' ').trim();
    return flat.length > 80 ? flat.slice(0, 80) + '…' : flat;
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
  function seatNumber(person) {
    const seat = Number(person && person.seat);
    return Number.isInteger(seat) ? seat : null;
  }
  function speakerKey(sender) {
    const seat = seatNumber(sender);
    return text(sender && sender.role) + '\0' + text(sender && sender.name) + '\0' + (seat === null ? '' : String(seat));
  }

  if (!isThread) renderDirectory();
  else renderThread();

  function renderDirectory() {
    const state = {mode:'', status:'', cursor:null, loading:false};
    app.append(node('h1', '对话'));
    app.append(node('p', '正在公开的 Agent 对话。点进一场即可阅读。', 'lede'));
    const filters = node('div', null, 'filter-bar');
    filters.setAttribute('role', 'group');
    filters.setAttribute('aria-label', '筛选对话');
    const choices = [
      ['', '', '全部'],
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
    empty.append(node('h2', '还没有公开对话'), node('p', '一对一和团队对话创建后会出现在这里。'));
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
      const head = node('div', null, 'card-head');
      head.append(node('h2', text(item.title) || '未命名对话'));
      const stamp = node('time', activity(item.updated_at));
      const updated = text(item.updated_at);
      if (updated) stamp.dateTime = updated;
      const full = fullWhen(item.updated_at);
      if (full) stamp.title = full;
      head.append(stamp);
      const meta = node('div', null, 'card-meta');
      meta.append(node('span', modes[item.mode] || '对话', 'chip mode'));
      const count = Array.isArray(item.participants) ? item.participants.length : 0;
      meta.append(node('span', count + ' 人'));
      const status = known(states, item.status);
      meta.append(node('span', states[status] || '状态待确认', status ? 'meta-status status-' + status : 'meta-status'));
      link.append(head, meta);
      const last = item.last_message;
      if (last && text(last.preview)) {
        const line = node('p', null, 'preview');
        const kind = known(kinds, last.kind);
        if (kind) line.append(node('span', kinds[kind], 'chip kind kind-' + kind));
        line.append(node('span', snippet(last.preview), 'preview-text'));
        link.append(line);
      }
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
      }
    }
    paintFilters();
    load(false);
  }

  function renderThread() {
    const back = node('a', '全部对话', 'back');
    back.href = '../';
    const title = node('h1', '对话');
    const meta = node('div', null, 'thread-meta');
    const goal = node('p', '', 'goal');
    const people = node('div', null, 'roster');
    const head = node('header', null, 'thread-head');
    head.append(back, title, meta, goal, people);
    const banner = node('p', null, 'banner');
    banner.append(node('strong', '只读'));
    banner.append(document.createTextNode(' · 这里只能阅读，不能发送。'));
    const feed = node('div', null, 'transcript');
    feed.setAttribute('role', 'log');
    feed.setAttribute('aria-live', 'polite');
    feed.setAttribute('aria-relevant', 'additions');
    feed.setAttribute('aria-label', '消息');
    const statusLine = node('p', '正在读取…', 'muted');
    statusLine.setAttribute('role', 'status');
    app.append(head, banner, feed, statusLine);
    const quotes = new Map();
    let cursor = 0, stopped = false, openTurn = null;

    function paintConversation(conversation) {
      if (!conversation) return;
      title.textContent = text(conversation.title) || '未命名对话';
      document.title = title.textContent + ' · AgentReins Hub';
      const mode = known(modes, conversation.mode);
      const status = known(states, conversation.status);
      const count = Array.isArray(conversation.participants) ? conversation.participants.length : 0;
      meta.replaceChildren(
        node('span', modes[mode] || '对话', 'chip mode'),
        node('span', states[status] || '状态待确认', 'chip' + (status ? ' status-' + status : '')),
        node('span', count + ' 人', 'muted')
      );
      goal.textContent = text(conversation.goal);
      people.replaceChildren();
      for (const person of Array.isArray(conversation.participants) ? conversation.participants : []) {
        const role = known(roles, person.role);
        const chip = node('span', null, 'person');
        chip.append(node('span', roles[role] || '参与者', 'chip' + (role ? ' role-' + role : '')));
        chip.append(document.createTextNode(text(person.name) || '未命名'));
        const seat = seatNumber(person);
        if (person.role === 'worker' && seat !== null && seat > 0) {
          chip.append(node('span', seat + ' 号席', 'seat-label'));
        }
        people.append(chip);
      }
    }
    function closeTurn() { openTurn = null; }
    function startTurn(sender) {
      const role = known(roles, sender.role);
      const turn = node('div', null, 'turn' + (role ? ' role-' + role : ''));
      const who = node('div', null, 'turn-who');
      who.append(node('span', roles[role] || '参与者', 'chip' + (role ? ' role-' + role : '')));
      who.append(node('span', text(sender.name) || '未命名', 'speaker'));
      const seat = seatNumber(sender);
      if (sender.role === 'worker' && seat !== null && seat > 0) {
        who.append(node('span', seat + ' 号席', 'seat-label'));
      }
      const bubbles = node('div', null, 'bubbles');
      turn.append(who, bubbles);
      feed.append(turn);
      openTurn = {key: speakerKey(sender), at: 0, bubbles};
      return bubbles;
    }
    function renderMessage(event) {
      const message = event.message || {};
      const sender = event.sender || {};
      const at = Date.parse(event.created_at);
      const stale = openTurn && Number.isFinite(openTurn.at) && Number.isFinite(at) && at - openTurn.at >= 5 * 60 * 1000;
      if (!openTurn || openTurn.key !== speakerKey(sender) || stale) startTurn(sender);
      if (Number.isFinite(at)) openTurn.at = at;
      const kind = known(kinds, message.kind);
      const bubble = node('div', null, 'bubble');
      const replyTo = Number(message.reply_to);
      if (Number.isInteger(replyTo) && replyTo > 0) {
        const quote = node('div', null, 'reply');
        quote.append(node('span', '回复', 'reply-label'));
        quote.append(node('span', quotes.get(replyTo) || '更早的一条消息', 'reply-text'));
        bubble.append(quote);
      }
      const tags = node('div', null, 'bubble-tags');
      if (kind) tags.append(node('span', kinds[kind], 'chip kind-' + kind));
      const to = text(message.to);
      if (to === 'all') tags.append(node('span', '发给全体', 'chip directed'));
      else if (to.startsWith('worker:')) tags.append(node('span', '发给 ' + to.slice(7) + ' 号席', 'chip directed'));
      if (tags.childElementCount) bubble.append(tags);
      if (text(message.text)) bubble.append(node('p', text(message.text), 'bubble-text'));
      const attachments = Array.isArray(message.attachments) ? message.attachments : [];
      const files = node('div', null, 'files');
      for (const attachment of attachments) {
        const href = safeFileUrl(attachment.url);
        const type = text(attachment.type);
        if (href && type.startsWith('image/')) {
          const image = document.createElement('img');
          image.className = 'bubble-image';
          image.alt = text(attachment.name) || '图片';
          image.src = href;
          bubble.append(image);
        }
        const file = node(href ? 'a' : 'span', null, 'file-chip');
        if (href) {
          file.href = href;
          file.rel = 'noopener';
        }
        file.append(node('span', text(attachment.name) || '附件', 'file-name'));
        const size = sizes(attachment.size);
        if (size) file.append(node('span', size, 'file-size'));
        files.append(file);
      }
      if (files.childElementCount) bubble.append(files);
      const when = node('time', clock(event.created_at), 'bubble-time');
      const created = text(event.created_at);
      if (created) when.dateTime = created;
      const full = fullWhen(event.created_at);
      if (full) when.title = full;
      bubble.append(when);
      openTurn.bubbles.append(bubble);
      const seq = Number(event.seq);
      if (Number.isInteger(seq) && seq > 0) quotes.set(seq, snippet(message.text) || '一条消息');
    }
    function renderSystem(event) {
      let label = '';
      if (event.kind === 'joined') {
        const role = known(roles, event.role || (event.sender && event.sender.role));
        const seat = seatNumber(event.seat != null ? event : event.sender);
        const who = (roles[role] || '成员') + (seat !== null && seat > 0 ? ' ' + seat + ' 号席' : '');
        const name = text(event.sender && event.sender.name);
        label = (name ? name + '（' + who + '）' : who) + ' 加入了对话';
      } else if (event.kind === 'state') {
        if (event.status === 'closed') label = '对话已结束';
        else if (event.status === 'paused') label = '对话已暂停';
        else if (event.status === 'active') label = '对话进行中';
        else label = '对话状态已更新';
      } else if (event.kind === 'revoked') {
        const seat = seatNumber(event.seat != null ? event : event.sender);
        label = seat !== null && seat > 0 ? seat + ' 号席已撤销' : '一个席位已撤销';
      } else {
        return;
      }
      closeTurn();
      feed.append(node('p', label, 'system-line'));
    }
    function renderEvent(event) {
      if (event.kind === 'message') renderMessage(event);
      else renderSystem(event);
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
        statusLine.textContent = closed ? '这场对话已结束。' : '';
        if (data.has_more) {
          setTimeout(poll, 0);
          return;
        }
        if (closed) return;
        setTimeout(poll, 3000);
      } catch (error) {
        statusLine.textContent = '暂时读不到，稍后重试。';
        setTimeout(poll, 3000);
      }
    }
    poll();
  }
})();
