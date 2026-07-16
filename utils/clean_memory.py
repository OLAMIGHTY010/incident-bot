import json
import re

memory_path = "c:/Users/Olatunbosunno/Downloads/incident-bot/incident_memory.json"

with open(memory_path, 'r', encoding='utf-8') as f:
    memory = json.load(f)

new_memory = {}
for k, v in memory.items():
    # Aggressively strip all leading numbers, underscores, and dashes
    ck = re.sub(r'^[\d_-]+', '', k).strip().lower()
    new_memory[ck] = v

with open(memory_path, 'w', encoding='utf-8') as f:
    json.dump(new_memory, f, indent=4)

print(f"Memory cleaned! Reduced from {len(memory)} to {len(new_memory)} ultra-clean keys.")
