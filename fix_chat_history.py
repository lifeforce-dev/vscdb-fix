#!/usr/bin/env python3
"""
VS Code Chat History Repair Tool
=================================

Fixes missing chat sessions in VS Code by rebuilding the session index.

Problem:
- Chat session files exist in: chatSessions/*.json or chatSessions/*.jsonl
- But they don't appear in VS Code's UI
- Because they're missing from: state.vscdb -> chat.ChatSessionStore.index

Solution:
- Scans session JSON files
- Rebuilds the index in state.vscdb
- Can recover orphaned sessions from other workspaces

Usage:
    # Auto-repair ALL workspaces
    python3 fix_chat_history.py
    
    # List all workspaces
    python3 fix_chat_history.py --list
    
    # Repair specific workspace
    python3 fix_chat_history.py <workspace_id>

Options:
    --list             List all workspaces with chat sessions
    --dry-run          Preview changes without modifying anything
    --yes              Skip confirmation prompts
    --remove-orphans   Remove orphaned index entries (default: keep)
    --recover-orphans  Copy orphaned sessions from other workspaces
    --help, -h         Show this help message

Examples:
    # Safe preview of what would be fixed
    python3 fix_chat_history.py --dry-run
    
    # Fix everything automatically
    python3 fix_chat_history.py --yes
    
    # Recover sessions from other workspaces
    python3 fix_chat_history.py --recover-orphans
    
    # List workspaces to find ID
    python3 fix_chat_history.py --list
    
    # Fix specific workspace
    python3 fix_chat_history.py f4c750964946a489902dcd863d1907de

IMPORTANT: Close VS Code completely before running this script!
"""

import json
import sqlite3
import shutil
import sys
import platform
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Set, Optional

def extract_project_name(folder_path: Optional[str]) -> Optional[str]:
    """Extract the project/folder name from a workspace folder path."""
    if not folder_path:
        return None
    
    # Handle URI format (file:///path/to/folder)
    if folder_path.startswith('file://'):
        folder_path = folder_path[7:]  # Remove 'file://'
    
    # Get the last component of the path (the folder name)
    try:
        return Path(folder_path).name
    except:
        return None

def get_vscode_storage_root() -> Path:
    """Get the VS Code workspace storage directory for the current platform."""
    home = Path.home()
    system = platform.system()
    
    if system == "Darwin":  # macOS
        return home / "Library/Application Support/Code/User/workspaceStorage"
    elif system == "Windows":
        return home / "AppData/Roaming/Code/User/workspaceStorage"
    else:  # Linux and others
        return home / ".config/Code/User/workspaceStorage"

def folders_match(folder1: Optional[str], folder2: Optional[str]) -> bool:
    """Check if two workspace folders likely refer to the same project."""
    if not folder1 or not folder2:
        return False
    
    name1 = extract_project_name(folder1)
    name2 = extract_project_name(folder2)
    
    if not name1 or not name2:
        return False
    
    # Case-insensitive comparison
    return name1.lower() == name2.lower()

