import json
import hashlib
import os
import sqlite3
import time
from pathlib import Path
from hermes_cli import kanban_db as kb
from .purge_archive import inventory, archive_path
from gateway.status import _try_acquire_file_lock, _release_file_lock


def execute_cleanup(conn, replacement_id):
    if conn.in_transaction:
        raise RuntimeError("Connection is already in a transaction")

    try:
        lineage = _load_lineage(conn, replacement_id)
    except Exception as e:
        return {
            "state": "failed",
            "items": [],
            "error": str(e)[:1024],
            "remediation": "Replay the original approved request.",
        }

    if not lineage.get("cleanup_required"):
        return {"state": "retained", "items": []}

    try:
        cur = conn.execute("PRAGMA database_list")
        db_file = None
        for row in cur.fetchall():
            if row[1] == "main" and row[2]:
                db_file = Path(row[2])
                break
        if db_file is None:
            return {
                "state": "failed",
                "items": [],
                "error": "Could not resolve main database file",
                "remediation": "Replay the original approved request.",
            }

        lock_name = (
            ".adrian-kanban-cleanup-"
            + hashlib.sha256(replacement_id.encode()).hexdigest()
            + ".lock"
        )
        lock_path = db_file.parent / lock_name

        with open(lock_path, "a+") as lock_handle:
            if not _try_acquire_file_lock(lock_handle):
                return {
                    "state": "pending",
                    "items": [],
                    "error": "Lock acquisition failed",
                    "remediation": "Replay the original approved request.",
                }

            try:
                cur = conn.execute(
                    "SELECT replacement_id, source_path, destination_path, kind, expected_inventory, board, predecessor_task_id "
                    "FROM task_purge_cleanup_items WHERE replacement_id = ? ORDER BY source_path",
                    (replacement_id,),
                )
                rows = cur.fetchall()

                if not rows:
                    return {
                        "state": "failed",
                        "items": [],
                        "error": "No cleanup items found for replacement",
                        "remediation": "Replay the original approved request.",
                    }

                items = []
                all_verified = True

                for row in rows:
                    r_id, src_str, dest_str, kind, exp_inv_str, board, pred_id = row
                    source = Path(src_str)
                    stored_dest = Path(dest_str)

                    # Load latest event
                    cur_ev = conn.execute(
                        "SELECT status, evidence_json, error FROM task_purge_cleanup_events "
                        "WHERE replacement_id = ? AND source_path = ? ORDER BY event_id DESC LIMIT 1",
                        (replacement_id, src_str),
                    )
                    ev_row = cur_ev.fetchone()

                    if (
                        not source.is_absolute()
                        or not stored_dest.is_absolute()
                        or board != lineage["board"]
                        or pred_id != lineage["predecessor_task_id"]
                        or ev_row is None
                        or ev_row[0] not in ("prepared", "failed", "verified")
                    ):
                        items.append({
                            "source": str(source),
                            "archive": str(stored_dest),
                            "state": "failed",
                            "evidence": None,
                            "error": "Invalid cleanup plan identity or journal head",
                        })
                        all_verified = False
                        continue

                    if ev_row[0] == "verified":
                        try:
                            evidence = json.loads(ev_row[1])
                            if (
                                not isinstance(evidence, dict)
                                or evidence.get("verified") is not True
                                or evidence.get("source") != str(source)
                                or evidence.get("archive") != str(stored_dest)
                                or evidence.get("sha256")
                                != json.loads(exp_inv_str)["sha256"]
                            ):
                                raise ValueError("Invalid verified evidence")
                            items.append({
                                "source": str(source),
                                "archive": str(stored_dest),
                                "state": "verified",
                                "evidence": evidence,
                                "error": None,
                            })
                        except Exception as e:
                            all_verified = False
                            items.append({
                                "source": str(source),
                                "archive": str(stored_dest),
                                "state": "failed",
                                "evidence": None,
                                "error": str(e)[:1024],
                            })
                        continue

                    # Missing head or not verified -> process
                    # Validate destination
                    try:
                        computed_dest = _destination(source, replacement_id)
                        if computed_dest != stored_dest:
                            raise ValueError("Destination mismatch")
                    except Exception as e:
                        all_verified = False
                        items.append({
                            "source": str(source),
                            "archive": str(stored_dest),
                            "state": "failed",
                            "evidence": None,
                            "error": str(e)[:1024],
                        })
                        # Append failed event
                        try:
                            with conn:
                                conn.execute(
                                    "INSERT INTO task_purge_cleanup_events (replacement_id, source_path, status, evidence_json, error, created_at) "
                                    "VALUES (?, ?, 'failed', ?, ?, ?)",
                                    (
                                        replacement_id,
                                        src_str,
                                        None,
                                        str(e)[:1024],
                                        int(time.time()),
                                    ),
                                )
                        except Exception:
                            if conn.in_transaction:
                                conn.rollback()
                        continue

                    # Revalidate overlaps
                    try:
                        # Prepared intent is already durable; this protects check-to-move against concurrent Kanban writers
                        conn.execute("BEGIN IMMEDIATE")
                        _check_overlap_with_existing(
                            conn, [(source, kind), (stored_dest, kind)], replacement_id
                        )
                    except Exception as e:
                        all_verified = False
                        items.append({
                            "source": str(source),
                            "archive": str(stored_dest),
                            "state": "failed",
                            "evidence": None,
                            "error": str(e)[:1024],
                        })
                        try:
                            with conn:
                                conn.execute(
                                    "INSERT INTO task_purge_cleanup_events (replacement_id, source_path, status, evidence_json, error, created_at) "
                                    "VALUES (?, ?, 'failed', ?, ?, ?)",
                                    (
                                        replacement_id,
                                        src_str,
                                        None,
                                        str(e)[:1024],
                                        int(time.time()),
                                    ),
                                )
                        except Exception:
                            if conn.in_transaction:
                                conn.rollback()
                        continue

                    # Validate inventory
                    try:
                        exp_inv = json.loads(exp_inv_str)
                        if not isinstance(exp_inv, dict):
                            raise ValueError("Inventory not dict")
                    except Exception as e:
                        all_verified = False
                        items.append({
                            "source": str(source),
                            "archive": str(stored_dest),
                            "state": "failed",
                            "evidence": None,
                            "error": str(e)[:1024],
                        })
                        try:
                            with conn:
                                conn.execute(
                                    "INSERT INTO task_purge_cleanup_events (replacement_id, source_path, status, evidence_json, error, created_at) "
                                    "VALUES (?, ?, 'failed', ?, ?, ?)",
                                    (
                                        replacement_id,
                                        src_str,
                                        None,
                                        str(e)[:1024],
                                        int(time.time()),
                                    ),
                                )
                        except Exception:
                            if conn.in_transaction:
                                conn.rollback()
                        continue

                    # Execute move
                    try:
                        stored_dest.parent.mkdir(parents=True, exist_ok=True)
                        # Recheck destination after mkdir
                        if _destination(source, replacement_id) != stored_dest:
                            raise ValueError("Destination mismatch after mkdir")

                        evidence = archive_path(
                            source, stored_dest, exp_inv, worktree=(kind == "worktree")
                        )

                        # Append verified event
                        with conn:
                            conn.execute(
                                "INSERT INTO task_purge_cleanup_events (replacement_id, source_path, status, evidence_json, error, created_at) "
                                "VALUES (?, ?, 'verified', ?, ?, ?)",
                                (
                                    replacement_id,
                                    src_str,
                                    json.dumps(evidence),
                                    None,
                                    int(time.time()),
                                ),
                            )

                        items.append({
                            "source": str(source),
                            "archive": str(stored_dest),
                            "state": "verified",
                            "evidence": evidence,
                            "error": None,
                        })
                    except Exception as e:
                        all_verified = False
                        err_msg = str(e)[:1024]
                        items.append({
                            "source": str(source),
                            "archive": str(stored_dest),
                            "state": "failed",
                            "evidence": None,
                            "error": err_msg,
                        })
                        try:
                            with conn:
                                conn.execute(
                                    "INSERT INTO task_purge_cleanup_events (replacement_id, source_path, status, evidence_json, error, created_at) "
                                    "VALUES (?, ?, 'failed', ?, ?, ?)",
                                    (
                                        replacement_id,
                                        src_str,
                                        None,
                                        err_msg,
                                        int(time.time()),
                                    ),
                                )
                        except Exception:
                            if conn.in_transaction:
                                conn.rollback()

                state = "verified" if all_verified else "failed"
                return {
                    "state": "failed" if not all_verified else "verified",
                    "items": items,
                    "remediation": None
                    if all_verified
                    else "Replay the original approved request.",
                }
            finally:
                _release_file_lock(lock_handle)
    except Exception as e:
        return {
            "state": "failed",
            "items": [],
            "error": str(e)[:1024],
            "remediation": "Replay the original approved request.",
        }


