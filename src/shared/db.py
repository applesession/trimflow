import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

DB_PATH = Path("data.db")
_write_lock = Lock()


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            title TEXT NOT NULL,
            title_ru TEXT,
            mal_id INTEGER,
            season INTEGER NOT NULL DEFAULT 1,
            episodes_range TEXT NOT NULL,
            processing_mode TEXT NOT NULL DEFAULT 'compilation',
            source_type TEXT NOT NULL,
            source_magnet TEXT,
            source_input_dir TEXT,
            source_download_dir TEXT,
            source_variant_codec TEXT,
            source_variant_label TEXT,
            skip_types TEXT,
            encoding TEXT,
            delivery TEXT,
            cleanup TEXT,
            processing TEXT,
            timing_detection TEXT,
            timing_providers TEXT,
            preferred_audio_language TEXT DEFAULT 'rus',
            watermark_path TEXT,
            output_dir TEXT,
            automation TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
        );

        CREATE TABLE IF NOT EXISTS completed_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER,
            status TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            output_display_name TEXT,
            output_video TEXT,
            output_timestamps TEXT,
            output_manifest TEXT,
            delivery_summary TEXT,
            partial_vk INTEGER NOT NULL DEFAULT 0,
            completion_source TEXT,
            completion_note TEXT,
            job_snapshot TEXT
        );

        CREATE TABLE IF NOT EXISTS episode_tracking (
            release_id INTEGER NOT NULL,
            episode INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued', 'completed')),
            recorded_at TEXT NOT NULL,
            PRIMARY KEY (release_id, episode, status)
        );

        CREATE TABLE IF NOT EXISTS discovery_blacklist (
            release_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            title_ru TEXT,
            season INTEGER NOT NULL DEFAULT 1,
            added_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'telegram'
        );

        CREATE TABLE IF NOT EXISTS ongoing_progress (
            progress_key TEXT PRIMARY KEY,
            has_full_publish INTEGER NOT NULL DEFAULT 0,
            last_full_episode INTEGER,
            last_full_range TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS skipped_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            release_id INTEGER,
            alias TEXT,
            title TEXT,
            episodes TEXT,
            reason TEXT,
            recorded_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runtime_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            updated_at TEXT,
            run_status TEXT NOT NULL DEFAULT 'idle',
            run_started_at TEXT,
            run_finished_at TEXT,
            current_stage TEXT,
            queue_progress TEXT,
            current_job TEXT,
            last_run TEXT
        );
        INSERT OR IGNORE INTO runtime_status (id, run_status) VALUES (1, 'idle');

        CREATE TABLE IF NOT EXISTS runtime_errors (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            run_status TEXT,
            context TEXT NOT NULL,
            stage TEXT,
            title TEXT,
            title_ru TEXT,
            season INTEGER,
            episodes_range TEXT,
            current_episode INTEGER,
            total_episodes INTEGER,
            message TEXT NOT NULL,
            error_type TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS telegram_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY
        );
    """)
    conn.commit()

    # Check if migration from JSON is needed
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    current_version = row["version"] if row else 0

    if current_version < 1:
        _migrate_from_json(conn)
        conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (1)")
        conn.commit()

    conn.close()


def _migrate_from_json(conn):
    """One-shot migration from old JSON files."""
    import json as _json
    from pathlib import Path as _Path

    now = _utc_now_iso()

    # jobs.json
    jobs_path = _Path("jobs.json")
    if jobs_path.exists():
        try:
            jobs = _json.loads(jobs_path.read_text(encoding="utf-8"))
            for job in jobs:
                row = _job_to_row(job)
                conn.execute(
                    """INSERT INTO jobs (
                        created_at, updated_at, title, title_ru, mal_id, season,
                        episodes_range, processing_mode, source_type, source_magnet,
                        source_input_dir, source_download_dir, source_variant_codec,
                        source_variant_label, skip_types, encoding, delivery, cleanup,
                        processing, timing_detection, timing_providers,
                        preferred_audio_language, watermark_path, output_dir, automation
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (now, now) + row,
                )
            print(f"[DB] Migrated {len(jobs)} jobs from jobs.json")
        except Exception as exc:
            print(f"[DB] Skipping jobs.json: {exc}")

    # completed_jobs.json
    completed_path = _Path("completed_jobs.json")
    if completed_path.exists():
        try:
            completed = _json.loads(completed_path.read_text(encoding="utf-8"))
            for entry in completed:
                conn.execute(
                    """INSERT INTO completed_jobs (
                        status, completed_at, output_display_name, output_video,
                        output_timestamps, output_manifest, delivery_summary, partial_vk,
                        completion_source, completion_note, job_snapshot
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry.get("status", ""),
                        entry.get("completed_at", now),
                        entry.get("output_display_name"),
                        entry.get("output_video"),
                        entry.get("output_timestamps"),
                        entry.get("output_manifest"),
                        _json.dumps(entry.get("delivery_summary", {})) if entry.get("delivery_summary") else None,
                        1 if entry.get("partial_vk") else 0,
                        entry.get("completion_source"),
                        entry.get("completion_note"),
                        _json.dumps(entry.get("job", {})),
                    ),
                )
            print(f"[DB] Migrated {len(completed)} entries from completed_jobs.json")
        except Exception as exc:
            print(f"[DB] Skipping completed_jobs.json: {exc}")

    # state.json (episode tracking, blacklist, ongoing progress, skipped items)
    state_path = _Path("state.json")
    if state_path.exists():
        try:
            state = _json.loads(state_path.read_text(encoding="utf-8"))

            # Episode tracking
            for key, entry in state.get("queued_release_episodes", {}).items():
                if isinstance(entry, dict):
                    conn.execute(
                        "INSERT OR IGNORE INTO episode_tracking (release_id, episode, status, recorded_at) VALUES (?, ?, 'queued', ?)",
                        (entry.get("release_id", 0), entry.get("episode", 0), entry.get("queued_at", now)),
                    )
            for key, entry in state.get("completed_release_episodes", {}).items():
                if isinstance(entry, dict):
                    conn.execute(
                        "INSERT OR IGNORE INTO episode_tracking (release_id, episode, status, recorded_at) VALUES (?, ?, 'completed', ?)",
                        (entry.get("release_id", 0), entry.get("episode", 0), entry.get("completed_at", now)),
                    )

            # Blacklist
            for item in state.get("discovery_blacklist", []):
                if isinstance(item, dict):
                    conn.execute(
                        "INSERT OR IGNORE INTO discovery_blacklist (release_id, title, title_ru, season, added_at, source) VALUES (?, ?, ?, ?, ?, ?)",
                        (item.get("release_id", 0), item.get("title", ""), item.get("title_ru"), item.get("season", 1), item.get("added_at", now), item.get("source", "telegram")),
                    )

            # Ongoing progress
            for key, entry in state.get("ongoing_progress", {}).items():
                if isinstance(entry, dict):
                    conn.execute(
                        "INSERT OR REPLACE INTO ongoing_progress (progress_key, has_full_publish, last_full_episode, last_full_range, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (key, 1 if entry.get("has_full_publish") else 0, entry.get("last_full_episode"), entry.get("last_full_range"), entry.get("updated_at", now)),
                    )

            # Skipped items
            for item in state.get("skipped_items", []):
                if isinstance(item, dict):
                    conn.execute(
                        "INSERT INTO skipped_items (release_id, alias, title, episodes, reason, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (item.get("release_id"), item.get("alias"), item.get("title"), _json.dumps(item.get("episodes", [])), item.get("reason"), item.get("recorded_at", now)),
                    )

            print("[DB] Migrated state.json")
        except Exception as exc:
            print(f"[DB] Skipping state.json: {exc}")

    print("[DB] Migration complete")


def _job_from_row(row):
    return {
        "title": row["title"],
        "title_ru": row["title_ru"],
        "mal_id": row["mal_id"],
        "season": row["season"],
        "episodes_range": row["episodes_range"],
        "processing_mode": row["processing_mode"],
        "source": {
            "type": row["source_type"],
            "magnet": row["source_magnet"],
            "input_dir": row["source_input_dir"],
            "download_dir": row["source_download_dir"],
            "variant_codec": row["source_variant_codec"],
            "variant_label": row["source_variant_label"],
        },
        "skip_types": json.loads(row["skip_types"]) if row["skip_types"] else None,
        "encoding": json.loads(row["encoding"]) if row["encoding"] else None,
        "delivery": json.loads(row["delivery"]) if row["delivery"] else None,
        "cleanup": json.loads(row["cleanup"]) if row["cleanup"] else None,
        "processing": json.loads(row["processing"]) if row["processing"] else None,
        "timing_detection": json.loads(row["timing_detection"]) if row["timing_detection"] else None,
        "timing_providers": json.loads(row["timing_providers"]) if row["timing_providers"] else None,
        "preferred_audio_language": row["preferred_audio_language"] or "rus",
        "watermark_path": row["watermark_path"],
        "output_dir": row["output_dir"],
        "automation": json.loads(row["automation"]) if row["automation"] else None,
    }


def _job_to_row(job):
    source = job.get("source", {})
    automation = job.get("automation")

    return (
        job.get("title", ""),
        job.get("title_ru"),
        job.get("mal_id"),
        int(job.get("season", 1)),
        job.get("episodes_range", ""),
        str(job.get("processing_mode", "compilation") or "compilation").strip().lower(),
        source.get("type", "magnet"),
        source.get("magnet"),
        source.get("input_dir"),
        source.get("download_dir"),
        source.get("variant_codec"),
        source.get("variant_label"),
        json.dumps(job.get("skip_types")) if job.get("skip_types") else None,
        json.dumps(job.get("encoding")) if job.get("encoding") else None,
        json.dumps(job.get("delivery")) if job.get("delivery") else None,
        json.dumps(job.get("cleanup")) if job.get("cleanup") else None,
        json.dumps(job.get("processing")) if job.get("processing") else None,
        json.dumps(job.get("timing_detection")) if job.get("timing_detection") else None,
        json.dumps(job.get("timing_providers")) if job.get("timing_providers") else None,
        job.get("preferred_audio_language") or "rus",
        job.get("watermark_path"),
        job.get("output_dir"),
        json.dumps(automation) if automation else None,
    )


def load_jobs():
    """Replaces JSON load_jobs — reads from SQLite."""
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    conn.close()
    return [_job_from_row(r) for r in rows]


def save_jobs(jobs):
    """Replaces JSON save_jobs — full replacement (used during discovery sync)."""
    conn = _get_conn()
    with _write_lock:
        conn.execute("DELETE FROM jobs")
        now = _utc_now_iso()
        for job in jobs:
            row = _job_to_row(job)
            conn.execute(
                """INSERT INTO jobs (
                    created_at, updated_at, title, title_ru, mal_id, season,
                    episodes_range, processing_mode, source_type, source_magnet,
                    source_input_dir, source_download_dir, source_variant_codec,
                    source_variant_label, skip_types, encoding, delivery, cleanup,
                    processing, timing_detection, timing_providers,
                    preferred_audio_language, watermark_path, output_dir, automation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, now) + row,
            )
        conn.commit()
    conn.close()


