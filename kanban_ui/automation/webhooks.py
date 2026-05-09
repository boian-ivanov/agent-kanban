"""HTTP webhook notifications.

Config lives in ``KANBAN_WEBHOOKS_FILE`` (default: ``kanban_data/webhooks.json``)::

    {
      "webhooks": [
        {
          "name": "Slack #dev",
          "url": "https://hooks.slack.com/services/...",
          "events": ["task_created", "task_moved", "task_commented"],
          "project_id": null,                     // null = all projects
          "format": "slack",                      // generic | slack | telegram
          "enabled": true
        },
        {
          "name": "Telegram bot",
          "url": "https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>",
          "events": ["task_moved"],
          "format": "telegram"
        }
      ]
    }

Supported events: ``task_created``, ``task_moved``, ``task_commented``,
``task_updated``. The emitter is invoked from the corresponding endpoints
in ``main.py`` via ``emit_event(...)``.

Hot-reload by file mtime. Every delivery runs as a fire-and-forget asyncio
task — if a webhook fails or responds slowly, the user's HTTP request is
not blocked. Delivery logs are available via ``/api/automation/status``.

Formats:
- ``generic``  — POST the raw JSON payload (event, task, ...).
- ``slack``    — POST ``{"text": "..."}`` for Slack Incoming Webhooks.
- ``telegram`` — POST ``{"text": "..."}`` for the Telegram Bot API
                 (chat_id is passed in the URL).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("kanban.automation.webhooks")

VALID_EVENTS = {"task_created", "task_moved", "task_commented", "task_updated"}
VALID_FORMATS = {"generic", "slack", "telegram"}

_status: dict[str, Any] = {
    "running": False,
    "config_file": None,
    "webhooks_loaded": 0,
    "last_emit_at": None,
    "delivered_total": 0,
    "failed_total": 0,
    "last_deliveries": [],   # last 20: {ts, name, event, status_code, ms}
    "last_errors": [],       # last 10: {ts, name, error}
    "config_mtime": None,
}


def webhooks_status() -> dict[str, Any]:
    return dict(_status)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Format adapters
# ---------------------------------------------------------------------------


def _short_title(t: dict[str, Any], n: int = 60) -> str:
    title = t.get("title", "")
    if len(title) > n:
        title = title[:n - 1] + "…"
    return title


def _fmt_text(event: str, payload: dict[str, Any]) -> str:
    """Human-readable line for slack/telegram."""
    t = payload.get("task") or {}
    project = (payload.get("project") or {}).get("name") or t.get("project_id", "?")
    title = _short_title(t)
    if event == "task_created":
        return f"+ [{project}] {t.get('id', '?')} \"{title}\" -> {t.get('status')}"
    if event == "task_moved":
        return (
            f"-> [{project}] {t.get('id', '?')} \"{title}\": "
            f"{payload.get('from_status')} -> {payload.get('to_status')}"
            + (f" - {payload.get('comment')}" if payload.get("comment") else "")
        )
    if event == "task_commented":
        return (
            f"[comment] [{project}] {t.get('id', '?')} \"{title}\": "
            f"{payload.get('comment', '')[:120]}"
        )
    if event == "task_updated":
        fields = payload.get("changed_fields") or []
        return f"[edit] [{project}] {t.get('id', '?')} \"{title}\" updated: {', '.join(fields) or '-'}"
    return f"[{project}] {event}: {t.get('id', '?')} \"{title}\""


def _build_body(format_: str, event: str, payload: dict[str, Any]) -> dict[str, Any]:
    if format_ == "slack":
        return {"text": _fmt_text(event, payload)}
    if format_ == "telegram":
        # chat_id must be in the URL (?chat_id=...). The text intentionally
        # avoids emoji so it stays parse_mode-independent.
        return {"text": _fmt_text(event, payload)}
    # generic — forward whatever we received
    return {
        "event": event,
        "ts": _now(),
        **payload,
    }


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class WebhookDispatcher:
    def __init__(self, config_file: Path):
        self.config_file = config_file
        self._mtime: float | None = None
        self._hooks: list[dict[str, Any]] = []
        # single client for the whole lifecycle — connection pooling
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _maybe_reload(self) -> None:
        if not self.config_file.exists():
            self._hooks = []
            self._mtime = None
            _status["webhooks_loaded"] = 0
            _status["config_mtime"] = None
            return
        mtime = self.config_file.stat().st_mtime
        if mtime == self._mtime:
            return
        try:
            data = json.loads(self.config_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            _push_error("_config_", f"webhooks.json invalid JSON: {e}")
            return
        hooks = data.get("webhooks", [])
        if not isinstance(hooks, list):
            _push_error("_config_", "'webhooks' must be a list")
            return
        valid: list[dict[str, Any]] = []
        for i, h in enumerate(hooks):
            err = _validate_hook(h, i)
            if err:
                _push_error(h.get("name", f"#{i}"), err)
                continue
            if h.get("enabled", True):
                valid.append(h)
        self._hooks = valid
        self._mtime = mtime
        _status["webhooks_loaded"] = len(valid)
        _status["config_mtime"] = datetime.fromtimestamp(
            mtime, tz=timezone.utc
        ).isoformat(timespec="seconds")
        log.info("webhooks.json reloaded: %d active hooks", len(valid))

    async def emit(self, event: str, payload: dict[str, Any]) -> None:
        """Does not block the caller: each webhook is dispatched as a separate task."""
        if event not in VALID_EVENTS:
            log.warning("unknown event: %s", event)
            return
        self._maybe_reload()
        if not self._hooks:
            return
        proj_id = (payload.get("task") or {}).get("project_id")
        for hook in self._hooks:
            if event not in hook.get("events", []):
                continue
            target_pid = hook.get("project_id")
            if target_pid is not None and target_pid != proj_id:
                continue
            asyncio.create_task(self._deliver(hook, event, payload))
        _status["last_emit_at"] = _now()

    async def _deliver(
        self, hook: dict[str, Any], event: str, payload: dict[str, Any]
    ) -> None:
        name = hook.get("name", "?")
        url = hook["url"]
        format_ = hook.get("format", "generic")
        body = _build_body(format_, event, payload)
        started = datetime.now(timezone.utc)
        try:
            client = await self._ensure_client()
            r = await client.post(url, json=body)
            ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            _push_delivery(name, event, r.status_code, ms)
            if r.status_code >= 400:
                _push_error(name, f"{event} → HTTP {r.status_code}: {r.text[:120]}")
                _status["failed_total"] += 1
            else:
                _status["delivered_total"] += 1
        except httpx.HTTPError as e:
            _push_error(name, f"{event}: {e}")
            _status["failed_total"] += 1


def _validate_hook(h: dict[str, Any], idx: int) -> str | None:
    if not isinstance(h, dict):
        return f"#{idx}: not an object"
    if not h.get("url"):
        return "missing 'url'"
    if not isinstance(h.get("events"), list) or not h["events"]:
        return "'events' must be non-empty list"
    for ev in h["events"]:
        if ev not in VALID_EVENTS:
            return f"unknown event '{ev}' (valid: {sorted(VALID_EVENTS)})"
    fmt = h.get("format", "generic")
    if fmt not in VALID_FORMATS:
        return f"unknown format '{fmt}' (valid: {sorted(VALID_FORMATS)})"
    return None


def _push_delivery(name: str, event: str, status_code: int, ms: int) -> None:
    _status["last_deliveries"].insert(0, {
        "ts": _now(), "name": name, "event": event,
        "status_code": status_code, "ms": ms,
    })
    _status["last_deliveries"] = _status["last_deliveries"][:20]


def _push_error(name: str, error: str) -> None:
    _status["last_errors"].insert(0, {
        "ts": _now(), "name": name, "error": error,
    })
    _status["last_errors"] = _status["last_errors"][:10]


# ---------------------------------------------------------------------------
# Singleton glue (set up by the FastAPI lifespan, used by the endpoints)
# ---------------------------------------------------------------------------

_dispatcher: WebhookDispatcher | None = None


def init_dispatcher(config_file: Path) -> WebhookDispatcher:
    global _dispatcher
    _dispatcher = WebhookDispatcher(config_file)
    _status["running"] = True
    _status["config_file"] = str(config_file)
    log.info("webhook dispatcher initialised: %s", config_file)
    return _dispatcher


async def shutdown_dispatcher() -> None:
    global _dispatcher
    if _dispatcher:
        await _dispatcher.aclose()
        _dispatcher = None
        _status["running"] = False


async def emit_event(event: str, payload: dict[str, Any]) -> None:
    """Convenience: emit without blocking. Safe if the dispatcher is not initialised."""
    if _dispatcher is None:
        return
    try:
        await _dispatcher.emit(event, payload)
    except Exception:
        log.exception("emit_event failed for %s", event)
