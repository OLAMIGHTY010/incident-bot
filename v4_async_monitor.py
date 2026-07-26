import asyncio
import json
import os
import re
import sys
import time
import csv
from datetime import datetime
from pathlib import Path

try:
    from plyer import notification
except ImportError:
    notification = None

from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from rich.console import Console
from rich.panel import Panel

import ai_agent

console = Console()

# ── Hooks for Dashboard ────────────────────────────────────────────────────
def default_logger(msg, bot_type="both"):
    print(f"[{bot_type.upper()}] {msg}")

def default_metric(key, val):
    auto_save_metrics_csv(key, val)

def auto_save_metrics_csv(key, val):
    os.makedirs("reports", exist_ok=True)
    file_path = "reports/metrics.csv"
    file_exists = os.path.isfile(file_path)
    with open(file_path, mode='a', newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "key", "value"])
        writer.writerow([datetime.now().isoformat(), key, val])

# Global callbacks for Dashboard Integration
log_callback = default_logger
metric_callback = default_metric
status_callback = None
unknown_incident_callback = None

def emit_log(msg, bot_type="both"):
    if log_callback:
        log_callback(msg, bot_type)

def emit_metric(key, val):
    metric_callback(key, val)

# Override console.print to intercept logs for the dashboard
def _intercept_print(*args, **kwargs):
    if args:
        msg = str(args[0])
        # Print safely to terminal, replacing unknown characters so Windows cp1252 doesn't crash
        try:
            safe_msg = msg.encode(sys.stdout.encoding or 'ascii', 'replace').decode(sys.stdout.encoding or 'ascii')
            print(safe_msg)
        except Exception:
            pass
            
        bot_type = "both"
        if "[Tab 1]" in msg:
            bot_type = "ack"
        elif "[Tab 2]" in msg:
            bot_type = "fill"
            
        # Strip rich formatting tags for the websocket
        clean_msg = re.sub(r'\[.*?\]', '', msg)
        clean_msg = clean_msg.replace("Tab 1 ", "").replace("Tab 2 ", "").replace("()", "").strip()
        emit_log(clean_msg, bot_type)
console.print = _intercept_print

# ── Config ─────────────────────────────────────────────────────────────────
BASE_URL = "https://sterlingobservability-sterlingbankng.msappproxy.net/one-monitor-v2/incidents"
MEMORY_FILE = Path("incident_memory.json")
ACK_TRACKER_FILE = Path("ack_tracker.json")
CHROME_PORT = 9222

# ── Shared State & Locks ───────────────────────────────────────────────────
acked_ids = set()
filled_ids = set()
temporarily_skipped_ids = set()

# Since we use asyncio, dictionary operations are mostly safe due to single-thread event loop,
# but file operations yield control, so we must lock when writing to JSON.
memory_lock = asyncio.Lock()


