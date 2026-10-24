"""Environment and env-file configuration. The API key is never part of repr()."""

from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(Exception):
    pass


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _remote_https(url: str, label: str) -> str:
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError(f"{label} 不能包含凭据、查询参数或片段")
    host = parsed.hostname or ""
    local = host in _LOCAL_HOSTS
    if parsed.scheme == "https" or (parsed.scheme == "http" and local):
        if not host:
            raise ConfigError(f"{label} 缺少主机")
        return url.rstrip("/")
    raise ConfigError(f"{label} 必须使用 HTTPS；HTTP 仅允许本机测试")


def api_root(hub_base: str) -> str:
    base = _remote_https(hub_base, "HUB_BASE_URL")
    if base.endswith("/v1"):
        return base
    return base + "/v1"


def agent_type_for(model: str) -> str:
    """Hub agent_type is limited to 40 characters. Model is the suffix."""
    prefix = "linux-worker/"
    cleaned = re.sub(r"\s+", "", model)[: 40 - len(prefix)]
    return prefix + cleaned if cleaned else "linux-worker"


def parse_allowlist(raw: str) -> list[str]:
    parts = re.split(r"[\s,]+", (raw or "").strip())
    items: list[str] = []
    for part in parts:
        if not part:
            continue
        if part == "*":
            raise ConfigError("TARGET_ALLOWLIST 不接受 *，请写明主机、域名或 CIDR")
        if "/" in part and not part.startswith("*."):
            try:
                ipaddress.ip_network(part, strict=False)
            except ValueError as exc:
                raise ConfigError(f"允许范围里的网段不合法：{part}") from exc
        elif re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", part) or (
            ":" in part and "/" not in part and not part.startswith("*.")
        ):
            try:
                ipaddress.ip_address(part)
            except ValueError as exc:
                raise ConfigError(f"允许范围里的地址不合法：{part}") from exc
        else:
            domain = part.lower().lstrip("*").lstrip(".")
            if not domain or ".." in domain or "/" in domain or any(c.isspace() for c in domain):
                raise ConfigError(f"允许范围里的域名不合法：{part}")
        items.append(part)
    return items


def _int(value: str, label: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} 必须是整数") from exc
    if number < low or number > high:
        raise ConfigError(f"{label} 必须在 {low} 到 {high} 之间")
    return number


def parse_env_file(path: Path) -> dict[str, str]:
    if path.is_symlink():
        raise ConfigError("配置文件不能是符号链接")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise ConfigError("配置文件权限过宽，请设置为 0600")
    values: dict[str, str] = {}
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise ConfigError(f"配置文件第 {line_no} 行不是 KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigError(f"配置文件第 {line_no} 行的键不合法")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


class Config:
    def __init__(self, values: dict[str, str], source: str = ""):
        self.source = source

        def pick(name: str, default: str | None = None) -> str:
            env = os.environ.get(name)
            if env:
                return env
            if name in values and values[name] != "":
                return values[name]
            if default is None:
                raise ConfigError(f"缺少 {name}")
            return default

        self.hub_base = _remote_https(pick("HUB_BASE_URL", "https://www.chuhaijian.com/task"), "HUB_BASE_URL")
        self.api_root = api_root(self.hub_base)
        self.alias = pick("AGENT_ALIAS").strip()
        if not self.alias or len(self.alias) > 60:
            raise ConfigError("AGENT_ALIAS 必须是 1 到 60 字符")
        self.llm_base = _remote_https(pick("LLM_BASE_URL"), "LLM_BASE_URL")
        self.llm_api_key = pick("LLM_API_KEY")
        self.model = pick("LLM_MODEL").strip()
        if not self.model or len(self.model) > 120:
            raise ConfigError("LLM_MODEL 必须是 1 到 120 字符")
        self.workspace = Path(pick("WORKSPACE_DIR", "workspace")).expanduser()
        if self.workspace.resolve() == Path("/"):
            raise ConfigError("工作区不能是 /")
        self.state_file = Path(pick("STATE_FILE", str(self.workspace.parent / "state.json"))).expanduser()
        self.invite = pick("TEAM_INVITE_CODE", "")
        self.allowlist = parse_allowlist(pick("TARGET_ALLOWLIST", ""))
        self.search_backend = pick("SEARCH_BACKEND", "http").strip().lower()
        if self.search_backend not in ("http", "openai"):
            raise ConfigError("SEARCH_BACKEND 只能是 http 或 openai")
        default_search = "https://api.duckduckgo.com/?q={query}&format=json&no_html=1&skip_disambig=1"
        self.search_url = pick("SEARCH_URL", default_search)
        if self.search_backend == "http" and "{query}" not in self.search_url:
            raise ConfigError("SEARCH_URL 必须包含 {query}")
        self.search_base = pick("SEARCH_BASE_URL", "")
        if self.search_backend == "openai":
            if not self.search_base:
                raise ConfigError("SEARCH_BACKEND=openai 时必须设置 SEARCH_BASE_URL")
            self.search_base = _remote_https(self.search_base, "SEARCH_BASE_URL")
        self.search_api_key = pick("SEARCH_API_KEY", "")
        self.search_model = pick("SEARCH_MODEL", self.model)
        self.max_steps = _int(pick("MAX_STEPS", "16"), "MAX_STEPS", 1, 100)
        self.max_tokens = _int(pick("MAX_TOKENS", "80000"), "MAX_TOKENS", 100, 2_000_000)
        self.shell_timeout = _int(pick("SHELL_TIMEOUT", "120"), "SHELL_TIMEOUT", 1, 300)
        self.shell_output_limit = _int(pick("SHELL_OUTPUT_LIMIT", "16000"), "SHELL_OUTPUT_LIMIT", 200, 100_000)
        self.relay_wait = _int(pick("RELAY_WAIT", "20"), "RELAY_WAIT", 0, 20)
        self.error_sleep = _int(pick("ERROR_SLEEP", "5"), "ERROR_SLEEP", 1, 120)
        self.llm_max_output = _int(pick("LLM_MAX_OUTPUT", "1024"), "LLM_MAX_OUTPUT", 64, 8192)
        self.shell_user = pick("SHELL_USER", "")
        self.config_path = Path(source) if source else None

    def secret_values(self) -> list[str]:
        values = [self.llm_api_key, self.search_api_key]
        return [item for item in values if item]

    def forbidden_paths(self) -> list[str]:
        paths = [str(self.state_file)]
        if self.config_path is not None:
            paths.append(str(self.config_path))
        return paths

    def __repr__(self) -> str:
        return (
            f"Config(alias={self.alias!r}, model={self.model!r}, "
            f"hub={self.hub_base!r}, llm={self.llm_base!r})"
        )


def load_config(path: Path | None = None) -> Config:
    chosen = path or (Path(os.environ["LINUX_WORKER_CONFIG"]) if os.environ.get("LINUX_WORKER_CONFIG") else None)
    values: dict[str, str] = {}
    source = ""
    if chosen is not None:
        source = str(chosen)
        values = parse_env_file(Path(chosen))
    return Config(values, source)