def _derive_sources(lineage: dict) -> list[tuple[Path, str]]:
    board = lineage["board"]
    pred = lineage["predecessor_task_id"]
    snap = lineage["snapshot"]

    kind = snap["workspace_kind"]
    ws_raw = snap["workspace_path"]
    att_raws = snap["attachment_paths"]

    # Validate attachment_paths is a list
    if not isinstance(att_raws, list):
        raise ValueError("attachment_paths must be a list")

    # Normalize workspace path
    ws_obj: Path | None = None
    if ws_raw is not None:
        if not isinstance(ws_raw, str):
            raise ValueError("workspace_path must be a string")
        if not ws_raw.strip():
            ws_obj = None
        else:
            if not Path(ws_raw).is_absolute():
                raise ValueError("workspace_path must be absolute")
            ws_str = os.path.abspath(ws_raw)
            if not ws_str:
                raise ValueError("workspace_path normalizes to empty")
            ws_obj = Path(ws_str)
            # Canonical object path: resolve parent, keep name
            ws_obj = ws_obj.parent.resolve() / ws_obj.name

            # Reject filesystem root
            if ws_obj.parent == ws_obj:
                raise ValueError("workspace is filesystem root")

            if kind == "scratch":
                root = kb.workspaces_root(board).resolve()
                # Strict beneath: path != root AND root in path.parents
                if ws_obj == root:
                    raise ValueError("workspace is scratch root")
                if root not in ws_obj.parents:
                    raise ValueError("workspace not under scratch root")
                # Reject .git directory relative to managed root
                if (ws_obj / ".git").is_dir():
                    raise ValueError("workspace is .git directory")

            elif kind == "worktree":
                # Worktree may be outside scratch root
                # If source exists, require root/.git regular nonsymlink FILE and root NOT symlink
                if ws_obj.exists():
                    git_file = ws_obj / ".git"
                    if not git_file.is_file() or git_file.is_symlink():
                        raise ValueError(
                            "worktree .git is not a regular nonsymlink file"
                        )
                    if ws_obj.is_symlink():
                        raise ValueError("worktree root is a symlink")
            else:
                raise ValueError(f"invalid workspace_kind: {kind}")

    # Normalize attachment paths
    att_objs: list[Path] = []
    att_root = kb.task_attachments_dir(pred, board).resolve()

    for a in att_raws:
        if not isinstance(a, str):
            raise ValueError("attachment path must be a string")
        if not a.strip():
            raise ValueError("attachment path must be nonblank")
        if not Path(a).is_absolute():
            raise ValueError("attachment path must be absolute")
        a_str = os.path.abspath(a)
        if not a_str:
            raise ValueError("attachment path normalizes to empty")
        a_obj = Path(a_str)
        a_obj = a_obj.parent.resolve() / a_obj.name

        # Reject filesystem root
        if a_obj.parent == a_obj:
            raise ValueError("attachment is filesystem root")

        # Skip if equal to or nested in selected workspace canonical OBJECT path
        if ws_obj is not None:
            if a_obj == ws_obj or ws_obj in a_obj.parents:
                continue

        # Require strict managed attachment root containment
        if a_obj == att_root:
            raise ValueError("attachment is attachment root")
        if att_root not in a_obj.parents:
            raise ValueError("attachment not under attachment root")

        att_objs.append(a_obj)

    # Dedupe attachment paths
    seen = set()
    unique_atts = []
    for p in att_objs:
        if p not in seen:
            seen.add(p)
            unique_atts.append(p)

    # Build result list
    result: list[tuple[Path, str]] = []
    if ws_obj is not None:
        result.append((ws_obj, kind))
    for p in unique_atts:
        result.append((p, "attachment"))

    # Reject any remaining source path ancestor/equal overlaps
    paths = [p for p, _ in result]
    for i, p1 in enumerate(paths):
        for j, p2 in enumerate(paths):
            if i == j:
                continue
            if p1 == p2:
                raise ValueError(f"duplicate path: {p1}")
            if p1 in p2.parents:
                raise ValueError(f"path {p1} is ancestor of {p2}")
            if p2 in p1.parents:
                raise ValueError(f"path {p2} is ancestor of {p1}")

    # Sort
    result.sort(key=lambda x: str(x[0]))
    return result


