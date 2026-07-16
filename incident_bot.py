"""
Sterling Bank Incident Management Bot
======================================
Connects to your already-logged-in Chrome browser and automates
incident form filling, button clicking, and smart auto-suggestions.

SETUP (run once):
    pip install playwright rich questionary
    playwright install chromium

USAGE:
    python incident_bot.py
"""

import json
import os
import re
import subprocess
import sys
import time
import msvcrt
from datetime import datetime, timedelta
from pathlib import Path

# Simple cross-console input with timeout for Windows (uses msvcrt)
def input_with_timeout(prompt: str, timeout: int = 15) -> str:
    """Prompt the user and wait for input up to `timeout` seconds. Returns the entered string or empty string on timeout."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    end = time.time() + timeout
    buf = []
    while time.time() < end:
        if msvcrt.kbhit():
            ch = msvcrt.getwche()
            if ch in ('\r', '\n'):
                sys.stdout.write('\n')
                return ''.join(buf)
            if ch == '\x08':  # backspace
                if buf:
                    buf.pop()
                    # erase last char from console
                    sys.stdout.write('\b \b')
                    sys.stdout.flush()
                continue
            buf.append(ch)
        time.sleep(0.05)
    sys.stdout.write('\n')
    return ''.join(buf)

# ── third-party (installed at runtime if missing) ──────────────────────────
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import print as rprint
    import questionary
    from plyer import notification
except ImportError:
    print("Installing required packages...")
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                           "playwright", "rich", "questionary", "plyer", "-q"])
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import print as rprint
    import questionary
    from plyer import notification

console = Console()

# ── Config ─────────────────────────────────────────────────────────────────
BASE_URL = "https://sterlingobservability-sterlingbankng.msappproxy.net/one-monitor-v2/incidents"
INCIDENT_URL = BASE_URL
MEMORY_FILE = Path("incident_memory.json")
CHROME_PORT = 9222  # Remote debugging port
PROCESSED_INCIDENTS = set()  # To track handled incidents in continuous mode


# ── Memory / Smart Suggestions ─────────────────────────────────────────────
def load_memory() -> list | dict:
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text())
        except Exception:
            pass
    return {}


def save_to_memory(incident: dict):
    memory = load_memory()
    incident["saved_at"] = datetime.now().isoformat()
    if isinstance(memory, list):
        memory.append(incident)
    else:
        title = incident.get("title", incident.get("description", "unknown_legacy_incident"))
        memory[title] = incident
    MEMORY_FILE.write_text(json.dumps(memory, indent=2))
    console.print("[green]✓ Incident saved to memory for future suggestions[/green]")


def find_similar(description: str) -> dict | None:
    """Return the most similar past incident based on keyword overlap."""
    if not description:
        return None
    memory = load_memory()
    if not memory:
        return None

    words = set(description.lower().split())
    best, best_score = None, 0
    
    if isinstance(memory, dict):
        for key, m in memory.items():
            past_words = set(key.lower().replace("-", " ").split())
            rc_desc = m.get("rc_description", "")
            if rc_desc:
                past_words.update(rc_desc.lower().split())
            score = len(words & past_words)
            if score > best_score:
                best_score, best = score, m
    else:
        for m in memory:
            past_words = set(m.get("description", m.get("title", "")).lower().split())
            score = len(words & past_words)
            if score > best_score:
                best_score, best = score, m
                
    return best if best_score >= 2 else None


# ── Chrome Connection ───────────────────────────────────────────────────────
def close_side_panel(page):
    """Aggressively attempts to close an open incident side panel. If all else fails, reloads the page."""
    try:
        page.keyboard.press("Escape")
        time.sleep(0.5)
        
        # Try finding and clicking an X or Close button via JS
        try:
            page.evaluate("""() => {
                let btns = Array.from(document.querySelectorAll('button, a'));
                let closeBtn = btns.find(b => {
                    let txt = b.innerText ? b.innerText.trim().toLowerCase() : '';
                    let html = b.innerHTML ? b.innerHTML.toLowerCase() : '';
                    return txt === 'x' || txt === 'close' || html.includes('fa-times') || html.includes('lucide-x');
                });
                if(closeBtn) closeBtn.click();
            }""")
        except:
            pass
            
        time.sleep(0.5)
        
        # Explicitly click the Tailwind backdrop if it exists
        try:
            backdrop = page.locator('.fixed.inset-0.bg-black, .bg-black.bg-opacity-50').first
            if backdrop.is_visible(timeout=200):
                backdrop.click(position={"x": 10, "y": 10}, force=True)
                time.sleep(0.5)
        except:
            pass
            
        # FINAL RESORT: If the backdrop is STILL visible, the modal is stuck!
        # A stuck modal will intercept all future clicks, breaking the loop. We MUST reload.
        try:
            stuck_backdrop = page.locator('.fixed.inset-0.bg-black, .bg-black.bg-opacity-50').first
            if stuck_backdrop.is_visible(timeout=500):
                console.print("[yellow]⚠ Modal stubbornly refused to close! Force reloading the page to clear the state...[/yellow]")
                page.reload(wait_until="domcontentloaded")
                # Wait for the persistent loading overlay to disappear
                try:
                    page.locator('.bg-black.bg-opacity-50').wait_for(state="hidden", timeout=10000)
                except Exception:
                    pass
                time.sleep(5)
                return True
        except:
            pass
            
    except Exception as e:
        console.print(f"[dim]Error closing panel: {e}[/dim]")
        
    return False


def launch_or_connect(playwright):
    """
    Try to connect to an already-running Chrome with remote debugging.
    If not running, guide the user to start it.
    """
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://localhost:{CHROME_PORT}")
        console.print(f"[green]✓ Connected to your existing Chrome session[/green]")
        return browser, False
    except Exception:
        console.print(Panel(
            f"""[yellow]Chrome is not running with remote debugging.[/yellow]

Please close Chrome completely, then run this command to reopen it:

[bold cyan]Windows:[/bold cyan]
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port={CHROME_PORT} --user-data-dir="%USERPROFILE%\\ChromeDebug"

[bold cyan]Mac:[/bold cyan]
  /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port={CHROME_PORT} --user-data-dir=/tmp/chrome-debug

[bold cyan]Linux:[/bold cyan]
  google-chrome --remote-debugging-port={CHROME_PORT} --user-data-dir=/tmp/chrome-debug