def insert_one_job(job):
    """Insert a single job without touching other jobs. Safe for concurrent use."""
    conn = _get_conn()
    with _write_lock:
        now = _utc_now_iso()
        row = _job_to_row(job)
        conn.execute(
            """INSERT INTO jobs (
                created_at, updated_at, title, title_ru, mal_id, season,
                episodes_range, processing_mode, source_type, source_magnet,
                source_input_dir, source_download_dir, source_variant_codec,
                source_variant_label, skip_types, encoding, delivery, cleanup,
                processing, timing_detection, timing_providers,
                preferred_audio_language, watermark_path, output_dir, automation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, now) + row,
        )
        conn.commit()
    conn.close()


def update_job_episodes_range(job, new_range):
    """Update episodes_range for a matching job — used by discovery when extending range."""
    conn = _get_conn()
    source = job.get("source", {})
    conn.execute(
        """UPDATE jobs SET episodes_range = ?, updated_at = ?
           WHERE title = ? AND season = ?
             AND source_type = ? AND source_magnet = ?
             AND processing_mode = ?""",
        (
            new_range, _utc_now_iso(),
            job.get("title", ""), int(job.get("season", 1)),
            source.get("type", "magnet"), source.get("magnet", ""),
            str(job.get("processing_mode", "compilation") or "compilation").strip().lower(),
        ),
    )
    conn.commit()
    conn.close()


def remove_completed_job(job):
    """Remove a completed job from the queue (runner calls this)."""
    conn = _get_conn()
    source = job.get("source", {})
    conn.execute(
        """DELETE FROM jobs
           WHERE title = ? AND season = ?
             AND source_type = ? AND source_magnet = ?
             AND processing_mode = ?""",
        (
            job.get("title", ""), int(job.get("season", 1)),
            source.get("type", "magnet"), source.get("magnet", ""),
            str(job.get("processing_mode", "compilation") or "compilation").strip().lower(),
        ),
    )
    conn.commit()
    conn.close()


def load_completed_jobs():
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM completed_jobs ORDER BY completed_at DESC").fetchall()
    conn.close()
    return [
        {
            "status": r["status"],
            "completed_at": r["completed_at"],
            "job": json.loads(r["job_snapshot"]) if r["job_snapshot"] else {},
            "output_display_name": r["output_display_name"],
            "output_video": r["output_video"],
            "output_timestamps": r["output_timestamps"],
            "output_manifest": r["output_manifest"],
            "delivery_summary": json.loads(r["delivery_summary"]) if r["delivery_summary"] else {},
            "partial_vk": bool(r["partial_vk"]),
            "completion_source": r["completion_source"],
            "completion_note": r["completion_note"],
        }
        for r in rows
    ]


def save_completed_jobs(jobs):
    """Append new completed entries."""
    conn = _get_conn()
    # Get existing count to avoid duplicates
    existing_count = conn.execute("SELECT COUNT(*) FROM completed_jobs").fetchone()[0]
    new_entries = jobs[existing_count:]
    for entry in new_entries:
        conn.execute(
            """INSERT INTO completed_jobs (
                status, completed_at, output_display_name, output_video,
                output_timestamps, output_manifest, delivery_summary, partial_vk,
                completion_source, completion_note, job_snapshot
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.get("status", ""),
                entry.get("completed_at", _utc_now_iso()),
                entry.get("output_display_name"),
                entry.get("output_video"),
                entry.get("output_timestamps"),
                entry.get("output_manifest"),
                json.dumps(entry.get("delivery_summary", {})) if entry.get("delivery_summary") else None,
                1 if entry.get("partial_vk") else 0,
                entry.get("completion_source"),
                entry.get("completion_note"),
                json.dumps(entry.get("job", {})),
            ),
        )
    conn.commit()
    conn.close()


