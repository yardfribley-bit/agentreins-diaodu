"""Decide whether a shell command may run.

Destructive actions against this host, reads of this agent's own keys, and
export of its own secrets are refused. Network tools are allowed only when
every literal target is on the operator allowlist (loopback is always allowed).
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


_FILE_EXT = {
    "txt", "py", "md", "json", "log", "sh", "out", "cfg", "conf", "yml", "yaml",
    "xml", "html", "css", "js", "png", "jpg", "jpeg", "gif", "pdf", "zip", "tar",
    "gz", "csv", "tsv", "toml", "ini", "lock", "map", "service",
}
_NETWORK_TOOLS = {
    "nmap", "curl", "wget", "nc", "ncat", "netcat", "socat", "ssh", "scp", "sftp",
    "dig", "nslookup", "host", "whois", "ping", "traceroute", "tracepath",
    "masscan", "nikto", "sqlmap", "hydra", "medusa", "nuclei", "gobuster", "ffuf",
    "rustscan", "netdiscover", "arp-scan", "nping", "hping3", "openssl", "telnet",
}
_NETWORK_WORD = re.compile(
    r"\b(?:curl|wget|nmap|nc|ncat|netcat|socat|nikto|sqlmap|hydra|masscan|nuclei)\b",
    re.I,
)
_SECRET_NAMES = (
    "LLM_API_KEY",
    "SEARCH_API_KEY",
    "DIAODU_TOKEN",
    "DIAODU_AGENT_TOKEN",
    "DIAODU_PAIR_CODE",
    "TEAM_INVITE_CODE",
)


@dataclass
class Decision:
    allowed: bool
    reason: str = ""


def deny(reason: str) -> Decision:
    return Decision(False, reason)


class Policy:
    def __init__(self, allowlist: list[str], workspace: Path, secrets: list[str], forbidden_paths: list[str]):
        self.workspace = workspace
        # Keep the caller's list so a token learned after startup is covered too.
        self.secrets = secrets
        self.forbidden_paths = [item for item in forbidden_paths if item]
        self.networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        self.domains: list[str] = []
        for item in allowlist:
            self._add(item)

    def _add(self, item: str) -> None:
        if "/" in item and not item.startswith("*."):
            self.networks.append(ipaddress.ip_network(item, strict=False))
            return
        try:
            ip = ipaddress.ip_address(item)
        except ValueError:
            ip = None
        if ip is not None:
            suffix = "/128" if ip.version == 6 else "/32"
            self.networks.append(ipaddress.ip_network(str(ip) + suffix, strict=False))
            return
        self.domains.append(item.lower().lstrip("*").lstrip("."))

    def allows(self, host: str) -> bool:
        host = host.strip("[]").lower().rstrip(".")
        if not host:
            return False
        if host == "localhost" or host.endswith(".localhost"):
            return True
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None:
            if ip.is_loopback:
                return True
            return any(ip in network for network in self.networks)
        return any(host == domain or host.endswith("." + domain) for domain in self.domains)


def decide(command: str, policy: Policy) -> Decision:
    if not isinstance(command, str) or not command.strip():
        return deny("命令不能为空")
    if "\x00" in command or len(command) > 4000:
        return deny("命令过长或含有空字节")
    folded = command.replace("'", "").replace('"', "")
    for probe in (command, folded):
        found = _static_deny(probe, policy)
        if found is not None:
            return found
    return _network_scope(command, policy)


def _static_deny(command: str, policy: Policy) -> Decision | None:
    if re.search(
        r"(?:^|[;&|`(\n]|&&|\|\|)\s*(?:sudo\s+)?(?:/usr/sbin/|/sbin/|/usr/bin/|/bin/)?"
        r"(?:shutdown|reboot|halt|poweroff|telinit)\b",
        command,
        re.I,
    ):
        return deny("拒绝关机或重启本机")
    if re.search(r"\b(?:mkfs(?:\.[a-z0-9]+)?|wipefs|fdisk|parted|mkswap)\b", command, re.I):
        return deny("拒绝格式化或改写磁盘分区")
    if re.search(r"\bdd\b", command) and re.search(r"\bof=/dev/", command):
        return deny("拒绝格式化或改写磁盘分区")
    if re.search(r":\s*\(\s*\)\s*\{", command) or re.search(
        r"\b(\w+)\s*\(\s*\)\s*\{[^}]*\b\1\b[^}]*\|[^}]*\b\1\b", command
    ):
        return deny("拒绝 fork bomb")
    if re.search(r"(?:^|[;&|`(\n]|&&|\|\|)\s*(?:sudo|doas|pkexec)\b", command) or re.search(
        r"(?:^|[;&|`(\n]|&&|\|\|)\s*su\b", command
    ):
        return deny("拒绝提权")
    if re.search(r"\.ssh\b|authorized_keys|\bid_rsa\b|\bid_ed25519\b|\bid_ecdsa\b|\bid_dsa\b", command):
        return deny("拒绝读取或修改 SSH 密钥")
    if re.search(r"/etc/(?:shadow|gshadow|sudoers)|/etc/ssh/|/etc/linux-worker", command):
        return deny("拒绝读取系统凭据")
    for path in policy.forbidden_paths:
        if path and path in command:
            return deny("拒绝读取或修改本代理的凭据文件")
    if re.search(r"(?:^|[;&|`(\n]|&&|\|\|)\s*(?:env|printenv|export\s+-p|compgen\s+-e)\b", command):
        return deny("拒绝导出环境变量或本代理密钥")
    if re.search(r"(?:^|[;&|`(\n]|&&|\|\|)\s*set\s*(?:[;#]|$)", command):
        return deny("拒绝导出环境变量或本代理密钥")
    if re.search(r"os\.environ|/proc/(?:self|\d+)/environ", command):
        return deny("拒绝导出环境变量或本代理密钥")
    for name in _SECRET_NAMES:
        if re.search(r"\b" + name + r"\b", command):
            return deny("拒绝导出环境变量或本代理密钥")
    for secret in policy.secrets:
        if len(secret) >= 6 and secret in command:
            return deny("拒绝外传本代理密钥")
    if re.search(r"\b(?:tcpdump|tshark|dumpcap|ngrep|wireshark)\b", command, re.I):
        return deny("拒绝抓包，以免带上本代理自己的流量")
    if "/dev/tcp" in command or "/dev/udp" in command:
        return deny("拒绝在本机监听或反弹 shell")
    if re.search(r"\b(?:nc|ncat|netcat)\b", command, re.I) and re.search(
        r"(?:^|\s)--listen\b|(?:^|\s)-[A-Za-z]*l", command
    ):
        return deny("拒绝在本机监听或反弹 shell")
    if re.search(r"\bsocat\b", command, re.I) and re.search(r"LISTEN|EXEC:|SYSTEM:", command):
        return deny("拒绝在本机监听或反弹 shell")
    if re.search(
        r"\b(?:curl|wget|nc|ncat|socat)\b[\s\S]{0,500}\|\s*(?:sudo\s+)?(?:bash|sh|zsh|dash|python3?|perl|ruby|php)\b",
        command,
        re.I,
    ):
        return deny("拒绝把下载内容送进解释器")
    if re.search(r"\$\(|`", command) and _NETWORK_WORD.search(command):
        return deny("拒绝用命令替换隐藏网络目标")
    rm = _rm_decision(command, policy)
    if rm is not None:
        return rm
    redirect = _redirect_decision(command, policy)
    if redirect is not None:
        return redirect
    return None


def _segments(command: str) -> list[str]:
    return re.split(r"(?:&&|\|\||[;|\n])", command)


def _command_names(command: str) -> list[str]:
    names = []
    for segment in _segments(command):
        tokens = segment.strip().split()
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens.pop(0)
        if tokens:
            names.append(Path(tokens[0].strip("'\"")).name)
    return names


def _rm_decision(command: str, policy: Policy) -> Decision | None:
    for segment in _segments(command):
        tokens = segment.strip().split()
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens.pop(0)
        if not tokens or Path(tokens[0].strip("'\"")).name != "rm":
            continue
        targets = [token.strip("'\"") for token in tokens[1:] if token != "--" and not token.startswith("-")]
        if not targets:
            return deny("拒绝删除根目录或用户主目录")
        for target in targets:
            if target in {"/", "/*", "/**", "/.", "~", "~/", "$HOME", "${HOME}", "/root", "/root/"}:
                return deny("拒绝删除根目录或用户主目录")
            if ".ssh" in target:
                return deny("拒绝读取或修改 SSH 密钥")
            if target.startswith(("/", "~", "$", "..")) and not _inside_workspace(target, policy):
                return deny("拒绝删除工作区以外的路径")
    return None


def _redirect_decision(command: str, policy: Policy) -> Decision | None:
    for target in re.findall(r"(?:>>?|\btee\b(?:\s+-a)?)\s+([^\s|&;]+)", command):
        cleaned = target.strip("'\"")
        if cleaned in {"/dev/null", "/dev/stdout", "/dev/stderr"}:
            continue
        if ".ssh" in cleaned:
            return deny("拒绝读取或修改 SSH 密钥")
        if any(path and path in cleaned for path in policy.forbidden_paths):
            return deny("拒绝读取或修改本代理的凭据文件")
        if cleaned.startswith("/") or cleaned.startswith("~") or cleaned.startswith("$") or cleaned.startswith(".."):
            if not _inside_workspace(cleaned, policy):
                return deny("拒绝把输出写到工作区以外")
    return None


def _inside_workspace(raw: str, policy: Policy) -> bool:
    text = raw.strip("'\"")
    if text in {"~", "~/"} or text.startswith("~/") or text.startswith("$HOME/") or text.startswith("${HOME}/"):
        text = "workspace-relative"
        root = policy.workspace
        if raw.startswith("~/"):
            text = raw[2:]
        elif raw.startswith("$HOME/"):
            text = raw[len("$HOME/") :]
        elif raw.startswith("${HOME}/"):
            text = raw[len("${HOME}/") :]
        candidate = (root / text).resolve() if text != "workspace-relative" else root.resolve()
    else:
        path = Path(text)
        if not path.is_absolute():
            path = policy.workspace / path
        try:
            candidate = path.resolve()
        except OSError:
            return False
        root = policy.workspace.resolve()
    root = policy.workspace.resolve()
    return candidate == root or root in candidate.parents


def _network_scope(command: str, policy: Policy) -> Decision:
    names = _command_names(command)
    invoked = any(name in _NETWORK_TOOLS for name in names) or _NETWORK_WORD.search(command) is not None
    targets, problem = _targets(command, collect_hosts=invoked)
    if problem:
        return deny(problem)
    if invoked and not targets:
        return deny("网络命令必须写出明确目标")
    for host in targets:
        if not policy.allows(host):
            return deny(f"目标不在允许范围内：{host}")
    return Decision(True)


def _targets(command: str, collect_hosts: bool) -> tuple[list[str], str | None]:
    found: list[str] = []
    for url in re.findall(r"https?://[^\s'\"<>]+", command, re.I):
        parsed = urlsplit(url)
        if parsed.username or parsed.password:
            return [], "URL 不能携带凭据"
        if parsed.hostname:
            found.append(parsed.hostname)
    for ip in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", command):
        found.append(ip)
    if not collect_hosts:
        return found, None
    for token in re.split(r"[\s;|&<>]+", command):
        token = token.strip("'\"`,")
        if not token or token.startswith("-") or token.startswith("/") or token.startswith("."):
            continue
        if token.startswith("$"):
            return [], "拒绝用变量隐藏网络目标"
        if re.fullmatch(r"0x[0-9a-fA-F]+", token) or re.fullmatch(r"\d{8,}", token):
            return [], "拒绝混淆的地址写法"
        if ":" in token and not token.startswith("["):
            host_part, _, port = token.rpartition(":")
            if port.isdigit():
                token = host_part
        if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", token):
            continue
        if token.lower() in {"localhost", "localhost."}:
            found.append("localhost")
            continue
        if "." not in token:
            continue
        if "/" in token:
            continue
        last = token.rsplit(".", 1)[-1].lower()
        if last in _FILE_EXT or not re.fullmatch(r"[A-Za-z]{2,24}", last):
            continue
        if re.fullmatch(r"[A-Za-z0-9._-]+", token):
            found.append(token)
    return found, None
