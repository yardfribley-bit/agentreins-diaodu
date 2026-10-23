from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from diaodu.library import Library
from diaodu.identity import Identity
from diaodu.runners import LiveRunners
from diaodu.server import doctor, serve
from diaodu.service import Service, load_config
from diaodu.store import Store
from diaodu.vault import Vault

ROOT = Path(__file__).resolve().parent.parent


def make_library(config: dict) -> Library:
    return Library(ROOT / "data" / "library", config["server"].get("public_base_path", ""))


def make_service(config: dict) -> Service:
    vault_path = config.setdefault("vault", {}).get("path") or str(ROOT / "vault")
    config["vault"]["path"] = vault_path
    return Service(Store(ROOT / "data" / "jobs"), LiveRunners(config), config, Vault(Path(vault_path)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="diaodu", description="任务")
    parser.add_argument("command", nargs="?", default="serve", choices=("serve", "doctor"))
    parser.add_argument("--config", type=Path, default=ROOT / "config.toml")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.command == "doctor":
        for item in doctor(config):
            mark = "可调用" if item["ready"] else "未接通"
            print(f"{item['label']}\t{mark}\t{item['detail']}")
        return 0
    service = None  # Shared platform never starts local agent runners.
    library = make_library(config)
    host = config["server"]["host"]
    port = int(config["server"]["port"])
    identity = Identity(library.root / "identity.sqlite3", os.environ.get("DIAODU_INVITE_CODE", ""))
    serve(service, host, port, library, identity, config["server"].get("secure_cookies",False),config["server"].get("public_origin",""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
