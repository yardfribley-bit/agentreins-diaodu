"""Print a team final report to PDF.

The production Ubuntu host needs a CJK font and one layout engine. Chromium
(or Google Chrome) is preferred; WeasyPrint is the fallback. See
``deploy/README.md`` for the apt packages, including ``fonts-noto-cjk``.

Only the report title, the team title, a display name and the Markdown are
embedded. Session ids, actor keys, invite codes and lease timestamps are not
accepted by this module.
"""
from __future__ import annotations

import html
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from diaodu import report_md


RENDER_VERSION = "1"
_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
)
_BROWSER_NAMES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "google-chrome-beta",
)


class PdfError(RuntimeError):
    """The host has no working PDF engine, or the engine produced nothing."""


def render_html(*, markdown, title, team_title, author, when, chars, engine="chromium"):
    blocks = report_md.number_headings(report_md.parse_blocks(markdown or ""))
    headings = report_md.collect_headings(blocks)
    article = report_md.blocks_to_html(blocks)
    toc = _toc(headings)
    cover_bits = []
    if team_title:
        cover_bits.append("<p class=\"meta\">%s</p>" % html.escape(team_title))
    if author:
        cover_bits.append("<p class=\"meta\">%s</p>" % html.escape(author))
    if when:
        cover_bits.append("<p class=\"meta\">%s</p>" % html.escape(when))
    if isinstance(chars, int) and chars >= 0:
        cover_bits.append("<p class=\"meta\">%s 字</p>" % format(chars, ","))
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>%s</title><style>%s%s</style></head><body>"
        "<section class=\"cover\"><p class=\"kicker\">AgentReins Hub · 最终报告</p>"
        "<h1>%s</h1>%s</section>%s<article class=\"body\">%s</article></body></html>"
    ) % (
        html.escape(title or "最终报告"),
        _css(engine),
        _font_css(),
        html.escape(title or "最终报告"),
        "".join(cover_bits),
        toc,
        article,
    )