class WorkspaceInfo:
    def __init__(self, workspace_dir: Path):
        self.path = workspace_dir
        self.id = workspace_dir.name
        self.sessions_dir = workspace_dir / "chatSessions"
        self.db_path = workspace_dir / "state.vscdb"

        # Load workspace metadata
        workspace_json = workspace_dir / "workspace.json"
        self.folder = None
        self.workspace_file = None
        if workspace_json.exists():
            try:
                with open(workspace_json, 'r') as f:
                    info = json.load(f)
                    # Check for folder-based workspace
                    if 'folder' in info:
                        folder = info['folder']
                        if isinstance(folder, str):
                            self.folder = folder
                        elif isinstance(folder, dict) and 'path' in folder:
                            self.folder = folder['path']
                    # Check for .code-workspace file
                    elif 'workspace' in info:
                        self.workspace_file = info['workspace']
            except:
                pass

        # Get session IDs from disk (both .json and .jsonl formats).
        self.sessions_on_disk: Set[str] = set()
        if self.sessions_dir.exists():
            for session_file in self.sessions_dir.glob("*.json"):
                self.sessions_on_disk.add(session_file.stem)
            for session_file in self.sessions_dir.glob("*.jsonl"):
                self.sessions_on_disk.add(session_file.stem)

        # Get session IDs from index
        self.sessions_in_index: Set[str] = set()
        if self.db_path.exists():
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                row = cursor.execute(
                    "SELECT value FROM ItemTable WHERE key = 'chat.ChatSessionStore.index'"
                ).fetchone()
                conn.close()

                if row:
                    index = json.loads(row[0])
                    self.sessions_in_index = set(index.get("entries", {}).keys())
            except:
                pass
    
    def get_display_name(self) -> str:
        """Get a user-friendly display name for this workspace."""
        # Try to get name from folder
        if self.folder:
            project_name = extract_project_name(self.folder)
            if project_name:
                return f"{project_name} ({self.id[:8]}...) [Folder]"
        
        # Try to get name from .code-workspace file
        if self.workspace_file:
            workspace_name = extract_project_name(self.workspace_file)
            if workspace_name:
                # Remove .code-workspace extension if present
                if workspace_name.endswith('.code-workspace'):
                    workspace_name = workspace_name[:-15]
                return f"{workspace_name} ({self.id[:8]}...) [Workspace File]"
        
        # Fallback to "Unknown"
        return f"Unknown ({self.id[:8]}...)"

    def get_session_path(self, session_id: str) -> Optional[Path]:
        """Return the path to a session file, checking .jsonl first then .json."""
        jsonl_path = self.sessions_dir / f"{session_id}.jsonl"
        if jsonl_path.exists():
            return jsonl_path
        json_path = self.sessions_dir / f"{session_id}.json"
        if json_path.exists():
            return json_path
        return None

    @property
    def missing_from_index(self) -> Set[str]:
        """Session files that exist but aren't in the index."""
        return self.sessions_on_disk - self.sessions_in_index

    @property
    def orphaned_in_index(self) -> Set[str]:
        """Index entries that don't have corresponding files."""
        return self.sessions_in_index - self.sessions_on_disk

    @property
    def needs_repair(self) -> bool:
        """True if the workspace has corrupted index."""
        return len(self.missing_from_index) > 0 or len(self.orphaned_in_index) > 0

    @property
    def has_sessions(self) -> bool:
        """True if workspace has any session files."""
        return len(self.sessions_on_disk) > 0

def scan_workspaces() -> List[WorkspaceInfo]:
    """Scan all VS Code workspaces and return their info."""
    storage_root = get_vscode_storage_root()

    if not storage_root.exists():
        return []

    workspaces = []
    for workspace_dir in storage_root.iterdir():
        if workspace_dir.is_dir():
            try:
                ws = WorkspaceInfo(workspace_dir)
                if ws.has_sessions:  # Only include workspaces with sessions
                    workspaces.append(ws)
            except Exception as e:
                print(f"⚠️  Warning: Failed to scan {workspace_dir.name}: {e}")

    return workspaces

