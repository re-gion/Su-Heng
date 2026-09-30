"""Timing comes from persisted transitions; missing provider telemetry stays unknown."""

import json
from collections import defaultdict
from datetime import UTC, datetime


async def task_timing(database, task_id):
    sampled_at = datetime.now(UTC)
    rows = await database.fetch_all(
        "SELECT ts,payload FROM event_log WHERE task_id=? AND event_type='task.status' ORDER BY seq",
        (task_id,),
    )
    transitions = []
    for row in rows:
        value = json.loads(row["payload"]).get("data", {})
        stamp = datetime.fromisoformat(row["ts"])
        if stamp.tzinfo is None:
            continue
        transitions.append((stamp.astimezone(UTC), value))
    if not transitions:
        return {
            "available": False,
            "active_seconds": None,
            "waiting_seconds": None,
            "phase_seconds": {},
            "sampled_at": sampled_at.isoformat(),
            "status": "queued",
        }
    phases = defaultdict(float)
    active = waiting = 0.0
    for index, (stamp, state) in enumerate(transitions):
        end = transitions[index + 1][0] if index + 1 < len(transitions) else sampled_at
        if state.get("status") in {"done", "failed", "queued"}:
            continue
        duration = max(0, (end - stamp).total_seconds())
        if state.get("status") == "paused":
            waiting += duration
        else:
            active += duration
            phases[state.get("phase", "unknown")] += duration
    return {
        "available": True,
        "wall_seconds": round(active + waiting, 2),
        "active_seconds": round(active, 2),
        "waiting_seconds": round(waiting, 2),
        "phase_seconds": {k: round(v, 2) for k, v in phases.items()},
        "sampled_at": sampled_at.isoformat(),
        "status": transitions[-1][1].get("status", "unknown"),
    }