# ── Async User Prompting ───────────────────────────────────────────────────
async def async_input_with_timeout(prompt: str, timeout: int = 15) -> str:
    """Non-blocking prompt. If no input within timeout, raises asyncio.TimeoutError."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    loop = asyncio.get_event_loop()
    
    # We run the blocking input() in a separate executor thread so it doesn't freeze the async loop!
    # Because input() blocks the terminal, if it times out, the input() remains blocking in the background thread.
    # To mitigate console mess, we just use standard executor wait.
    task = loop.run_in_executor(None, sys.stdin.readline)
    try:
        result = await asyncio.wait_for(task, timeout=timeout)
        return result.strip()
    except asyncio.TimeoutError:
        if notification:
            notification.notify(title="Manual Action Required", message=prompt, timeout=10)
        sys.stdout.write('\n')
        raise


# ── Memory / Smart Suggestions ─────────────────────────────────────────────
def load_memory_sync() -> list | dict:
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text())
        except Exception:
            pass
    return {}

async def load_memory() -> list | dict:
    # Use sync for simple reads, no yield needed for tiny JSON reads
    return load_memory_sync()

async def save_to_memory(relational_updates: dict):
    async with memory_lock:
        existing = load_memory_sync()
        if isinstance(existing, list):
            existing = {}
        existing.update(relational_updates)
        MEMORY_FILE.write_text(json.dumps(existing, indent=4))


# ── Chrome Connection ───────────────────────────────────────────────────────
async def close_side_panel(page):
    """Aggressively attempts to close an open incident side panel."""
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
        
        try:
            await page.evaluate("""() => {
                let btns = Array.from(document.querySelectorAll('button, a'));
                let closeBtn = btns.find(b => {
                    let txt = b.innerText ? b.innerText.trim().toLowerCase() : '';
                    let html = b.innerHTML ? b.innerHTML.toLowerCase() : '';
                    return txt === 'x' || txt === 'close' || txt === 'cancel' || html.includes('fa-times') || html.includes('lucide-x');
                });
                if(closeBtn) closeBtn.click();
            }""")
        except Exception:
            pass
            
        await asyncio.sleep(0.5)
        
        try:
            backdrop = page.locator('.fixed.inset-0.bg-black, .bg-black.bg-opacity-50').locator('visible=true').last
            if await backdrop.is_visible(timeout=200):
                await backdrop.click(position={"x": 10, "y": 10}, force=True)
                await asyncio.sleep(0.5)
        except Exception:
            pass
            
        try:
            stuck_backdrop = page.locator('.fixed.inset-0.bg-black, .bg-black.bg-opacity-50').locator('visible=true').last
            if await stuck_backdrop.is_visible(timeout=500):
                # We intentionally do nothing here. The dashboard's CSS often leaves a ghost backdrop that isn't actually blocking anything.
                pass
        except Exception:
            pass
            
    except Exception as e:
        console.print(f"[dim]Error closing panel: {e}[/dim]")
        
    return False


import urllib.request
import json

async def launch_or_connect(playwright):
    try:
        browser = await playwright.chromium.connect_over_cdp(f"http://localhost:{CHROME_PORT}")
        console.print(f"[green]Connected to your existing Chrome session directly![/green]")
        return browser
    except Exception as e:
        console.print(f"[red]Could not connect to Chrome on port 9222: {e}[/red]")
        raise Exception("Chrome CDP Connection Failed")


# ── Incident List Handler ───────────────────────────────────────────────────
async def get_incident_rows(page) -> list:
    selectors = [
        "tbody tr",
        "table tr:not(:first-child)",
        "tr",
        "[class*='incident-row']",
        "[class*='alert-row']",
        "[data-testid*='incident']",
        "li[class*='incident']",
    ]
    for sel in selectors:
        rows = await page.query_selector_all(sel)
        if rows:
            return rows
    return []


async def scrape_incident_data(page) -> dict:
    data = {}
    fields_to_scrape = {
        "rc_description": ["Root Cause Description"],
        "rc_category": ["Root Cause Category"],
        "rc_responsibility": ["Root Cause Responsibility"]
    }
    
    for key, labels in fields_to_scrape.items():
        for lbl in labels:
            try:
                label_loc = page.locator(f':text("{lbl}")').locator('visible=true').last
                if await label_loc.is_visible(timeout=500):
                    parent = label_loc.locator("xpath=../..")
                    txt = await parent.inner_text()
                    txt = txt.strip()
                    val = txt.replace(lbl, "", 1).strip()
                    val = re.sub(r'\bEdit\b', '', val, flags=re.IGNORECASE).strip()
                    val = re.sub(r'\bClose\b', '', val, flags=re.IGNORECASE).strip()
                    
                    lines = [line.strip() for line in val.split("\n") if line.strip()]
                    lines = [line for line in lines if not re.match(r'(?i)^(approved|acknowledged) by', line)]
                    if lines:
                        val = lines[0]
                        
                    garbage = ["AllLowMediumHigh", "PriorityStatus", "Select incident category", "Still learning..."]
                    if val and not any(g in val for g in garbage):
                        data[key] = val
                        break
            except Exception:
                continue
    return data

async def wait_for_success(page, action_name, tab_prefix=""):
    try:
        banner = page.locator('text="Success", .toast, .Toastify, [role="alert"]').first
        await banner.wait_for(state="visible", timeout=1500)
        console.print(f"  {tab_prefix}[green]✓ Success banner seen for {action_name}[/green]")
        return True
    except Exception:
        console.print(f"  {tab_prefix}[yellow]⚠ No success banner seen for {action_name}[/yellow]")
        return False

async def goto_page(page, target_page, bot_prefix="[Tab 2] "):
    """Navigate to a specific page number by clicking Next repeatedly."""
    if target_page <= 1:
        return
    console.print(f"{bot_prefix}[dim]Returning to page {target_page}...[/dim]")
    for _ in range(target_page - 1):
        next_selectors = [
            'button[aria-label*="Next"]', 'button[aria-label*="next"]',
            'button[title*="Next"]', 'button:has-text("Next")',
            'a:has-text("Next")', '.lucide-chevron-right', 'button.next-page'
        ]
        clicked = False
        for sel in next_selectors:
            try:
                btn = page.locator(sel).locator('visible=true').first
                if await btn.is_visible(timeout=300):
                    is_disabled = await btn.evaluate("el => el.disabled || el.getAttribute('aria-disabled') === 'true'")
                    if not is_disabled:
                        await btn.click()
                        await asyncio.sleep(2.0)
                        clicked = True
                        break
            except Exception:
                continue
        if not clicked:
            break  # Can't go further — stop


async def paginate_or_refresh(page, bot_prefix=""):
    """Tries to click the 'Next' page button. If it can't (last page), it reloads to reset to Page 1."""
    next_selectors = [
        'button[aria-label*="Next"]',
        'button[aria-label*="next"]',
        'button[title*="Next"]',
        'button:has-text("Next")',
        'a:has-text("Next")',
        '.lucide-chevron-right',
        '.fa-chevron-right',
        'button.next-page'
    ]
    for sel in next_selectors:
        try:
            btn = page.locator(sel).locator('visible=true').first
            if await btn.is_visible(timeout=200):
                is_disabled = await btn.evaluate("el => el.disabled || el.classList.contains('disabled') || el.getAttribute('aria-disabled') === 'true'")
                if not is_disabled:
                    if bot_prefix == "[Tab 2] ":
                        console.print(f"[dim]{bot_prefix}Fast-forwarding to Next Page...[/dim]")
                    else:
                        console.print(f"[dim]{bot_prefix}All incidents processed. Moving to Next Page...[/dim]")
                    await btn.click()
                    await asyncio.sleep(5.0) # Increased to wait for React state to update
                    return True
        except Exception:
            continue
            
    # If we reach here, there's no Next button, or it's disabled (we are on the last page)
    console.print(f"[dim]{bot_prefix}End of queue. Refreshing to jump back to Page 1...[/dim]")
    try:
        # Some dashboards have a "Refresh" button, we try clicking that first
        refresh_btn = page.locator('button:has-text("Refresh"), button[title*="Refresh"], .lucide-refresh-cw').locator('visible=true').first
        if await refresh_btn.is_visible(timeout=500):
            await refresh_btn.click()
            await asyncio.sleep(2)
        else:
            await page.reload(wait_until="domcontentloaded")
            await asyncio.sleep(3)
    except Exception:
        await asyncio.sleep(2)
        
    return False