def extract_session_metadata(session_path: Path, session_id: str) -> Dict:
    """Extract title, timestamp, and other metadata from a session file.

    Handles both the old single-JSON format and the newer JSONL format.
    Returns a dict suitable for use as an index entry.
    """
    title = "Untitled Session"
    last_message_date = 0
    creation_date = 0
    is_empty = True
    initial_location = "panel"
    has_pending_edits = False

    try:
        if session_path.suffix == ".jsonl":
            with open(session_path, "r", encoding="utf-8") as f:
                first_line = f.readline().strip()
            if first_line:
                header = json.loads(first_line)
                v = header.get("v", {})
                initial_location = v.get("initialLocation", "panel")
                creation_date = v.get("creationDate", 0)
                has_pending_edits = v.get("hasPendingEdits", False)

                requests = v.get("requests", [])
                if isinstance(requests, list) and requests:
                    is_empty = False
                    first_req = requests[0]
                    msg = first_req.get("message", {})
                    parts = msg.get("parts", [])
                    text_parts = [
                        p.get("text", "")
                        for p in parts
                        if isinstance(p, dict) and "text" in p
                    ]
                    if text_parts:
                        title = text_parts[0].strip()
                        if len(title) > 100:
                            title = title[:97] + "..."
                        if not title:
                            title = "Untitled Session"

                    last_req = requests[-1]
                    last_message_date = last_req.get("timestamp", 0)
        else:
            with open(session_path, "r", encoding="utf-8") as f:
                session_data = json.load(f)

            initial_location = session_data.get("initialLocation", "panel")
            creation_date = session_data.get("creationDate", 0)
            has_pending_edits = session_data.get("hasPendingEdits", False)

            requests = session_data.get("requests", [])
            if requests:
                is_empty = False
                first_req = requests[0]
                msg = first_req.get("message", {})
                parts = msg.get("parts", [])
                text_parts = [
                    p.get("text", "")
                    for p in parts
                    if isinstance(p, dict) and "text" in p
                ]
                if text_parts:
                    title = text_parts[0].strip()
                    if len(title) > 100:
                        title = title[:97] + "..."
                    if not title:
                        title = "Untitled Session"

                last_req = requests[-1]
                last_message_date = last_req.get("timestamp", 0)
    except Exception:
        pass

    if not last_message_date and creation_date:
        last_message_date = creation_date

    # Build the index entry. Newer VS Code uses additional fields.
    entry: Dict = {
        "sessionId": session_id,
        "title": title,
        "lastMessageDate": last_message_date,
        "initialLocation": initial_location,
        "isEmpty": is_empty,
    }

    # JSONL sessions use the newer index format with extra fields.
    if session_path.suffix == ".jsonl":
        timing: Dict = {}
        if creation_date:
            timing["created"] = creation_date
        entry["timing"] = timing
        entry["hasPendingEdits"] = has_pending_edits
        entry["isExternal"] = False
        entry["lastResponseState"] = 1
    else:
        entry["isImported"] = False

    return entry


def find_orphan_in_other_workspaces(session_id: str, current_workspace: WorkspaceInfo, all_workspaces: List[WorkspaceInfo]) -> Optional[Dict]:
    """Check if an orphaned session ID exists as a file in another workspace.
    
    Returns a dict with workspace info and whether it's the same project folder.
    """
    for ws in all_workspaces:
        if ws.id != current_workspace.id and session_id in ws.sessions_on_disk:
            same_project = folders_match(current_workspace.folder, ws.folder)
            return {
                'workspace': ws,
                'same_project': same_project
            }
    return None

def repair_workspace(workspace: WorkspaceInfo, dry_run: bool = False, show_details: bool = False, remove_orphans: bool = False) -> Dict:
    """Repair a workspace's chat session index."""
    result = {
        'success': False,
        'sessions_restored': 0,
        'sessions_removed': 0,
        'error': None,
        'restored_sessions': []
    }

    try:
        # Build new index from all session files
        entries = {}
        
        # If not removing orphans, start with existing index entries
        if not remove_orphans and workspace.db_path.exists():
            try:
                conn = sqlite3.connect(workspace.db_path)
                cursor = conn.cursor()
                row = cursor.execute(
                    "SELECT value FROM ItemTable WHERE key = 'chat.ChatSessionStore.index'"
                ).fetchone()
                conn.close()
                
                if row:
                    existing_index = json.loads(row[0])
                    entries = existing_index.get("entries", {})
            except:
                pass

        for session_id in sorted(workspace.sessions_on_disk):
            session_path = workspace.get_session_path(session_id)
            if not session_path:
                print(f"      Warning: No file found for session {session_id}")
                continue

            try:
                entry = extract_session_metadata(session_path, session_id)
                entries[session_id] = entry

                # Track if this session will be restored.
                if session_id in workspace.missing_from_index:
                    result['restored_sessions'].append({
                        'id': session_id,
                        'title': entry['title'],
                        'date': entry['lastMessageDate']
                    })

            except Exception as e:
                print(f"      Warning: Failed to read {session_id}: {e}")

        if not dry_run:
            # Create backup
            backup_path = str(workspace.db_path) + f".backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            shutil.copy2(workspace.db_path, backup_path)

            # Update database
            new_index = {
                "version": 1,
                "entries": entries
            }

            conn = sqlite3.connect(workspace.db_path)
            cursor = conn.cursor()

            index_json = json.dumps(new_index, separators=(',', ':'))
            cursor.execute(
                "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
                ('chat.ChatSessionStore.index', index_json)
            )

            conn.commit()
            conn.close()

        result['success'] = True
        result['sessions_restored'] = len(workspace.missing_from_index)

        # Only count removed sessions if we're actually removing orphans
        if remove_orphans:
            result['sessions_removed'] = len(workspace.orphaned_in_index)
        else:
            result['sessions_removed'] = 0

    except Exception as e:
        result['error'] = str(e)

    return result

