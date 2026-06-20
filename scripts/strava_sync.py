#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}
DEFAULT_SCOPE = "read,activity:read_all"
DEFAULT_INITIAL_DAYS = 30
DEFAULT_PER_PAGE = 200
READ_BUDGET_SAFETY = 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync Strava running activities into the local vault."
    )
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent.parent),
        help="Vault root. Defaults to the parent directory of this script.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    auth_parser = subparsers.add_parser("auth", help="Authorize this vault with Strava.")
    auth_parser.add_argument("--client-id", help="Strava application client ID.")
    auth_parser.add_argument("--client-secret", help="Strava application client secret.")
    auth_parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Local callback port. The callback domain in Strava should allow localhost.",
    )
    auth_parser.add_argument(
        "--scope",
        default=DEFAULT_SCOPE,
        help=f"Comma-separated Strava OAuth scopes. Default: {DEFAULT_SCOPE}",
    )

    sync_parser = subparsers.add_parser(
        "sync",
        help="Refresh activities from Strava and regenerate weekly markdown files.",
    )
    sync_parser.add_argument(
        "--after",
        help="Only request activities on or after YYYY-MM-DD. Overrides cached sync state.",
    )
    sync_parser.add_argument(
        "--initial-days",
        type=int,
        default=DEFAULT_INITIAL_DAYS,
        help=(
            "When no prior record exists, only import this many recent days. "
            f"Default: {DEFAULT_INITIAL_DAYS}"
        ),
    )

    subparsers.add_parser(
        "render", help="Regenerate markdown files from the cached Strava activity JSON."
    )
    subparsers.add_parser("status", help="Show the current cache and auth status.")

    return parser.parse_args()


@dataclass
class Paths:
    root: Path

    @property
    def cache_dir(self) -> Path:
        return self.root / ".strava-sync"

    @property
    def credentials_path(self) -> Path:
        return self.cache_dir / "credentials.json"

    @property
    def state_path(self) -> Path:
        return self.cache_dir / "state.json"

    @property
    def activities_dir(self) -> Path:
        return self.cache_dir / "activities"

    @property
    def records_dir(self) -> Path:
        return self.root / "Records" / "Strava"


def ensure_dirs(paths: Paths) -> None:
    paths.cache_dir.mkdir(parents=True, exist_ok=True)
    paths.activities_dir.mkdir(parents=True, exist_ok=True)
    paths.records_dir.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, payload: Any, *, chmod_600: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    if chmod_600:
        path.chmod(0o600)


