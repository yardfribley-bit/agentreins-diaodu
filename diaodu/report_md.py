"""Small Markdown subset for team reports.

The output is HTML with every piece of author text escaped. Raw HTML in the
source stays text, and links are limited to http(s). The conversation page
has its own DOM renderer; this one feeds the PDF.
"""
from __future__ import annotations

import html
import re
from urllib.parse import urlsplit


_HEADING = re.compile(r"^(#{1,6})\s+(\S.*?)\s*#*\s*$")
_UL = re.compile(r"^(\s*)([-*+])\s+(\S.*)$")
_OL = re.compile(r"^(\s*)(\d{1,6})[.)]\s+(\S.*)$")
_FENCE = re.compile(r"^(```+|~~~+)")
_HR = re.compile(r"^(\*\s*){3,}$|^(-+\s*){3,}$|^(_\s*){3,}$")
_ID = re.compile(r"sec-\d{1,6}")


def parse_blocks(source):
    if not isinstance(source, str):
        return []
    text = source.replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.split("\n")
    blocks = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        fence = _FENCE.match(line)
        if fence:
            mark = fence.group(1)[0]
            opener = len(fence.group(1))
            body = []
            index += 1
            while index < len(lines) and not _fence_close(lines[index], mark, opener):
                body.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            blocks.append({"type": "code", "text": "\n".join(body)})
            continue
        if _table_at(lines, index):
            header = _split_row(lines[index])
            index += 2
            rows = []
            while index < len(lines) and lines[index].strip() and "|" in lines[index] and not _table_at(lines, index):
                if _HEADING.match(lines[index]) or _FENCE.match(lines[index]):
                    break
                rows.append(_split_row(lines[index]))
                index += 1
            blocks.append({"type": "table", "header": header, "rows": rows})
            continue
        heading = _HEADING.match(line)
        if heading:
            blocks.append({"type": "heading", "level": len(heading.group(1)), "text": heading.group(2).strip()})
            index += 1
            continue
        if _HR.match(line.strip()):
            blocks.append({"type": "hr"})
            index += 1
            continue
        if line.lstrip().startswith(">"):
            quoted = []
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                quoted.append(re.sub(r"^\s*>\s?", "", lines[index]))
                index += 1
            blocks.append({"type": "quote", "blocks": parse_blocks("\n".join(quoted))})
            continue
        unordered = _UL.match(line)
        if unordered:
            items = []
            while index < len(lines) and _UL.match(lines[index]):
                item = _UL.match(lines[index]).group(3)
                index += 1
                while index < len(lines) and lines[index].startswith("  ") and lines[index].strip() and not _starts_block(lines, index):
                    item += "\n" + lines[index].strip()
                    index += 1
                items.append(item)
            blocks.append({"type": "ul", "items": items})
            continue
        ordered = _OL.match(line)
        if ordered:
            items = []
            while index < len(lines) and _OL.match(lines[index]):
                item = _OL.match(lines[index]).group(3)
                index += 1
                while index < len(lines) and lines[index].startswith("  ") and lines[index].strip() and not _starts_block(lines, index):
                    item += "\n" + lines[index].strip()
                    index += 1
                items.append(item)
            blocks.append({"type": "ol", "items": items})
            continue
        paragraph = [line]
        index += 1
        while index < len(lines) and lines[index].strip() and not _starts_block(lines, index):
            paragraph.append(lines[index])
            index += 1
        blocks.append({"type": "p", "text": "\n".join(paragraph)})
    return blocks


def number_headings(blocks, counter=None):
    if counter is None:
        counter = [0]
    for block in blocks:
        if block.get("type") == "heading":
            counter[0] += 1
            block["id"] = "sec-%d" % counter[0]
        elif block.get("type") == "quote":
            number_headings(block.get("blocks") or [], counter)
    return blocks


def collect_headings(blocks, found=None):
    if found is None:
        found = []
    for block in blocks:
        if block.get("type") == "heading" and block.get("level", 9) <= 3 and _ID.fullmatch(block.get("id") or ""):
            found.append({"id": block["id"], "level": block["level"], "text": block.get("text") or ""})
        elif block.get("type") == "quote":
            collect_headings(block.get("blocks") or [], found)
    return found