def list_workspaces_mode():
    """List all workspaces with chat sessions."""
    print()
    print("=" * 70)
    print("VS Code Workspaces with Chat Sessions")
    print("=" * 70)
    print()

    workspaces = scan_workspaces()

    if not workspaces:
        print("No workspaces with chat sessions found.")
        return 0

    print(f"Found {len(workspaces)} workspace(s):")
    print()

    for i, ws in enumerate(workspaces, 1):
        status = "⚠️  NEEDS REPAIR" if ws.needs_repair else "✅ HEALTHY"
        print(f"{i}. {ws.get_display_name()} - {status}")
        
        # Show full ID if we have Unknown workspace
        if not ws.folder and not ws.workspace_file:
            print(f"   ID: {ws.id}")
        
        if ws.folder:
            print(f"   Folder: {ws.folder}")
        elif ws.workspace_file:
            print(f"   Workspace file: {ws.workspace_file}")
        
        print(f"   Sessions on disk: {len(ws.sessions_on_disk)}")
        print(f"   Sessions in index: {len(ws.sessions_in_index)}")
        
        if ws.missing_from_index:
            print(f"   ⚠️  Missing from index: {len(ws.missing_from_index)}")
        
        if ws.orphaned_in_index:
            print(f"   🗑️  Orphaned in index: {len(ws.orphaned_in_index)}")
        
        print()

    needs_repair = [ws for ws in workspaces if ws.needs_repair]
    
    if needs_repair:
        print(f"📊 Summary: {len(needs_repair)} workspace(s) need repair")
        print()
        print("To repair all workspaces:")
        print("  python3 fix_chat_history.py")
        print()
        print("To repair a specific workspace:")
        print(f"  python3 fix_chat_history.py {needs_repair[0].id}")
        print()
    else:
        print("✅ All workspaces are healthy!")
        print()

    return 0

