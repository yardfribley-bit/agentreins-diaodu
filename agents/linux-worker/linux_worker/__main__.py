"""python -m linux_worker"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from linux_worker.config import ConfigError, load_config
from linux_worker.hub import CredentialsError
from linux_worker.redact import RedactingFilter
from linux_worker.state import StateError
from linux_worker.worker import Worker


def configure_logging(secrets: list[str]) -> None:
    logger = logging.getLogger("linux_worker")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter(secrets))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AgentReins Linux 执行端（团队席位或一对一 Topic）")
    parser.add_argument("--config", type=Path, help="KEY=VALUE 配置文件，权限须为 0600")
    parser.add_argument("--once", action="store_true", help="处理当前已到达的事件后退出")
    parser.add_argument("--invite", help="团队邀请码，只在本次进程使用，不写入日志")
    parser.add_argument("--topic-invite", help="一对一 Topic 码，只在本次进程使用，不写入日志")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.invite:
            config.invite = args.invite.strip()
        if args.topic_invite:
            config.topic_invite = args.topic_invite.strip()
        configure_logging(config.secret_values())
        worker = Worker(config)
        if args.once:
            worker.run_once()
        else:
            worker.run_forever()
    except (ConfigError, StateError, CredentialsError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
