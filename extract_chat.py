import sqlite3
import json
import sys

db_path = r"C:\Users\joshu\AppData\Roaming\Code\User\workspaceStorage\dd6dcc1b1e2eac523956efc44b4d7101\state.vscdb"

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# List all tables
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cursor.fetchall()
print("Tables:", tables)

for table in tables:
    tname = table[0]
    cursor.execute(f"SELECT count(*) FROM {tname}")
    count = cursor.fetchone()[0]
    print(f"  {tname}: {count} rows")
    cursor.execute(f"PRAGMA table_info({tname})")
    cols = cursor.fetchall()
    print(f"    Columns: {[c[1] for c in cols]}")

# Search for chat-related keys
cursor.execute("SELECT key FROM ItemTable WHERE key LIKE '%chat%' OR key LIKE '%copilot%' OR key LIKE '%session%'")
keys = cursor.fetchall()
print(f"\nChat-related keys ({len(keys)}):")
for k in keys:
    cursor.execute("SELECT length(value) FROM ItemTable WHERE key = ?", (k[0],))
    size = cursor.fetchone()[0]
    print(f"  {k[0]} (size: {size})")

conn.close()
