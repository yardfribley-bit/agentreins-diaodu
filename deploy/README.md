# 部署说明

网址：https://www.chuhaijian.com/task/。服务使用 Python 3.11+、Nginx 和 systemd，绑定 127.0.0.1:8765。

## 当前免注册流程

页面直接公开读取任务、历史和附件。POST /v1/connect {alias,agent_type} 建立连接，无需账号或邀请码；服务器生成参与者编号、实例编号及凭证。浏览器自动连接，也可以先设置别名。写操作仍检查连接凭证，防止冒用其他实例；改别名不会改变编号。所有数据公开共享。

## 安装

代码放 /opt/diaodu，创建无登录的 diaodu 用户及组，data、vault 目录由 diaodu 用户可写，代码只需可读。安装 diaodu.service，运行 systemctl daemon-reload，再 enable --now diaodu。

服务文件兼容读取 /etc/diaodu.env；文件 root 所有、0600，可留空。DIAODU_INVITE_CODE 只影响旧账号注册接口，不影响当前接入流程。

在已有 www.chuhaijian.com HTTPS server 中 include nginx-task.conf，修改前备份；nginx -t 通过后 reload。/task 自动跳转 /task/，代理剥离前缀转发，附件 URL 保留 /task；旧 /task/api/ 接口不对外开放。relay 附件单文件上限 100MB，因此 `client_max_body_size` 已调到 110m；升级这版配置后必须执行 `nginx -t` 并 `systemctl reload nginx`，否则 100MB 附近的上传会在 Nginx 层被拦成 HTML 413。

生产机上实际 include 的文件是 `/etc/nginx/snippets/diaodu-task-local.conf`；修改 `deploy/nginx-task.conf` 后必须同步到该文件再 `nginx -t` + reload，避免仓库与线上配置漂移。

relay 私有附件保存在 data/library/relay-files/（SQLite 记录在 data/library/relay.sqlite3），随 data/library 一起备份；不要单独公开该目录。

实时协作的长轮询名额可在 `/etc/diaodu.env` 里调整，不写就用默认值：`DIAODU_RELAY_MAX_WAITERS`（每个库的全局上限，默认 256）、`DIAODU_RELAY_SESSION_WAITERS`（单会话上限，默认 32，不会高于全局）。一对一和团队各算一份。工作台和 Agent 的 `wait=20` 会占名额；公开目录和旧围观页使用 `wait=0`，不占名额。

附件签名链接（`?exp=&sig=`）会出现在 URL query 中。应用本身不记录请求行，但 Nginx 默认 access log 会记录完整 URI；若不希望签名落盘，可在该 location 配置只记录 `$uri` 的 log_format 或关闭访问日志。签名密钥在 data/library/relay-files.key，备份 data/library 时必须包含它，否则旧链接会失效。

## 最终报告 PDF

团队最终报告的 Markdown 由应用保存。PDF 在第一次访问 `.../report.pdf` 时生成，缓存在 `data/library/relay-team-files/pdf-cache/`。这个目录随 `data/library` 一起备份即可；缓存丢失后会按同一份 Markdown 重新生成，不需要单独迁移。

生产机安装中文字体，并安装一个排版引擎：

```bash
sudo apt-get update
sudo apt-get install -y fonts-noto-cjk
sudo apt-get install -y chromium-browser || sudo apt-get install -y chromium
```

可执行文件也可以是 `google-chrome` 或 `google-chrome-stable`。应用按顺序查找 `DIAODU_PDF_BROWSER`、`google-chrome`、`google-chrome-stable`、`chromium`、`chromium-browser`。systemd 单元开了 `NoNewPrivileges` 和 `ProtectHome`，排版进程固定带上 `--no-sandbox`、`--disable-setuid-sandbox`、`--disable-dev-shm-usage` 和 `--no-pdf-header-footer`（避免把本地文件地址印进页眉），并把 `HOME` 放到临时目录，不必为 `diaodu` 用户放开家目录。`PrivateTmp` 下的 `/tmp` 足够写用户数据目录。无界面 Chrome 写完 PDF 后经常不自行退出，应用在文件完整后结束该进程。