def parse_incident_age(text, first_word):
    """Parse the incident age in minutes from the row text.
    Handles two formats:
    1. Row text contains 'YYYY-MM-DD HH:MM:SS'
    2. Incident ID starts with 'YYYYMMDDHHMM-' (e.g. '202607181131-altpro-...')
    Returns (unique_id, age_minutes, incident_time_str)
    """
    unique_id = first_word
    age_minutes = 0
    incident_time_str = None
    
    # Try format 1: YYYY-MM-DD HH:MM:SS in the row text
    match = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', text)
    if match:
        incident_time_str = match.group(1)
        try:
            incident_time = datetime.strptime(incident_time_str, "%Y-%m-%d %H:%M:%S")
            age_minutes = abs((datetime.now() - incident_time).total_seconds() / 60)
        except Exception:
            age_minutes = 0
    else:
        # Try format 2: YYYYMMDDHHMM at the start of the incident ID
        id_match = re.match(r'(\d{12})', first_word)
        if id_match:
            ts = id_match.group(1)  # e.g. '202607181131'
            incident_time_str = ts
            unique_id = first_word
            try:
                # The incident ID is generated by the backend in UTC
                incident_time = datetime.strptime(ts, "%Y%m%d%H%M")
                age_minutes = abs((datetime.utcnow() - incident_time).total_seconds() / 60)
            except Exception:
                age_minutes = 0
        else:
            unique_id = text[:60]
    
    return unique_id, age_minutes, incident_time_str

# ── Thread 1: The Acknowledger (Tab 1) ──────────────────────────────────────
def load_ack_tracker():
    """Load the last seen incident timestamp from disk."""
    if ACK_TRACKER_FILE.exists():
        try:
            data = json.loads(ACK_TRACKER_FILE.read_text())
            return data.get("last_incident_time", None), data.get("acked_ids", [])
        except Exception:
            pass
    return None, []

