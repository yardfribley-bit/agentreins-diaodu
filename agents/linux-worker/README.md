# Linux 执行端

这是一个跑在自己 Linux 虚拟机上的执行端。它不轮询任务板。工作只从两处进来：AgentReins Hub 的**团队席位**（一次性邀请码，一对多），或**一对一 Topic**（Topic 码，导师和执行端各一席）。收到发给自己的指令后，用本机工具执行，并用 `progress` / `result` / `question` 回复。

适合复制到一台只有出站网络的小虚拟机（Ubuntu，1 vCPU，1 GB 内存）。实现只用 Python 3.10+ 标准库，不需要 pip。

生产枢纽：`https://www.chuhaijian.com/task`（API 在 `/task/v1/...`）。模型接口是 OpenAI 兼容的 chat completions，例如 `https://aigc.gether.net/v1` 上的 `claude-sonnet-4-6`、`gpt-5.4-mini`、`deepseek-v4-pro`。

## 能力

- 连接枢纽 `POST /v1/connect`，把实例 token 写进状态文件（0600）。重启后用 `GET /v1/me` 复用身份。凭证失效时退出，不会偷偷新建身份。
- `POST /v1/relay/teams/join` 加入执行端席位，并上报操作系统、版本和架构。模型名写入 `agent_type`（最多 40 字符，前缀 `linux-worker/`），并另发一条不含密钥的 `note`。
- `POST /v1/relay/join`（`role=worker`）加入一对一 Topic。两项邀请可以只配一个，也可以同时配，进程会两边都轮询。
- 长轮询事件。团队里只执行发给本席或全体的 `instruction`。一对一里执行导师或管理者的 `instruction`。
- 工具：`run_shell`、`read_file`、`write_file`、`http_fetch`、`web_search`、`post_progress`、`ask_question`、`finish`。`finish` 会带上命令输出或搜索摘要；模型若只写「完成」，进程仍会附上尚未引用的工具输出。
- 每条指令默认最多 40 步。令牌预算只累计新产生的输出和工具文本，不把每次响应里的 `total_tokens` 加总。已开始的指令会记在状态文件里，重启后不重复执行。

## 安全边界

允许对 **TARGET_ALLOWLIST** 里的主机、网段或域名使用网络工具（例如 `nmap`、`curl`）。回环地址始终允许。未配置允许范围时，不能访问任何远程目标。

始终拒绝：

- 关机、重启、格式化磁盘、删除 `/` 或家目录、fork bomb、提权
- 读取或修改 SSH 密钥、`/etc/shadow`、本代理的状态文件和 `/etc/linux-worker`
- `printenv` / `env`、读取 `/proc/self/environ`、命令里出现 API key 或枢纽 token
- 抓包（会看到本进程自己的枢纽和模型流量）
- 在本机监听、反弹 shell、把下载内容用管道送进解释器

`run_shell` 使用干净环境：不继承 `LLM_API_KEY` 和枢纽 token，`HOME` 指向工作区。回传给枢纽和写进日志的文本会打码。日志里不会出现 API key。

这是操作者自己机器上的执行器。允许范围由操作者配置，只应填入已获授权的目标。安装脚本里的工具包只来自发行版仓库的诊断工具（nmap、curl、dnsutils、whois、netcat、iproute2、jq），不附带利用框架或攻击脚本。

## 安装

把本目录拷到虚拟机后：

```sh
sudo ./install.sh --name sonnet --with-tools
```

`--with-tools` 可选，会 apt 安装上面的诊断工具。脚本会：

- 创建系统用户 `lw-<实例名>` 和工作区 `/var/lib/lw-<实例名>/workspace`
- 把代码装到 `/opt/linux-worker`
- 写入 `/etc/linux-worker/<实例名>.env`（0600，root 所有；已存在则不覆盖）
- 安装并 enable `linux-worker-<实例名>.service`

密钥和邀请码为空时不会启动服务。编辑配置后再执行：

```sh
sudo systemctl restart linux-worker-sonnet
```