# --- Episode tracking (replaces state.json deduplication) ---

def build_seen_episode_key(release_id, episode_number):
    return f"{release_id}:{int(episode_number):03d}"


def get_tracked_episode_keys():
    conn = _get_conn()
    rows = conn.execute("SELECT release_id, episode FROM episode_tracking").fetchall()
    conn.close()
    return {build_seen_episode_key(r["release_id"], r["episode"]) for r in rows}


def mark_episodes_queued(release_id, episode_numbers):
    conn = _get_conn()
    now = _utc_now_iso()
    for ep in episode_numbers:
        conn.execute(
            "INSERT OR IGNORE INTO episode_tracking (release_id, episode, status, recorded_at) VALUES (?, ?, 'queued', ?)",
            (release_id, int(ep), now),
        )
    conn.commit()
    conn.close()


def mark_episodes_completed(release_id, episode_numbers):
    conn = _get_conn()
    now = _utc_now_iso()
    for ep in episode_numbers:
        conn.execute(
            "DELETE FROM episode_tracking WHERE release_id = ? AND episode = ? AND status = 'queued'",
            (release_id, int(ep)),
        )
        conn.execute(
            "INSERT OR IGNORE INTO episode_tracking (release_id, episode, status, recorded_at) VALUES (?, ?, 'completed', ?)",
            (release_id, int(ep), now),
        )
    conn.commit()
    conn.close()


