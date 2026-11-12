from __future__ import annotations

import csv
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .battery import BatterySnapshot
from .jsonl_log import (
    DEFAULT_BACKUP_COUNT,
    DEFAULT_MAX_BYTES,
    append_jsonl_line,
    backup_path,
)
from .lid_sleep import MacSleepSnapshot
from .models import AgentStatus, HookEvent
from .providers import default_state_dir


STATUS_AUDIT_LOG_NAME = "event-status.jsonl"
STATUS_HISTORY_LOG_NAME = "status-history.jsonl"
RAW_PREVIEW_LIMIT = 2000
MESSAGE_PREVIEW_LIMIT = 240
LOG_BACKUP_COUNT = DEFAULT_BACKUP_COUNT
STATUS_AUDIT_MAX_BYTES = DEFAULT_MAX_BYTES
# The settings chart reads up to 48h of history and the status bar appends a
# record every refresh, so a rollover clips the graph -- read_status_history_records
# opens the live file only, which no cap can prevent. The history log keeps no
# backups because a backup would be retained bytes the chart cannot reach.
#
# This cap does not cover the widest chart window. A record measures about 1 KiB,
# so 16 MiB holds roughly 16,800 of them. The 15s refresh timer alone writes
# 11,520 records in 48h, and StatusBarController.refresh_ also runs once per
# inbound hook event, so an active machine reaches the cap well inside the 48h
# the chart offers. Raising the cap or teaching the chart to read a backup is
# what would close that; until then the 48h window empties at each rollover.
STATUS_HISTORY_MAX_BYTES = 16 * 1024 * 1024
STATUS_HISTORY_BACKUP_COUNT = 0
AUDIT_COLUMNS = (
    "audited_at",
    "logged_at",
    "provider",
    "hook_event",
    "status",
    "status_label",
    "origin",
    "display_name",
    "session_id",
    "agent_id",
    "cwd",
    "tool_name",
    "message",
    "raw_preview",
)


def default_status_audit_log_path(home: Path | None = None) -> Path:
    return default_state_dir(home) / STATUS_AUDIT_LOG_NAME


def default_status_history_log_path(home: Path | None = None) -> Path:
    return default_state_dir(home) / STATUS_HISTORY_LOG_NAME


def append_status_audit_record(
    event: HookEvent,
    status: AgentStatus | None,
    *,
    path: Path | None = None,
) -> None:
    target = path or default_status_audit_log_path()
    try:
        append_jsonl_line(
            target,
            status_audit_record(event, status),
            max_bytes=STATUS_AUDIT_MAX_BYTES,
            backup_count=LOG_BACKUP_COUNT,
        )
    except OSError:
        pass


def append_status_history_record(
    record: dict[str, object],
    *,
    path: Path | None = None,
) -> None:
    target = path or default_status_history_log_path()
    append_jsonl_line(
        target,
        record,
        max_bytes=STATUS_HISTORY_MAX_BYTES,
        backup_count=STATUS_HISTORY_BACKUP_COUNT,
    )