def save_ack_tracker(last_time, recent_ids):
    """Save the last seen incident timestamp and recent IDs to disk."""
    try:
        ACK_TRACKER_FILE.write_text(json.dumps({
            "last_incident_time": last_time,
            "acked_ids": list(recent_ids)[-200:],  # Keep last 200 IDs
            "updated_at": datetime.now().isoformat()
        }, indent=2))
    except Exception:
        pass

async def ack_worker(page):
    """Constantly scans for unacknowledged incidents and silences them to protect SLAs."""
    console.print("[Tab 1] [cyan]Tab 1 (Siren Silencer) is online.[/cyan]")
    last_heartbeat = 0
    
    # Load previous state from disk so we know where we left off
    saved_time, saved_ids = load_ack_tracker()
    if saved_time:
        console.print(f"[Tab 1] [cyan]Resuming from last seen incident time: {saved_time}[/cyan]")
    for sid in saved_ids:
        acked_ids.add(sid)

    async def inject_observer():
        """Inject a MutationObserver into the page that signals us when the table changes."""
        try:
            await page.evaluate("""
                () => {
                    if (window.__incidentObserver) window.__incidentObserver.disconnect();
                    window.__tableChanged = false;
                    const target = document.querySelector('table tbody');
                    if (!target) return;
                    window.__incidentObserver = new MutationObserver(() => {
                        window.__tableChanged = true;
                    });
                    window.__incidentObserver.observe(target, { childList: true, subtree: true, characterData: true });
                }
            """)
        except Exception:
            pass

    async def table_has_changed():
        """Check and reset the JS flag."""
        try:
            changed = await page.evaluate("() => { const v = window.__tableChanged; window.__tableChanged = false; return v; }")
            return bool(changed)
        except Exception:
            return False

    async def scan_and_ack():
        """One fast scan of all visible rows. Returns True if any action was taken."""
        nonlocal saved_time
        rows_locator = page.locator('table tbody tr')
        rows_count = await rows_locator.count()
        if rows_count == 0:
            return False

        processed_any = False
        newest_time = saved_time

        for i in range(rows_count):
            try:
                r = rows_locator.nth(i)
                if not await r.is_visible():
                    continue
                text = await r.inner_text()
                text = text.strip()
                if not text:
                    continue
                parts = text.split()
                if not parts:
                    continue
                first = parts[0]

                unique_id, age_minutes, incident_time_str = parse_incident_age(text, first)

                if unique_id in acked_ids:
                    continue

                if incident_time_str:
                    if newest_time is None or incident_time_str > newest_time:
                        newest_time = incident_time_str

                if re.search(r'(?i)acknowledged\s+by', text) or re.search(r'(?i)acknowledged|resolved|closed', text):
                    acked_ids.add(unique_id)
                    continue

                if age_minutes >= 5:
                    if metric_callback: metric_callback('ignored', 1)
                    acked_ids.add(unique_id)
                    continue

                # NEW incident — click immediately!
                link = r.locator('td').nth(1).locator('span').first
                try:
                    await link.evaluate("el => el.scrollIntoView({block: 'center'})")
                    await asyncio.sleep(0.2)
                    await link.click(timeout=5000, force=True)
                except Exception:
                    await r.scroll_into_view_if_needed()
                    await r.click(force=True)
                await asyncio.sleep(0.5)

                ack_btn = page.locator('button:has-text("Acknowledge"), button:has-text("Assign to me")').locator('visible=true').last
                if await ack_btn.is_visible(timeout=1200):
                    console.print(f"[Tab 1] [green]⚡ '{first}' acknowledged![/green]")
                    await ack_btn.click(force=True)
                    await asyncio.sleep(0.15)
                    if metric_callback: metric_callback('acked', 1)

                acked_ids.add(unique_id)
                await close_side_panel(page)
                processed_any = True

            except Exception as inner_e:
                err_str = str(inner_e).lower()
                if any(k in err_str for k in ("context was destroyed", "detached", "target closed", "not attached")):
                    break
                continue

        if newest_time and newest_time != saved_time:
            saved_time = newest_time
            save_ack_tracker(saved_time, acked_ids)

        return processed_any

    # Inject the observer initially
    await inject_observer()
    last_reload = time.time()
    RELOAD_INTERVAL = 5.0   # Hard-reload every 5 seconds (only when idle)

    while True:
        try:
            now = time.time()

            # Hard reload on interval to fetch fresh data from the server
            if now - last_reload >= RELOAD_INTERVAL:
                await page.reload(wait_until="domcontentloaded")
                try:
                    await page.locator('table tbody tr').first.wait_for(state="visible", timeout=5000)
                except Exception:
                    pass
                await inject_observer()
                last_reload = time.time()

                if time.time() - last_heartbeat >= 60:
                    console.print("[Tab 1] [dim]Siren Silencer is active...[/dim]")
                    last_heartbeat = time.time()

            # Always scan after reload
            did_work = await scan_and_ack()
            if did_work:
                last_reload = time.time()  # Reset reload timer so we don't interrupt active work

            # Also scan immediately if the observer detected a table change
            if await table_has_changed():
                did_work = await scan_and_ack()
                if did_work:
                    last_reload = time.time()

            await asyncio.sleep(0.1)  # Tight loop — near-instant reaction

        except Exception as e:
            err = str(e).lower()
            if "context was destroyed" not in err and "net::err_aborted" not in err:
                console.print(f"[Tab 1 Error] {e}")
            await asyncio.sleep(2.0)
            last_reload = 0  # Force a reload on next iteration

