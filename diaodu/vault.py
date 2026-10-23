"""Obsidian 知识库：一个文件夹里的 Markdown。用 Obsidian 打开这个目录即可。"""

from __future__ import annotations

import re
from pathlib import Path


class Vault:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        welcome = self.root / "欢迎.md"
        if not welcome.exists():
            welcome.write_text(
                "# 欢迎\n\n"
                "这是一个知识库。每一篇笔记是一个 Markdown 文件。\n\n"
                "用 Obsidian 打开这个文件夹，或让代理用钥匙来读、来写。\n",
                encoding="utf-8",
            )

    def list_notes(self) -> list[str]:
        names = []
        for path in self.root.rglob("*.md"):
            if any(part.startswith(".") for part in path.relative_to(self.root).parts):
                continue
            names.append(path.relative_to(self.root).as_posix())
        names.sort()
        return names

    def read(self, relative: str) -> str:
        path = self._resolve(relative)
        if not path.is_file():
            raise FileNotFoundError(relative)
        return path.read_text(encoding="utf-8")

    def write(self, relative: str, text: str) -> str:
        if len(text) > 1_000_000:
            raise ValueError("笔记超过 1MB")
        path = self._note_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            raise ValueError("路径不合法")
        body = text if text.endswith("\n") else text + "\n"
        path.write_text(body, encoding="utf-8")
        return path.relative_to(self.root.resolve()).as_posix()

    def search(self, query: str) -> list[dict]:
        needle = query.strip().casefold()
        if not needle:
            return []
        hits = []
        for name in self.list_notes():
            if needle in name.casefold():
                hits.append({"path": name, "line": 1, "text": name})
            for number, line in enumerate(self.read(name).splitlines(), 1):
                if needle in line.casefold():
                    hits.append({"path": name, "line": number, "text": line[:240]})
                    if len(hits) >= 50:
                        return hits
        return hits

    def add(self, title: str, body: str) -> str:
        stem = re.sub(r"[\\/:\n\r\t]+", " ", title).strip() or "笔记"
        stem = stem[:40]
        path = self.root / f"{stem}.md"
        number = 2
        while path.exists():
            path = self.root / f"{stem}-{number}.md"
            number += 1
        path.write_text(body.rstrip() + "\n", encoding="utf-8")
        return path.name

    def _note_path(self, relative: str) -> Path:
        raw = relative.strip().replace("\\", "/")
        parts = [part for part in raw.split("/") if part]
        if not parts or any(part in (".", "..") or part.startswith(".") for part in parts):
            raise ValueError("路径不合法")
        if not parts[-1].endswith(".md"):
            raise ValueError("只接受 Markdown")
        return self._resolve("/".join(parts))

    def _resolve(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        root = self.root.resolve()
        if path != root and root not in path.parents:
            raise ValueError("不能读知识库以外的文件")
        return path
