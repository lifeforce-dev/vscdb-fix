#!/usr/bin/env python3
"""
Repair VS Code Copilot chat session corruption where:
- Session exists in chat.ChatSessionStore.index
- But chatSessions/<session_id>.jsonl is missing

The tool searches other workspaceStorage folders for the same session_id JSONL
and copies the best candidate into the broken workspace.

Usage:
  python repair_jsonl_sessions.py --dry-run
  python repair_jsonl_sessions.py --workspace-id <id>
  python repair_jsonl_sessions.py --yes

Important:
  Close VS Code completely before running repairs.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def get_workspace_storage_root() -> Path:
    home = Path.home()
    system = platform.system()
    if system == "Windows":
        return home / "AppData/Roaming/Code/User/workspaceStorage"
    if system == "Darwin":
        return home / "Library/Application Support/Code/User/workspaceStorage"
    return home / ".config/Code/User/workspaceStorage"


def decode_file_uri(raw: str) -> str:
    if not raw.startswith("file://"):
        return raw
    value = raw[7:]
    value = value.replace("%3A", ":")
    value = value.replace("%2F", "/")
    value = value.replace("%20", " ")
    return value


def extract_project_name(raw_path: Optional[str]) -> Optional[str]:
    if not raw_path:
        return None
    path = decode_file_uri(raw_path)
    return Path(path).name or None


@dataclass
class WorkspaceData:
    id: str
    path: Path
    workspace_ref: Optional[str] = None
    folder_ref: Optional[str] = None
    sessions_dir: Path = field(init=False)
    db_path: Path = field(init=False)
    indexed_session_ids: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.sessions_dir = self.path / "chatSessions"
        self.db_path = self.path / "state.vscdb"

    @property
    def display_name(self) -> str:
        if self.folder_ref:
            project = extract_project_name(self.folder_ref)
            if project:
                return f"{project} ({self.id[:8]}...) [Folder]"
        if self.workspace_ref:
            project = extract_project_name(self.workspace_ref)
            if project:
                if project.endswith(".workspace"):
                    project = project.rsplit(".", 1)[0]
                return f"{project} ({self.id[:8]}...) [Workspace]"
        return f"Unknown ({self.id[:8]}...)"

    def project_key(self) -> Optional[str]:
        ref = self.folder_ref or self.workspace_ref
        value = extract_project_name(ref)
        return value.lower() if value else None


def load_workspace_metadata(workspace_dir: Path) -> tuple[Optional[str], Optional[str]]:
    workspace_json = workspace_dir / "workspace.json"
    if not workspace_json.exists():
        return None, None

    try:
        data = json.loads(workspace_json.read_text(encoding="utf-8"))
    except Exception:
        return None, None

    folder_ref = None
    workspace_ref = None

    folder_value = data.get("folder")
    if isinstance(folder_value, str):
        folder_ref = folder_value
    elif isinstance(folder_value, dict):
        maybe_path = folder_value.get("path")
        if isinstance(maybe_path, str):
            folder_ref = maybe_path

    workspace_value = data.get("workspace")
    if isinstance(workspace_value, str):
        workspace_ref = workspace_value

    return workspace_ref, folder_ref


def load_indexed_session_ids(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        row = cursor.execute(
            "SELECT value FROM ItemTable WHERE key = 'chat.ChatSessionStore.index'"
        ).fetchone()
        conn.close()
    except Exception:
        return set()

    if not row:
        return set()

    try:
        payload = json.loads(row[0])
    except Exception:
        return set()

    entries = payload.get("entries", {})
    if not isinstance(entries, dict):
        return set()

    return set(entries.keys())


def scan_workspaces(storage_root: Path) -> list[WorkspaceData]:
    if not storage_root.exists():
        return []

    workspaces: list[WorkspaceData] = []
    for child in storage_root.iterdir():
        if not child.is_dir():
            continue

        workspace_ref, folder_ref = load_workspace_metadata(child)
        ws = WorkspaceData(
            id=child.name,
            path=child,
            workspace_ref=workspace_ref,
            folder_ref=folder_ref,
        )
        ws.indexed_session_ids = load_indexed_session_ids(ws.db_path)
        workspaces.append(ws)

    return workspaces


def locate_candidate_sources(session_id: str, workspaces: list[WorkspaceData], target_id: str) -> list[tuple[WorkspaceData, Path]]:
    candidates: list[tuple[WorkspaceData, Path]] = []
    for ws in workspaces:
        if ws.id == target_id:
            continue
        # Check both .jsonl and .json extensions.
        for ext in (".jsonl", ".json"):
            candidate = ws.sessions_dir / f"{session_id}{ext}"
            if candidate.exists() and candidate.is_file():
                candidates.append((ws, candidate))
                break
    return candidates


def choose_best_candidate(target: WorkspaceData, candidates: list[tuple[WorkspaceData, Path]]) -> tuple[WorkspaceData, Path]:
    target_key = target.project_key()

    def score(item: tuple[WorkspaceData, Path]) -> tuple[int, float]:
        ws, path = item
        same_project = 1 if target_key and ws.project_key() == target_key else 0
        return same_project, path.stat().st_mtime

    return sorted(candidates, key=score, reverse=True)[0]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Repair missing Copilot chat JSONL sessions.")
    parser.add_argument("--workspace-id", help="Repair only one workspaceStorage ID.")
    parser.add_argument("--dry-run", action="store_true", help="Preview fixes without copying files.")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    storage_root = get_workspace_storage_root()
    workspaces = scan_workspaces(storage_root)

    if not workspaces:
        print("No VS Code workspace storage directories found.")
        return 1

    target_workspaces = [w for w in workspaces if w.indexed_session_ids]
    if args.workspace_id:
        target_workspaces = [w for w in target_workspaces if w.id == args.workspace_id]
        if not target_workspaces:
            print(f"Workspace '{args.workspace_id}' not found or has no chat index.")
            return 1

    planned_repairs: list[tuple[WorkspaceData, str, WorkspaceData, Path]] = []

    for ws in target_workspaces:
        for session_id in sorted(ws.indexed_session_ids):
            # Session already exists on disk (either format).
            jsonl_dest = ws.sessions_dir / f"{session_id}.jsonl"
            json_dest = ws.sessions_dir / f"{session_id}.json"
            if jsonl_dest.exists() or json_dest.exists():
                continue

            candidates = locate_candidate_sources(session_id, workspaces, ws.id)
            if not candidates:
                continue

            source_ws, source_path = choose_best_candidate(ws, candidates)
            planned_repairs.append((ws, session_id, source_ws, source_path))

    if not planned_repairs:
        print("No recoverable missing JSONL sessions detected.")
        return 0

    print(f"Found {len(planned_repairs)} recoverable missing session(s):")
    for ws, session_id, source_ws, source_path in planned_repairs:
        print(f"  - {session_id}")
        print(f"    target: {ws.display_name}")
        print(f"    source: {source_ws.display_name}")
        print(f"    file:   {source_path}")

    if args.dry_run:
        print("\nDry run complete. No files were copied.")
        return 0

    if not args.yes:
        answer = input("\nProceed with file copy repair? (yes/no): ").strip().lower()
        if answer not in {"yes", "y"}:
            print("Aborted.")
            return 1

    repaired = 0
    for ws, session_id, _, source_path in planned_repairs:
        # Preserve the source file's extension when copying.
        target_file = ws.sessions_dir / source_path.name
        ws.sessions_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_file)
        repaired += 1

    print(f"\nRepair complete. Restored {repaired} session file(s).")
    print("Reopen VS Code Chat history to verify the sessions are clickable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
