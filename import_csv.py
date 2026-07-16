import csv
import json
import os
from datetime import datetime

csv_path = r"C:\Users\Olatunbosunno\Downloads\incidents_2026-05-01T00_00_to_2026-07-12.csv"
memory_file = "incident_memory.json"

if not os.path.exists(csv_path):
    print(f"Error: Could not find CSV file at {csv_path}")
    exit(1)

# Load existing memory if any
memory = {}
if os.path.exists(memory_file):
    with open(memory_file, 'r', encoding='utf-8') as f:
        try:
            memory = json.load(f)
        except:
            memory = {}

if isinstance(memory, list):
    print("Warning: Existing memory was a list (legacy format). Creating fresh dictionary.")
    memory = {}

valid_count = 0
total_count = 0

with open(csv_path, 'r', encoding='utf-8-sig') as f:
    reader = csv.DictReader(f)
    for row in reader:
        total_count += 1
        
        # Get fields safely
        incident_id = row.get("ID", "").strip()
        rc_desc = row.get("Root Cause", "").strip()
        rc_cat = row.get("Category", "").strip()
        priority = row.get("Priority", "").strip()
        rc_resp = row.get("Responsibility", "").strip()
        
        # Validation checks
        if not incident_id or not rc_desc:
            continue
            
        if rc_desc.lower() == "still learning..." or "required if" in rc_desc.lower() or len(rc_desc) < 5:
            continue
            
        # Extract characteristic key
        if "-" in incident_id:
            characteristic_key = incident_id.split("-", 1)[1].lower()
        else:
            characteristic_key = incident_id.lower()
            
        # Add to memory
        memory[characteristic_key] = {
            "rc_description": rc_desc,
            "rc_category": rc_cat,
            "rc_responsibility": rc_resp,
            "priority": priority if priority else "High",
            "saved_at": datetime.now().isoformat(),
            "source": "csv_import"
        }
        valid_count += 1

# Save updated memory
with open(memory_file, 'w', encoding='utf-8') as f:
    json.dump(memory, f, indent=4)

print(f"Successfully processed {total_count} rows.")
print(f"Imported {valid_count} valid, completed incidents into memory.")
print(f"Memory now contains {len(memory)} unique incident patterns.")