def now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def require_credentials(paths: Paths) -> Dict[str, Any]:
    credentials = load_json(paths.credentials_path, {})
    required = {"client_id", "client_secret", "refresh_token"}
    missing = sorted(key for key in required if not credentials.get(key))
    if missing:
        joined = ", ".join(missing)
        raise SystemExit(
            f"Missing credentials in {paths.credentials_path}: {joined}. "
            "Run `python3 scripts/strava_sync.py auth ...` first."
        )
    return credentials


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    auth_code: Optional[str] = None
    auth_error: Optional[str] = None
    expected_state: Optional[str] = None

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        state = params.get("state", [None])[0]
        error = params.get("error", [None])[0]
        code = params.get("code", [None])[0]
        handler = type(self)

        if state != handler.expected_state:
            self._send_page(400, "State mismatch. You can close this tab and retry.")
            handler.auth_error = "state_mismatch"
            return

        if error:
            handler.auth_error = error
            self._send_page(
                400,
                f"Authorization failed: {error}. You can close this tab and retry.",
            )
            return

        if not code:
            handler.auth_error = "missing_code"
            self._send_page(400, "No authorization code received.")
            return

        handler.auth_code = code
        self._send_page(200, "Strava authorization complete. You can close this tab.")

    def log_message(self, *_args: Any) -> None:
        return

    def _send_page(self, status: int, body: str) -> None:
        content = (
            "<html><body style='font-family: sans-serif; margin: 2rem;'>"
            f"<p>{body}</p></body></html>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


@dataclass
class RateLimitSnapshot:
    overall_limit_short: Optional[int] = None
    overall_limit_daily: Optional[int] = None
    overall_usage_short: Optional[int] = None
    overall_usage_daily: Optional[int] = None
    read_limit_short: Optional[int] = None
    read_limit_daily: Optional[int] = None
    read_usage_short: Optional[int] = None
    read_usage_daily: Optional[int] = None

    def remaining_read_requests(self) -> Optional[int]:
        remaining: List[int] = []

        if self.read_limit_short is not None and self.read_usage_short is not None:
            remaining.append(self.read_limit_short - self.read_usage_short)
        if self.read_limit_daily is not None and self.read_usage_daily is not None:
            remaining.append(self.read_limit_daily - self.read_usage_daily)
        if not remaining and self.overall_limit_short is not None and self.overall_usage_short is not None:
            remaining.append(self.overall_limit_short - self.overall_usage_short)
        if not remaining and self.overall_limit_daily is not None and self.overall_usage_daily is not None:
            remaining.append(self.overall_limit_daily - self.overall_usage_daily)

        if not remaining:
            return None
        return min(remaining)

    def summary_text(self) -> str:
        parts: List[str] = []
        if self.read_usage_short is not None and self.read_limit_short is not None:
            parts.append(f"read 15min {self.read_usage_short}/{self.read_limit_short}")
        if self.read_usage_daily is not None and self.read_limit_daily is not None:
            parts.append(f"read daily {self.read_usage_daily}/{self.read_limit_daily}")
        if not parts and self.overall_usage_short is not None and self.overall_limit_short is not None:
            parts.append(f"overall 15min {self.overall_usage_short}/{self.overall_limit_short}")
        if not parts and self.overall_usage_daily is not None and self.overall_limit_daily is not None:
            parts.append(f"overall daily {self.overall_usage_daily}/{self.overall_limit_daily}")
        return ", ".join(parts)


class RateLimitExceeded(RuntimeError):
    pass


class StravaClient:
    def __init__(self, paths: Paths, credentials: Dict[str, Any]):
        self.paths = paths
        self.credentials = credentials
        self.rate_limits: Optional[RateLimitSnapshot] = None

    def ensure_access_token(self) -> None:
        expires_at = int(self.credentials.get("expires_at") or 0)
        if expires_at > int(time.time()) + 120:
            return
        refreshed = self.exchange_token(refresh_token=self.credentials["refresh_token"])
        self._update_tokens(refreshed)

    def exchange_token(
        self,
        *,
        code: Optional[str] = None,
        refresh_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {
            "client_id": self.credentials["client_id"],
            "client_secret": self.credentials["client_secret"],
        }
        if code:
            payload.update({"code": code, "grant_type": "authorization_code"})
        elif refresh_token:
            payload.update({"refresh_token": refresh_token, "grant_type": "refresh_token"})
        else:
            raise ValueError("Either code or refresh_token must be provided.")

        return self._request_json(
            "POST",
            "https://www.strava.com/oauth/token",
            data=payload,
            authenticated=False,
        )

    def list_activities(
        self,
        after_epoch: Optional[int] = None,
        before_epoch: Optional[int] = None,
        *,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> List[Dict[str, Any]]:
        self.ensure_access_token()
        params: Dict[str, Any] = {"page": page, "per_page": per_page}
        if after_epoch is not None:
            params["after"] = after_epoch
        if before_epoch is not None:
            params["before"] = before_epoch
        return self._request_json(
            "GET",
            "https://www.strava.com/api/v3/athlete/activities",
            params=params,
        )

    def list_laps(self, activity_id: int) -> List[Dict[str, Any]]:
        self.ensure_access_token()
        return self._request_json(
            "GET",
            f"https://www.strava.com/api/v3/activities/{activity_id}/laps",
        )

    def remaining_read_budget(self, safety_margin: int = READ_BUDGET_SAFETY) -> Optional[int]:
        if not self.rate_limits:
            return None
        remaining = self.rate_limits.remaining_read_requests()
        if remaining is None:
            return None
        return remaining - safety_margin

    def next_short_reset_text(self) -> str:
        next_reset = ((int(time.time()) // 900) + 1) * 900
        return datetime.fromtimestamp(next_reset).strftime("%Y-%m-%d %H:%M:%S")

    def _update_tokens(self, token_payload: Dict[str, Any]) -> None:
        self.credentials.update(
            {
                "access_token": token_payload["access_token"],
                "refresh_token": token_payload["refresh_token"],
                "expires_at": token_payload["expires_at"],
                "athlete": token_payload.get("athlete", self.credentials.get("athlete")),
                "updated_at": now_iso(),
            }
        )
        save_json(self.paths.credentials_path, self.credentials, chmod_600=True)

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        authenticated: bool = True,
        retry_on_401: bool = True,
    ) -> Any:
        if params:
            encoded = urllib.parse.urlencode(params)
            url = f"{url}?{encoded}"

        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.credentials['access_token']}"

        payload = None
        if data is not None:
            payload = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                self._update_rate_limits(response.headers)
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            self._update_rate_limits(exc.headers)
            if authenticated and exc.code == 401 and retry_on_401:
                refreshed = self.exchange_token(refresh_token=self.credentials["refresh_token"])
                self._update_tokens(refreshed)
                return self._request_json(
                    method,
                    url,
                    authenticated=True,
                    retry_on_401=False,
                )
            if exc.code == 429:
                message = self._build_rate_limit_message(body)
                raise RateLimitExceeded(message) from exc
            raise SystemExit(f"Strava API request failed ({exc.code}): {body}") from exc
        except urllib.error.URLError as exc:
            raise SystemExit(f"Network error while talking to Strava: {exc}") from exc

    def _update_rate_limits(self, headers: Any) -> None:
        self.rate_limits = RateLimitSnapshot(
            overall_limit_short=parse_header_pair(headers, "X-RateLimit-Limit", 0),
            overall_limit_daily=parse_header_pair(headers, "X-RateLimit-Limit", 1),
            overall_usage_short=parse_header_pair(headers, "X-RateLimit-Usage", 0),
            overall_usage_daily=parse_header_pair(headers, "X-RateLimit-Usage", 1),
            read_limit_short=parse_header_pair(headers, "X-ReadRateLimit-Limit", 0),
            read_limit_daily=parse_header_pair(headers, "X-ReadRateLimit-Limit", 1),
            read_usage_short=parse_header_pair(headers, "X-ReadRateLimit-Usage", 0),
            read_usage_daily=parse_header_pair(headers, "X-ReadRateLimit-Usage", 1),
        )

    def _build_rate_limit_message(self, body: str) -> str:
        summary = self.rate_limits.summary_text() if self.rate_limits else ""
        reset_at = self.next_short_reset_text()
        pieces = ["Strava read rate limit reached."]
        if summary:
            pieces.append(summary + ".")
        pieces.append(f"Retry after the next 15-minute reset at about {reset_at}.")
        if body:
            pieces.append(f"Response: {body}")
        return " ".join(pieces)


def cmd_auth(paths: Paths, args: argparse.Namespace) -> None:
    ensure_dirs(paths)

    client_id = args.client_id or input("Strava client_id: ").strip()
    client_secret = args.client_secret or input("Strava client_secret: ").strip()
    if not client_id or not client_secret:
        raise SystemExit("Both client_id and client_secret are required.")

    credentials = load_json(paths.credentials_path, {})
    credentials.update({"client_id": client_id, "client_secret": client_secret})
    save_json(paths.credentials_path, credentials, chmod_600=True)

    OAuthCallbackHandler.auth_code = None
    OAuthCallbackHandler.auth_error = None
    OAuthCallbackHandler.expected_state = secrets.token_urlsafe(16)

    redirect_uri = f"http://localhost:{args.port}/exchange_token"
    auth_query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "approval_prompt": "auto",
            "scope": args.scope,
            "state": OAuthCallbackHandler.expected_state,
        }
    )
    auth_url = f"https://www.strava.com/oauth/authorize?{auth_query}"

    try:
        server = HTTPServer(("127.0.0.1", args.port), OAuthCallbackHandler)
    except OSError as exc:
        raise SystemExit(
            f"Could not bind localhost:{args.port}. Try a different --port value."
        ) from exc

    server.timeout = 1
    print("Opening the Strava authorization page in your browser...")
    print(f"If it does not open automatically, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    deadline = time.time() + 300
    try:
        while time.time() < deadline:
            server.handle_request()
            if OAuthCallbackHandler.auth_code or OAuthCallbackHandler.auth_error:
                break
    finally:
        server.server_close()

    if OAuthCallbackHandler.auth_error:
        raise SystemExit(f"Strava authorization failed: {OAuthCallbackHandler.auth_error}")
    if not OAuthCallbackHandler.auth_code:
        raise SystemExit("Timed out waiting for Strava authorization.")

    client = StravaClient(paths, credentials)
    token_payload = client.exchange_token(code=OAuthCallbackHandler.auth_code)
    client._update_tokens(token_payload)
    athlete = token_payload.get("athlete") or {}
    athlete_name = " ".join(
        part for part in [athlete.get("firstname"), athlete.get("lastname")] if part
    ).strip()
    print(f"Authorization complete for athlete: {athlete_name or athlete.get('id')}")
    print(f"Credentials saved to {paths.credentials_path}")


def cmd_sync(paths: Paths, args: argparse.Namespace) -> None:
    ensure_dirs(paths)
    credentials = require_credentials(paths)
    client = StravaClient(paths, credentials)
    state = load_json(paths.state_path, {})

    latest_cached = latest_cached_record(paths)
    latest_cached_epoch = (
        int(state["latest_record_epoch"])
        if state.get("latest_record_epoch")
        else (latest_cached["epoch"] if latest_cached else None)
    )

    if args.after:
        after_epoch = parse_after_date(args.after)
        sync_basis = f"custom after {args.after}"
    elif latest_cached_epoch is not None:
        after_epoch = latest_cached_epoch - 1
        sync_basis = "incremental after latest cached record"
    else:
        after_epoch = int(time.time()) - args.initial_days * 86400
        sync_basis = f"initial bootstrap, last {args.initial_days} days"

    queue: List[Dict[str, Any]] = []
    queued_ids = set()
    pending_summaries = state.get("pending_summaries") or []

    def enqueue(summary: Dict[str, Any], *, force: bool = False) -> None:
        activity_id = summary.get("id")
        if not activity_id or activity_id in queued_ids:
            return
        if summary.get("type") not in RUN_TYPES:
            return
        if force or should_sync_summary(paths, summary):
            queue.append(summary)
            queued_ids.add(activity_id)

    for summary in pending_summaries:
        enqueue(summary, force=True)

    summaries = fetch_activity_window(client, after_epoch=after_epoch)
    run_summaries = [item for item in summaries if item.get("type") in RUN_TYPES]
    for summary in run_summaries:
        enqueue(summary)

    print(
        f"Fetched {len(summaries)} activities from Strava "
        f"({len(run_summaries)} running activities, {len(queue)} queued to sync)."
    )
    print(f"Sync basis: {sync_basis}")

    written = 0
    stop_reason: Optional[str] = None
    remaining_queue: List[Dict[str, Any]] = []

    for index, summary in enumerate(queue):
        remaining_budget = client.remaining_read_budget()
        if remaining_budget is not None and remaining_budget <= 0:
            remaining_queue = queue[index:]
            stop_reason = (
                "Read budget nearly exhausted. "
                f"Re-run sync after about {client.next_short_reset_text()}."
            )
            break

        activity_id = summary["id"]
        try:
            laps = client.list_laps(activity_id)
        except RateLimitExceeded as exc:
            remaining_queue = queue[index:]
            stop_reason = str(exc)
            break

        payload = {
            "synced_at": now_iso(),
            "summary": summary,
            "detail": summary,
            "laps": laps,
        }
        save_json(activity_cache_path(paths, activity_id), payload)
        written += 1

    if not remaining_queue and written < len(queue):
        remaining_queue = queue[written:]

    if remaining_queue:
        state["pending_summaries"] = remaining_queue
    else:
        state.pop("pending_summaries", None)

    state.update(
        {
            "last_sync_at": now_iso(),
            "last_sync_epoch": int(time.time()),
            "last_summary_count": len(summaries),
            "last_run_count": len(run_summaries),
            "pending_run_count": len(remaining_queue),
        }
    )
    if not remaining_queue:
        state.pop("pending_summaries", None)

    latest_after_sync = latest_cached_record(paths)
    if latest_after_sync:
        state["latest_record_epoch"] = latest_after_sync["epoch"]
        state["latest_record_local"] = latest_after_sync["local"]

    save_json(paths.state_path, state)

    render_records(paths)
    print(f"Updated {written} cached running activities.")
    if client.rate_limits and client.rate_limits.summary_text():
        print(f"Rate limit usage: {client.rate_limits.summary_text()}")
    if stop_reason:
        print(stop_reason)
        print(
            f"{len(remaining_queue)} activities are still queued and will resume on the next "
            "successful sync."
        )
    print(f"Rendered weekly markdown files to {paths.records_dir}")


def cmd_render(paths: Paths) -> None:
    ensure_dirs(paths)
    render_records(paths)
    print(f"Rendered weekly markdown files to {paths.records_dir}")


def cmd_status(paths: Paths) -> None:
    credentials = load_json(paths.credentials_path, {})
    state = load_json(paths.state_path, {})
    activity_count = len(list(paths.activities_dir.glob("*.json"))) if paths.activities_dir.exists() else 0
    latest_cached = latest_cached_record(paths)

    print(f"Vault root: {paths.root}")
    print(f"Credentials file: {paths.credentials_path}")
    print(f"Cached activities: {activity_count}")

    athlete = credentials.get("athlete") or {}
    if credentials.get("client_id"):
        athlete_name = " ".join(
            part for part in [athlete.get("firstname"), athlete.get("lastname")] if part
        ).strip()
        print(f"Authorized athlete: {athlete_name or athlete.get('id', 'unknown')}")
        print(f"Token expires at epoch: {credentials.get('expires_at', 'unknown')}")
    else:
        print("Authorized athlete: not configured")

    if state.get("last_sync_at"):
        print(f"Last sync at: {state['last_sync_at']}")
        print(f"Last running activities fetched: {state.get('last_run_count', 0)}")
        print(f"Pending running activities: {state.get('pending_run_count', 0)}")
    else:
        print("Last sync at: never")

    if state.get("latest_record_local"):
        print(f"Latest cached record: {state['latest_record_local']}")
    elif latest_cached:
        print(f"Latest cached record: {latest_cached['local']}")
    else:
        print("Latest cached record: none")


def parse_header_pair(headers: Any, name: str, index: int) -> Optional[int]:
    value = headers.get(name) if headers else None
    if not value:
        return None
    parts = [part.strip() for part in value.split(",")]
    if len(parts) <= index:
        return None
    try:
        return int(parts[index])
    except ValueError:
        return None


def activity_epoch(activity: Dict[str, Any]) -> int:
    timestamp = activity.get("start_date") or activity.get("start_date_local")
    if not timestamp:
        raise ValueError(f"Activity {activity.get('id')} is missing a timestamp.")
    return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp())


def activity_cache_path(paths: Paths, activity_id: int) -> Path:
    return paths.activities_dir / f"{activity_id}.json"


def should_sync_summary(paths: Paths, summary: Dict[str, Any]) -> bool:
    cache_path = activity_cache_path(paths, int(summary["id"]))
    return not cache_path.exists()


def fetch_activity_window(
    client: StravaClient,
    *,
    after_epoch: Optional[int] = None,
    before_epoch: Optional[int] = None,
) -> List[Dict[str, Any]]:
    summaries: List[Dict[str, Any]] = []
    page = 1

    while True:
        batch = client.list_activities(
            after_epoch=after_epoch,
            before_epoch=before_epoch,
            page=page,
            per_page=DEFAULT_PER_PAGE,
        )
        if not batch:
            break
        summaries.extend(batch)
        if len(batch) < DEFAULT_PER_PAGE:
            break
        page += 1

    return summaries


def latest_cached_record(paths: Paths) -> Optional[Dict[str, Any]]:
    latest_epoch: Optional[int] = None
    latest_local: Optional[str] = None

    for activity_file in paths.activities_dir.glob("*.json"):
        payload = load_json(activity_file, {})
        detail = payload.get("detail") or payload.get("summary") or {}
        if detail.get("type") not in RUN_TYPES:
            continue
        epoch = activity_epoch(detail)
        local = detail.get("start_date_local") or detail.get("start_date") or ""
        if latest_epoch is None or epoch > latest_epoch:
            latest_epoch = epoch
            latest_local = local

    if latest_epoch is None or latest_local is None:
        return None
    return {"epoch": latest_epoch, "local": latest_local}


def render_records(paths: Paths) -> None:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for activity_file in sorted(paths.activities_dir.glob("*.json")):
        payload = load_json(activity_file, {})
        detail = payload.get("detail") or {}
        if detail.get("type") not in RUN_TYPES:
            continue
        week_key = build_week_key(detail)
        groups.setdefault(week_key, []).append(payload)

    for existing_file in paths.records_dir.glob("*.md"):
        existing_file.unlink()

    for week_key, activities in groups.items():
        activities.sort(key=lambda item: (activity_local_date(item), item.get("detail", {}).get("start_date")))
        rendered = render_week_markdown(week_key, activities)
        target_path = paths.records_dir / f"{week_key}.md"
        target_path.write_text(rendered, encoding="utf-8")


def build_week_key(activity: Dict[str, Any]) -> str:
    date_text = local_date_text(activity)
    year, month, day = [int(part) for part in date_text.split("-")]
    week_number = ((day - 1) // 7) + 1
    return f"{year:04d}-{month:02d}-W{week_number}"


def local_date_text(activity: Dict[str, Any]) -> str:
    if activity.get("start_date_local"):
        return activity["start_date_local"][:10]
    if activity.get("start_date"):
        return activity["start_date"][:10]
    raise ValueError(f"Activity {activity.get('id')} is missing a start date.")


def activity_local_date(payload: Dict[str, Any]) -> str:
    detail = payload.get("detail") or {}
    return local_date_text(detail)


def render_week_markdown(week_key: str, activities: Iterable[Dict[str, Any]]) -> str:
    lines = [
        "<!-- Auto-generated by scripts/strava_sync.py. Edit the source in Strava or rerun sync. -->",
        f"# {week_key}",
        "",
    ]

    first = True
    for payload in activities:
        if not first:
            lines.append("")
            lines.append("")
        first = False
        lines.extend(render_activity_block(payload))

    lines.append("")
    return "\n".join(lines)


def render_activity_block(payload: Dict[str, Any]) -> List[str]:
    detail = payload.get("detail") or {}
    laps = payload.get("laps") or []

    title = build_activity_title(detail)
    lines = [title, ""]

    summary_bits = [
        f"Type: {detail.get('type', 'Run')}",
        f"Avg HR: {format_number(detail.get('average_heartrate'))}",
        f"Max HR: {format_number(detail.get('max_heartrate'))}",
        f"Elevation Gain: {format_number(detail.get('total_elevation_gain'))} m",
    ]
    lines.append(" | ".join(summary_bits))
    lines.append("")

    if laps:
        lines.extend(render_lap_table(detail, laps))
    else:
        lines.append("No lap data returned by Strava for this activity.")

    return lines


def render_lap_table(detail: Dict[str, Any], laps: List[Dict[str, Any]]) -> List[str]:
    lines = [
        "| Lap | Distance (km) | Time | Pace | Avg HR | Elev Gain (m) | Cadence |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]

    for index, lap in enumerate(laps, start=1):
        distance = meters_to_km(lap.get("distance"))
        moving_time = lap.get("moving_time") or lap.get("elapsed_time")
        row = [
            str(index),
            f"{distance:.2f}" if distance is not None else "",
            format_duration(moving_time),
            format_pace(lap.get("distance"), moving_time),
            format_number(lap.get("average_heartrate")),
            format_number(lap.get("total_elevation_gain")),
            format_number(lap.get("average_cadence")),
        ]
        lines.append(f"| {' | '.join(row)} |")

    summary_row = [
        "Summary",
        f"{meters_to_km(detail.get('distance')):.2f}" if detail.get("distance") is not None else "",
        format_duration(detail.get("moving_time") or detail.get("elapsed_time")),
        format_pace(detail.get("distance"), detail.get("moving_time") or detail.get("elapsed_time")),
        format_number(detail.get("average_heartrate")),
        format_number(detail.get("total_elevation_gain")),
        format_number(detail.get("average_cadence")),
    ]
    lines.append(f"| {' | '.join(summary_row)} |")
    return lines


def build_activity_title(detail: Dict[str, Any]) -> str:
    date_text = local_date_text(detail)
    activity_date = datetime.strptime(date_text, "%Y-%m-%d")
    label = f"{ordinal(activity_date.day)} {activity_date.strftime('%B')}"
    distance = meters_to_km(detail.get("distance"))
    moving_time = detail.get("moving_time") or detail.get("elapsed_time")
    pace = format_pace(detail.get("distance"), moving_time)
    name = detail.get("name") or detail.get("type") or "Run"
    distance_text = f"{distance:.2f} km" if distance is not None else "--"
    return f"{label} -- {name} ({distance_text} / {format_duration(moving_time)} / {pace})"


def parse_after_date(value: str) -> int:
    try:
        dt = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit("--after must be in YYYY-MM-DD format.") from exc
    return int(dt.timestamp())


def ordinal(day: int) -> str:
    if 10 <= day % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def meters_to_km(value: Any) -> Optional[float]:
    if value is None:
        return None
    return float(value) / 1000.0


def format_duration(value: Any) -> str:
    if value in (None, ""):
        return ""
    total_seconds = int(round(float(value)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_pace(distance_m: Any, seconds: Any) -> str:
    if not distance_m or not seconds:
        return ""
    distance_m = float(distance_m)
    seconds = float(seconds)
    if distance_m <= 0 or seconds <= 0:
        return ""
    sec_per_km = seconds / (distance_m / 1000.0)
    minutes = int(sec_per_km // 60)
    secs = int(round(sec_per_km % 60))
    if secs == 60:
        minutes += 1
        secs = 0
    return f"{minutes}'{secs:02d}\""


def format_number(value: Any) -> str:
    if value in (None, ""):
        return ""
    number = float(value)
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return f"{number:.2f}"


def main() -> None:
    args = parse_args()
    paths = Paths(root=Path(args.root).resolve())

    if args.command == "auth":
        cmd_auth(paths, args)
    elif args.command == "sync":
        cmd_sync(paths, args)
    elif args.command == "render":
        cmd_render(paths)
    elif args.command == "status":
        cmd_status(paths)
    else:
        raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
