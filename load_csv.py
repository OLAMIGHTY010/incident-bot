import csv
import json
import glob
import os
from datetime import datetime
from pathlib import Path

MEMORY_FILE = Path(r"c:\Users\Olatunbosunno\Downloads\incident-bot\incident_memory.json")
# Find the largest incidents CSV in Downloads
downloads_dir = r"c:\Users\Olatunbosunno\Downloads"
csv_files = glob.glob(os.path.join(downloads_dir, "incidents*.csv"))

if not csv_files:
    print("No CSV files found.")
    exit(1)

# Pick the one with the highest size to get the latest/most complete
csv_path = max(csv_files, key=os.path.getsize)
print(f"Using CSV: {csv_path}")

memory = []
if MEMORY_FILE.exists():
    try:
        memory = json.loads(MEMORY_FILE.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        pass

existing_ids = {m.get("title", "").split()[0] for m in memory if m.get("title")}
added = 0

with open(csv_path, "r", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    for row in reader:
        inc_id = row.get('ID', '').strip()
        if not inc_id or inc_id in existing_ids:
            continue
            
        # Combine fields to help find_similar accurately match this
        desc = f"{inc_id} {row.get('Platform', '')} {row.get('Root Cause', '')}"
        
        data = {
            "title": f"{inc_id} (Loaded from CSV)",
            "description": desc,
            "priority": row.get("Priority", ""),
            "status": row.get("Status", ""),
            "category": row.get("Category", ""),
            "root_cause_description": row.get("Root Cause", ""),
            "root_cause_responsibility": row.get("Responsibility", ""),
            "saved_at": datetime.now().isoformat()
        }
        memory.append(data)
        existing_ids.add(inc_id)
        added += 1

MEMORY_FILE.write_text(json.dumps(memory, indent=2), encoding="utf-8")
print(f"Successfully loaded {added} new historical incidents into the bot's brain!")