def repair_single_workspace(workspace_id: str, dry_run: bool, remove_orphans: bool, recover_orphans: bool, auto_yes: bool):
    """Repair a specific workspace by ID."""
    storage_root = get_vscode_storage_root()
    workspace_path = storage_root / workspace_id

    if not workspace_path.exists():
        print(f"❌ Error: Workspace ID '{workspace_id}' not found")
        print()
        print("Run with --list to see available workspaces.")
        return 1

    print()
    print("=" * 70)
    print("VS Code Chat History Repair Tool - Single Workspace")
    print("=" * 70)
    print()

    if dry_run:
        print("🔍 DRY RUN MODE - No changes will be made")
        print()

    workspace = WorkspaceInfo(workspace_path)
    
    print(f"🔧 Workspace: {workspace.get_display_name()}")
    if not workspace.folder and not workspace.workspace_file:
        print(f"   ID: {workspace.id}")
    if workspace.folder:
        print(f"   Folder: {workspace.folder}")
    elif workspace.workspace_file:
        print(f"   Workspace file: {workspace.workspace_file}")
    
    print(f"   Sessions on disk: {len(workspace.sessions_on_disk)}")
    print(f"   Sessions in index: {len(workspace.sessions_in_index)}")
    print()

    if not workspace.needs_repair:
        print("✅ This workspace doesn't need repair!")
        return 0

    # Show what needs fixing
    if workspace.missing_from_index:
        print(f"⚠️  Missing from index: {len(workspace.missing_from_index)}")
    
    recoverable_orphans = {}
    
    if workspace.orphaned_in_index:
        orphan_msg = f"🗑️  Orphaned in index: {len(workspace.orphaned_in_index)}"
        if remove_orphans:
            orphan_msg += " (will be removed)"
        else:
            orphan_msg += " (will be kept)"
        print(orphan_msg)
        
        # Check if orphans exist in other workspaces
        all_workspaces = scan_workspaces()
        for session_id in workspace.orphaned_in_index:
            found_info = find_orphan_in_other_workspaces(session_id, workspace, all_workspaces)
            if found_info:
                recoverable_orphans[session_id] = found_info
                found_ws = found_info['workspace']
                same_project = found_info['same_project']
                
                if same_project:
                    project_name = extract_project_name(workspace.folder)
                    print(f"   💡 Session {session_id[:8]}... found in workspace: {found_ws.get_display_name()}")
                    print(f"      ⭐ Same project folder: '{project_name}' - likely belongs here!")
                else:
                    print(f"   💡 Session {session_id[:8]}... found in workspace: {found_ws.get_display_name()}")
        
        if recoverable_orphans and not recover_orphans:
            print(f"   💡 Use --recover-orphans to copy these {len(recoverable_orphans)} session(s) back")
    
    print()

    # Recover orphaned sessions if requested
    if recover_orphans and recoverable_orphans and not dry_run:
        print("📥 Recovering orphaned sessions...")
        
        workspace.sessions_dir.mkdir(parents=True, exist_ok=True)
        
        for session_id, found_info in recoverable_orphans.items():
            found_ws = found_info['workspace']
            source_file = found_ws.get_session_path(session_id)
            if not source_file:
                print(f"   Warning: Source file for {session_id[:8]}... not found")
                continue
            target_file = workspace.sessions_dir / source_file.name
            
            try:
                shutil.copy2(source_file, target_file)
                print(f"   ✅ Copied {session_id[:8]}... from {found_ws.get_display_name()}")
                workspace.sessions_on_disk.add(session_id)
            except Exception as e:
                print(f"   ❌ Failed to copy {session_id[:8]}...: {e}")
        
        print()

    # Confirm before proceeding
    if not dry_run and not auto_yes:
        print("⚠️  This will modify the database for this workspace.")
        print("   A backup will be created before making changes.")
        print()
        response = input("Proceed with repair? (yes/no): ").strip().lower()
        if response not in ['yes', 'y']:
            print()
            print("❌ Aborted.")
            return 1
        print()

    # Repair
    print("🔧 Repairing workspace...")
    result = repair_workspace(workspace, dry_run=dry_run, remove_orphans=remove_orphans)

    if result['success']:
        print()
        print("=" * 70)
        print("✨ REPAIR COMPLETE" if not dry_run else "🔍 DRY RUN COMPLETE")
        print("=" * 70)
        print()
        print(f"📊 Summary:")
        if result['sessions_restored'] > 0:
            print(f"   Sessions restored: {result['sessions_restored']}")
        if result['sessions_removed'] > 0:
            print(f"   Orphaned entries removed: {result['sessions_removed']}")
        print()
        
        if not dry_run:
            print("📝 Next Steps:")
            print("   1. Start VS Code")
            print("   2. Open the Chat view")
            print("   3. Your sessions should now be visible!")
            print()
            print("💾 Backup created for the database")
            print()
        else:
            print("To apply these changes, run without --dry-run:")
            print(f"   python3 fix_chat_history.py {workspace_id}")
            print()
        
        return 0
    else:
        print(f"❌ Repair failed: {result['error']}")
        return 1

