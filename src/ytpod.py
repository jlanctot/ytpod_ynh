#!/usr/bin/env python3
"""YTPod: YouTube channels/playlists -> podcast RSS + local audio media.

The application intentionally uses only Python's standard library plus yt-dlp.
It is designed to run as a single YunoHost-managed systemd service.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

try:
    import tomllib
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("Python 3.11+ is required") from exc

APP = "ytpod"
CONFIG_PATH = Path(os.environ.get("YTPOD_CONFIG", "/etc/ytpod/feeds.toml"))
DATA_DIR = Path(os.environ.get("YTPOD_DATA", "/var/lib/ytpod"))
STATE_DIR = DATA_DIR / "state"
MEDIA_DIR = DATA_DIR / "media"
BASE_URL = os.environ.get("YTPOD_BASE_URL", "http://127.0.0.1:8746/ytpod").rstrip("/")
SECRET_PATH = STATE_DIR / "secret"
LOG_NAME = "ytpod"
MAX_TITLE = 300
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,128}$")
FEED_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{24,128}$")

YT_DLP = os.environ.get("YTPOD_YTDLP", str(Path(sys.executable).with_name("yt-dlp")))

DEFAULTS: dict[str, Any] = {
    "update_interval_hours": 6,
    "default_keep_last": 20,
    "audio_bitrate": "96K",
}

LOG_LOCK = threading.Lock()
CONFIG_LOCK = threading.Lock()
REFRESH_LOCK = threading.Lock()
REFRESHING: set[str] = set()


@dataclass(frozen=True)
class Feed:
    id: str
    name: str
    source: str
    keep_last: int
    since: str
    artwork_url: str
    token: str


class AppError(Exception):
    """Expected user-facing application error."""


def ensure_directories() -> None:
    for path in (DATA_DIR, STATE_DIR, MEDIA_DIR, DATA_DIR / "artwork"):
        path.mkdir(parents=True, exist_ok=True)


def log(message: str) -> None:
    ensure_directories()
    line = f"{dt.datetime.now(dt.timezone.utc).isoformat()} {message}\n"
    with LOG_LOCK:
        try:
            log_dir = Path("/var/log/ytpod")
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "ytpod.log").open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass


def ensure_secret() -> bytes:
    ensure_directories()
    if SECRET_PATH.exists():
        raw = SECRET_PATH.read_text(encoding="utf-8").strip()
        try:
            return bytes.fromhex(raw)
        except ValueError:
            pass
    secret = secrets.token_bytes(32)
    tmp = SECRET_PATH.with_suffix(".tmp")
    tmp.write_text(secret.hex(), encoding="utf-8")
    os.replace(tmp, SECRET_PATH)
    try:
        SECRET_PATH.chmod(0o600)
    except OSError:
        pass
    return secret


def csrf_token(username: str) -> str:
    today = dt.datetime.now(dt.timezone.utc).date().isoformat().encode()
    return hmac.new(ensure_secret(), username.encode() + b"|" + today, "sha256").hexdigest()


def valid_csrf(username: str, token: str) -> bool:
    return hmac.compare_digest(csrf_token(username), token)


def read_toml() -> dict[str, Any]:
    ensure_directories()
    if not CONFIG_PATH.exists():
        return dict(DEFAULTS, feeds=[])
    with CONFIG_PATH.open("rb") as fh:
        raw = tomllib.load(fh)
    config = dict(DEFAULTS)
    config.update({k: v for k, v in raw.items() if k != "feeds"})
    config["feeds"] = raw.get("feeds", [])
    return config


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def write_toml(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# YTPod configuration. Values are managed by the YunoHost app.",
        "",
        f"update_interval_hours = {float(config.get('update_interval_hours', DEFAULTS['update_interval_hours'])):g}",
        f"default_keep_last = {int(config.get('default_keep_last', DEFAULTS['default_keep_last']))}",
        f"audio_bitrate = {_toml_string(str(config.get('audio_bitrate', DEFAULTS['audio_bitrate'])))}",
        "",
    ]
    for feed in config.get("feeds", []):
        normalized = normalize_feed(feed)
        lines += ["[[feeds]]"]
        for key in ("id", "name", "source", "since", "artwork_url", "token"):
            lines.append(f"{key} = {_toml_string(str(normalized.get(key, '')))}")
        lines.append(f"keep_last = {int(normalized['keep_last'])}")
        lines.append("")

    payload = "\n".join(lines).rstrip() + "\n"
    fd, temp_name = tempfile.mkstemp(prefix="feeds.", dir=str(CONFIG_PATH.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, CONFIG_PATH)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def normalize_feed(raw: dict[str, Any]) -> dict[str, Any]:
    feed_id = str(raw.get("id", "")).strip().lower()
    name = str(raw.get("name", "")).strip()
    source = str(raw.get("source", "")).strip()
    keep_last = int(raw.get("keep_last", DEFAULTS["default_keep_last"]))
    since = str(raw.get("since", "")).strip()
    artwork_url = str(raw.get("artwork_url", "")).strip()
    token = str(raw.get("token", "")).strip()

    if not FEED_ID_RE.fullmatch(feed_id):
        raise AppError("Feed ID must contain 2–63 lowercase letters, numbers, '_' or '-'.")
    if not name or len(name) > 200:
        raise AppError("Feed name is required and must be at most 200 characters.")
    if not source.startswith(("https://", "http://")):
        raise AppError("Source must be an HTTP(S) URL.")
    if keep_last < 1 or keep_last > 500:
        raise AppError("Keep-last must be between 1 and 500.")
    if since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        raise AppError("Since must be a date in YYYY-MM-DD format.")
    if artwork_url and not artwork_url.startswith(("https://", "http://")):
        raise AppError("Artwork URL must be an HTTP(S) URL.")
    if token and not TOKEN_RE.fullmatch(token):
        raise AppError("Feed token is invalid.")
    if not token:
        token = secrets.token_urlsafe(32)
    return {
        "id": feed_id,
        "name": name,
        "source": source,
        "keep_last": keep_last,
        "since": since,
        "artwork_url": artwork_url,
        "token": token,
    }


def feeds() -> list[dict[str, Any]]:
    return [normalize_feed(item) for item in read_toml().get("feeds", [])]


def get_feed(feed_id: str, token: str | None = None) -> Feed:
    for raw in feeds():
        if raw["id"] == feed_id:
            if token is not None and not hmac.compare_digest(raw["token"], token):
                raise AppError("Not found")
            return Feed(**raw)
    raise AppError("Not found")


def state_path(feed_id: str) -> Path:
    return STATE_DIR / f"{feed_id}.json"


def load_state(feed_id: str) -> dict[str, Any]:
    path = state_path(feed_id)
    if not path.exists():
        return {"items": {}, "last_run": None, "last_error": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"items": {}, "last_run": None, "last_error": "State file was unreadable."}


def save_state(feed_id: str, state: dict[str, Any]) -> None:
    path = state_path(feed_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def run_ytdlp(args: list[str], timeout: int = 900) -> str:
    command = [YT_DLP, *args]
    log(f"running yt-dlp: {' '.join(command[:4])} ...")
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    except FileNotFoundError as exc:
        raise AppError(f"yt-dlp executable not found at {YT_DLP}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AppError("yt-dlp timed out.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "yt-dlp failed").strip()
        raise AppError(detail[-2000:]) from exc
    return result.stdout


def discover(source: str, keep_last: int, since: str) -> list[dict[str, Any]]:
    limit = min(500, max(25, keep_last * 2))
    args = [
        "--flat-playlist",
        "--dump-single-json",
        "--skip-download",
        "--ignore-errors",
        "--no-warnings",
        "--playlist-end",
        str(limit),
        source,
    ]
    if since:
        args[0:0] = ["--dateafter", since.replace("-", "")]
    payload = run_ytdlp(args, timeout=600)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise AppError("yt-dlp returned invalid JSON while discovering videos.") from exc
    entries = data.get("entries") if isinstance(data, dict) else None
    if entries is None:
        entries = [data]
    out: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        video_id = str(entry.get("id", "")).strip()
        if not VIDEO_ID_RE.fullmatch(video_id):
            continue
        live_status = str(entry.get("live_status", ""))
        if live_status in {"is_live", "is_upcoming"}:
            continue
        out.append(entry)
    return out


def get_metadata(video_url: str) -> dict[str, Any]:
    payload = run_ytdlp(
        [
            "--skip-download",
            "--no-warnings",
            "--no-playlist",
            "--dump-single-json",
            video_url,
        ],
        timeout=300,
    )
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise AppError("yt-dlp returned invalid JSON for video metadata.") from exc


def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={quote(video_id)}"


def iso_datetime(raw: str | None, upload_date: str | None) -> str:
    if raw:
        try:
            value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=dt.timezone.utc)
            return value.astimezone(dt.timezone.utc).isoformat()
        except ValueError:
            pass
    if upload_date and re.fullmatch(r"\d{8}", upload_date):
        value = dt.datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=dt.timezone.utc)
        return value.isoformat()
    return dt.datetime.now(dt.timezone.utc).isoformat()


def safe_title(value: Any) -> str:
    title = str(value or "Untitled video").strip()
    return title[:MAX_TITLE]


def download_audio(feed: Feed, metadata: dict[str, Any], bitrate: str) -> tuple[str, int]:
    video_id = str(metadata.get("id", "")).strip()
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise AppError("Video ID is invalid.")
    output_template = str(MEDIA_DIR / f"{feed.id}-{video_id}.%(ext)s")
    before = {p.name: p.stat().st_mtime_ns for p in MEDIA_DIR.glob(f"{feed.id}-{video_id}.*") if p.is_file()}
    run_ytdlp(
        [
            "--no-playlist",
            "--no-warnings",
            "--format",
            "bestaudio/best",
            "--extract-audio",
            "--audio-format",
            "m4a",
            "--audio-quality",
            bitrate,
            "--embed-metadata",
            "--no-overwrites",
            "--output",
            output_template,
            video_url(video_id),
        ],
        timeout=1800,
    )
    candidates = [p for p in MEDIA_DIR.glob(f"{feed.id}-{video_id}.*") if p.is_file()]
    if not candidates:
        raise AppError("yt-dlp completed but no media file was produced.")
    candidates.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
    path = candidates[0]
    if path.name in before and path.stat().st_mtime_ns == before[path.name]:
        pass
    return path.name, path.stat().st_size


def prune_feed(feed: Feed, state: dict[str, Any]) -> None:
    items: dict[str, dict[str, Any]] = state.get("items", {})
    ordered = sorted(items.items(), key=lambda kv: kv[1].get("published", ""), reverse=True)
    keep_ids = {video_id for video_id, _ in ordered[: feed.keep_last]}
    for video_id, record in list(items.items()):
        if video_id in keep_ids:
            continue
        filename = record.get("file")
        if filename:
            try:
                (MEDIA_DIR / filename).unlink(missing_ok=True)
            except OSError:
                pass
        items.pop(video_id, None)
    state["items"] = items


def refresh_feed(feed_id: str) -> None:
    with REFRESH_LOCK:
        if feed_id in REFRESHING:
            return
        REFRESHING.add(feed_id)
    try:
        feed = get_feed(feed_id)
        config = read_toml()
        state = load_state(feed.id)
        state["last_error"] = None
        entries = discover(feed.source, feed.keep_last, feed.since)
        entries = entries[: min(500, max(feed.keep_last * 2, feed.keep_last))]
        existing: dict[str, dict[str, Any]] = state.get("items", {})
        bitrate = str(config.get("audio_bitrate", DEFAULTS["audio_bitrate"]))
        for entry in reversed(entries):
            video_id = str(entry.get("id", ""))
            if not VIDEO_ID_RE.fullmatch(video_id):
                continue
            record = existing.get(video_id)
            try:
                if record and record.get("file") and (MEDIA_DIR / record["file"]).exists():
                    continue
                metadata = get_metadata(video_url(video_id))
                if str(metadata.get("live_status", "")) in {"is_live", "is_upcoming"}:
                    continue
                upload_date = str(metadata.get("upload_date", ""))
                if feed.since and upload_date and upload_date < feed.since.replace("-", ""):
                    continue
                filename, size = download_audio(feed, metadata, bitrate)
                published = iso_datetime(metadata.get("timestamp") and dt.datetime.fromtimestamp(int(metadata["timestamp"]), tz=dt.timezone.utc).isoformat(), upload_date)
                existing[video_id] = {
                    "id": video_id,
                    "title": safe_title(metadata.get("title") or entry.get("title")),
                    "description": str(metadata.get("description") or metadata.get("title") or ""),
                    "published": published,
                    "duration": int(metadata.get("duration") or 0),
                    "file": filename,
                    "size": size,
                    "channel": str(metadata.get("channel") or metadata.get("uploader") or feed.name),
                }
                save_state(feed.id, state)
            except AppError as exc:
                log(f"feed {feed.id} video {video_id} failed: {exc}")
                continue
        prune_feed(feed, state)
        state["last_run"] = dt.datetime.now(dt.timezone.utc).isoformat()
        save_state(feed.id, state)
        log(f"feed {feed.id} refreshed successfully")
    except AppError as exc:
        state = load_state(feed_id)
        state["last_run"] = dt.datetime.now(dt.timezone.utc).isoformat()
        state["last_error"] = str(exc)
        save_state(feed_id, state)
        log(f"feed {feed_id} failed: {exc}")
    except Exception as exc:  # pragma: no cover - defensive service guard
        state = load_state(feed_id)
        state["last_run"] = dt.datetime.now(dt.timezone.utc).isoformat()
        state["last_error"] = f"Unexpected error: {exc}"
        save_state(feed_id, state)
        log(f"feed {feed_id} unexpected error: {exc}")
    finally:
        with REFRESH_LOCK:
            REFRESHING.discard(feed_id)


def due_feeds() -> list[str]:
    try:
        interval = float(read_toml().get("update_interval_hours", DEFAULTS["update_interval_hours"]))
    except (TypeError, ValueError):
        interval = DEFAULTS["update_interval_hours"]
    interval_seconds = max(300, interval * 3600)
    now = time.time()
    result: list[str] = []
    for raw in feeds():
        last_run = load_state(raw["id"]).get("last_run")
        try:
            timestamp = dt.datetime.fromisoformat(str(last_run)).timestamp() if last_run else 0
        except ValueError:
            timestamp = 0
        if now - timestamp >= interval_seconds:
            result.append(raw["id"])
    return result


def scheduler() -> None:
    while True:
        for feed_id in due_feeds():
            threading.Thread(target=refresh_feed, args=(feed_id,), daemon=True, name=f"refresh-{feed_id}").start()
        time.sleep(60)


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def rss_datetime(value: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    except ValueError:
        return dt.datetime.now(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def media_url(feed: Feed, filename: str) -> str:
    return f"{BASE_URL}/media/{quote(feed.id)}/{quote(feed.token)}/{quote(filename)}"


def feed_url(feed: Feed) -> str:
    return f"{BASE_URL}/feed/{quote(feed.id)}/{quote(feed.token)}.xml"


def build_rss(feed: Feed) -> str:
    state = load_state(feed.id)
    items = sorted(state.get("items", {}).values(), key=lambda x: x.get("published", ""), reverse=True)
    channel_description = f"YouTube videos from {feed.name}"
    image = f'<itunes:image href="{html.escape(feed.artwork_url, quote=True)}" />' if feed.artwork_url else ""
    entries: list[str] = []
    for item in items[: feed.keep_last]:
        description = html.escape(str(item.get("description", "")))
        title = html.escape(str(item.get("title", "Untitled")))
        guid = html.escape(f"ytpod:{feed.id}:{item.get('id', '')}", quote=True)
        filename = str(item.get("file", ""))
        size = int(item.get("size", 0) or 0)
        enclosure = (
            f'<enclosure url="{html.escape(media_url(feed, filename), quote=True)}" '
            f'length="{size}" type="audio/mp4" />'
        )
        duration = format_duration(int(item.get("duration", 0) or 0))
        entries.append(
            "<item>"
            f"<title>{title}</title>"
            f"<description>{description}</description>"
            f"<pubDate>{html.escape(rss_datetime(str(item.get('published', ''))))}</pubDate>"
            f"<guid isPermaLink=\"false\">{guid}</guid>"
            f"<itunes:duration>{html.escape(duration)}</itunes:duration>"
            f"{enclosure}"
            "</item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
        "<channel>"
        f"<title>{html.escape(feed.name)}</title>"
        f"<link>{html.escape(feed.source, quote=True)}</link>"
        f"<description>{html.escape(channel_description)}</description>"
        f"<language>en-us</language>"
        f"<itunes:author>{html.escape(feed.name)}</itunes:author>"
        "<itunes:explicit>no</itunes:explicit>"
        f"{image}"
        f"{''.join(entries)}"
        "</channel></rss>"
    )


def html_page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang='en'><head>"
        "<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)} · YTPod</title>"
        "<style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:1100px;margin:3rem auto;padding:0 1rem;line-height:1.5;color:#222}"
        "h1,h2{line-height:1.2}label{display:block;font-weight:600;margin:.8rem 0 .25rem}input,button{font:inherit;padding:.55rem;border:1px solid #bbb;border-radius:.4rem;box-sizing:border-box}input[type=text],input[type=url],input[type=number],input[type=date]{width:100%}button{cursor:pointer;background:#f4f4f4}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem}.card{border:1px solid #ddd;border-radius:.6rem;padding:1rem}.muted{color:#666}.error{background:#fff0f0;border:1px solid #e5a0a0;padding:.8rem;border-radius:.4rem}.ok{background:#f0fff3;border:1px solid #a7d5b1;padding:.8rem;border-radius:.4rem}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all;font-size:.9rem}form.inline{display:inline}.actions{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.8rem}</style>"
        "</head><body>" + body + "</body></html>"
    ).encode("utf-8")


def parse_form(body: bytes) -> dict[str, str]:
    values = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: vals[-1] if vals else "" for key, vals in values.items()}


def admin_username(handler: BaseHTTPRequestHandler) -> str:
    username = str(handler.headers.get("Auth-User", "")).strip()
    if not username:
        raise AppError("Administrator authentication is required.")
    return username


def redirect(handler: BaseHTTPRequestHandler, location: str) -> None:
    handler.send_response(HTTPStatus.SEE_OTHER)
    handler.send_header("Location", location)
    handler.end_headers()


class Handler(BaseHTTPRequestHandler):
    server_version = "YTPod/0.1"

    def _send(self, status: int, content_type: str, body: bytes, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path
            if path == "/" or path == "":
                self._send(200, "text/html; charset=utf-8", html_page("YTPod", "<h1>YTPod</h1><p>The podcast bridge is running.</p><p><a href='./admin/'>Open administration</a></p>"))
                return
            if path.startswith("/feed/") and path.endswith(".xml"):
                parts = path.strip("/").split("/")
                if len(parts) != 3:
                    self._send(404, "text/plain; charset=utf-8", b"Not found")
                    return
                feed_id = unquote(parts[1])
                token = unquote(parts[2][:-4])
                feed = get_feed(feed_id, token)
                body = build_rss(feed).encode("utf-8")
                self._send(200, "application/rss+xml; charset=utf-8", body, {"ETag": f'W/"{len(body)}-{len(load_state(feed.id).get("items", {}))}"'})
                return
            if path.startswith("/media/"):
                parts = path.strip("/").split("/")
                if len(parts) < 4:
                    self._send(404, "text/plain; charset=utf-8", b"Not found")
                    return
                feed_id, token = unquote(parts[1]), unquote(parts[2])
                filename = unquote("/".join(parts[3:]))
                feed = get_feed(feed_id, token)
                expected = re.compile(rf"^{re.escape(feed.id)}-[A-Za-z0-9_-]+\.m4a$")
                if not expected.fullmatch(filename):
                    self._send(404, "text/plain; charset=utf-8", b"Not found")
                    return
                path_obj = (MEDIA_DIR / filename).resolve()
                if path_obj.parent != MEDIA_DIR.resolve() or not path_obj.is_file():
                    self._send(404, "text/plain; charset=utf-8", b"Not found")
                    return
                self._send_file(path_obj)
                return
            if path.startswith("/admin"):
                self._admin_get(path)
                return
            self._send(404, "text/plain; charset=utf-8", b"Not found")
        except AppError as exc:
            if str(exc) == "Not found":
                self._send(404, "text/plain; charset=utf-8", b"Not found")
            else:
                self._send(400, "text/plain; charset=utf-8", str(exc).encode())
        except Exception as exc:  # pragma: no cover
            log(f"GET error: {exc}")
            self._send(500, "text/plain; charset=utf-8", b"Internal server error")

    def _send_file(self, path_obj: Path) -> None:
        size = path_obj.stat().st_size
        range_header = self.headers.get("Range")
        start, end = 0, size - 1
        status = HTTPStatus.OK
        if range_header and range_header.startswith("bytes="):
            spec = range_header[6:].split(",", 1)[0].strip()
            first, _, last = spec.partition("-")
            try:
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                elif last:
                    length = int(last)
                    start = max(0, size - length)
                if start < 0 or start >= size or end < start:
                    raise ValueError
                end = min(end, size - 1)
                status = HTTPStatus.PARTIAL_CONTENT
            except ValueError:
                self._send(416, "text/plain; charset=utf-8", b"Invalid range", {"Content-Range": f"bytes */{size}"})
                return
        length = end - start + 1
        content_type = mimetypes.guess_type(path_obj.name)[0] or "audio/mp4"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Disposition", f'inline; filename="{path_obj.name}"')
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path_obj.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining:
                chunk = fh.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _admin_get(self, path: str) -> None:
        if path in {"/admin", "/admin/"}:
            username = admin_username(self)
            cards = []
            for feed in feeds():
                state = load_state(feed["id"])
                status = html.escape(str(state.get("last_error") or state.get("last_run") or "Never"))
                cards.append(
                    "<div class='card'>"
                    f"<h2>{html.escape(feed['name'])}</h2>"
                    f"<p class='muted mono'>{html.escape(feed['source'])}</p>"
                    f"<p><strong>Episodes:</strong> {len(state.get('items', {}))}<br><strong>Last run:</strong> {status}</p>"
                    f"<p class='mono'>{html.escape(feed_url(Feed(**feed)))}</p>"
                    "<div class='actions'>"
                    f"<form class='inline' method='post' action='./refresh'><input type='hidden' name='csrf' value='{html.escape(csrf_token(username), quote=True)}'><input type='hidden' name='id' value='{html.escape(feed['id'], quote=True)}'><button>Refresh now</button></form>"
                    f"<form class='inline' method='post' action='./delete'><input type='hidden' name='csrf' value='{html.escape(csrf_token(username), quote=True)}'><input type='hidden' name='id' value='{html.escape(feed['id'], quote=True)}'><button>Delete</button></form>"
                    "</div></div>"
                )
            body = (
                "<h1>YTPod administration</h1>"
                "<p>Feeds are private-by-URL. Pocket Casts should subscribe to the feed URL shown below.</p>"
                f"<div class='grid'>{''.join(cards) or '<div class=card><p>No feeds configured.</p></div>'}</div>"
                "<hr><h2>Add feed</h2>"
                f"<form method='post' action='./add'><input type='hidden' name='csrf' value='{html.escape(csrf_token(username), quote=True)}'>"
                "<label for='id'>Feed ID</label><input id='id' name='id' placeholder='newsroom' pattern='[a-z0-9][a-z0-9_-]{1,62}' required>"
                "<label for='name'>Feed name</label><input id='name' name='name' placeholder='Newsroom videos' required>"
                "<label for='source'>YouTube channel or playlist URL</label><input id='source' name='source' type='url' placeholder='https://www.youtube.com/@example/videos' required>"
                "<label for='keep_last'>Keep last episodes</label><input id='keep_last' name='keep_last' type='number' min='1' max='500' value='20' required>"
                "<label for='since'>Only download videos on/after</label><input id='since' name='since' type='date'>"
                "<label for='artwork_url'>Podcast artwork URL (optional)</label><input id='artwork_url' name='artwork_url' type='url' placeholder='https://example.org/artwork.jpg'>"
                "<p><button>Add feed</button></p></form>"
            )
            self._send(200, "text/html; charset=utf-8", html_page("Administration", body))
            return
        self._send(404, "text/plain; charset=utf-8", b"Not found")

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path
            if not path.startswith("/admin/"):
                self._send(404, "text/plain; charset=utf-8", b"Not found")
                return
            username = admin_username(self)
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            form = parse_form(body)
            if not valid_csrf(username, form.get("csrf", "")):
                self._send(403, "text/plain; charset=utf-8", b"Invalid CSRF token")
                return
            if path == "/admin/add":
                self._add_feed(form)
                redirect(self, "./")
                return
            if path == "/admin/delete":
                self._delete_feed(form.get("id", ""))
                redirect(self, "./")
                return
            if path == "/admin/refresh":
                feed_id = form.get("id", "")
                if feed_id not in {feed["id"] for feed in feeds()}:
                    raise AppError("Unknown feed")
                threading.Thread(target=refresh_feed, args=(feed_id,), daemon=True).start()
                redirect(self, "./")
                return
            self._send(404, "text/plain; charset=utf-8", b"Not found")
        except AppError as exc:
            self._send(400, "text/plain; charset=utf-8", str(exc).encode())
        except Exception as exc:  # pragma: no cover
            log(f"POST error: {exc}")
            self._send(500, "text/plain; charset=utf-8", b"Internal server error")

    def _add_feed(self, form: dict[str, str]) -> None:
        new_feed = normalize_feed(
            {
                "id": form.get("id", ""),
                "name": form.get("name", ""),
                "source": form.get("source", ""),
                "keep_last": form.get("keep_last", "20"),
                "since": form.get("since", ""),
                "artwork_url": form.get("artwork_url", ""),
            }
        )
        with CONFIG_LOCK:
            config = read_toml()
            if any(item.get("id") == new_feed["id"] for item in config.get("feeds", [])):
                raise AppError("A feed with that ID already exists.")
            config.setdefault("feeds", []).append(new_feed)
            write_toml(config)

    def _delete_feed(self, feed_id: str) -> None:
        if not FEED_ID_RE.fullmatch(feed_id):
            raise AppError("Invalid feed ID.")
        with CONFIG_LOCK:
            config = read_toml()
            old = config.get("feeds", [])
            config["feeds"] = [item for item in old if str(item.get("id", "")) != feed_id]
            if len(config["feeds"]) == len(old):
                raise AppError("Unknown feed")
            write_toml(config)
        state = load_state(feed_id)
        for item in state.get("items", {}).values():
            filename = str(item.get("file", ""))
            if filename:
                try:
                    (MEDIA_DIR / filename).unlink(missing_ok=True)
                except OSError:
                    pass
        try:
            state_path(feed_id).unlink(missing_ok=True)
        except OSError:
            pass


def serve(bind: str, port: int) -> None:
    ensure_directories()
    ensure_secret()
    threading.Thread(target=scheduler, daemon=True, name="scheduler").start()
    server = ThreadingHTTPServer((bind, port), Handler)
    log(f"listening on {bind}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8746)
    parser.add_argument("--refresh", metavar="FEED_ID")
    args = parser.parse_args()
    if args.refresh:
        refresh_feed(args.refresh)
        return
    if args.serve:
        serve(args.bind, args.port)
        return
    parser.error("Use --serve or --refresh FEED_ID")


if __name__ == "__main__":
    main()
