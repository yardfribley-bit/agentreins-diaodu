/* Public read-only relay view. Data is inserted with textContent only. */
(() => {
  'use strict';
  const parts = location.pathname.split('/').filter(Boolean);
  const token = parts.length ? decodeURIComponent(parts[parts.length - 1]) : '';
  const marker = location.pathname.indexOf('/relay/watch/');
  const base = marker >= 0 ? location.pathname.slice(0, marker) : '';
  const title = document.getElementById('watch-title');
  const goal = document.getElementById('watch-goal');
  const status = document.getElementById('watch-status');
  const feed = document.getElementById('watch-feed');
  let cursor = 0, stopped = false, failures = 0;

  const text = value => typeof value === 'string' ? value : typeof value === 'number' ? String(value) : '';
  const sizes = size => {
    const bytes = Number(size);
    if (!Number.isFinite(bytes) || bytes < 0) return '';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1024 / 1024).toFixed(1) + ' MB';
  };
  const when = value => {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
    }).format(date);
  };
  function node(tag, textValue, className) {
    const element = document.createElement(tag);
    if (textValue !== undefined && textValue !== null) element.textContent = text(textValue);
    if (className) element.className = className;
    return element;
  }
  function renderSession(session) {
    if (!session) return;
    title.textContent = '公开围观 · ' + (text(session.title) || '未命名 Topic');
    goal.textContent = text(session.goal);
  }
  function renderEvent(event) {
    const article = node('article', null, 'watch-event');
    const sender = event.sender || {};
    const meta = node('div', null, 'watch-meta');
    meta.append(node('strong', sender.name || '参与者'), node('span', '#' + event.seq, 'muted'),
                node('time', when(event.created_at)));
    article.append(meta);
    if (event.kind === 'message' && event.message) {
      const message = event.message;
      article.append(node('p', message.text, 'watch-text'));
      const attachments = Array.isArray(message.attachments) ? message.attachments : [];
      for (const attachment of attachments) {
        article.append(node('p', '附件：' + text(attachment.name) + ' · ' + sizes(attachment.size),
                            'watch-attachment'));
      }
      if (message.reply_to) article.append(node('p', '回复 #' + message.reply_to, 'watch-reply'));
    } else if (event.kind === 'joined') {
      article.append(node('p', (text(event.role) || '成员') + ' 已加入', 'watch-system'));
    } else if (event.kind === 'state') {
      article.append(node('p', '会话状态：' + text(event.status), 'watch-system'));
    } else if (event.kind === 'revoked') {
      article.append(node('p', '一个席位已被撤销', 'watch-system'));
    } else {
      return;
    }
    feed.append(article);
    feed.scrollTop = feed.scrollHeight;
  }
  async function poll() {
    if (stopped || !token) { status.textContent = '公开链接无效。'; return; }
    try {
      const response = await fetch(base + '/v1/relay/public/' + encodeURIComponent(token) +
                                   '/events?after=' + cursor + '&wait=20',
                                   {headers: {'Accept': 'application/json'}});
      if (response.status === 404) {
        stopped = true;
        feed.replaceChildren();
        status.textContent = '公开围观已关闭或链接已失效。';
        return;
      }
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const data = await response.json();
      renderSession(data.session);
      for (const event of data.events || []) renderEvent(event);
      cursor = Number(data.next_cursor) || cursor;
      failures = 0;
      const closed = data.session && data.session.status === 'closed';
      status.textContent = closed ? '会话已结束，这里只保留历史。' : '只读同步中 · 环境信息不展示';
      if (closed && !data.has_more) return;
      setTimeout(poll, data.has_more ? 0 : 300);
    } catch (error) {
      failures += 1;
      status.textContent = '连接中断，重试中…';
      setTimeout(poll, Math.min(10000, 1000 * 2 ** Math.min(failures, 3)));
    }
  }
  poll();
})();