def status_history_record(
    *,
    agent_mode: str,
    display_status: str,
    battery: BatterySnapshot | None,
    mac_sleep: MacSleepSnapshot | None,
    lid_closed: bool | None,
    keep_awake_requested: bool,
    keep_awake_active: bool,
    sleep_prevention_policy: str,
    sleep_prevention_battery_safeguard_active: bool,
    sleep_prevention_min_battery_percent: float | None,
    closed_lid_awake_requested: bool,
    closed_lid_awake_active: bool,
    recorded_at: datetime | None = None,
    cpu_temperature_c: float | None = None,
    case_temperature_c: float | None = None,
) -> dict[str, object]:
    at = recorded_at or datetime.now(timezone.utc)
    battery_percent = battery.percent if battery is not None else None
    adapter_power = battery.adapter_power if battery is not None else None
    return {
        "recorded_at": at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agent_status": agent_mode,
        "display_status": display_status,
        "cpu_temperature_c": cpu_temperature_c,
        "case_temperature_c": case_temperature_c,
        "battery_level": battery_percent,
        "battery_charging": battery.is_charging if battery is not None else None,
        "battery_charged": battery.is_charged if battery is not None else None,
        "battery_present": battery.battery_present if battery is not None else None,
        "battery_power_watts": round(battery.battery_watts, 2) if battery is not None else None,
        "charger_connected": battery.is_plugged if battery is not None else None,
        "adapter_connected": battery.adapter_connected if battery is not None else None,
        "charger_power_watts": round(adapter_power, 2) if adapter_power is not None else None,
        "adapter_watts": battery.adapter_watts if battery is not None else None,
        "adapter_voltage": round(battery.adapter_voltage, 2) if battery is not None else None,
        "adapter_current": round(battery.adapter_current, 2) if battery is not None else None,
        "adapter_name": battery.adapter_name if battery is not None else "",
        "adapter_manufacturer": battery.adapter_manufacturer if battery is not None else "",
        "adapter_model": battery.adapter_model if battery is not None else "",
        "lid_closed": lid_closed,
        "lid_status": bool_label(lid_closed, true_label="closed", false_label="open"),
        "sidepulse_keep_awake_requested": keep_awake_requested,
        "sidepulse_keep_awake_active": keep_awake_active,
        "sleep_prevention_policy": sleep_prevention_policy,
        "sleep_prevention_battery_safeguard_active": sleep_prevention_battery_safeguard_active,
        "sleep_prevention_min_battery_percent": sleep_prevention_min_battery_percent,
        "sidepulse_closed_lid_awake_requested": closed_lid_awake_requested,
        "sidepulse_closed_lid_awake_active": closed_lid_awake_active,
        "mac_sleep_prevented": mac_sleep.sleep_prevented if mac_sleep is not None else None,
        "mac_sleep_disabled": mac_sleep.sleep_disabled if mac_sleep is not None else None,
        "mac_prevent_system_sleep": (
            mac_sleep.prevent_system_sleep if mac_sleep is not None else None
        ),
        "mac_prevent_user_idle_system_sleep": (
            mac_sleep.prevent_user_idle_system_sleep if mac_sleep is not None else None
        ),
        "mac_prevent_user_idle_display_sleep": (
            mac_sleep.prevent_user_idle_display_sleep if mac_sleep is not None else None
        ),
        "mac_user_is_active": mac_sleep.user_is_active if mac_sleep is not None else None,
        "mac_sleep_status": sleep_status_label(mac_sleep),
        "mac_sleep_error": mac_sleep.error if mac_sleep is not None and mac_sleep.error else "",
    }


def status_audit_record(event: HookEvent, status: AgentStatus | None) -> dict[str, str]:
    return {
        "audited_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "logged_at": event.logged_at.isoformat(),
        "provider": event.provider,
        "hook_event": event.event_name,
        "status": status.mode.value if status is not None else "",
        "status_label": status.mode_label if status is not None else "",
        "origin": status.origin if status is not None and status.origin else event.origin or "",
        "display_name": status.display_name if status is not None else "",
        "session_id": event.session_id or "",
        "agent_id": event.status_key,
        "cwd": event.cwd or "",
        "tool_name": event.tool_name or "",
        "message": truncate_preview(event.message or raw_message(event.raw), MESSAGE_PREVIEW_LIMIT),
        "raw_preview": truncate_preview(json_preview(event.raw), RAW_PREVIEW_LIMIT),
    }


