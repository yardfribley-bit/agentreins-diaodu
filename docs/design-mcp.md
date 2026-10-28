# 设计 MCP

已能调用 Hub 任务 API 的智能体，可以用同一套 Hub 凭证访问设计 MCP，不必再配置设计服务自己的 Bearer。

## 地址

公开地址（Nginx 已有的 `/task/` 反代，无需新 location）：

`https://www.chuhaijian.com/task/v1/mcp/design/`

这是 MCP Streamable HTTP。`POST` 发送 JSON-RPC（`initialize`、`tools/list`、`tools/call`），`GET` 接收服务端事件流，`DELETE` 结束会话。带不带末尾斜杠都可以。

不带凭证的请求返回 401。凭证错误同样是 401。

## 认证

与其他 `/task/v1/` 写接口相同：

- 智能体：`Authorization: Bearer <Hub 实例凭证>`。凭证来自 `POST /task/v1/connect`，或已经在用的任务 API 令牌。
- 浏览器：现有 `diaodu_session` Cookie，写操作再加 `X-CSRF-Token`。

示例（只放 Hub 凭证，不放设计服务令牌）：

```json
{"mcpServers":{"design":{"type":"streamable-http","url":"https://www.chuhaijian.com/task/v1/mcp/design/","headers":{"Authorization":"Bearer <Hub 实例凭证>"}}}}
```

设计服务自己的 Bearer 只存在于服务器环境变量 `DESIGN_MCP_TOKEN`。diaodu 在转发到 `127.0.0.1:18790` 时附上它，响应里不会带回这个令牌，调用方也不需要知道它。

`GET /task/v1/mcp/design/health` 不需要登录，只返回代理是否已配置（`ok`、`service`、`configured`）。它不连接设计服务，也不包含令牌。

工具列表和调用语义以设计服务为准。Hub 只做认证和转发，不改 JSON-RPC 正文。Stitch 一类工具可能要几分钟；响应按到达顺序转发，不在 diaodu 里攒完整包。
