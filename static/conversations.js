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
  function personLabel(person) {
    const role = roles[person.role] || '参与者';
    const seat = Number(person.seat);
    const suffix = person.role === 'worker' && Number.isInteger(seat) && seat > 0 ? ' · ' + seat + ' 号席' : '';
    return role + ' ' + (text(person.name) || '未命名') + suffix;
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
      const link = node('a', null, 'conversation-card');
      link.href = './' + id + '/';
      const top = node('div', null, 'card-top');
      top.append(node('span', modes[item.mode] || '对话', 'badge mode'));
      top.append(node('span', states[item.status] || '状态待确认', 'badge ' + text(item.status)));
      top.append(node('span', when(item.updated_at), 'muted'));
      link.append(top, node('h2', text(item.title) || '未命名对话'), node('p', text(item.goal), 'muted'));
      const people = Array.isArray(item.participants) ? item.participants.map(personLabel).join(' · ') : '';
      if (people) link.append(node('p', people, 'muted'));
      const last = item.last_message;
      if (last && text(last.preview)) {
        link.append(node('p', (kinds[last.kind] || '消息') + ' · ' + text(last.preview), 'preview'));
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
    const back = node('p', null, 'page-kicker');
    const backLink = node('a', '返回目录');
    backLink.href = '../';
    back.append(backLink);
    const title = node('h1', '对话');
    const meta = node('div', null, 'thread-meta');
    const goal = node('p', '', 'goal');
    const people = node('p', '', 'people');
    const banner = node('div', null, 'banner');
    banner.append(node('p', null));
    banner.firstChild.append(node('strong', '只读。'));
    banner.firstChild.append(document.createTextNode(' 不能从这里加入或发送。加入仍需要配对码或邀请。定向消息会完整显示。'));
    const feed = node('div', null, 'feed');
    feed.setAttribute('aria-live', 'polite');
    const statusLine = node('p', '正在读取…', 'muted');
    statusLine.setAttribute('role', 'status');
    app.append(back, title, meta, goal, people, banner, feed, statusLine);
    let cursor = 0, stopped = false;

    function paintConversation(conversation) {
      if (!conversation) return;
      title.textContent = text(conversation.title) || '未命名对话';
      document.title = title.textContent + ' · AgentReins Hub';
      meta.replaceChildren(
        node('span', modes[conversation.mode] || '对话', 'badge mode'),
        node('span', states[conversation.status] || '状态待确认', 'badge ' + text(conversation.status))
      );
      goal.textContent = text(conversation.goal);
      const names = Array.isArray(conversation.participants) ? conversation.participants.map(personLabel) : [];
      people.textContent = names.join(' · ');
    }
    function renderEvent(event) {
      const article = node('article', null, 'event');
      const message = event.kind === 'message' ? event.message || null : null;
      const kind = message && Object.prototype.hasOwnProperty.call(kinds, message.kind) ? message.kind : '';
      if (kind) article.classList.add('kind-' + kind);
      const row = node('div', null, 'event-meta');
      const sender = event.sender || {};
      const seat = Number(sender.seat);
      const seatLabel = sender.role === 'worker' && Number.isInteger(seat) && seat > 0 ? ' · ' + seat + ' 号席' : '';
      row.append(node('strong', (text(sender.name) || roles[sender.role] || '参与者') + seatLabel));
      if (kind) row.append(node('span', kinds[kind], 'kind'));
      row.append(node('span', '#' + text(event.seq)), node('time', when(event.created_at)));
      article.append(row);
      if (message) {
        article.append(node('p', text(message.text), 'event-text'));
        const to = text(message.to);
        if (to === 'all') article.append(node('p', '定向：全体', 'event-note'));
        else if (to.startsWith('worker:')) article.append(node('p', '定向：' + to.slice(7) + ' 号席', 'event-note'));
        if (message.reply_to) article.append(node('p', '回复 #' + text(message.reply_to), 'event-note'));
        const attachments = Array.isArray(message.attachments) ? message.attachments : [];
        for (const attachment of attachments) {
          const line = node('div', null, 'attachment');
          line.append(node('span', '附件：' + text(attachment.name) + ' · ' + sizes(attachment.size)));
          const href = safeFileUrl(attachment.url);
          if (href) {
            const link = node('a', '下载');
            link.href = href;
            link.rel = 'noopener';
            line.append(link);
          }
          article.append(line);
        }
      } else if (event.kind === 'joined') {
        const seat = Number(event.seat);
        article.append(node('p', (roles[event.role] || '成员') +
                            (Number.isInteger(seat) && seat > 0 ? ' ' + seat + ' 号席' : '') + ' 已加入', 'system'));
      } else if (event.kind === 'state') {
        article.append(node('p', '会话状态：' + (states[event.status] || text(event.status)), 'system'));
      } else if (event.kind === 'revoked') {
        article.append(node('p', '一个席位已被撤销', 'system'));
      } else {
        return;
      }
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