def repair_all_workspaces(dry_run: bool, auto_yes: bool, remove_orphans: bool, recover_orphans: bool):
    """Auto-repair all workspaces that need it."""
    print()
    print("=" * 70)
    print("VS Code Chat History Repair Tool - Auto Repair")
    print("=" * 70)
    print()

    if dry_run:
        print("🔍 DRY RUN MODE - No changes will be made")
        print()
    
    if remove_orphans:
        print("🗑️  REMOVE ORPHANS MODE - Orphaned index entries will be removed")
        print()
    
    if recover_orphans:
        print("📥 RECOVER ORPHANS MODE - Orphaned sessions will be copied from other workspaces")
        print()

    # Scan all workspaces
    print("🔍 Scanning VS Code workspaces...")
    workspaces = scan_workspaces()

    if not workspaces:
        print("No workspaces with chat sessions found.")
        return 0

    print(f"   Found {len(workspaces)} workspace(s) with chat sessions")
    print()

    # Find workspaces that need repair
    needs_repair = [ws for ws in workspaces if ws.needs_repair]

    if not needs_repair:
        print("✅ All workspaces are healthy! No repairs needed.")
        return 0

    # Display workspaces that need repair
    print(f"🔧 Found {len(needs_repair)} workspace(s) needing repair:")
    print()

    total_missing = 0
    total_orphaned = 0
    recoverable_orphans = {}  # session_id -> source workspace

    for i, ws in enumerate(needs_repair, 1):
        print(f"{i}. Workspace: {ws.get_display_name()}")
        # Show full ID if we have Unknown workspace
        if not ws.folder and not ws.workspace_file:
            print(f"   ID: {ws.id}")
        if ws.folder:
            print(f"   Folder: {ws.folder}")
        elif ws.workspace_file:
            print(f"   Workspace file: {ws.workspace_file}")
        print(f"   Sessions on disk: {len(ws.sessions_on_disk)}")
        print(f"   Sessions in index: {len(ws.sessions_in_index)}")

        if ws.missing_from_index:
            print(f"   ⚠️  Missing from index: {len(ws.missing_from_index)}")
            total_missing += len(ws.missing_from_index)

        if ws.orphaned_in_index:
            orphan_msg = f"   🗑️  Orphaned in index: {len(ws.orphaned_in_index)}"
            if remove_orphans:
                orphan_msg += " (will be removed)"
            else:
                orphan_msg += " (will be kept - use --remove-orphans to remove)"
            print(orphan_msg)
            total_orphaned += len(ws.orphaned_in_index)
            
            # Check if orphans exist in other workspaces
            for session_id in ws.orphaned_in_index:
                found_info = find_orphan_in_other_workspaces(session_id, ws, workspaces)
                if found_info:
                    recoverable_orphans[session_id] = found_info
                    found_ws = found_info['workspace']
                    same_project = found_info['same_project']
                    
                    if same_project:
                        # Highlight that it's from the same project
                        project_name = extract_project_name(ws.folder)
                        print(f"      💡 Session {session_id[:8]}... found in workspace: {found_ws.get_display_name()}")
                        print(f"         ⭐ Same project folder: '{project_name}' - likely belongs here!")
                    else:
                        print(f"      💡 Session {session_id[:8]}... found in workspace: {found_ws.get_display_name()}")

        print()

    print(f"📊 Total issues:")
    print(f"   Sessions to restore: {total_missing}")
    print(f"   Orphaned entries: {total_orphaned}")
    if recoverable_orphans:
        print(f"   🔍 Orphans found in other workspaces: {len(recoverable_orphans)}")
        if recover_orphans:
            print(f"      📥 Will be recovered (copied back)")
        else:
            print(f"      (Use --recover-orphans to copy them back)")
    print()

    # Copy orphaned sessions from other workspaces if requested
    total_recovered = 0
    if recover_orphans and recoverable_orphans and not dry_run:
        print("📥 Recovering orphaned sessions from other workspaces...")
        print()
        
        # Group by target workspace
        recovery_map = {}  # workspace -> list of (session_id, source_workspace)
        for session_id, found_info in recoverable_orphans.items():
            # Find which workspace needs this session
            for ws in needs_repair:
                if session_id in ws.orphaned_in_index:
                    if ws not in recovery_map:
                        recovery_map[ws] = []
                    recovery_map[ws].append((session_id, found_info['workspace']))
                    break
        
        for target_ws, sessions_to_recover in recovery_map.items():
            print(f"   Recovering to: {target_ws.get_display_name()}")
            
            # Ensure sessions directory exists
            target_ws.sessions_dir.mkdir(parents=True, exist_ok=True)
            
            for session_id, source_ws in sessions_to_recover:
                source_file = source_ws.get_session_path(session_id)
                if not source_file:
                    print(f"      Warning: Source file for {session_id[:8]}... not found")
                    continue
                target_file = target_ws.sessions_dir / source_file.name
                
                try:
                    shutil.copy2(source_file, target_file)
                    print(f"      ✅ Copied {session_id[:8]}... from {source_ws.get_display_name()}")
                    total_recovered += 1
                    # Update the workspace's sessions_on_disk to include this session
                    target_ws.sessions_on_disk.add(session_id)
                except Exception as e:
                    print(f"      ❌ Failed to copy {session_id[:8]}...: {e}")
            
            print()
        
        print(f"📥 Recovered {total_recovered} session(s)")
        print()
    elif recover_orphans and recoverable_orphans and dry_run:
        print("📥 DRY RUN: Would recover these sessions:")
        for session_id, found_info in recoverable_orphans.items():
            found_ws = found_info['workspace']
            print(f"   {session_id[:8]}... from {found_ws.get_display_name()}")
        print()

    # Confirm before proceeding
    if not dry_run and not auto_yes:
        print("⚠️  This will modify the database for these workspaces.")
        print("   Backups will be created before making changes.")
        print()
        response = input("Proceed with repair? (yes/no): ").strip().lower()
        if response not in ['yes', 'y']:
            print()
            print("❌ Aborted.")
            return 1
        print()

    # Repair all workspaces
    print("🔧 Repairing workspaces...")
    print()

    success_count = 0
    fail_count = 0

    for ws in needs_repair:
        print(f"   Repairing: {ws.get_display_name()}")
        if ws.folder:
            print(f"      Path: {ws.folder}")

        result = repair_workspace(ws, dry_run=dry_run, show_details=dry_run, remove_orphans=remove_orphans)

        if result['success']:
            if result['sessions_restored'] > 0:
                print(f"      ✅ Will restore {result['sessions_restored']} session(s)" if dry_run else f"      ✅ Restored {result['sessions_restored']} session(s)")

            if result['sessions_removed'] > 0:
                print(f"      🗑️  Will remove {result['sessions_removed']} orphaned entr(y|ies)" if dry_run else f"      🗑️  Removed {result['sessions_removed']} orphaned entr(y|ies)")
            success_count += 1
        else:
            print(f"      ❌ Failed: {result['error']}")
            fail_count += 1

        print()

    # Summary
    print("=" * 70)
    if dry_run:
        print("🔍 DRY RUN COMPLETE")
    else:
        print("✨ REPAIR COMPLETE")
    print("=" * 70)
    print()
    print(f"📊 Results:")
    print(f"   Workspaces repaired: {success_count}")
    if fail_count > 0:
        print(f"   Failed: {fail_count}")
    print(f"   Total sessions restored: {total_missing}")
    if total_orphaned > 0 and remove_orphans:
        print(f"   Total orphaned entries removed: {total_orphaned}")
    print()

    if not dry_run:
        print("📝 Next Steps:")
        print("   1. Start VS Code")
        print("   2. Open the Chat view")
        print("   3. Your sessions should now be visible!")
        print()
        print("💾 Backups were created for all modified databases")
        print("   (in case you need to restore)")
        print()
    else:
        print("To apply these changes, run without --dry-run:")
        print(f"   python3 fix_chat_history.py")
        print()

    return 0 if fail_count == 0 else 1

