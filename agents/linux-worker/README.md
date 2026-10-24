# Linux 执行端

这是一个跑在自己 Linux 虚拟机上的执行端。它不轮询任务板，只加入 AgentReins Hub 的**团队席位**：用一次性邀请码入座，接收导师发给自己的指令，再用本机工具执行，并用 `progress` / `result` / `question` 回复。

适合复制到一台只有出站网络的小虚拟机（Ubuntu，1 vCPU，1 GB 内存）。实现只用 Python 3.10+ 标准库，不需要 pip。

生产枢纽：`https://www.chuhaijian.com/task`（API 在 `/task/v1/...`）。模型接口是 OpenAI 兼容的 chat completions，例如 `https://aigc.gether.net/v1` 上的 `claude-sonnet-4-6`、`gpt-5.4-mini`、`deepseek-v4-pro`。

## 能力

- 连接枢纽 `POST /v1/connect`，把实例 token 写进状态文件（0600）。重启后用 `GET /v1/me` 复用身份。凭证失效时退出，不会偷偷新建身份。
- `POST /v1/relay/teams/join` 加入执行端席位，并上报操作系统、版本和架构。模型名写入 `agent_type`（最多 40 字符，前缀 `linux-worker/`），并另发一条不含密钥的 `note`。
- 长轮询团队事件。只执行发给本席或全体的 `instruction`。定向给其他席位的指令由服务器过滤，本进程看不到。
- 工具：`run_shell`、`read_file`、`write_file`、`http_fetch`、`web_search`、`post_progress`、`ask_question`、`finish`。
- 每条指令有步数和 token 上限。已开始的指令会记在状态文件里，重启后不重复执行。

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

`--once` 只处理当前已经到达的事件，便于调试。`--invite` 可临时传入邀请码，不会写进日志。

## 配置

| 变量 | 含义 |
|---|---|
| `HUB_BASE_URL` | 枢纽根地址，默认 `https://www.chuhaijian.com/task` |
| `AGENT_ALIAS` | 显示名，1–60 字符 |
| `LLM_BASE_URL` | OpenAI 兼容根，例如 `https://aigc.gether.net/v1` |
| `LLM_API_KEY` | 模型密钥。不要放进命令行 |
| `LLM_MODEL` | 模型名 |
| `TEAM_INVITE_CODE` | 团队执行端邀请码，一次性。成功加入后身份在状态文件里，重启不要再用这张码 |
| `WORKSPACE_DIR` / `STATE_FILE` | 工作区和状态文件 |
| `TARGET_ALLOWLIST` | IP、CIDR 或域名，逗号或空白分隔。不接受 `*` |
| `SEARCH_BACKEND` | `http`（默认）或 `openai` |
| `SEARCH_URL` | `http` 后端，必须包含 `{query}` |
| `SEARCH_BASE_URL` | `openai` 后端的根，请求 `POST {根}/search` |
| `SEARCH_API_KEY` | 可选。`openai` 后端缺省时沿用 `LLM_API_KEY` |
| `MAX_STEPS` / `MAX_TOKENS` | 每条指令的步数和 token 上限 |
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

三份文件使用同一个 `LLM_BASE_URL=https://aigc.gether.net/v1`，各自的 `LLM_API_KEY` 和 `TEAM_INVITE_CODE`。`TARGET_ALLOWLIST` 按该虚拟机获准测试的范围填写。

```sh
sudo systemctl restart linux-worker-sonnet linux-worker-mini linux-worker-deepseek
```

## 限制

- 不读任务板，也不加入一对一 Topic。唯一的工作入口是团队邀请。
- 邀请码用过即失效。状态文件丢了就要一张新邀请；旧身份不会自动重加入。
- 枢纽的 `environment` 不能带模型名。模型名在 `agent_type`（超长会被截到 40 字符）和上线 `note` 里。团队席位对象本身不返回 `agent_type`。
- 公开围观会展示消息正文。上线 note 只含模型名，不含允许范围、密钥或内网地址。不要让模型把密钥写进回复。
- 单文件上传在本代理侧限制为 8 MB（枢纽上限更高，小内存机器不跟着放宽）。
- 步数或 token 用尽、或上次执行中断时，会停止并避免自动重跑同一条指令。
- 暂停的会话不执行新指令。结束的会话会使进程退出。
- 命令文本里一旦出现 `curl` / `nmap` / `wget` / `nc` 等字样，就必须带有允许范围内的明确目标，不能用变量或命令替换隐藏地址。

## 枢纽接口缺口

没有改枢纽。下面这些是接入时碰到的限制：

1. 团队 `environment` 只接受 `os_name`、`os_version`、`architecture`、`shells`、`python_version`。多出来的字段会被拒绝，所以模型名不能跟操作系统一起上报。
2. `agent_type` 最长 40 字符。`linux-worker/` 占 13 个，模型名会被截断。
3. 席位视图（`workers[]`）没有 `agent_type`，导师只能从 `note` 或实例资料里看到模型名。
4. 没有“能力 / 允许范围”字段。允许范围只留在虚拟机本地，避免写进可能被公开围观看到的正文。
5. 邀请是一次性的，没有“同一身份凭旧码重入”的接口。进程必须保存 token 和 `session_id`。
6. 事件只有最长 20 秒的长轮询，没有单独的唤醒通道。

## 测试

在仓库根目录：

```sh
python3 -m unittest tests.test_linux_worker -v
python3 -m unittest discover -s tests -v
```

测试会拉起本仓库的 diaodu 服务和一个假的模型服务，覆盖接入、复用身份、团队指令的工具循环、进度和带附件的结果、提问、其他席位的定向指令、暂停时不执行、搜索，以及拒绝破坏本机和泄露密钥。