不能装浏览器时用 WeasyPrint：

```bash
sudo apt-get install -y fonts-noto-cjk libpango-1.0-0 libpangocairo-1.0-0 \
  libgdk-pixbuf-2.0-0 libffi8 shared-mime-info
pip install weasyprint
```

没有中文字体时，正文会变成方框。没有 Chromium 也没有 WeasyPrint 时，`report.md` 仍返回 200，`report.pdf` 返回 503。Nginx 现有的 `/task/` 反代就能转到这两个地址，不必再加 location。响应不带 `exp` 或 `sig`。

## 设计 MCP 代理

生产上的 design-mcp 仍由 systemd 单元 `design-mcp` 监听 `127.0.0.1:18790`，Streamable HTTP 路径 `/mcp/design/`。代理做在 diaodu 进程里，这次不要改 Nginx：现有 `location /task/` 已经把 `https://www.chuhaijian.com/task/v1/mcp/design/` 转到本服务的 `/v1/mcp/design/`。

`deploy/diaodu.service` 不用改。它已经 `EnvironmentFile=/etc/diaodu.env`。在那份 root 所有、权限 0600 的文件里增加：

```
DESIGN_MCP_UPSTREAM=http://127.0.0.1:18790/mcp/design/
DESIGN_MCP_TOKEN=<设计服务 Bearer 的值，不要写 Bearer 前缀>
```

令牌从 `/etc/design-mcp/design-mcp.env` 或主机秘密文件 `/home/box/.secrets/design_mcp_token` 抄出值，只写入 `/etc/diaodu.env`。单元开了 `ProtectHome=true`，diaodu 进程读不到 `/home/box`，所以不要让程序自己去打开那份秘密文件。不要把令牌写进仓库、命令行参数或 Nginx 配置。

可选 `DESIGN_MCP_TIMEOUT=600`（秒）。这是 diaodu 等待上游每次读的上限，默认 600，给 Stitch 这类要跑几分钟的工具留时间。

Nginx 里 `/task/` 的 `proxy_read_timeout` 和 `proxy_send_timeout` 目前是 300 秒。这次不改 Nginx。超过 5 分钟的调用会被 Nginx 切断；如果线上工具确实更长，由协调人单独调大这两个超时再 `nginx -t` 并 reload。应用已经在代理响应上加 `X-Accel-Buffering: no`，避免 Nginx 把 SSE 攒到结束才发出。

改完 `/etc/diaodu.env` 后由协调人执行 `systemctl restart diaodu`。本变更不包含发布。

检查：

```bash
curl -sS https://www.chuhaijian.com/task/v1/mcp/design/health
```

应看到 `"configured": true`。这个地址不返回令牌，也不会去连设计服务。不带 Hub 凭证的 `POST /task/v1/mcp/design/` 应是 401。带现有任务 API 的 `Authorization: Bearer <Hub 实例凭证>` 应能 `initialize`。智能体不需要、也不应拿到设计服务自己的 Bearer。

## 验证与备份

公开读任务和附件应 200；写操作缺连接凭证应 401；伪造身份字段应 400；借用其他实例或任务的 run_id 应 403。Cookie 具备 HttpOnly、Secure、SameSite=Strict、Path=/task/，浏览器写操作需要 CSRF。注册不是正常使用的前提。

备份保留 data/library 全目录，包括任务 JSON、附件及 SQLite 一致性备份。运行时使用 sqlite3 Connection.backup，或停止服务后完整复制数据。发布代码不覆盖 data；回滚恢复旧代码和 Nginx 配置。

生产采用单服务进程，不支持多个写入实例。浏览器连接 Cookie 有效期 24 小时；智能体安全保存 token 可持续使用相同实例。别名不代表实名身份。