def _check_overlap_with_existing(conn, sources, replacement_id):
    """
    sources: list of (Path, kind)
    Query tasks.workspace_path and task_attachments.stored_path across all boards,
    ignoring NULL/blank. Query predecessor_workspace_snapshot from
    task_purge_replacements where replacement_id != ? and
    repository_disposition_action = 'retain'.
    JSON must be dict; workspace_path optional str; attachment_paths list[str];
    reject malformed fields (raise ValueError), do not skip.
    Collect all stored paths. For each source and collected path, normalize
    absolute lexical paths using Path(os.path.abspath(raw)), plus resolve()
    versions. Compare every combination: equality OR a in b.parents OR b in
    a.parents. Raise ValueError with 'shared path' if overlap. No exception
    swallowing. Do not call kb functions. No special root restrictions.
    """
    collected = set()

    # Query tasks.workspace_path
    cur = conn.execute(
        "SELECT workspace_path FROM tasks WHERE workspace_path IS NOT NULL AND TRIM(workspace_path) != ''"
    )
    for row in cur.fetchall():
        wp = row[0]
        if wp is None or wp.strip() == "":
            continue
        collected.add(wp)

    # Query task_attachments.stored_path
    cur = conn.execute(
        "SELECT stored_path FROM task_attachments WHERE stored_path IS NOT NULL AND TRIM(stored_path) != ''"
    )
    for row in cur.fetchall():
        sp = row[0]
        if sp is None or sp.strip() == "":
            continue
        collected.add(sp)

    # Query predecessor_workspace_snapshot
    cur = conn.execute(
        """
        SELECT predecessor_workspace_snapshot
        FROM task_purge_replacements
        WHERE replacement_id != ? AND repository_disposition_action = 'retain'
        """,
        (replacement_id,),
    )
    for row in cur.fetchall():
        raw = row[0]
        if raw is None:
            raise ValueError("predecessor_workspace_snapshot is required but was None")
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            raise ValueError(f"malformed predecessor_workspace_snapshot JSON: {e}")
        if not isinstance(data, dict):
            raise ValueError("predecessor_workspace_snapshot must be a dict")
        # workspace_path optional str
        if "workspace_path" in data:
            wp = data["workspace_path"]
            if wp is not None and not isinstance(wp, str):
                raise ValueError("workspace_path must be a string or None")
            if isinstance(wp, str) and wp.strip() != "":
                collected.add(wp)
        # attachment_paths list[str]
        if "attachment_paths" not in data:
            raise ValueError("attachment_paths is required")
        ap = data["attachment_paths"]
        if not isinstance(ap, list):
            raise ValueError("attachment_paths must be a list")
        for item in ap:
            if not isinstance(item, str):
                raise ValueError("attachment_paths items must be strings")
            if item.strip() != "":
                collected.add(item)

    # Normalize collected paths
    normalized_collected = set()
    for raw in collected:
        try:
            p = Path(os.path.abspath(raw))
            normalized_collected.add(p)
            normalized_collected.add(p.resolve())
        except (OSError, ValueError) as e:
            raise ValueError(f"cannot normalize path {raw!r}: {e}")

    # For each source, normalize and compare
    for source_path, _kind in sources:
        try:
            src_abs = Path(os.path.abspath(str(source_path)))
            src_resolved = src_abs.resolve()
        except (OSError, ValueError) as e:
            raise ValueError(f"cannot normalize source path {source_path!r}: {e}")

        for coll in normalized_collected:
            # Compare src_abs vs coll
            if src_abs == coll:
                raise ValueError("shared path")
            if coll in src_abs.parents:
                raise ValueError("shared path")
            if src_abs in coll.parents:
                raise ValueError("shared path")
            # Compare src_resolved vs coll
            if src_resolved == coll:
                raise ValueError("shared path")
            if coll in src_resolved.parents:
                raise ValueError("shared path")
            if src_resolved in coll.parents:
                raise ValueError("shared path")