# ── Thread 2: The Filler (Tab 2) ────────────────────────────────────────────
async def fill_worker(page):
    """Scans for acknowledged incidents and fills them, prompting user safely if unknown."""
    console.print("[Tab 2] [cyan]Tab 2 (Background Filler) is online.[/cyan]")
    incident_failure_counts = {}
    current_page = 1  # Track which page we are on
    
    while True:
        try:
            rows_locator = page.locator('table tbody tr')
            rows_count = await rows_locator.count()
            if rows_count == 0:
                await asyncio.sleep(5)
                continue
                
            processed_any = False
            consecutive_failures = 0
            
            for i in range(rows_count):
                try:
                    r = rows_locator.nth(i)
                    if not await r.is_visible():
                        continue
                        
                    text = await r.inner_text()
                    text = text.strip()
                    if not text: continue
                    
                    parts = text.split()
                    if not parts: continue
                    first = parts[0]
                    
                    # Parse age from incident ID or row text
                    unique_id, age_minutes, _ = parse_incident_age(text, first)
                    
                    if unique_id in filled_ids or unique_id in temporarily_skipped_ids:
                        continue
                    
                    # Open modal
                    link = r.locator('td').nth(1).locator('span').first
                    try:
                        await link.evaluate("el => el.scrollIntoView({block: 'center'})")
                        await asyncio.sleep(0.2)
                        await link.click(timeout=5000, force=True)
                    except Exception:
                        await r.scroll_into_view_if_needed()
                        await r.click(force=True)
                    await asyncio.sleep(0.5)
                    
                    # Verify modal opened using the standard dialog role
                    modal_indicator = page.locator('[role="dialog"], .fixed.inset-y-0.right-0, .lucide-x').locator('visible=true').first
                    if not await modal_indicator.is_visible(timeout=25000):
                        console.print(f"[Tab 2] [dim]Modal didn't seem to open for {first}, trying click again...[/dim]")
                        try:
                            await link.click(timeout=5000, force=True)
                            await asyncio.sleep(1.0)
                        except Exception as click_err:
                            console.print(f"[Tab 2] [red]Row click failed: {click_err}[/red]")
                            
                        # If it STILL didn't open, handle the failure
                        if not await modal_indicator.is_visible(timeout=25000):
                            incident_failure_counts[unique_id] = incident_failure_counts.get(unique_id, 0) + 1
                            if incident_failure_counts[unique_id] >= 2:
                                console.print(f"[Tab 2] [red]Cannot open modal for '{first}' after multiple retries. Permanently skipping this broken incident.[/red]")
                                filled_ids.add(unique_id)
                                consecutive_failures = 0  # Reset — this one is just broken, not the dashboard
                                continue
                                
                            consecutive_failures += 1
                            console.print(f"[Tab 2] [dim]Skipping '{first}' for now. (Failure {consecutive_failures}/5)[/dim]")
                            temporarily_skipped_ids.add(unique_id)
                            
                            if consecutive_failures >= 5:
                                console.print("[Tab 2] [red]5 consecutive failures — Dashboard appears frozen! Reloading...[/red]")
                                await page.reload(wait_until="domcontentloaded")
                                try:
                                    await page.locator('table tbody tr').first.wait_for(state="visible", timeout=8000)
                                except Exception:
                                    pass
                                await asyncio.sleep(2)
                                # Navigate BACK to the page we were on
                                await goto_page(page, current_page)
                                consecutive_failures = 0
                                break  # Break inner loop to restart sweep from top
                            continue
                            
                    # Reset failures on success
                    consecutive_failures = 0
                
                    # The user explicitly requested the filler to never acknowledge incidents.
                    # We will simply try to scrape/fill.
                    
                    # Scroll down to ensure fields are rendered if the dashboard uses lazy-loading!
                    await page.keyboard.press('PageDown')
                    await asyncio.sleep(0.5)
                    
                    try:
                        await page.locator('text="Root Cause Category"').last.wait_for(state="visible", timeout=2000)
                    except Exception:
                        pass
                        
                    scraped_data = await scrape_incident_data(page)
                    has_valid_data = False
                    for v in scraped_data.values():
                        v_str = str(v).strip().lower()
                        if len(v_str) > 2 and "required if" not in v_str and "alllowmediumhigh" not in v_str and "select incident" not in v_str:
                            has_valid_data = True
                            break
                
                    # Characteristic key
                    characteristic_key = first
                    # Strip ALL leading numbers, dashes, and underscores (to handle chained timestamps)
                    characteristic_key = re.sub(r'^[\d_-]+', '', characteristic_key).strip().lower()
                
                    if has_valid_data:
                        console.print(f"[Tab 2] [cyan]Incident '{characteristic_key}' is already filled. Learning values...[/cyan]")
                        scraped_data["priority"] = scraped_data.get("priority", "Low")
                    
                        is_garbage = False
                        invalid_terms = ["* required if", "alllowmediumhigh", "prioritystatus", "select incident", "still learning"]
                        for v in scraped_data.values():
                            if any(term in str(v).lower() for term in invalid_terms):
                                is_garbage = True
                            
                        if not is_garbage and "All" not in scraped_data.get("priority", ""):
                            scraped_data["saved_at"] = datetime.now().isoformat()
                            await save_to_memory({characteristic_key: scraped_data})
                        
                        filled_ids.add(unique_id)
                        await close_side_panel(page)
                        processed_any = True
                        break  # Break inner loop to fetch fresh rows (avoids detached DOM)
                    else:
                        # Form needs filling
                        disk_memory = await load_memory()
                        found_in_disk = False
                        matched_data = {}
                    
                        if isinstance(disk_memory, dict):
                            if characteristic_key in disk_memory:
                                matched_data = disk_memory[characteristic_key]
                                found_in_disk = True
                            else:
                                clean_key = characteristic_key.replace("...", "").lower()
                                for mem_key, mem_data in disk_memory.items():
                                    clean_mem = mem_key.replace("...", "").lower()
                                    if clean_key.startswith(clean_mem) or clean_mem.startswith(clean_key):
                                        if len(clean_mem) > 5 or clean_mem == clean_key:
                                            matched_data = mem_data
                                            found_in_disk = True
                                            break
                                        
                        if not found_in_disk:
                            console.print(f"\n[Tab 2] [bold yellow]⚠ UNKNOWN INCIDENT DETECTED:[/bold yellow] [cyan]'{characteristic_key}'[/cyan]")
                            console.print(f"[Tab 2] [dim]Querying OpenRouter AI for '{characteristic_key}'...[/dim]")
                            
                            predicted_data, ai_error = await ai_agent.predict_incident_fields(characteristic_key, disk_memory if isinstance(disk_memory, dict) else {})
                            
                            if predicted_data:
                                console.print(f"[Tab 2] [green]AI Auto-Filled '{characteristic_key}'![/green]")
                                if metric_callback: metric_callback('ai_filled', 1)
                                
                                matched_data = predicted_data
                                found_in_disk = True
                                
                                # Save the AI's prediction to memory permanently
                                predicted_data["saved_at"] = datetime.now().isoformat()
                                predicted_data["source"] = "ai"
                                await save_to_memory({characteristic_key: predicted_data})
                            else:
                                console.print(f"[Tab 2] [dim]AI Prediction failed ({ai_error}). Falling back to Manual Queue.[/dim]")
                                if log_callback: log_callback(f"[Tab 2] [red]AI Error: {ai_error}[/red]", "fill")
                                
                                # Send Windows Desktop Notification
                                if notification:
                                    try:
                                        notification.notify(
                                            title="Incident Bot Needs Help!",
                                            message=f"Manual filling required for: {characteristic_key}",
                                            app_name="Incident Bot",
                                            timeout=5
                                        )
                                    except Exception:
                                        pass
                                
                                if unknown_incident_callback:
                                    unknown_incident_callback(characteristic_key)
                                    
                                # Asynchronous behavior: skip it for now and continue working in the background!
                                temporarily_skipped_ids.add(unique_id)
                                await close_side_panel(page)
                                continue
                                
                        # Start filling
                        console.print(f"[Tab 2] [cyan]Applying data for '{characteristic_key}'...[/cyan]")
                    
                        fill_data = {
                            "rc_description": matched_data["rc_description"],
                            "rc_category": matched_data["rc_category"],
                            "rc_responsibility": matched_data["rc_responsibility"]
                        }
                    
                        fields_successfully_filled = 0
                    
                        for field_key, field_value in fill_data.items():
                            if field_value and len(str(field_value)) > 2 and "required if" not in str(field_value).lower() and "alllowmediumhigh" not in str(field_value).lower():
                                try:
                                    lbl_map = {
                                        "rc_description": "Root Cause Description",
                                        "rc_category": "Root Cause Category",
                                        "rc_responsibility": "Root Cause Responsibility"
                                    }
                                    lbl = lbl_map[field_key]
                                
                                    label_loc = page.locator(f':text("{lbl}")').locator('visible=true').last
                                    if not await label_loc.is_visible(timeout=3000):
                                        console.print(f"  [Tab 2] [red]✗ Label '{lbl}' not found on screen[/red]")
                                        continue
                                        
                                    parent_block = label_loc.locator("xpath=..")
                                    
                                    # Try to find an Edit button
                                    edit_selectors = 'button:has-text("Edit"), a:has-text("Edit"), :text-is("Edit"), button:has(svg.lucide-pen-square)'
                                    edit_btn = parent_block.locator(edit_selectors).locator('visible=true').first
                                    if not await edit_btn.is_visible(timeout=500):
                                        parent_block = label_loc.locator("xpath=../..")
                                        edit_btn = parent_block.locator(edit_selectors).locator('visible=true').first
                                        if not await edit_btn.is_visible(timeout=500):
                                            parent_block = label_loc.locator("xpath=../../..")
                                            edit_btn = parent_block.locator(edit_selectors).locator('visible=true').first
                                            
                                    if await edit_btn.is_visible(timeout=500):
                                        await edit_btn.click(force=True)
                                        await asyncio.sleep(1.0)
                                    else:
                                        # Click the block itself (for priority or direct-edit fields)
                                        await parent_block.click(force=True)
                                        await asyncio.sleep(1.0)
                                        
                                    # Find the input/select/textarea by expanding the parent block upwards!
                                    input_loc = None
                                    for level in ["..", "../..", "../../..", "../../../..", "../../../../..", "../../../../../.."]:
                                        test_loc = label_loc.locator(f"xpath={level}").locator('input:not([type="checkbox"]):not([type="radio"]), textarea, select').locator('visible=true').first
                                        if await test_loc.is_visible(timeout=200):
                                            input_loc = test_loc
                                            break
                                            
                                    input_filled = False
                                    if input_loc:
                                        tag = await input_loc.evaluate("el => el.tagName.toLowerCase()")
                                        if tag == "select":
                                            # Native select
                                            try:
                                                await input_loc.select_option(label=str(field_value), timeout=1000)
                                                input_filled = True
                                            except Exception:
                                                pass
                                        else:
                                            # Native input or textarea
                                            await input_loc.fill(str(field_value))
                                            input_filled = True
                                        await asyncio.sleep(0.5)
                                        
                                    if not input_filled:
                                        # Maybe it's a custom React dropdown.
                                        # We might need to click a dropdown trigger if "Edit" just revealed a closed dropdown box
                                        for level in ["..", "../..", "../../..", "../../../.."]:
                                            trigger = label_loc.locator(f"xpath={level}").locator('button[aria-haspopup], div[class*="control"], .lucide-chevron-down').locator('visible=true').first
                                            if await trigger.is_visible(timeout=200):
                                                await trigger.click(force=True)
                                                await asyncio.sleep(0.5)
                                                break
                                                
                                        option_selectors = f'[role="option"]:has-text("{field_value}"), option:has-text("{field_value}"), li:has-text("{field_value}"), div[class*="option"]:has-text("{field_value}")'
                                        option = page.locator(option_selectors).locator('visible=true').first
                                        if await option.is_visible(timeout=1500):
                                            await option.click(force=True)
                                            await asyncio.sleep(0.5)
                                            input_filled = True
                                            
                                    if not input_filled:
                                        console.print(f"  [Tab 2] [red]✗ Input/Option not visible for {lbl}[/red]")
                                        continue
                                        
                                    fields_successfully_filled += 1
                                        
                                    # Find and Click Save Button
                                    save_btn = None
                                    for level in ["..", "../..", "../../..", "../../../..", "../../../../.."]:
                                        test_save = label_loc.locator(f"xpath={level}").locator('button:has-text("Save"), :text-is("Save"), button.bg-blue-600').locator('visible=true').first
                                        if await test_save.is_visible(timeout=200):
                                            save_btn = test_save
                                            break
                                            
                                    if save_btn:
                                        await save_btn.click(force=True)
                                        await asyncio.sleep(1.5)
                                    else:
                                        console.print(f"  [Tab 2] [dim]Save button not found for {lbl} (assuming auto-save)[/dim]")
                                            
                                except Exception as e:
                                    console.print(f"  [Tab 2] [red]Error on {field_key}: {str(e)[:100]}[/red]")
                                    pass
                                
                        if fields_successfully_filled == 0:
                            console.print("[Tab 2] [red]Failed to fill ANY fields! Aborting Finish Update.[/red]")
                            temporarily_skipped_ids.add(unique_id)
                            await close_side_panel(page)
                            continue
                                
                        # Finish Update
                        finish_btn = page.locator('button:has-text("Finish Update")').first
                        if await finish_btn.is_visible(timeout=3000):
                            await finish_btn.click(force=True)
                            await wait_for_success(page, "Finish Update", "[Tab 2] ")
                            if metric_callback: metric_callback('filled', 1)
                        
                        filled_ids.add(unique_id)
                        await close_side_panel(page)
                        processed_any = True
                    
                except Exception as inner_e:
                    err_str = str(inner_e).lower()
                    if "execution context was destroyed" in err_str or "detached" in err_str or "target closed" in err_str or "not attached" in err_str:
                        break # Break inner loop, instantly fetch fresh rows
                    else:
                        raise # Bubble up to outer
                        
            if not processed_any:
                temporarily_skipped_ids.clear()
                moved_next = await paginate_or_refresh(page, "[Tab 2] ")
                if moved_next:
                    current_page += 1
                else:
                    console.print("[Tab 2] [dim]Sweep complete. Looping back to Page 1...[/dim]")
                    current_page = 1
                    await asyncio.sleep(5)
        except Exception as e:
            if "Execution context was destroyed" not in str(e):
                console.print(f"[Tab 2 Error] {e}")
            await asyncio.sleep(5)