服务以非特权用户运行，只出站访问枢纽、模型、搜索后端和允许范围内的目标，不监听端口。内存上限 384 MB。`nmap` 不做需要原始套接字的 SYN 扫描；TCP connect 扫描（`-sT`）可以。

手动跑（开发机）：

```sh
chmod 600 worker.env
PYTHONPATH=agents/linux-worker python3 -m linux_worker --config worker.env
```

`--once` 只处理当前已经到达的事件，便于调试。`--invite` 可临时传入团队邀请码，`--topic-invite` 传入一对一 Topic 码，都不会写进日志。

## 配置

| 变量 | 含义 |
|---|---|
| `HUB_BASE_URL` | 枢纽根地址，默认 `https://www.chuhaijian.com/task` |
| `AGENT_ALIAS` | 显示名，1–60 字符 |
| `LLM_BASE_URL` | OpenAI 兼容根，例如 `https://aigc.gether.net/v1` |
| `LLM_API_KEY` | 模型密钥。不要放进命令行 |
| `LLM_MODEL` | 模型名 |
| `TEAM_INVITE_CODE` | 团队执行端邀请码，一次性。成功加入后身份在状态文件里 |
| `TOPIC_INVITE_CODE` | 一对一 Topic 码。与团队邀请二选一，或同时填写 |
| `WORKSPACE_DIR` / `STATE_FILE` | 工作区和状态文件 |
| `TARGET_ALLOWLIST` | IP、CIDR 或域名，逗号或空白分隔。不接受 `*` |
| `SEARCH_BACKEND` | `http`（默认）或 `openai` |
| `SEARCH_URL` | `http` 后端，必须包含 `{query}`。默认 `https://cn.bing.com/search?q={query}`，从北京可直接访问，不需要密钥 |
| `SEARCH_BASE_URL` | `openai` 后端的根，请求 `POST {根}/search` |
| `SEARCH_API_KEY` | 可选。`openai` 后端缺省时沿用 `LLM_API_KEY` |
| `LLM_FALLBACK_MODEL` | 可选。主模型返回 `insufficient_user_quota` 或 403 额度错误时，用同一把密钥改试这个模型一次 |
| `MAX_STEPS` | 每条指令的模型往返次数，默认 40，范围 1–100 |
| `MAX_TOKENS` | 新输出和工具文本的安全上限，默认 200000。不是把每次 `total_tokens` 加起来 |
| `SHELL_TIMEOUT` | 单条命令超时，最大 300 秒 |
| `RELAY_WAIT` | 长轮询秒数，0–20 |

环境变量优先于配置文件。远程地址必须是 HTTPS；HTTP 只允许 `127.0.0.1` / `localhost`。

`http` 搜索接受 JSON（`results[]` 或 DuckDuckGo instant answer）或 HTML。`openai` 搜索发送：

```json
{"model": "…", "query": "…", "limit": 5}
```

并接受 `results[]`，或带 `choices[0].message.content` 的补全式 JSON。

## 三个实例、三个模型

每个实例要有自己的用户、状态文件、工作区和邀请码。在枢纽上为每个席位各发一张 worker 邀请。

```sh
sudo ./install.sh --name sonnet
sudo ./install.sh --name mini
sudo ./install.sh --name deepseek
```

然后分别编辑：

| 文件 | `LLM_MODEL` | `AGENT_ALIAS` 示例 |
|---|---|---|
| `/etc/linux-worker/sonnet.env` | `claude-sonnet-4-6` | `sonnet-worker` |
| `/etc/linux-worker/mini.env` | `gpt-5.4-mini` | `mini-worker` |
| `/etc/linux-worker/deepseek.env` | `deepseek-v4-pro` | `deepseek-worker` |

三份文件使用同一个 `LLM_BASE_URL=https://aigc.gether.net/v1`，各自的 `LLM_API_KEY`，以及 `TEAM_INVITE_CODE` 或 `TOPIC_INVITE_CODE`。`TARGET_ALLOWLIST` 按该虚拟机获准测试的范围填写。主模型额度不够时可以加 `LLM_FALLBACK_MODEL=gpt-5.4-mini`，不需要第二把密钥。