def _destination(source: Path, replacement_id: str) -> Path:
    """
    source already absolute canonical object path.
    Compute source.parent / '.adrian-kanban-purge-archive' /
    sha256(replacement_id.encode()).hexdigest() /
    sha256(str(source).encode()).hexdigest()
    Ensure dest.parent.resolve() is strictly beneath source.parent.resolve()
    (not equal), dest not source/descendant thereof, source not descendant dest.
    No mkdir. Symlink archive ancestors permitted ONLY if resolved containment
    still holds; no blanket symlink ban. Return dest.
    """
    path_hash = hashlib.sha256(
        (replacement_id + "\0" + str(source)).encode("utf-8")
    ).hexdigest()[:32]
    dest = source.parent / ".adrian-kanban-purge-archive" / path_hash

    src_parent_resolved = source.parent.resolve()
    dest_parent_resolved = dest.parent.resolve()

    # dest.parent.resolve() must be strictly beneath source.parent.resolve()
    if dest_parent_resolved == src_parent_resolved:
        raise ValueError("destination parent is not strictly beneath source parent")
    if src_parent_resolved not in dest_parent_resolved.parents:
        raise ValueError("destination parent is not beneath source parent")

    # dest must not be source or a descendant of source
    dest_resolved = dest.resolve()
    source_resolved = source.resolve()
    if dest_resolved == source_resolved:
        raise ValueError("destination is the source")
    if source_resolved in dest_resolved.parents:
        raise ValueError("destination is a descendant of source")

    # source must not be a descendant of dest
    if dest_resolved in source_resolved.parents:
        raise ValueError("source is a descendant of destination")

    return dest