# ── Main ────────────────────────────────────────────────────────────────────
async def main_ack():
    async with async_playwright() as p:
        browser = await launch_or_connect(p)
        contexts = browser.contexts
        if not contexts or not contexts[0].pages:
            console.print("[Tab 1] [red]No active Chrome pages found.[/red]")
            return
            
        tab1 = contexts[0].pages[0]
        console.print(Panel.fit(
            f"[bold green]Siren Bot - Acknowledger[/bold green]\n"
            f"[dim]Tracking Tab 1[/dim]\n"
            f"→ Tab 1: {tab1.url}"
        ))
        
        await ack_worker(tab1)

async def main_fill():
    async with async_playwright() as p:
        browser = await launch_or_connect(p)
        contexts = browser.contexts
        if not contexts or not contexts[0].pages:
            console.print("[Tab 2] [red]No active Chrome pages found.[/red]")
            return
            
        context = contexts[0]
        pages = context.pages
        
        if not pages:
            console.print("[red]No active Chrome pages found.[/red]")
            return
            
        tab1 = pages[0]
        # Open a second tab if needed
        if len(pages) < 2:
            console.print("[cyan]Opening Tab 2 for the Background Filler...[/cyan]")
            tab2 = await context.new_page()
            await tab2.goto(tab1.url)
            await tab2.wait_for_load_state("networkidle")
        else:
            tab2 = pages[1]
            console.print("[cyan]Using existing second tab for the Background Filler...[/cyan]")
            
        console.print(Panel(
            "[bold green]Dual-Tab Architecture Initialized![/bold green]\n"
            "Tab 1 is monitoring SLAs (Siren Silencer).\n"
            "Tab 2 is filling forms & learning.",
            title="Sterling Bank Bot V4 Async"
        ))
        
        # Run fill worker
        await fill_worker(tab2)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("[green]Bot stopped.[/green]")
