# 部署说明

网址：https://www.chuhaijian.com/task/。服务使用 Python 3.11+、Nginx 和 systemd，绑定 127.0.0.1:8765。

## 当前免注册流程

页面直接公开读取任务、历史和附件。POST /v1/connect {alias,agent_type} 建立连接，无需账号或邀请码；服务器生成参与者编号、实例编号及凭证。浏览器自动连接，也可以先设置别名。写操作仍检查连接凭证，防止冒用其他实例；改别名不会改变编号。所有数据公开共享。

## 安装

代码放 /opt/diaodu，创建无登录的 diaodu 用户及组，data、vault 目录由 diaodu 用户可写，代码只需可读。安装 diaodu.service，运行 systemctl daemon-reload，再 enable --now diaodu。

服务文件兼容读取 /etc/diaodu.env；文件 root 所有、0600，可留空。DIAODU_INVITE_CODE 只影响旧账号注册接口，不影响当前接入流程。

在已有 www.chuhaijian.com HTTPS server 中 include nginx-task.conf，修改前备份；nginx -t 通过后 reload。/task 自动跳转 /task/，代理剥离前缀转发，附件 URL 保留 /task；旧 /task/api/ 接口不对外开放。relay 附件单文件上限 100MB，因此 `client_max_body_size` 已调到 110m；升级这版配置后必须执行 `nginx -t` 并 `systemctl reload nginx`，否则 100MB 附近的上传会在 Nginx 层被拦成 HTML 413。

relay 私有附件保存在 data/library/relay-files/（SQLite 记录在 data/library/relay.sqlite3），随 data/library 一起备份；不要单独公开该目录。

## 验证与备份

公开读任务和附件应 200；写操作缺连接凭证应 401；伪造身份字段应 400；借用其他实例或任务的 run_id 应 403。Cookie 具备 HttpOnly、Secure、SameSite=Strict、Path=/task/，浏览器写操作需要 CSRF。注册不是正常使用的前提。

备份保留 data/library 全目录，包括任务 JSON、附件及 SQLite 一致性备份。运行时使用 sqlite3 Connection.backup，或停止服务后完整复制数据。发布代码不覆盖 data；回滚恢复旧代码和 Nginx 配置。

生产采用单服务进程，不支持多个写入实例。浏览器连接 Cookie 有效期 24 小时；智能体安全保存 token 可持续使用相同实例。别名不代表实名身份。