def _load_lineage(conn: sqlite3.Connection, replacement_id: str) -> dict:
    cur = conn.execute(
        "SELECT board_slug, predecessor_task_id, cleanup_required, predecessor_workspace_snapshot FROM task_purge_replacements WHERE replacement_id=?",
        (replacement_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(
            f"Missing task_purge_replacements row for replacement_id={replacement_id!r}"
        )
    board_slug, predecessor_task_id, cleanup_required, snapshot_json = row
    if not isinstance(cleanup_required, int) or cleanup_required not in (0, 1):
        raise ValueError(f"cleanup_required must be int 0|1, got {cleanup_required!r}")
    try:
        parsed = json.loads(snapshot_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"predecessor_workspace_snapshot is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError("predecessor_workspace_snapshot must parse to a dict")
    return {
        "board": board_slug,
        "predecessor_task_id": predecessor_task_id,
        "cleanup_required": cleanup_required,
        "snapshot": parsed,
    }


def prepare_cleanup(conn: sqlite3.Connection, replacement_id: str) -> None:
    if not conn.in_transaction:
        raise ValueError("prepare_cleanup must be called inside a transaction")

    lineage = _load_lineage(conn, replacement_id)
    if lineage["cleanup_required"] == 0:
        return

    sources = _derive_sources(lineage)
    if not sources:
        raise ValueError(
            "No cleanup sources derived for replacement_id=" + repr(replacement_id)
        )

    _check_overlap_with_existing(conn, sources, replacement_id)

    board = lineage["board"]
    predecessor_task_id = lineage["predecessor_task_id"]

    for source, kind in sources:
        inv = inventory(source, worktree=(kind == "worktree"))
        dest = _destination(source, replacement_id)
        inv_json = json.dumps(inv, sort_keys=True, separators=(",", ":"))

        conn.execute(
            "INSERT INTO task_purge_cleanup_items(replacement_id, source_path, destination_path, kind, expected_inventory, board, predecessor_task_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                replacement_id,
                str(source),
                str(dest),
                kind,
                inv_json,
                board,
                predecessor_task_id,
            ),
        )

        conn.execute(
            "INSERT INTO task_purge_cleanup_events(replacement_id, source_path, status, evidence_json, error, created_at) VALUES (?, ?, 'prepared', ?, NULL, ?)",
            (
                replacement_id,
                str(source),
                inv_json,
                int(time.time()),
            ),
        )
