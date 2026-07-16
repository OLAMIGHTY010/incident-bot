import csv
import json
import os
from datetime import datetime

CSV_FILE = r"C:\Users\Olatunbosunno\Downloads\incidents_2026-06-01T07_00_to_2026-07-11.csv"
MEMORY_FILE = r"C:\Users\Olatunbosunno\Downloads\incident-bot\incident_memory.json"

def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
            try:
                return json.load(f)
            except:
                return []
    return []

def save_memory(data):
    with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def main():
    if not os.path.exists(CSV_FILE):
        print(f"Error: File not found {CSV_FILE}")
        return
        
    memory = load_memory()
    added = 0
    
    with open(CSV_FILE, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        
        for row in reader:
            rc = row.get("Root Cause", "").strip()
            # If the incident is NOT filled out, skip it!
            if not rc or rc.lower() == "still learning...":
                continue
                
            incident_data = {
                "description": row.get("ID", ""),
                "title": row.get("ID", ""),
                "root_cause": rc,
                "category": row.get("Category", ""),
                "priority": row.get("Priority", ""),
                "status": row.get("Status", ""),
                "root_cause_responsibility": row.get("Responsibility", ""),
                "saved_at": datetime.now().isoformat()
            }
            
            # Add to memory!
            memory.append(incident_data)
            added += 1
            
    save_memory(memory)
    print(f"Successfully cleaned the data and imported {added} fully-filled incidents into the bot's memory!")

if __name__ == "__main__":
    main()
