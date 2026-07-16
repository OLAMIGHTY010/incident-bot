import csv
import json
import re
from pathlib import Path
from datetime import datetime

csv_path = r"c:\Users\Olatunbosunno\Downloads\incidents_2026-05-01T00_00_to_2026-07-12.csv"
memory_path = Path(r"c:\Users\Olatunbosunno\Downloads\incident-bot\incident_memory.json")

def clean_key(raw_key):
    # Strip leading numbers and optional dash
    cleaned = re.sub(r'^\d+-?', '', raw_key).strip().lower()
    return cleaned

def migrate():
    if memory_path.exists():
        with open(memory_path, 'r', encoding='utf-8') as f:
            memory = json.load(f)
    else:
        memory = {}

    print(f"Loaded {len(memory)} items from incident_memory.json")
    
    new_memory = {}
    for k, v in memory.items():
        ck = clean_key(k)
        new_memory[ck] = v
        
    print(f"After cleaning existing keys, {len(new_memory)} unique items remain.")
    
    csv_count = 0
    added_from_csv = 0
    
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or len(row) < 11:
                continue
                
            csv_count += 1
            raw_key = row[0]
            
            if "incident" in raw_key.lower() and len(raw_key) < 20:
                continue
                
            ck = clean_key(raw_key)
            
            if ck in new_memory:
                continue
                
            desc = row[6].strip()
            cat = row[7].strip()
            prio = row[8].strip()
            resp = row[10].strip()
            
            if len(desc) < 3 or not cat:
                continue
                
            new_memory[ck] = {
                "priority": prio if prio else "Low",
                "rc_description": desc,
                "rc_category": cat,
                "rc_responsibility": resp,
                "saved_at": datetime.now().isoformat(),
                "user_provided": True
            }
            added_from_csv += 1
            
    print(f"Processed {csv_count} rows from CSV.")
    print(f"Added {added_from_csv} NEW unique mappings from CSV.")
    print(f"Final memory size: {len(new_memory)} unique items.")
    
    with open(memory_path, 'w', encoding='utf-8') as f:
        json.dump(new_memory, f, indent=4)
        
migrate()