def main():
    # Parse flags
    dry_run = '--dry-run' in sys.argv
    auto_yes = '--yes' in sys.argv
    remove_orphans = '--remove-orphans' in sys.argv
    recover_orphans = '--recover-orphans' in sys.argv
    list_mode = '--list' in sys.argv
    show_help = '--help' in sys.argv or '-h' in sys.argv

    if show_help:
        print(__doc__)
        return 0

    # List mode
    if list_mode:
        return list_workspaces_mode()

    # Find first non-flag argument to use as workspace id
    workspace_id = None
    for arg in sys.argv[1:]:
        if not arg.startswith('-'):
            workspace_id = arg
            break

    # Single workspace mode
    if workspace_id:
        if not dry_run and not auto_yes:
            print("⚠️  IMPORTANT: Please close VS Code completely before continuing!")
            print()
            response = input("Have you closed VS Code? (yes/no): ").strip().lower()
            if response not in ['yes', 'y']:
                print()
                print("❌ Aborted. Please close VS Code and run this script again.")
                return 1
            print()

        return repair_single_workspace(workspace_id, dry_run, remove_orphans, recover_orphans, auto_yes)

    # Auto-repair all workspaces mode (default)
    if not dry_run and not auto_yes:
        print()
        print("⚠️  IMPORTANT: Please close VS Code completely before continuing!")
        print()
        response = input("Have you closed VS Code? (yes/no): ").strip().lower()
        if response not in ['yes', 'y']:
            print()
            print("❌ Aborted. Please close VS Code and run this script again.")
            return 1

    return repair_all_workspaces(dry_run, auto_yes, remove_orphans, recover_orphans)

if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