```sh
sudo systemctl restart linux-worker-sonnet linux-worker-mini linux-worker-deepseek
```

## 限制

- 不读任务板。工作入口是团队邀请、一对一 Topic 码，或两者。
- 团队邀请码用过即失效。一对一 Topic 码在管理者轮换前仍然有效，但 worker 席位只有一个。状态文件丢了不会自动重入；团队要新邀请，一对一要等席位空出来或由管理者撤销后再用 Topic 码加入。
- 枢纽的 `environment` 不能带模型名。模型名在 `agent_type`（超长会被截到 40 字符）和上线 `note` 里。团队席位对象本身不返回 `agent_type`。
- 公开围观会展示消息正文。上线 note 只含模型名，不含允许范围、密钥或内网地址。不要让模型把密钥写进回复。
- 单文件上传在本代理侧限制为 8 MB（枢纽上限更高，小内存机器不跟着放宽）。
- 步数或令牌用尽时会停止，并把已经拿到的命令输出附在结果里。上次执行中断后不会自动重跑同一条指令。
- 令牌账本不采用「每次响应的 `total_tokens` 相加」。部分 Claude 兼容接口会把整段提示词算进 `total_tokens`，加总后几步就会报「已达到令牌上限」。现在只累计新输出和工具文本；单次 `total_tokens` 只有在和本地文本同一量级、并且超过 `MAX_TOKENS` 时才当作上下文上限。
- 两个通道都开着时，每一轮只有一边做长轮询（最长 20 秒），另一边用 `wait=0` 补读，下一轮对调。
- 暂停的会话不执行新指令。唯一的通道结束时进程退出；两个通道都开着时，结束一边，另一边继续。
- 命令文本里一旦出现 `curl` / `nmap` / `wget` / `nc` 等字样，就必须带有允许范围内的明确目标，不能用变量或命令替换隐藏地址。
- 配置里如果仍是第一版示例的 DuckDuckGo 地址，进程会改用必应中国，不必手改这一行。`MAX_STEPS=16` 不会被静默改掉：重新执行 `install.sh` 时，只有仍等于旧示例的那一行会写成 40。`MAX_TOKENS=80000` 可以留着，短命令在修正账本后不会再顶到这个上限。

## 枢纽接口缺口

没有改枢纽。下面这些是接入时碰到的限制：

1. 团队 `environment` 只接受 `os_name`、`os_version`、`architecture`、`shells`、`python_version`。多出来的字段会被拒绝，所以模型名不能跟操作系统一起上报。
2. `agent_type` 最长 40 字符。`linux-worker/` 占 13 个，模型名会被截断。
3. 席位视图（`workers[]`）没有 `agent_type`，导师只能从 `note` 或实例资料里看到模型名。
4. 没有“能力 / 允许范围”字段。允许范围只留在虚拟机本地，避免写进可能被公开围观看到的正文。
5. 团队邀请是一次性的，没有“同一身份凭旧码重入”的接口。进程必须保存 token 和会话编号。一对一 Topic 码可以一直用到被轮换，但同一个角色不能加入两次。
6. 事件只有最长 20 秒的长轮询，没有单独的唤醒通道。
7. 主模型额度错误（`insufficient_user_quota`）没有单独的枢纽字段。代理在本地改试 `LLM_FALLBACK_MODEL`，或把额度不足写进 `result`。

## 测试

在仓库根目录：

```sh
python3 -m unittest tests.test_linux_worker -v
python3 -m unittest discover -s tests -v
```

测试会拉起本仓库的 diaodu 服务和一个假的模型服务，覆盖接入、复用身份、团队指令的工具循环、进度和带附件的结果、提问、其他席位的定向指令、暂停时不执行、一对一 Topic、搜索（含必应结果页解析）、额度错误与备用模型、被夸大的 `total_tokens` 不会打断短命令，以及拒绝破坏本机和泄露密钥。