def raw_message(raw: dict[str, Any]) -> str:
    for key in ("message", "last_assistant_message", "prompt"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def bool_label(
    value: bool | None,
    *,
    true_label: str = "true",
    false_label: str = "false",
) -> str:
    if value is True:
        return true_label
    if value is False:
        return false_label
    return "unknown"


def sleep_status_label(mac_sleep: MacSleepSnapshot | None) -> str:
    if mac_sleep is None:
        return "unknown"
    prevented = mac_sleep.sleep_prevented
    if prevented is True:
        return "prevented"
    if prevented is False:
        return "allowed"
    return "unknown"


def json_preview(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        return str(value)


def truncate_preview(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def read_status_audit_records(path: Path | None = None) -> list[dict[str, str]]:
    return list(iter_status_audit_records(path))


def iter_status_audit_records(path: Path | None = None) -> Iterator[dict[str, str]]:
    source = path or default_status_audit_log_path()

    # Oldest first: rotation moves the live file to .1 and shifts .1 to .2, so a
    # higher index is older. The export is the diagnostics path, so it reads the
    # whole retention window rather than stopping at the last rollover.
    sources = [backup_path(source, index) for index in range(LOG_BACKUP_COUNT, 0, -1)]
    sources.append(source)
    for candidate in sources:
        try:
            handle = candidate.open(encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield {column: str(obj.get(column, "")) for column in AUDIT_COLUMNS}


def read_status_history_records(
    path: Path | None = None,
    *,
    limit: int | None = None,
) -> list[dict[str, object]]:
    source = path or default_status_history_log_path()
    try:
        if limit is not None and limit > 0:
            lines = read_recent_lines(source, limit)
        else:
            lines = source.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    records: list[dict[str, object]] = []
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def read_recent_lines(path: Path, max_lines: int) -> list[str]:
    if max_lines <= 0:
        return []

    chunk_size = 8192
    chunks: list[bytes] = []
    newline_count = 0
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        while position > 0 and newline_count <= max_lines:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")

    data = b"".join(reversed(chunks))
    return data.decode("utf-8", errors="replace").splitlines()[-max_lines:]


def export_status_audit_csv(
    destination: Path,
    *,
    source: Path | None = None,
) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_COLUMNS)
        writer.writeheader()
        for record in iter_status_audit_records(source):
            writer.writerow(record)
            count += 1
    return count


def export_status_audit_html(
    destination: Path,
    *,
    source: Path | None = None,
) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with destination.open("w", encoding="utf-8") as handle:
        handle.write(
            "\n".join(
                [
                    "<!doctype html>",
                    '<meta charset="utf-8">',
                    "<title>SidePulse Agent Debug Log</title>",
                    "<style>",
                    "body{font:14px -apple-system,BlinkMacSystemFont,sans-serif;margin:24px;color:#1d1d1f}",
                    "h1{font-size:22px;margin:0 0 4px}",
                    "p{color:#6e6e73;margin:0 0 20px}",
                    "table{border-collapse:collapse;width:100%;table-layout:fixed}",
                    "th,td{border-bottom:1px solid #ddd;padding:7px 8px;text-align:left;vertical-align:top;word-wrap:break-word}",
                    "th{position:sticky;top:0;background:#fff;font-weight:600}",
                    "td.raw{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}",
                    "</style>",
                    "<h1>SidePulse Agent Debug Log</h1>",
                    f"<p>Exported {html.escape(generated_at)}</p>",
                    "<table>",
                    "<thead><tr>",
                    "".join(f"<th>{html.escape(column)}</th>" for column in AUDIT_COLUMNS),
                    "</tr></thead><tbody>",
                ]
            )
            + "\n"
        )
        for record in iter_status_audit_records(source):
            handle.write(table_row(record))
            handle.write("\n")
            count += 1
        handle.write(f"</tbody></table><p>{count} events</p>\n")
    return count


def table_row(record: dict[str, str]) -> str:
    cells = []
    for column in AUDIT_COLUMNS:
        css_class = ' class="raw"' if column == "raw_preview" else ""
        cells.append(f"<td{css_class}>{html.escape(record.get(column, ''))}</td>")
    return "<tr>" + "".join(cells) + "</tr>"