def unmark_episodes_queued(release_id, episode_numbers):
    conn = _get_conn()
    for ep in episode_numbers:
        conn.execute(
            "DELETE FROM episode_tracking WHERE release_id = ? AND episode = ? AND status = 'queued'",
            (release_id, int(ep)),
        )
    conn.commit()
    conn.close()


def get_discovery_blacklist():
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM discovery_blacklist ORDER BY title").fetchall()
    conn.close()
    return [
        {
            "release_id": r["release_id"],
            "title": r["title"],
            "title_ru": r["title_ru"],
            "season": r["season"],
            "added_at": r["added_at"],
            "source": r["source"],
        }
        for r in rows
    ]


def find_blacklist_item(release_id):
    conn = _get_conn()
    row = conn.execute("SELECT * FROM discovery_blacklist WHERE release_id = ?", (release_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "release_id": row["release_id"],
        "title": row["title"],
        "title_ru": row["title_ru"],
        "season": row["season"],
        "added_at": row["added_at"],
        "source": row["source"],
    }


def add_to_blacklist(item):
    conn = _get_conn()
    existing = find_blacklist_item(item["release_id"])
    if existing:
        conn.close()
        return True
    conn.execute(
        """INSERT INTO discovery_blacklist (release_id, title, title_ru, season, added_at, source)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (item["release_id"], item["title"], item.get("title_ru"), item.get("season", 1), item.get("added_at", _utc_now_iso()), item.get("source", "telegram")),
    )
    conn.commit()
    conn.close()
    return False


def remove_from_blacklist(release_id):
    conn = _get_conn()
    row = conn.execute("SELECT * FROM discovery_blacklist WHERE release_id = ?", (release_id,)).fetchone()
    if not row:
        conn.close()
        raise RuntimeError("blacklist_entry_not_found")
    item = {
        "release_id": row["release_id"],
        "title": row["title"],
        "title_ru": row["title_ru"],
        "season": row["season"],
        "added_at": row["added_at"],
        "source": row["source"],
    }
    conn.execute("DELETE FROM discovery_blacklist WHERE release_id = ?", (release_id,))
    conn.commit()
    conn.close()
    return item


def record_skipped_item(item):
    conn = _get_conn()
    conn.execute(
        """INSERT INTO skipped_items (release_id, alias, title, episodes, reason, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            item.get("release_id"),
            item.get("alias"),
            item.get("title"),
            json.dumps(item.get("episodes", [])),
            item.get("reason"),
            item.get("recorded_at", _utc_now_iso()),
        ),
    )
    conn.commit()
    conn.close()


def save_ongoing_progress(key, entry):
    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO ongoing_progress
           (progress_key, has_full_publish, last_full_episode, last_full_range, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (key, 1 if entry.get("has_full_publish") else 0, entry.get("last_full_episode"), entry.get("last_full_range"), entry.get("updated_at", _utc_now_iso())),
    )
    conn.commit()
    conn.close()


def load_ongoing_progress():
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM ongoing_progress").fetchall()
    conn.close()
    return {
        r["progress_key"]: {
            "has_full_publish": bool(r["has_full_publish"]),
            "last_full_episode": r["last_full_episode"],
            "last_full_range": r["last_full_range"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    }


# --- Runtime ---

def load_runtime_status():
    conn = _get_conn()
    row = conn.execute("SELECT * FROM runtime_status WHERE id = 1").fetchone()
    conn.close()
    if not row:
        return {
            "schema_version": 1, "updated_at": None, "run_status": "idle",
            "run_started_at": None, "run_finished_at": None, "current_stage": None,
            "queue_progress": {"current_job_index": 0, "total_jobs": 0, "jobs_processed": 0, "jobs_failed": 0},
            "current_job": None, "last_run": None,
        }
    return {
        "schema_version": 1,
        "updated_at": row["updated_at"],
        "run_status": row["run_status"],
        "run_started_at": row["run_started_at"],
        "run_finished_at": row["run_finished_at"],
        "current_stage": row["current_stage"],
        "queue_progress": json.loads(row["queue_progress"]) if row["queue_progress"] else {"current_job_index": 0, "total_jobs": 0, "jobs_processed": 0, "jobs_failed": 0},
        "current_job": json.loads(row["current_job"]) if row["current_job"] else None,
        "last_run": json.loads(row["last_run"]) if row["last_run"] else None,
    }


def save_runtime_status(status):
    conn = _get_conn()
    conn.execute(
        """UPDATE runtime_status SET
           updated_at = ?, run_status = ?, run_started_at = ?, run_finished_at = ?,
           current_stage = ?, queue_progress = ?, current_job = ?, last_run = ?
           WHERE id = 1""",
        (
            status.get("updated_at"), status.get("run_status", "idle"),
            status.get("run_started_at"), status.get("run_finished_at"),
            status.get("current_stage"),
            json.dumps(status.get("queue_progress", {})),
            json.dumps(status.get("current_job")),
            json.dumps(status.get("last_run")),
        ),
    )
    conn.commit()
    conn.close()


def load_runtime_errors():
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM runtime_errors ORDER BY created_at DESC LIMIT 20").fetchall()
    conn.close()
    return {
        "schema_version": 1,
        "updated_at": rows[0]["created_at"] if rows else None,
        "errors": [
            {
                "id": r["id"], "created_at": r["created_at"], "run_status": r["run_status"],
                "context": r["context"], "stage": r["stage"], "title": r["title"],
                "title_ru": r["title_ru"], "season": r["season"], "episodes_range": r["episodes_range"],
                "current_episode": r["current_episode"], "total_episodes": r["total_episodes"],
                "message": r["message"], "error_type": r["error_type"],
            }
            for r in rows
        ],
    }


def append_runtime_error(entry):
    conn = _get_conn()
    conn.execute(
        """INSERT INTO runtime_errors (
            id, created_at, run_status, context, stage, title, title_ru,
            season, episodes_range, current_episode, total_episodes,
            message, error_type
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            entry["id"], entry["created_at"], entry.get("run_status", ""),
            entry["context"], entry.get("stage"), entry.get("title"), entry.get("title_ru"),
            entry.get("season"), entry.get("episodes_range"),
            entry.get("current_episode"), entry.get("total_episodes"),
            entry["message"], entry["error_type"],
        ),
    )
    # Trim to 20
    count = conn.execute("SELECT COUNT(*) FROM runtime_errors").fetchone()[0]
    if count > 20:
        conn.execute(
            "DELETE FROM runtime_errors WHERE id NOT IN (SELECT id FROM runtime_errors ORDER BY created_at DESC LIMIT 20)"
        )
    conn.commit()
    conn.close()


# --- Telegram state ---

def load_telegram_state():
    conn = _get_conn()
    rows = conn.execute("SELECT key, value FROM telegram_state").fetchall()
    conn.close()
    data = {r["key"]: r["value"] for r in rows}
    return {
        "schema_version": int(data.get("schema_version", 1)),
        "last_update_id": json.loads(data["last_update_id"]) if data.get("last_update_id") else None,
        "last_handled_at": json.loads(data["last_handled_at"]) if data.get("last_handled_at") else None,
        "pending_actions": json.loads(data["pending_actions"]) if data.get("pending_actions") else {},
        "jobs_pagination": json.loads(data["jobs_pagination"]) if data.get("jobs_pagination") else {},
        "notification_details": json.loads(data["notification_details"]) if data.get("notification_details") else {},
    }


def save_telegram_state(state):
    conn = _get_conn()
    pairs = {
        "schema_version": str(state.get("schema_version", 1)),
        "last_update_id": json.dumps(state.get("last_update_id")),
        "last_handled_at": json.dumps(state.get("last_handled_at")),
        "pending_actions": json.dumps(state.get("pending_actions", {})),
        "jobs_pagination": json.dumps(state.get("jobs_pagination", {})),
        "notification_details": json.dumps(state.get("notification_details", {})),
    }
    for key, value in pairs.items():
        conn.execute(
            "INSERT OR REPLACE INTO telegram_state (key, value, updated_at) VALUES (?, ?, datetime('now'))",
            (key, value),
        )
    conn.commit()
    conn.close()
