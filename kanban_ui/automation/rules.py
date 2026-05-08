"""Rule engine: применяет правила из ``KANBAN_RULES_FILE``.

Файл ``rules.json``::

    {
      "rules": [
        {
          "name": "Закрытые задачи в архив через 30 дней",
          "enabled": true,
          "project_id": null,
          "trigger": {"type": "task_idle", "status": "done", "days": 30},
          "action":  {"type": "move_to", "status": "cancelled",
                      "comment": "Авто-архив: 30 дней в done"}
        }
      ]
    }

Triggers:
    - task_idle: status=X, days=N — задачи в колонке X дольше N дней
    - task_count_in_status: status=X, gt=N | lt=N — счётчик задач в колонке

Actions:
    - move_to: status=X, comment? — переместить в колонку
    - add_comment: comment — записать в history
    - set_priority: priority=high|normal|low, comment?

Все мутации — actor=automation. Hot-reload по mtime файла.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from kanban_store import Store, STATUSES

log = logging.getLogger("kanban.automation.rules")

DEFAULT_INTERVAL = float(os.environ.get("KANBAN_AUTOMATION_INTERVAL", "60"))

VALID_TRIGGERS = {"task_idle", "task_count_in_status", "task_moved"}
VALID_ACTIONS = {"move_to", "add_comment", "set_priority", "run_command"}
VALID_PRIORITIES = {"high", "normal", "low"}

# Polling-mode триггеры обрабатываются в _run_once.
# Reactive-mode триггеры обрабатываются в emit_rule_event() — вызывается
# из endpoint'ов сразу после события, без дополнительного polling-задержки.
REACTIVE_TRIGGERS = {"task_moved"}

_status: dict[str, Any] = {
    "running": False,
    "rules_file": None,
    "interval_sec": DEFAULT_INTERVAL,
    "rules_loaded": 0,
    "last_run_at": None,
    "last_run_actions": [],     # actions последнего прогона: {ts, rule, task_id, action}
    "last_reactive": [],        # реактивные триггеры (task_moved): {ts, rule, task_id, action}
    "last_errors": [],          # ошибки парсинга или применения: {ts, rule, error}
    "config_mtime": None,
}


def rules_status() -> dict[str, Any]:
    return dict(_status)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str) -> datetime:
    # ISO с offset (например "2026-05-08T13:38:14+00:00") или без — оба ОК.
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _validate_rule(rule: dict[str, Any], idx: int) -> list[str]:
    errs = []
    name = rule.get("name", f"#{idx}")
    if "trigger" not in rule or not isinstance(rule["trigger"], dict):
        errs.append(f"{name}: missing 'trigger' object")
        return errs
    if "action" not in rule or not isinstance(rule["action"], dict):
        errs.append(f"{name}: missing 'action' object")
        return errs
    t = rule["trigger"]
    a = rule["action"]
    if t.get("type") not in VALID_TRIGGERS:
        errs.append(f"{name}: trigger.type must be one of {sorted(VALID_TRIGGERS)}")
    else:
        if t["type"] == "task_idle":
            if t.get("status") not in STATUSES:
                errs.append(f"{name}: trigger.status invalid")
            if not isinstance(t.get("days"), (int, float)) or t["days"] < 0:
                errs.append(f"{name}: trigger.days must be a non-negative number")
        elif t["type"] == "task_count_in_status":
            if t.get("status") not in STATUSES:
                errs.append(f"{name}: trigger.status invalid")
            if "gt" not in t and "lt" not in t:
                errs.append(f"{name}: trigger needs 'gt' or 'lt'")
        elif t["type"] == "task_moved":
            # обязательно to_status, опционально from_status и project_id
            if t.get("to_status") not in STATUSES:
                errs.append(f"{name}: task_moved.to_status invalid")
            if t.get("from_status") is not None and t.get("from_status") not in STATUSES:
                errs.append(f"{name}: task_moved.from_status invalid")
    if a.get("type") not in VALID_ACTIONS:
        errs.append(f"{name}: action.type must be one of {sorted(VALID_ACTIONS)}")
    else:
        if a["type"] == "move_to" and a.get("status") not in STATUSES:
            errs.append(f"{name}: action.status invalid")
        if a["type"] == "add_comment" and not a.get("comment"):
            errs.append(f"{name}: action.comment required")
        if a["type"] == "set_priority" and a.get("priority") not in VALID_PRIORITIES:
            errs.append(f"{name}: action.priority invalid")
        if a["type"] == "run_command":
            if not a.get("cmd"):
                errs.append(f"{name}: run_command.cmd required (path to executable)")
    return errs


def _load_rules(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.exists():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [], [f"rules.json invalid JSON: {e}"]
    rules = data.get("rules", [])
    if not isinstance(rules, list):
        return [], ["rules.json: 'rules' must be a list"]
    errs: list[str] = []
    for i, r in enumerate(rules):
        errs.extend(_validate_rule(r, i))
    if errs:
        return [], errs
    return [r for r in rules if r.get("enabled", True)], []


def _matching_tasks(store: Store, rule: dict[str, Any]) -> list[Any]:
    t = rule["trigger"]
    project_id = rule.get("project_id")
    tasks = store.list_tasks(
        status=t["status"],
        project_id=project_id,
    )
    if t["type"] == "task_idle":
        cutoff = _now() - timedelta(days=float(t["days"]))
        return [task for task in tasks if _parse_iso(task.moved_at) < cutoff]
    elif t["type"] == "task_count_in_status":
        n = len(tasks)
        if "gt" in t and n > t["gt"]:
            return tasks
        if "lt" in t and n < t["lt"]:
            return tasks
        return []
    return []


def _apply_action(
    store: Store,
    task_id: str,
    action: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> str:
    """Применяет action, возвращает короткое описание для лога.

    ``context`` — опциональный dict с данными для подстановки placeholders
    в run_command (task_id, project_id, from_status, to_status, title).
    """
    a_type = action["type"]
    comment = action.get("comment", "")
    if a_type == "move_to":
        store.move_task(
            task_id,
            action["status"],
            actor="automation",
            comment=comment or None,
        )
        return f"move_to {action['status']}"
    elif a_type == "add_comment":
        store.add_comment(task_id, comment, actor="automation")
        return f"comment: {comment[:40]}"
    elif a_type == "set_priority":
        store.update_fields(
            task_id,
            actor="automation",
            priority=action["priority"],
        )
        if comment:
            store.add_comment(task_id, comment, actor="automation")
        return f"priority -> {action['priority']}"
    elif a_type == "run_command":
        # Запуск в фоне, не ждём результата. context подставляется в args
        # как {task_id}, {project_id}, {from_status}, {to_status}, {title}.
        # Логи команды могут писать в "log_file" если указан.
        # action.env (опц.) — словарь env-переменных для subprocess.
        ctx = dict(context or {})
        ctx.setdefault("task_id", task_id)
        cmd = action["cmd"]
        raw_args = action.get("args", [])
        try:
            args = [str(a).format(**ctx) for a in raw_args]
        except KeyError as e:
            return f"run_command: missing placeholder {e}"
        log_file = action.get("log_file")
        env_extra = action.get("env") or {}
        if not isinstance(env_extra, dict):
            return "run_command: 'env' must be an object"
        asyncio.create_task(_spawn_command(cmd, args, log_file, ctx, env_extra))
        return f"run_command {Path(cmd).name} {args}"
    return f"unknown action {a_type}"


async def _spawn_command(
    cmd: str,
    args: list[str],
    log_file: str | None,
    ctx: dict[str, Any],
    env_extra: dict[str, Any] | None = None,
) -> None:
    """Запускает subprocess в фоне, не блокируя вызывающую корутину."""
    try:
        kw: dict[str, Any] = {}
        if log_file:
            log_path = Path(log_file).expanduser()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            f = open(log_path, "ab")
            kw["stdout"] = f
            kw["stderr"] = asyncio.subprocess.STDOUT
        else:
            kw["stdout"] = asyncio.subprocess.DEVNULL
            kw["stderr"] = asyncio.subprocess.DEVNULL
        if env_extra:
            merged_env = {**os.environ, **{str(k): str(v) for k, v in env_extra.items()}}
            kw["env"] = merged_env
        proc = await asyncio.create_subprocess_exec(cmd, *args, **kw)
        log.info(
            "run_command spawned: pid=%d cmd=%s args=%s ctx=%s",
            proc.pid, cmd, args, ctx,
        )
        # Не ждём завершения — agent-launcher может работать минуты/часы.
    except FileNotFoundError:
        log.error("run_command: cmd not found: %s", cmd)
        _status["last_errors"].insert(
            0, {"ts": _now().isoformat(timespec="seconds"),
                "rule": "run_command", "error": f"executable not found: {cmd}"}
        )
        _status["last_errors"] = _status["last_errors"][:10]
    except Exception as e:
        log.exception("run_command failed: %s", cmd)
        _status["last_errors"].insert(
            0, {"ts": _now().isoformat(timespec="seconds"),
                "rule": "run_command", "error": f"{cmd}: {e}"}
        )
        _status["last_errors"] = _status["last_errors"][:10]


def _run_once(store: Store, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions_log: list[dict[str, Any]] = []
    for rule in rules:
        # reactive-rules не запускаются по таймеру — только через emit_rule_event
        if rule["trigger"]["type"] in REACTIVE_TRIGGERS:
            continue
        name = rule.get("name", "?")
        try:
            tasks = _matching_tasks(store, rule)
        except Exception as e:
            log.exception("rule '%s' selection failed", name)
            _status["last_errors"].insert(
                0, {"ts": _now().isoformat(timespec="seconds"), "rule": name, "error": str(e)}
            )
            _status["last_errors"] = _status["last_errors"][:10]
            continue
        for task in tasks:
            try:
                desc = _apply_action(store, task.id, rule["action"])
                actions_log.append(
                    {
                        "ts": _now().isoformat(timespec="seconds"),
                        "rule": name,
                        "task_id": task.id,
                        "action": desc,
                    }
                )
                log.info("rule '%s' applied to %s: %s", name, task.id, desc)
            except Exception as e:
                log.exception("rule '%s' action on %s failed", name, task.id)
                _status["last_errors"].insert(
                    0,
                    {
                        "ts": _now().isoformat(timespec="seconds"),
                        "rule": name,
                        "error": f"{task.id}: {e}",
                    },
                )
                _status["last_errors"] = _status["last_errors"][:10]
    return actions_log


_engine: "RuleEngine | None" = None


def emit_rule_event(event: str, payload: dict[str, Any]) -> None:
    """Реактивная обработка события (вызывается из endpoint'ов).

    Применяет все enabled rules с trigger.type=event, чьи фильтры
    совпадают с payload. Сейчас поддерживается ``task_moved``:
        payload = {"task": {...}, "from_status": "...", "to_status": "...",
                   "comment": "..." | None}
    """
    if _engine is None:
        return
    _engine._maybe_reload()
    rules = _engine._rules
    task = payload.get("task") or {}
    task_id = task.get("id")
    if not task_id:
        return
    for rule in rules:
        trig = rule["trigger"]
        if trig["type"] != event:
            continue
        if event == "task_moved":
            if trig.get("to_status") and trig["to_status"] != payload.get("to_status"):
                continue
            if trig.get("from_status") and trig["from_status"] != payload.get("from_status"):
                continue
            if trig.get("project_id") and trig["project_id"] != task.get("project_id"):
                continue
            if rule.get("project_id") and rule["project_id"] != task.get("project_id"):
                continue
            ctx = {
                "task_id":     task_id,
                "title":       task.get("title", ""),
                "project_id":  task.get("project_id", ""),
                "from_status": payload.get("from_status", ""),
                "to_status":   payload.get("to_status", ""),
            }
            try:
                desc = _apply_action(_engine.store, task_id, rule["action"], ctx)
                _status["last_reactive"].insert(0, {
                    "ts": _now().isoformat(timespec="seconds"),
                    "rule": rule.get("name", "?"),
                    "event": event,
                    "task_id": task_id,
                    "action": desc,
                })
                _status["last_reactive"] = _status["last_reactive"][:20]
                log.info("reactive rule '%s' on %s: %s",
                         rule.get("name", "?"), task_id, desc)
            except Exception as e:
                log.exception("reactive rule '%s' failed", rule.get("name", "?"))
                _status["last_errors"].insert(0, {
                    "ts": _now().isoformat(timespec="seconds"),
                    "rule": rule.get("name", "?"),
                    "error": f"{task_id}: {e}",
                })
                _status["last_errors"] = _status["last_errors"][:10]


class RuleEngine:
    """Async-loop, который раз в interval_sec прогоняет polling-rules.

    Reactive-rules (task_moved) обрабатываются через emit_rule_event(),
    вызываемый из endpoint'ов сразу после события.
    """

    def __init__(self, store: Store, rules_file: Path, interval: float = DEFAULT_INTERVAL):
        global _engine
        _engine = self
        self.store = store
        self.rules_file = rules_file
        self.interval = interval
        self._stop = asyncio.Event()
        self._rules: list[dict[str, Any]] = []
        self._mtime: float | None = None

    def _maybe_reload(self) -> None:
        if not self.rules_file.exists():
            self._rules = []
            self._mtime = None
            _status["rules_loaded"] = 0
            _status["config_mtime"] = None
            return
        mtime = self.rules_file.stat().st_mtime
        if mtime == self._mtime:
            return
        rules, errs = _load_rules(self.rules_file)
        for e in errs:
            _status["last_errors"].insert(
                0, {"ts": _now().isoformat(timespec="seconds"), "rule": "_config_", "error": e}
            )
        _status["last_errors"] = _status["last_errors"][:10]
        self._rules = rules
        self._mtime = mtime
        _status["rules_loaded"] = len(rules)
        _status["config_mtime"] = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(timespec="seconds")
        log.info("rules.json reloaded: %d active rules (errors: %d)", len(rules), len(errs))

    async def run(self) -> None:
        _status["running"] = True
        _status["rules_file"] = str(self.rules_file)
        _status["interval_sec"] = self.interval
        log.info("rule engine started: %s (interval=%ss)", self.rules_file, self.interval)
        try:
            while not self._stop.is_set():
                try:
                    self._maybe_reload()
                    if self._rules:
                        actions = _run_once(self.store, self._rules)
                        if actions:
                            _status["last_run_actions"] = actions[-20:]
                except Exception:
                    log.exception("rule engine: run failed")
                _status["last_run_at"] = _now().isoformat(timespec="seconds")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            _status["running"] = False
            log.info("rule engine stopped")

    def stop(self) -> None:
        self._stop.set()
