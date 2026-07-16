# Sterling Bank Incident Automation Bot

Automates incident form filling on the Sterling Observability portal — for free, no subscription needed.

---

## ⚙️ One-Time Setup

### 1. Install Python (if not installed)
Download from https://python.org — version 3.9 or higher

### 2. Install the bot dependencies
Open a terminal / command prompt and run:
```
pip install playwright rich questionary plyer
playwright install chromium
```

---

## 🚀 How to Run (Every Time)

### Step 1 — Open Chrome with remote debugging

**Close Chrome first**, then run:

**Windows (copy-paste into Command Prompt):**
```
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%USERPROFILE%\ChromeDebug"
```

**Mac:**
```
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug
```

### Step 2 — Log into the Sterling portal
In the Chrome window that opens, go to:
```
https://sterlingobservability-sterlingbankng.msappproxy.net/one-monitor-v2/incidents
```
Log in with your Microsoft/SSO account as normal.

### Step 3 — Run the bot
In your terminal:
```
python incident_bot.py
```

---

## 🧠 Smart Memory Feature

Every incident you fill is saved to `incident_memory.json`.

When you open a new incident, the bot automatically checks if a similar one was handled before and **pre-fills the form** — you just press Enter to confirm!

The more you use it, the smarter it gets.

---

## 📋 What the Bot Does

**Continuous Monitoring Mode (NEW):**
- Runs in the background and continuously monitors for new incidents.
- Auto-acknowledges new incidents as soon as they appear.
- Auto-fills Root Cause, Category, Responsibility, and Description based on past similar issues.
- Triggers a **Desktop Notification Alert** for High/Critical priority issues.
- Auto-submits and saves to memory.

**Interactive Mode:**
1. Connects to your logged-in Chrome (no password needed)
2. Opens the incidents page and lists all rows
3. For each incident, lets you:
   - Fill: Title, Priority, Status, Category, Assignee, Description, Root Cause fields, Resolution
   - Get smart suggestions from past incidents
   - Auto-submit OR manually click Submit
4. Saves every incident to memory for future suggestions

---

## 🔧 Troubleshooting

| Problem | Fix |
|---|---|
| "Chrome not running" error | Make sure you closed Chrome first, then used the debug command above |
| Form fields not filling | The selectors may need updating — share a screenshot and I'll fix them |
| Page not loading | Make sure you're logged into SSO first in the Chrome window |
| `pip` not found | Try `pip3` instead of `pip` |

---

## 📁 Files

- `incident_bot.py` — Main automation script
- `incident_memory.json` — Auto-created; stores past incidents for smart suggestions
- `README.md` — This file
