"""Anonymous GET routes for the partner report feed.

Nginx strips /task, so production
https://www.chuhaijian.com/task/v1/public/reports.json
arrives here as /v1/public/reports.json. No credential is read.
"""
import hashlib
import json
from email.utils import formatdate
from urllib.parse import parse_qs

from diaodu.identity import AuthError
from diaodu.relay_http import _directory_rate_limit, _one_query
from diaodu.report_public import etag_for, http_date, not_modified

FEED_CACHE = "public, max-age=60, must-revalidate"
FILE_CACHE = "public, max-age=60, must-revalidate"


def get(handler, library, parsed):
    _directory_rate_limit(handler)
    query = parse_qs(parsed.query, keep_blank_values=True)
    _one_query(query, set())
    teams = getattr(library, "teams", None)
    if teams is None:
        raise AuthError("没有这个公开报告", 404)
    parts = parsed.path.strip("/").split("/")
    if parts == ["v1", "public", "reports.json"]:
        _feed(handler, teams)
        return
    if (len(parts) == 5 and parts[:3] == ["v1", "public", "reports"]
            and parts[4] in ("report.md", "report.pdf")):
        kind = "markdown" if parts[4] == "report.md" else "pdf"
        _file(handler, teams, parts[3], kind)
        return
    handler._json(404, {"error": "没有这个公开报告"})


def _feed(handler, teams):
    document, stamp = teams.public_feed()
    body = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    tag = etag_for(body)
    headers = {
        "ETag": tag,
        "Cache-Control": FEED_CACHE,
        "X-Content-Type-Options": "nosniff",
    }
    if stamp:
        headers["Last-Modified"] = http_date(stamp)
    if not_modified(handler.headers.get("If-None-Match"),
                    handler.headers.get("If-Modified-Since"), tag, stamp):
        _empty(handler, 304, headers)
        return
    _bytes(handler, 200, "application/json; charset=utf-8", body, headers)


def _file(handler, teams, public_id, kind):
    meta, path = teams.approved_report_path(public_id, kind)
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    tag = '"' + digest + '"'
    content_type = "text/markdown; charset=utf-8" if kind == "markdown" else "application/pdf"
    headers = {
        "ETag": tag,
        "Cache-Control": FILE_CACHE,
        "X-Content-Type-Options": "nosniff",
        "Last-Modified": formatdate(float(meta["updated"]), usegmt=True),
        "Content-Disposition": "inline; filename=\"" + meta["name"] + "\"",
    }
    if not_modified(handler.headers.get("If-None-Match"),
                    handler.headers.get("If-Modified-Since"), tag, meta["updated"]):
        _empty(handler, 304, headers)
        return
    _bytes(handler, 200, content_type, data, headers)


def _empty(handler, status, headers):
    handler.send_response(status)
    for key, value in headers.items():
        handler.send_header(key, value)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def _bytes(handler, status, content_type, body, headers):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    for key, value in headers.items():
        handler.send_header(key, value)
    handler.end_headers()
    if not getattr(handler, "_head_only", False):
        try:
            handler.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