def write_pdf(destination, **kwargs):
    """Write a PDF file. Returns ``chromium`` or ``weasyprint``."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    errors = []
    browser = find_browser()
    if browser:
        try:
            _chrome_pdf(destination, browser, **kwargs)
            return "chromium"
        except PdfError as exc:
            errors.append(str(exc))
    if _weasyprint_available():
        try:
            _weasy_pdf(destination, **kwargs)
            return "weasyprint"
        except PdfError as exc:
            errors.append(str(exc))
    detail = "；".join(errors) if errors else "未找到 Chromium 或 WeasyPrint"
    raise PdfError(detail)


def find_browser():
    override = os.environ.get("DIAODU_PDF_BROWSER")
    if override:
        candidate = Path(override)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        found = shutil.which(override)
        if found:
            return found
    for name in _BROWSER_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def _weasyprint_available():
    try:
        import weasyprint  # noqa: F401
    except Exception:
        return False
    return True


def _chrome_pdf(destination, browser, **kwargs):
    html_text = render_html(engine="chromium", **kwargs)
    with tempfile.TemporaryDirectory(prefix="diaodu-report-") as temporary:
        root = Path(temporary)
        html_path = root / "report.html"
        html_path.write_text(html_text, encoding="utf-8")
        profile = root / "profile"
        profile.mkdir()
        output = root / "out.pdf"
        command = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--no-pdf-header-footer",
            "--print-to-pdf=" + str(output),
            "--user-data-dir=" + str(profile),
            html_path.resolve().as_uri(),
        ]
        env = os.environ.copy()
        env["HOME"] = str(root)
        env["XDG_CONFIG_HOME"] = str(root / "config")
        env["XDG_CACHE_HOME"] = str(root / "cache")
        _run(command, env, output)
        if not output.is_file():
            # Older Chrome builds drop the file in the working directory.
            command[0:1] = [browser, "--headless"]
            _run(command, env, output, cwd=root)
        produced = output if output.is_file() else root / "output.pdf"
        if not produced.is_file() or produced.stat().st_size < 500:
            raise PdfError("Chromium 没有写出 PDF")
        data = produced.read_bytes()
        if not data.startswith(b"%PDF"):
            raise PdfError("Chromium 写出的文件不是 PDF")
        destination.write_bytes(data)


def _weasy_pdf(destination, **kwargs):
    html_text = render_html(engine="weasyprint", **kwargs)
    try:
        from weasyprint import HTML
        HTML(string=html_text, base_url=str(destination.parent)).write_pdf(str(destination))
    except PdfError:
        raise
    except Exception as exc:
        raise PdfError("WeasyPrint 失败") from exc
    if not destination.is_file() or destination.stat().st_size < 500 or not destination.read_bytes().startswith(b"%PDF"):
        raise PdfError("WeasyPrint 没有写出 PDF")


def _run(command, env, expected, cwd=None):
    """Wait until Chromium has written a PDF, then stop it.

    Headless Chrome on this host prints the file and then keeps the browser
    process alive, so waiting for the process to exit always times out.
    """
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=env, cwd=cwd, start_new_session=True)
    except OSError as exc:
        raise PdfError("无法启动排版进程") from exc
    timed_out = False
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if _pdf_ready(expected):
                return
            if process.poll() is not None:
                break
            time.sleep(0.1)
        else:
            timed_out = process.poll() is None
        if _pdf_ready(expected):
            return
    finally:
        err = _finish(process)
    if _pdf_ready(expected):
        return
    if timed_out:
        raise PdfError("排版超时")
    tail = (err or b"")[-400:].decode("utf-8", "replace")
    raise PdfError("排版进程退出 %s %s" % (process.returncode, tail[:180]))


def _pdf_ready(path):
    try:
        if not path.is_file():
            return False
        size = path.stat().st_size
        if size < 500:
            return False
        data = path.read_bytes()
    except OSError:
        return False
    if not data.startswith(b"%PDF") or b"%%EOF" not in data[-2048:]:
        return False
    time.sleep(0.15)
    try:
        return path.stat().st_size == size
    except OSError:
        return False


def _finish(process):
    if process.poll() is None:
        _kill(process)
    err = b""
    pipe = process.stderr
    if pipe is not None:
        try:
            err = pipe.read() or b""
        except Exception:
            err = b""
        try:
            pipe.close()
        except Exception:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    return err


def _kill(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _toc(headings):
    if not headings:
        return ""
    items = []
    for item in headings:
        items.append('<li class="lv-%d"><a href="#%s">%s</a></li>' % (
            item["level"], item["id"], report_md.inline_html(item["text"])))
    return '<nav class="toc"><h2>目录</h2><ol>%s</ol></nav>' % "".join(items)


def _font_files():
    found = [path for path in _FONT_CANDIDATES if os.path.isfile(path)]
    if found:
        return found[:1]
    root = Path("/usr/share/fonts")
    if not root.is_dir():
        return []
    for pattern in ("**/NotoSansCJK-Regular.ttc", "**/NotoSansCJK-Regular.otf",
                    "**/NotoSansSC-Regular.otf", "**/DroidSansFallbackFull.ttf",
                    "**/wqy-microhei.ttc"):
        matches = sorted(root.glob(pattern))
        if matches:
            return [str(matches[0])]
    return []


def _font_css():
    files = _font_files()
    if not files:
        return ""
    path = files[0]
    suffix = Path(path).suffix.lower()
    fmt = " format('collection')" if suffix in (".ttc", ".otc") else ""
    uri = Path(path).resolve().as_uri()
    return ("@font-face{font-family:'ReportCJK';src:url('%s')%s;"
            "font-weight:400;font-style:normal;}") % (uri, fmt)


def _css(engine):
    page = (
        "@page{size:A4;margin:18mm 16mm 20mm 16mm;"
        "@bottom-left{content:'最终报告';font-family:'ReportCJK','Noto Sans CJK SC',"
        "'Noto Sans CJK JP','Source Han Sans SC','WenQuanYi Micro Hei','Droid Sans Fallback',sans-serif;"
        "font-size:9pt;color:#5c6570;}"
        "@bottom-right{content:counter(page);font-family:'ReportCJK','Noto Sans CJK SC',sans-serif;"
        "font-size:9pt;color:#5c6570;}}"
    )
    if engine == "weasyprint":
        # WeasyPrint uses the same margin boxes. Keep the rule identical so a
        # cached HTML string is not required; the engine flag only selects this
        # block if a future build needs a running element instead.
        page = page
    return (
        page
        + "html,body{margin:0;padding:0;}"
        + "body{font-family:'ReportCJK','Noto Sans CJK SC','Noto Sans CJK JP',"
        + "'Source Han Sans SC','Noto Sans SC','WenQuanYi Micro Hei','Droid Sans Fallback',sans-serif;"
        + "font-size:11pt;line-height:1.7;color:#1d2330;}"
        + ".cover{page-break-after:always;padding-top:42mm;}"
        + ".cover .kicker{margin:0;color:#5c6570;font-size:11pt;letter-spacing:.08em;}"
        + ".cover h1{margin:14pt 0 12pt;font-size:26pt;line-height:1.35;font-weight:700;}"
        + ".cover .meta{margin:0 0 4pt;font-size:12pt;color:#3a4150;}"
        + ".toc{page-break-after:always;}"
        + ".toc h2{margin:0 0 10pt;font-size:16pt;}"
        + ".toc ol{margin:0;padding-left:1.2em;}"
        + ".toc li{margin:4pt 0;}"
        + ".toc .lv-2{margin-left:1em;}.toc .lv-3{margin-left:2em;}"
        + "article h1{font-size:18pt;line-height:1.35;margin:16pt 0 8pt;}"
        + "article h2{font-size:15pt;line-height:1.35;margin:16pt 0 6pt;"
        + "border-bottom:1px solid #e3e6ec;padding-bottom:3pt;}"
        + "article h3{font-size:13pt;margin:12pt 0 4pt;}"
        + "p{margin:0 0 8pt;}ul,ol{margin:0 0 8pt;padding-left:1.3em;}"
        + "a{color:#2f5bea;}"
        + "pre{white-space:pre-wrap;word-wrap:break-word;background:#f6f7f9;"
        + "padding:8pt;border-radius:4pt;font-size:9.5pt;line-height:1.5;}"
        + "pre,code{font-family:'ReportCJK','Noto Sans Mono CJK SC','WenQuanYi Micro Hei Mono',"
        + "'Droid Sans Fallback',ui-monospace,monospace;}"
        + "blockquote{margin:0 0 8pt;padding-left:10pt;border-left:3px solid #d0d4dc;color:#3a4150;}"
        + "table{width:100%;border-collapse:collapse;margin:8pt 0 12pt;}"
        + "th,td{border:1px solid #d0d4dc;padding:4pt 6pt;vertical-align:top;"
        + "text-align:left;word-wrap:break-word;overflow-wrap:anywhere;}"
        + "th{background:#eef2fd;font-weight:600;}"
        + "tr{break-inside:avoid;}"
        + "h1,h2,h3{break-after:avoid;}"
    )