def blocks_to_html(blocks):
    parts = []
    for block in blocks:
        kind = block.get("type")
        if kind == "heading":
            level = block.get("level")
            if not isinstance(level, int) or not 1 <= level <= 6:
                continue
            attr = ' id="%s"' % block["id"] if _ID.fullmatch(block.get("id") or "") else ""
            parts.append("<h%d%s>%s</h%d>" % (level, attr, inline_html(block.get("text") or ""), level))
        elif kind == "p":
            parts.append("<p>%s</p>" % inline_html(block.get("text") or ""))
        elif kind == "ul":
            parts.append("<ul>%s</ul>" % "".join("<li>%s</li>" % inline_html(item) for item in block.get("items") or []))
        elif kind == "ol":
            parts.append("<ol>%s</ol>" % "".join("<li>%s</li>" % inline_html(item) for item in block.get("items") or []))
        elif kind == "quote":
            parts.append("<blockquote>%s</blockquote>" % blocks_to_html(block.get("blocks") or []))
        elif kind == "code":
            parts.append("<pre><code>%s</code></pre>" % html.escape(block.get("text") or ""))
        elif kind == "table":
            parts.append(_table_html(block.get("header") or [], block.get("rows") or []))
        elif kind == "hr":
            parts.append("<hr>")
    return "".join(parts)


def inline_html(source):
    if not isinstance(source, str) or not source:
        return ""
    out = []
    index = 0
    length = len(source)
    while index < length:
        if source[index] == "\\" and index + 1 < length:
            out.append(html.escape(source[index + 1]))
            index += 2
            continue
        if source[index] == "\n":
            out.append("<br>\n")
            index += 1
            continue
        if source[index] == "`":
            end = source.find("`", index + 1)
            if end != -1:
                out.append("<code>%s</code>" % html.escape(source[index + 1:end]))
                index = end + 1
                continue
        if source.startswith("**", index) or source.startswith("__", index):
            mark = source[index:index + 2]
            end = source.find(mark, index + 2)
            if end > index + 2:
                out.append("<strong>%s</strong>" % inline_html(source[index + 2:end]))
                index = end + 2
                continue
        if source[index] == "[":
            rendered, index = _link(source, index)
            if rendered is not None:
                out.append(rendered)
                continue
        if source[index] == "*":
            end = source.find("*", index + 1)
            if end > index + 1 and "\n" not in source[index + 1:end]:
                out.append("<em>%s</em>" % inline_html(source[index + 1:end]))
                index = end + 1
                continue
        nxt = index + 1
        while nxt < length and source[nxt] not in "*_`[\\\n":
            nxt += 1
        out.append(html.escape(source[index:nxt]))
        index = nxt
    return "".join(out)


def safe_href(url):
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or len(url) > 2000 or url.startswith("//"):
        return None
    if any(ord(char) < 32 or char in ' "<>\\' for char in url):
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return html.escape(url, quote=True)


def _link(source, index):
    close = source.find("]", index + 1)
    if close == -1 or close + 1 >= len(source) or source[close + 1] != "(":
        return None, index
    end = source.find(")", close + 2)
    if end == -1:
        return None, index
    label = inline_html(source[index + 1:close])
    href = safe_href(source[close + 2:end])
    if href:
        return '<a href="%s">%s</a>' % (href, label), end + 1
    return label, end + 1


def _table_html(header, rows):
    width = max(len(header), 1)
    def fit(cells):
        cells = list(cells)[:width]
        if len(cells) < width:
            cells.extend([""] * (width - len(cells)))
        return cells
    head = "".join("<th>%s</th>" % inline_html(cell) for cell in fit(header))
    body = []
    for row in rows:
        body.append("<tr>%s</tr>" % "".join("<td>%s</td>" % inline_html(cell) for cell in fit(row)))
    return '<div class="table-wrap"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (
        head, "".join(body))


def _split_row(line):
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def _table_at(lines, index):
    if index + 1 >= len(lines) or "|" not in lines[index]:
        return False
    header = _split_row(lines[index])
    separator = _split_row(lines[index + 1])
    if len(header) < 2 or len(separator) < 2:
        return False
    return all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator)


def _fence_close(line, mark, opener):
    trimmed = line.strip()
    return len(trimmed) >= max(3, opener) and trimmed == mark * len(trimmed)


def _starts_block(lines, index):
    line = lines[index]
    if not line.strip():
        return True
    if _FENCE.match(line) or _HEADING.match(line) or _HR.match(line.strip()):
        return True
    if line.lstrip().startswith(">") or _UL.match(line) or _OL.match(line):
        return True
    return _table_at(lines, index)