After Chrome opens, [bold]log into the Sterling portal[/bold], then press Enter here.
""",
            title="[red]Action Required[/red]"
        ))
        input("Press Enter once Chrome is open and you are logged in...")
        browser = playwright.chromium.connect_over_cdp(f"http://localhost:{CHROME_PORT}")
        console.print("[green]✓ Connected![/green]")
        return browser, False


# ── Form Filler ─────────────────────────────────────────────────────────────
PRIORITY_OPTIONS = ["Critical", "High", "Medium", "Low"]
STATUS_OPTIONS   = ["Open", "In Progress", "Resolved", "Closed"]

def collect_incident_data(suggested: dict | None = None) -> dict:
    """Interactively collect incident details, pre-filling from suggestions."""
    console.print(Panel("[bold]Fill Incident Details[/bold]\n(Press Enter to accept suggestions)", style="blue"))

    def ask(label, key, choices=None):
        default = suggested.get(key, "") if suggested else ""
        if choices:
            console.print(f"[cyan]{label} options:[/cyan] {', '.join(choices)}")
            ans = input(f"{label} [{default}]: ").strip()
            # If user typed something not in choices but valid, we could enforce it, but let's be flexible
            return ans if ans else default
        ans = input(f"{label} [{default}]: ").strip()
        return ans if ans else default

    if suggested:
        console.print(f"[yellow]💡 Similar past incident found! Pre-filling from memory...[/yellow]")

    data = {
        "title":       ask("Incident Title",  "title"),
        "priority":    ask("Priority",        "priority",    PRIORITY_OPTIONS),
        "status":      ask("Status",          "status",      STATUS_OPTIONS),
        "category":    ask("Category",        "category"),
        "assignee":    ask("Assignee",        "assignee"),
        "description": ask("Description",     "description"),
        "root_cause":  ask("Root Cause",      "root_cause"),
        "root_cause_responsibility": ask("Root Cause Responsibility", "root_cause_responsibility"),
        "root_cause_category":       ask("Root Cause Category",       "root_cause_category"),
        "root_cause_description":    ask("Root Cause Description",    "root_cause_description"),
        "resolution":  ask("Resolution Notes","resolution"),
    }
    return data




def fill_form_on_page(page, data: dict):
    """
    Try to fill common form field patterns on the incident page.
    Adjust selectors below to match your actual platform's HTML.
    """
    console.print("[cyan]Filling form fields...[/cyan]")
    
    # Remove status from data so it never touches the global status filters
    if "status" in data:
        del data["status"]

    container_selector = page.evaluate("""() => {
        // 1. Try to find a standard modal/drawer container that is ACTUALLY VISIBLE
        let modals = Array.from(document.querySelectorAll('div[role="dialog"], .modal, .drawer, aside, .cdk-overlay-pane, .offcanvas'))
                          .filter(m => m.offsetWidth > 0 && m.offsetHeight > 0);
        if (modals.length > 0) {
            let panel = modals[modals.length - 1];
            panel.setAttribute('data-bot-active-panel', 'true');
            return '[data-bot-active-panel="true"]';
        }
        
        // 2. Fallback to text matching if no standard modal classes are found
        let els = Array.from(document.querySelectorAll('div, form')).filter(e => {
            let text = e.innerText || "";
            return text.includes('Root Cause') && !text.includes('Incident Management') && e.offsetWidth > 0 && e.offsetHeight > 0;
        });
        if (els.length > 0) {
            // Grab the LARGEST visible container (first in document order) that matches
            let panel = els[0];
            panel.setAttribute('data-bot-active-panel', 'true');
            return '[data-bot-active-panel="true"]';
        }
        return 'body';
    }""")
    
    container = page.locator(container_selector)
    if container_selector != 'body':
        console.print("  [dim]Targeting active incident side-panel...[/dim]")
    else:
        console.print("  [dim][red]Warning: Could not isolate side-panel! Aborting form fill to protect background filters.[/red][/dim]")
        return
        
    filled = 0
    
    priority_filled = False
    priority_val = data.get("priority", "")
    if priority_val:
        priority_selectors = [
            f'button:has-text("{priority_val}")',
            f'[data-priority="{priority_val.lower()}"]',
            f'.priority-{priority_val.lower()}',
            f'[aria-label*="{priority_val}" i]',
        ]
        for sel in priority_selectors:
            try:
                el = container.locator(sel).first
                if el.is_visible(timeout=200):
                    el.click()
                    console.print(f"  [green]✓[/green] Clicked priority: {priority_val}")
                    priority_filled = True
                    break
            except Exception:
                continue

    # Map our internal keys to display labels the dashboard might use
    label_mappings = {
        "title": ["Title"],
        "priority": ["Priority", "Incident Priority"],
        "status": ["Status"],
        "category": ["Category"],
        "assignee": ["Assignee"],
        "description": ["Description"],
        "root_cause": ["Root Cause", "Root Cause Category", "Root Cause Description"],
        "root_cause_responsibility": ["Responsibility", "Root Cause Responsibility"],
        "root_cause_category": ["Root Cause Category", "Category"],
        "root_cause_description": ["Root Cause Description", "Root Cause"],
        "resolution": ["Resolution Notes", "Resolution"]
    }
    
    for key, value in data.items():
        if not value or str(value).strip() == "":
            continue

        # For Priority, we check the global dropdowns if not inline
        if key == "priority":
            p_selectors = [
                f'li:has-text("{value}")',
                f'div[role="option"]:has-text("{value}")',
                f'button:has-text("{value}")',
                f'span:has-text("{value}")'
            ]
            for sel in p_selectors:
                try:
                    loc = container.locator(sel).first
                    if loc.is_visible(timeout=300):
                        loc.click()
                        console.print(f"  [green]✓[/green] Selected {key} option: {value}")
                        filled += 1
                        priority_filled = True
                        break
                except:
                    pass
            if priority_filled:
                continue
        
        # Handle Inline Editing (Click Edit -> Fill -> Click Save)
        labels = label_mappings.get(key, [key.replace("_", " ").title()])
        for lbl in labels:
            try:
                # 1. Find the label text inside the container
                label_loc = container.locator(f'text="{lbl}"').first
                if not label_loc.is_visible(timeout=200):
                    continue
                    
                # 2. Find the Edit button next to it
                edit_btn = label_loc.locator("xpath=..").locator('text="Edit"').first
                if not edit_btn.is_visible(timeout=200):
                    edit_btn = label_loc.locator("xpath=../..").locator('text="Edit"').first
                    
                if edit_btn.is_visible(timeout=500):
                    edit_btn.click()
                    time.sleep(0.5)
                    
                    # 3. Find the input/select that appeared
                    parent_block = label_loc.locator("xpath=../..")
                    input_el = parent_block.locator('input, textarea, select').first
                    if input_el.is_visible(timeout=1000):
                        tag = input_el.evaluate("el => el.tagName.toLowerCase()")
                        if tag == "select":
                            input_el.select_option(label=value)
                        else:
                            input_el.fill(value)
                            
                        # 4. Click Save
                        save_btn = parent_block.locator('text="Save"').first
                        if save_btn.is_visible(timeout=500):
                            save_btn.click()
                            time.sleep(0.5)
                            
                        console.print(f"  [green]✓[/green] Filled {lbl} via Inline Edit")
                        filled += 1
                        break
            except Exception:
                pass

    if filled == 0 and not priority_val:
        console.print("[yellow]⚠ Could not auto-detect form fields or no data to fill.[/yellow]")
        console.print("[yellow]  The bot will show you the page — fill manually or update selectors in the script.[/yellow]")
        
        # DEBUG: Print all visible buttons to help figure out what went wrong
        try:
            buttons = container.evaluate("""(cont) => {
                let btns = Array.from(cont.querySelectorAll('button, a, div[role="button"]'));
                return btns.filter(b => b.offsetWidth > 0).map(b => (b.innerText || b.getAttribute('aria-label') || '').trim()).filter(t => t.length > 0);
            }""")
            console.print(f"[magenta]  [DEBUG] Visible buttons in panel:[/magenta] {list(set(buttons))}")
        except:
            pass
    else:
        console.print(f"[green]✓ Filled {filled} field(s)[/green]")

    return filled


# ── Incident List Handler ───────────────────────────────────────────────────
def get_incident_rows(page) -> list:
    """Try to extract incident rows from the table/list on the page."""
    # Common row selectors for monitoring dashboards
    selectors = [
        "table tbody tr",
        "[class*='incident-row']",
        "[class*='alert-row']",
        "[data-testid*='incident']",
        "li[class*='incident']",
    ]
    for sel in selectors:
        rows = page.query_selector_all(sel)
        if rows:
            console.print(f"[green]✓ Found {len(rows)} incident rows[/green]")
            return rows
    return []


def process_incidents(page, auto_submit: bool = False):
    """Main loop: iterate incidents, collect data, fill forms."""
    rows = get_incident_rows(page)

    if not rows:
        console.print("[yellow]Could not auto-detect incident list rows.[/yellow]")
        console.print("[yellow]Opening the page for manual navigation...[/yellow]")
        console.print("\nTip: Update `get_incident_rows()` selectors to match your page's HTML.\n")
        input("Press Enter to open the incident form directly...")
        handle_single_incident(page, auto_submit)
        return

    table = Table(title="PAY Incidents Found", show_lines=True)
    table.add_column("#", style="cyan", width=4)
    table.add_column("Text Preview", style="white")
    for i, row in enumerate(rows[:20]):
        table.add_row(str(i + 1), row.inner_text()[:80].replace("\n", " "))
    console.print(table)

    console.print("\n[bold cyan]Options:[/bold cyan]")
    console.print("1. Process ALL incidents one by one")
    console.print("2. Pick a specific incident")
    console.print("3. Fill one form with same data for all")
    console.print("4. Exit")
    
    choice = input("\nWhat would you like to do? (1-4): ").strip()

    if choice == "4":
        return

    if choice == "2":
        idx = input("Enter incident number (1-based): ").strip()
        if idx.isdigit() and 1 <= int(idx) <= len(rows):
            rows = [rows[int(idx) - 1]]
        else:
            console.print("[red]Invalid incident number.[/red]")
            return

    if choice == "3":
        data = collect_incident_data(find_similar("pay"))
        for row in rows:
            try:
                row.click()
                page.wait_for_load_state("domcontentloaded", timeout=5000)
                fill_form_on_page(page, data)
                maybe_submit(page, auto_submit)
                save_to_memory(data)
                close_side_panel(page)
                time.sleep(1)
            except Exception as e:
                console.print(f"[red]Error on row: {e}[/red]")
        return

    # Process one by one
    for i, row in enumerate(rows):
        console.print(f"\n[bold]── Incident {i+1}/{len(rows)} ──[/bold]")
        preview = row.inner_text()[:100].replace("\n", " ")
        console.print(f"[dim]{preview}[/dim]")

        console.print("[cyan]Actions:[/cyan] [F]ill this incident, [S]kip, S[t]op")
        action = input("Action (f/s/t) [f]: ").strip().lower()

        if action == "t":
            break
        if action == "s":
            continue

        try:
            row.click()
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception as e:
            console.print(f"[red]Could not open incident: {e}[/red]")
            continue

        suggested = find_similar(preview)
        data = collect_incident_data(suggested)
        
        fill_form_on_page(page, data)
        maybe_submit(page, auto_submit)
        save_to_memory(data)

        cont = input("Close incident panel? (y/n) [y]: ").strip().lower()
        if cont != 'n':
            close_side_panel(page)


def handle_single_incident(page, auto_submit: bool):
    suggested = find_similar("")
    data = collect_incident_data(suggested)
    fill_form_on_page(page, data)
    maybe_submit(page, auto_submit)
    save_to_memory(data)


def maybe_submit(page, auto_submit: bool):
    submit_selectors = [
        'button:has-text("Finish Update")',
        'button[type="submit"]',
        'button:has-text("Save")',
        'button:has-text("Submit")',
        'button:has-text("Update")',
        'button:has-text("Confirm")',
    ]
    if not auto_submit:
        go = input("Submit/Save the form now? (y/n) [y]: ").strip().lower()
        if go == 'n':
            console.print("[yellow]Skipped submit — you can click manually.[/yellow]")
            input("Press Enter when done, then continue...")
            return

    for sel in submit_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click()
                console.print(f"[green]✓ Clicked submit: {sel}[/green]")
                time.sleep(2)
                return
        except Exception:
            continue
    console.print("[yellow]Could not find submit button — click it manually.[/yellow]")
    if not auto_submit:
        input("Press Enter when done...")


def acknowledge_incident(page):
    """Attempts to click the Acknowledge or Assign to Me button."""
    ack_selectors = [
        'button:has-text("Acknowledge")',
        'button:has-text("Assign to me")',
        'a:has-text("Acknowledge")',
        '[aria-label="Acknowledge"]',
    ]
    for sel in ack_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click()
                console.print(f"  [green]✓[/green] Acknowledged incident")
                time.sleep(1)
                return True
        except Exception:
            continue
            
    console.print("  [cyan]ℹ Incident is already acknowledged (or no button found)[/cyan]")
    return False

def trigger_alert(priority, title):
    """Trigger a desktop notification for immediate issues."""
    if priority and priority.lower() in ["critical", "high"]:
        try:
            notification.notify(
                title=f"🚨 {priority.upper()} Incident Alert",
                message=title[:50] + "..." if len(title) > 50 else title,
                app_name="Sterling Incident Bot",
                timeout=10
            )
            console.print(f"[red bold]🚨 ALERT: {priority.upper()} incident detected![/red bold]")
        except Exception as e:
            console.print(f"[red bold]🚨 ALERT: {priority.upper()} incident detected! (Desktop notification failed: {e})[/red bold]")

def set_date_to_today(page):
    """Heuristic to find a date range dropdown and set it to Today or Last 24 Hours."""
    try:
        res = page.evaluate(r"""() => {
            let els = Array.from(document.querySelectorAll('button, div[role="button"], a, span')).filter(e => {
                let txt = (e.innerText || "").toLowerCase().trim();
                return txt.startsWith("last ") || txt.includes("day") || txt.includes("month") || txt.includes("week") || txt.match(/\w+ \d+, \d{4}/);
            });
            if (els.length > 0) {
                let txt = (els[0].innerText || "").toLowerCase().trim();
                if (txt === "today") return false; // Already today!
                els[0].click();
                return true;
            }
            return false;
        }""")
        
        if res:
            time.sleep(1)
            page.evaluate(r"""() => {
                let opts = Array.from(document.querySelectorAll('li, div[role="option"], a, button, span')).filter(e => {
                    let txt = (e.innerText || "").toLowerCase().trim();
                    return txt === "today" || txt === "last 24 hours" || txt === "1 day";
                });
                if (opts.length > 0) {
                    opts[0].click();
                }
            }""")
            return True
    except:
        pass
    return False

def get_incident_age(row_text: str) -> timedelta:
    """Extracts the timestamp from the incident string and returns its age."""
    # Try YYYYMMDDHHMM prefix first (e.g. 202606282016)
    match = re.search(r'^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})', row_text)
    if match:
        try:
            dt_str = f"{match.group(1)}-{match.group(2)}-{match.group(3)} {match.group(4)}:{match.group(5)}:00"
            incident_time = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
            return datetime.now() - incident_time
        except:
            pass
            
    # Try standard YYYY-MM-DD HH:MM:SS format in the text
    match2 = re.search(r'(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})', row_text)
    if match2:
        try:
            incident_time = datetime.strptime(match2.group(1), "%Y-%m-%d %H:%M:%S")
            return datetime.now() - incident_time
        except:
            pass
            
    return timedelta(0)





def scrape_incident_data(page) -> dict:
    """Safely extracts clean field values from an open incident modal."""
    data = {}
    
    # We only care about these 3 core fields for the memory mapping
    fields_to_scrape = {
        "rc_description": ["Root Cause Description"],
        "rc_category": ["Root Cause Category"],
        "rc_responsibility": ["Root Cause Responsibility"]
    }
    
    import re
    for key, labels in fields_to_scrape.items():
        for lbl in labels:
            try:
                # Find the deepest visible element containing the label text
                label_loc = page.locator(f':text("{lbl}")').locator('visible=true').last
                if label_loc.is_visible(timeout=500):
                    # Go up to the container block (two levels up to capture the sibling value)
                    parent = label_loc.locator("xpath=../..")
                    # Get the live, visible text
                    txt = parent.inner_text().strip()
                    
                    # The text will look like "Root Cause Description\nEdit\nStill learning..."
                    # We strip out the label itself, the word Edit, and take the core value
                    val = txt.replace(lbl, "", 1).strip()
                    # Remove "Edit" if it's at the end or anywhere standalone
                    val = re.sub(r'\bEdit\b', '', val, flags=re.IGNORECASE).strip()
                    val = re.sub(r'\bClose\b', '', val, flags=re.IGNORECASE).strip()
                    
                    # If it's multi-line, take the first line of the ACTUAL value
                    # Sometimes there are empty lines, so split by \n and take the first non-empty line
                    lines = [line.strip() for line in val.split("\n") if line.strip()]
                    lines = [line for line in lines if not re.match(r'(?i)^(approved|acknowledged) by', line)]
                    if lines:
                        val = lines[0]
                        
                    # Filter out obvious garbage that might have bled through
                    garbage = ["AllLowMediumHigh", "PriorityStatus", "Select incident category", "Still learning..."]
                    if val and not any(g in val for g in garbage):
                        data[key] = val
                        break
            except Exception as e:
                continue
                
    return data

def learn_from_history(browser):
    """Scrape approved incidents from the currently loaded page."""
    console.print("[bold cyan]Starting Historical Learning Mode...[/bold cyan]")
    
    console.print(Panel(
        "[yellow]Please open your Chrome window and manually filter the incidents to show the past history (e.g., from Jan 1st) and ensure they have loaded.[/yellow]",
        title="[cyan]Manual Filter Required[/cyan]"
    ))
    input("Press Enter here once the past incidents are visible on your screen...")
    
    # Grab the page AFTER the user has finished their manual actions
    contexts = browser.contexts
    if not contexts or not contexts[0].pages:
        console.print("[red]Error: Could not find any open Chrome tabs.[/red]")
        return
        
    # Find the tab with the incidents portal, or just use the last active tab
    page = None
    for p in contexts[0].pages:
        if "incident" in p.url.lower() or "monitor" in p.url.lower():
            page = p
            break
    if not page:
        page = contexts[0].pages[-1]

    # Refresh our element handles just in case
    time.sleep(1)
    
    learned_count = 0
    page_num = 1
    
    while True:
        rows = get_incident_rows(page)
        if not rows:
            if page_num == 1:
                console.print("[yellow]No historical incidents found on the page.[/yellow]")
            break
            
        num_rows = len(rows)
        console.print(f"[green]✓ Found {num_rows} historical incidents on Page {page_num}.[/green]")
        
        for i in range(num_rows):
            try:
                # Re-fetch rows because navigating back destroys the old DOM elements
                current_rows = get_incident_rows(page)
                if i >= len(current_rows):
                    break
                row = current_rows[i]
                
                row_text = row.inner_text().strip()
                if not row_text:
                    continue
                    
                preview = row_text[:80].replace("\n", " ")
                
                # Skip open/ongoing incidents
                skip_keywords = ["ongoing", "open", "in progress", "new"]
                if any(k in row_text.lower() for k in skip_keywords):
                    console.print(f"[dim]Skipping (not resolved): {preview}[/dim]")
                    continue
                    
                console.print(f"\n[cyan]Scraping Incident {i+1}/{num_rows} (Page {page_num}):[/cyan] {preview}")
                original_url = page.url
                row.click()
                # Wait for navigation or modal to appear
                time.sleep(3)
                
                data = scrape_incident_data(page)
                
                # Require at least some root cause or description to be considered useful
                if data and ("root_cause" in data or "description" in data or "title" in data):
                    save_to_memory(data)
                    learned_count += 1
                    
                    # Print what we actually captured so the user can see it working
                    rc_preview = data.get('root_cause', '')[:60] + ('...' if len(data.get('root_cause', '')) > 60 else '')
                    title_preview = data.get('title', preview)[:60]
                    
                    console.print(f"[bold green]  ✓ Captured Incident![/bold green] [cyan]{title_preview}[/cyan]")
                    if rc_preview:
                        console.print(f"      [dim]Root Cause:[/dim] {rc_preview}")
                    if 'root_cause_category' in data:
                        console.print(f"      [dim]Category:[/dim] {data['root_cause_category']}")
                    
                    console.print(f"  [bold magenta]Total incidents captured so far: {learned_count}[/bold magenta]")
                else:
                    console.print("[yellow]  ⚠ No useful data found in this incident.[/yellow]")
                
                # Smart return to list without hard reloading
                if page.url != original_url:
                    page.go_back(wait_until="domcontentloaded")
                    time.sleep(2)
                else:
                    # It's an SPA modal or side-panel. Try to close it softly.
                    page.keyboard.press("Escape")
                    time.sleep(1)
                    
                    # If escape didn't work, look for a close button
                    close_selectors = [
                        'button[aria-label="Close" i]',
                        'button.close',
                        '.btn-close',
                        'i.fa-times',
                        'button:has-text("Close")',
                        'a:has-text("Back")'
                    ]
                    for sel in close_selectors:
                        try:
                            btn = page.query_selector(sel)
                            if btn and btn.is_visible():
                                btn.click()
                                time.sleep(1)
                                break
                        except Exception:
                            pass
                
            except Exception as e:
                console.print(f"[red]Error scraping row: {e}[/red]")
                try:
                    if page.url != original_url:
                        page.go_back(wait_until="domcontentloaded")
                        time.sleep(2)
                    else:
                        page.keyboard.press("Escape")
                except:
                    pass
                    
        # Try to go to the next page
        next_selectors = [
            'button[aria-label*="Next" i]',
            'a[aria-label*="Next" i]',
            'button:has-text("Next")',
            'a:has-text("Next")',
            '.pagination-next',
            '.next-page',
            'i.fa-angle-right',
            'button[title*="Next" i]',
            'a[title*="Next" i]',
            'li.next a',
            'button:has-text(">")'
        ]
        
        found_next = False
        for sel in next_selectors:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    disabled = btn.get_attribute("disabled")
                    aria_disabled = btn.get_attribute("aria-disabled")
                    class_attr = btn.get_attribute("class") or ""
                    
                    if disabled is None and aria_disabled != "true" and "disabled" not in class_attr.lower():
                        btn.click()
                        console.print(f"\n[bold cyan]➡ Moving to Page {page_num + 1}...[/bold cyan]")
                        # Just wait for the AJAX/React to render the new rows without expecting a hard browser reload
                        time.sleep(4)
                        found_next = True
                        page_num += 1
                        break
            except Exception:
                continue
                
        if not found_next:
            console.print("\n[yellow]Could not automatically find the 'Next' button.[/yellow]")
            ans = input("Click the 'Next Page' button in Chrome yourself, then type 'y' here to continue, or 'n' to stop: ").strip().lower()
            if ans == 'y':
                time.sleep(2) # Give the browser a second to render the next page
                page_num += 1
                continue
            else:
                break
                
    console.print(f"\n[bold green]Learning Complete! Added {learned_count} incidents to memory.[/bold green]")


# ── Memory Viewer ───────────────────────────────────────────────────────────
def view_memory():
    memory = load_memory()
    if not memory:
        console.print("[yellow]No incidents in memory yet.[/yellow]")
        return
    table = Table(title=f"Incident Memory ({len(memory)} records)", show_lines=True)
    table.add_column("Date", style="cyan", width=20)
    table.add_column("Title", style="white", width=30)
    table.add_column("Priority", style="magenta", width=10)
    table.add_column("Category", style="green", width=15)
    for m in memory[-20:]:
        table.add_row(
            m.get("saved_at", "")[:19],
            m.get("title", "")[:30],
            m.get("priority", ""),
            m.get("category", ""),
        )
    console.print(table)


def v3_dynamic_monitor(page):
    """V3 Dynamic Relational Memory Monitor"""
    console.print("[bold cyan]Starting v3 Relational Memory Monitor... (Press Ctrl+C to stop)[/bold cyan]")
    
    # Internal key-value dictionary for relational memory mappings
    relational_memory = {}
    
    def wait_for_success(action_name):
        try:
            banner = page.locator('text="Success"').first
            banner.wait_for(state="visible", timeout=5000)
            console.print(f"  [green]✓ Success banner seen for {action_name}[/green]")
            time.sleep(1) # let it fade
            return True
        except:
            console.print(f"  [yellow]⚠ No success banner seen for {action_name}[/yellow]")
            return False

    acked_ids = set()
    filled_ids = set()
    temporarily_skipped_ids = set()

    while True:
        try:
            # 1. Validate Dashboard Filters
            console.print("[dim]Ensuring filters are active & refreshing page for new incidents...[/dim]")
            page.reload(wait_until="domcontentloaded")
            time.sleep(1.5)
            
            temporarily_skipped_ids.clear()
            
            rows = get_incident_rows(page)
            if not rows:
                time.sleep(5)
                continue
                
            while True:
                current_rows = get_incident_rows(page)
                
                # High priority rows (not Linux/Openshift)
                hp_unacked = None; hp_unacked_id = None
                hp_unfilled = None; hp_unfilled_id = None
                
                # Low priority rows (Linux/Openshift)
                lp_unacked = None; lp_unacked_id = None
                lp_unfilled = None; lp_unfilled_id = None
                
                for r in current_rows:
                    try:
                        text = r.inner_text().strip()
                        if not text: continue
                        
                        text_lower = text.lower()
                        is_low_priority = "linux server" in text_lower or "openshift cluster" in text_lower
                        
                        first = text.split()[0]
                        if first in temporarily_skipped_ids:
                            continue
                        
                        if first not in acked_ids:
                            if is_low_priority:
                                if not lp_unacked: lp_unacked = r; lp_unacked_id = first
                            else:
                                if not hp_unacked: hp_unacked = r; hp_unacked_id = first
                        elif first not in filled_ids:
                            if is_low_priority:
                                if not lp_unfilled: lp_unfilled = r; lp_unfilled_id = first
                            else:
                                if not hp_unfilled: hp_unfilled = r; hp_unfilled_id = first
                    except:
                        continue
                        
                target_row = None
                target_id = None
                phase = 0
                
                if hp_unacked:
                    target_row = hp_unacked; target_id = hp_unacked_id; phase = 1
                elif lp_unacked:
                    target_row = lp_unacked; target_id = lp_unacked_id; phase = 1
                elif hp_unfilled:
                    target_row = hp_unfilled; target_id = hp_unfilled_id; phase = 2
                elif lp_unfilled:
                    target_row = lp_unfilled; target_id = lp_unfilled_id; phase = 2
                    console.print("[dim]Idle task: Filling low priority Linux/Openshift incident...[/dim]")
                else:
                    break # All current incidents have been processed!
                    
                row = target_row
                row_text = row.inner_text().strip()
                
                preview = row_text[:80].replace("\n", " ")
                parts = preview.split()
                if not parts: continue
                first_part = parts[0]
                
                # Extract characteristic key (e.g., "altpro-transferapi")
                if "-" in first_part:
                    characteristic_key = first_part.split("-", 1)[1].lower()
                else:
                    characteristic_key = first_part.lower()
                    
                if phase == 1:
                    console.print(f"\n[bold red]PHASE 1 (Siren Silencer)[/bold red]: Checking Ack status for [cyan]{characteristic_key}[/cyan]...")
                else:
                    console.print(f"\n[bold cyan]PHASE 2 (Background Filler)[/bold cyan]: Filling data for [cyan]{characteristic_key}[/cyan]...")
                
                # 2. Select an Incident
                try:
                    incident_id_raw = parts[0]
                    incident_id_clean = incident_id_raw.strip(".")
                    try:
                        # Always use Playwright's native click to trigger React events!
                        cells = row.query_selector_all("td")
                        if len(cells) > 1:
                            cells[1].click()
                        elif len(cells) > 0:
                            cells[0].click()
                        else:
                            row.click()
                        console.print(f"  [green]Successfully clicked cell for '{characteristic_key}'![/green]")
                    except Exception as e:
                        console.print(f"  [yellow]⚠ Native click failed: {e}. Falling back to row.click()[/yellow]")
                        row.click()
                        
                    console.print(f"  [dim]Waiting for modal to slide out...[/dim]")
                    time.sleep(1)
                except Exception as e:
                    console.print(f"  [yellow]Failed to click incident row: {e}[/yellow]")
                    continue
                    
                # Attempt to extract the UNTRUNCATED characteristic key from the modal title
                try:
                    timestamp = first_part.split("-")[0]
                    # Find all links that might be the title
                    for el in page.locator('h2 a').all():
                        if el.is_visible():
                            text = el.inner_text().strip()
                            if timestamp in text and " - " in text:
                                full_id = text.split(" - ")[-1].strip()
                                if "-" in full_id:
                                    characteristic_key = full_id.split("-", 1)[1].lower()
                                    console.print(f"  [dim]Resolved full incident name: {characteristic_key}[/dim]")
                                    break
                except Exception:
                    pass
                    
                is_unacknowledged = False
                ack_btn = None
                
                # Highly robust text search. .last ensures we get the one in the modal (usually appended to DOM end)
                for text in ["Acknowledge", "Assign to me"]:
                    # Use regex to match exactly "Acknowledge" (ignoring whitespace and case), so we don't accidentally match "Acknowledged" status labels
                    btn = page.locator(f'text=/^\\s*{text}\\s*$/i').locator('visible=true').last
                    if btn.is_visible(timeout=1000):
                        is_unacknowledged = True
                        ack_btn = btn
                        break
                        
                is_empty = "learning" in row_text.lower()
                
                if phase == 1:
                    if is_unacknowledged:
                        age_minutes = 0
                        import re
                        from datetime import datetime
                        match = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', row_text)
                        if match:
                            try:
                                incident_time = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
                                age_minutes = (datetime.now() - incident_time).total_seconds() / 60
                            except:
                                age_minutes = 0
                                
                        if age_minutes >= 5:
                            console.print(f"  [red]Incident is {int(age_minutes)} minutes old (>= 5 mins)! Skipping Acknowledgment.[/red]")
                        else:
                            console.print("  [yellow]Incident is unacknowledged. Acknowledging to stop SLA clock...[/yellow]")
                            if ack_btn:
                                ack_btn.click()
                            wait_for_success("Acknowledge")
                    else:
                        console.print("  [dim]Incident is already acknowledged.[/dim]")
                    
                    acked_ids.add(target_id)
                    close_side_panel(page)
                    continue

                # --- Phase 2 (Background Filler) follows below ---
                
                # Wait for React to render the form fields before trying to read them!
                try:
                    page.locator('label:has-text("Priority"), :text("Priority")').last.wait_for(state="visible", timeout=8000)
                except:
                    pass
                
                # Scrape the modal to see if it actually has data
                scraped_data = scrape_incident_data(page)
                has_valid_data = bool(scraped_data.get("rc_description") and len(scraped_data["rc_description"]) > 5 and "required if" not in scraped_data["rc_description"].lower())

                if not is_unacknowledged and has_valid_data:
                    # 4A. Incident is completely filled and acknowledged. Learn & Store.
                    console.print(f"[cyan]Incident is completed. Learning values for '{characteristic_key}'...[/cyan]")
                    scraped_data["priority"] = scraped_data.get("priority", "Low") # fallback
                    
                    # Ensure we don't save garbage from UI error messages
                    is_garbage = False
                    invalid_terms = ["* required if", "alllowmediumhigh", "prioritystatus", "select incident", "still learning"]
                    for v in scraped_data.values():
                        if any(term in str(v).lower() for term in invalid_terms):
                            is_garbage = True
                            
                    if not is_garbage and "All" not in scraped_data.get("priority", ""):
                        relational_memory[characteristic_key] = scraped_data
                        relational_memory[characteristic_key]["saved_at"] = datetime.now().isoformat()
                        
                        existing = load_memory() or {}
                        if isinstance(existing, list):
                            existing = {}
                        existing.update(relational_memory)
                        
                        with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
                            json.dump(existing, f, indent=4)
                        console.print("✓ Incident saved to memory for future suggestions")
                    
                    filled_ids.add(target_id)
                    close_side_panel(page)
                    continue
                        
                else:
                    # 4B. Fill
                    if characteristic_key not in relational_memory:
                        console.print(f"  [dim]'{characteristic_key}' not in live memory. Checking incident_memory.json...[/dim]")
                        disk_memory = load_memory()
                        
                        found_in_disk = False
                        if isinstance(disk_memory, dict):
                            # Try exact match first
                            if characteristic_key in disk_memory:
                                relational_memory[characteristic_key] = disk_memory[characteristic_key]
                                found_in_disk = True
                            else:
                                # Try strict prefix/suffix matching to avoid greedy rubbish matches
                                clean_key = characteristic_key.replace("...", "").lower()
                                for mem_key, mem_data in disk_memory.items():
                                    clean_mem = mem_key.replace("...", "").lower()
                                    # Only match if one is a substantial prefix of the other, or exact match
                                    if clean_key.startswith(clean_mem) or clean_mem.startswith(clean_key):
                                        if len(clean_mem) > 5 or clean_mem == clean_key:
                                            relational_memory[characteristic_key] = mem_data
                                            found_in_disk = True
                                            break
                        else:
                            # Simple lookup from legacy memory list
                            for item in disk_memory:
                                # Strict match
                                if characteristic_key.replace("...", "").lower().startswith(item.get("title", "").lower()):
                                    relational_memory[characteristic_key] = {
                                        "priority": item.get("priority", "High"),
                                        "rc_description": item.get("root_cause_description", item.get("root_cause", "")),
                                        "rc_category": item.get("root_cause_category", item.get("category", "")),
                                        "rc_responsibility": item.get("root_cause_responsibility", item.get("responsibility", ""))
                                    }
                                    found_in_disk = True
                                    break
                                    
                        if not found_in_disk:
                            console.print(f"\n[bold yellow]⚠ UNKNOWN INCIDENT DETECTED:[/bold yellow] [cyan]'{characteristic_key}'[/cyan]")
                            console.print("[dim]I don't have the correct data for this. Let's map it now so I can automate it forever![/dim]")
                            
                            raw_input = input_with_timeout("Priority (Low/Medium/High) [15s timeout, Press Enter to skip]: ", timeout=15)
                            user_priority = raw_input.strip() if raw_input else ""
                            if user_priority:
                                user_desc = input("Root Cause Description: ").strip()
                                user_cat = input("Root Cause Category: ").strip()
                                user_resp = input("Root Cause Responsibility: ").strip()
                                
                                new_data = {
                                    "priority": user_priority,
                                    "rc_description": user_desc,
                                    "rc_category": user_cat,
                                    "rc_responsibility": user_resp,
                                    "saved_at": datetime.now().isoformat(),
                                    "user_provided": True
                                }
                                
                                relational_memory[characteristic_key] = new_data
                                existing = load_memory() or {}
                                if isinstance(existing, list): existing = {}
                                existing.update(relational_memory)
                                with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
                                    json.dump(existing, f, indent=4)
                                    
                                console.print("[green]✓ Saved to memory! Applying instantly...[/green]")
                            else:
                                console.print(f"  [yellow]Skipping '{characteristic_key}' temporarily. I will ask you again next sweep![/yellow]")
                                temporarily_skipped_ids.add(target_id)
                                close_side_panel(page)
                                continue
                            
                    matched_data = relational_memory[characteristic_key]
                    if not matched_data or not matched_data.get("rc_description"):
                        console.print("[yellow]  ⚠ Memory data is invalid. Skipping.[/yellow]")
                        filled_ids.add(target_id)
                        close_side_panel(page)
                        continue
                        
                    # Now the incident is ready. We can find the edit fields.
                    console.print(f"[cyan]Applying matched data for '{characteristic_key}'...[/cyan]")
                    
                    fill_data = {
                        "priority": matched_data.get("priority", "Low"),
                        "rc_description": matched_data["rc_description"],
                        "rc_category": matched_data["rc_category"],
                        "rc_responsibility": matched_data["rc_responsibility"]
                    }
                    
                    # Use explicit sequential Edit->Save logic
                    for field_key, field_value in fill_data.items():
                        # Prevent trying to type garbage values learned from old faulty scraping
                        if field_value and len(str(field_value)) > 2 and "required if" not in str(field_value).lower() and "alllowmediumhigh" not in str(field_value).lower():
                            try:
                                # Map internal keys to UI labels
                                lbl_map = {
                                    "priority": "Priority",
                                    "rc_description": "Root Cause Description",
                                    "rc_category": "Root Cause Category",
                                    "rc_responsibility": "Root Cause Responsibility"
                                }
                                lbl = lbl_map[field_key]
                                
                                # Locate the specific block for this label
                                if field_key == "priority":
                                    label_loc = page.locator(':text("Priority")').locator('visible=true').last
                                    if not label_loc.is_visible(timeout=1000):
                                        continue
                                        
                                    # For priority, the layout is slightly different. The Edit button is right next to the "Low Priority" badge.
                                    # Search up to 3 levels to find the Edit button
                                    parent_block = label_loc.locator("xpath=..")
                                    edit_selectors = 'button:has-text("Edit"), a:has-text("Edit"), :text-is("Edit"), button:has(svg.lucide-pen-square)'
                                    edit_btn = parent_block.locator(edit_selectors).locator('visible=true').first
                                    if not edit_btn.is_visible(timeout=500):
                                        parent_block = label_loc.locator("xpath=../..")
                                        edit_btn = parent_block.locator(edit_selectors).locator('visible=true').first
                                        if not edit_btn.is_visible(timeout=500):
                                            parent_block = label_loc.locator("xpath=../../..")
                                            edit_btn = parent_block.locator(edit_selectors).locator('visible=true').first
                                else:
                                    parent_block = page.locator(f'div:has(label:has-text("{lbl}"))').last
                                    if not parent_block.is_visible(timeout=1000):
                                        continue
                                    edit_btn = parent_block.locator('button:has-text("Edit")').first
                                    
                                if edit_btn.is_visible(timeout=1000):
                                    edit_btn.click()
                                    time.sleep(0.5)
                                else:
                                    if field_key == "priority":
                                        console.print("  [dim]DEBUG: Priority has no Edit button. Clicking parent block directly...[/dim]")
                                        parent_block.click(force=True)
                                        time.sleep(0.5)
                                    else:
                                        console.print(f"  [dim]DEBUG: Found label '{lbl}' but could not find 'Edit' button next to it![/dim]")
                                        continue
                                
                                # Find the true input field (excluding hidden UI state checkboxes)
                                input_field = parent_block.locator('input:not([type="checkbox"]):not([type="radio"]), textarea, select').first
                                
                                if input_field.is_visible(timeout=2000):
                                    tag = input_field.evaluate("el => el.tagName.toLowerCase()")
                                    
                                    if tag == "select":
                                        try:
                                            input_field.select_option(label=field_value, timeout=2000)
                                        except Exception:
                                            console.print(f"  [red]⚠ Could not select '{field_value}' in '{lbl}' dropdown (invalid option). Skipping.[/red]")
                                            page.keyboard.press("Escape")
                                            continue
                                    else:
                                        # Not a select, so type or use React fallback
                                        input_field.fill(str(field_value))
                                else:
                                    # Universal fallback for custom React/Angular dropdowns
                                    try:
                                        # Only click parent block if we didn't already click it for Priority
                                        if field_key != "priority":
                                            parent_block.click()
                                            time.sleep(0.5)
                                        
                                        option = page.locator(f'text="{field_value}"').last
                                        if option.is_visible(timeout=500):
                                            option.click()
                                        else:
                                            page.keyboard.type(str(field_value), delay=50)
                                            time.sleep(0.5)
                                            page.keyboard.press("Enter")
                                    except Exception:
                                        pass
                                    time.sleep(0.5)
                                        
                                save_btn = parent_block.locator('button:has-text("Save")').first
                                if save_btn.is_visible(timeout=1000):
                                    if save_btn.is_enabled(timeout=1000):
                                        save_btn.click()
                                        wait_for_success(f"Save {lbl}")
                                        time.sleep(0.5) # Give UI time to recover before clicking next Edit
                                    else:
                                        console.print(f"  [yellow]⚠ 'Save' button is disabled for {lbl} (invalid input). Escaping...[/yellow]")
                                        page.keyboard.press("Escape")
                                        time.sleep(1)
                                        
                            except Exception as e:
                                console.print(f"  [red]⚠ Failed to fill '{lbl}': {e}[/red]")
                                continue
                        else:
                            console.print(f"  [dim]DEBUG: Could not find any label for '{field_key}' on the screen![/dim]")
                                
                    # Finalize
                    finish_btn = page.locator('button:has-text("Finish Update")').first
                    if finish_btn.is_visible(timeout=1000):
                        finish_btn.click()
                        wait_for_success("Finish Update")
                        
                    filled_ids.add(target_id)
                    close_side_panel(page)

            console.print("\n[dim]Finished checking all visible incidents. Waiting 10 seconds before next refresh...[/dim]")
            time.sleep(10)

        except KeyboardInterrupt:
            console.print("\n[yellow]v3 Monitoring stopped by user.[/yellow]")
            break
        except Exception as e:
            console.print(f"[red]Error in v3 loop: {e}[/red]")
            time.sleep(5)


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    console.print(Panel(
        "[bold cyan]Sterling Bank Incident Bot[/bold cyan]\n"
        "Automates incident form filling on the observability portal",
        style="cyan"
    ))

    if "--v3" in sys.argv:
        console.print("[green]Bypassing menu: Starting v3 monitor directly from command line![/green]")
        action = "🔄 v3 Dynamic Memory (Real-time Monitor)"
    else:
        action = questionary.select(
            "What do you want to do?",
            choices=[
                "🔄 v3 Dynamic Relational Memory Monitor",
                "🧠 Learn from Past Incidents (Build Memory)",
                "🚀 Interactive Mode (Process incidents one-by-one)",
                "📋 View incident memory / past incidents",
                "🗑  Clear memory",
                "❌ Exit",
            ]
        ).ask()

    if "View" in action:
        view_memory()
        return

    if "Clear" in action:
        if questionary.confirm("Delete all saved incident data?").ask():
            MEMORY_FILE.unlink(missing_ok=True)
            console.print("[green]Memory cleared.[/green]")
        return

    if "Exit" in action:
        return

    is_learning = "Learn" in action
    
    with sync_playwright() as p:
        browser, _ = launch_or_connect(p)

        # Use existing page or open new one
        contexts = browser.contexts
        if contexts and contexts[0].pages:
            page = contexts[0].pages[0]
        else:
            page = browser.new_page()

        if is_learning:
            learn_from_history(browser)
            return

        if "incident" not in page.url.lower() and "monitor" not in page.url.lower():
            console.print(f"[cyan]Navigating to incidents page...[/cyan]")
            try:
                page.goto(INCIDENT_URL, wait_until="domcontentloaded", timeout=30000)
            except:
                pass
        else:
            console.print(f"[cyan]Using already open page: {page.url}[/cyan]")
            
        console.print(f"[green]✓ Page ready: {page.title()}[/green]")

        time.sleep(2)  # Let dynamic content settle

        if "v3" in action.lower():
            v3_dynamic_monitor(page)
        else:
            process_incidents(page, auto_submit=True)

    console.print("\n[bold green]✓ Done! Bot finished.[/bold green]")


if __name__ == "__main__":
    main()
