import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from rich.console import Console
from rich.panel import Panel

import ai_agent

console = Console()

# ── Hooks for Dashboard ────────────────────────────────────────────────────
def default_logger(msg, bot_type="both"):
    print(f"[{bot_type.upper()}] {msg}")

def default_metric(key, val):
    pass

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
                console.print("[yellow]⚠ Modal stubbornly refused to close! Force reloading the page...[/yellow]")
                await page.reload(wait_until="domcontentloaded")
                try:
                    await page.locator('.bg-black.bg-opacity-50').locator('visible=true').last.wait_for(state="hidden", timeout=10000)
                except Exception:
                    pass
                await asyncio.sleep(5)
                return True
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
                    console.print(f"[dim]{bot_prefix}All incidents processed. Moving to Next Page...[/dim]")
                    await btn.click()
                    await asyncio.sleep(2)
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

# ── Thread 1: The Acknowledger (Tab 1) ──────────────────────────────────────
async def ack_worker(page):
    """Constantly scans for unacknowledged incidents and silences them to protect SLAs."""
    console.print("[Tab 1] [cyan]Tab 1 (Siren Silencer) is online.[/cyan]")
    while True:
        try:
            rows = await get_incident_rows(page)
            if not rows:
                await asyncio.sleep(5)
                continue
                
            processed_any = False
            for r in rows:
                try:
                    text = await r.inner_text()
                    text = text.strip()
                    if not text: continue
                    
                    parts = text.split()
                    if not parts: continue
                    first = parts[0]
                    
                    # Create a truly unique ID using the timestamp if available
                    unique_id = first
                    age_minutes = 0
                    match = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', text)
                    if match:
                        unique_id = f"{first}_{match.group(1)}"
                        try:
                            incident_time = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
                            # Dashboard timestamps are in UTC. Compare against current UTC time to prevent timezone bugs!
                            age_minutes = (datetime.utcnow() - incident_time).total_seconds() / 60
                        except Exception:
                            age_minutes = 0
                    else:
                        # Fallback to the first 60 chars of the row text if no timestamp
                        unique_id = text[:60]
                    
                    if unique_id in acked_ids:
                        continue
                        
                    # HUGE SPEEDUP: If the dashboard already prints "Acknowledged by", we don't even need to click it!
                    if re.search(r'(?i)acknowledged\s+by', text):
                        acked_ids.add(unique_id)
                        continue
                        
                    # Click it
                    await r.click(force=True)
                    await asyncio.sleep(1.0) # Ensure side panel animation finishes
                    
                    is_unacknowledged = False
                    ack_btn = page.locator('text=/^\\s*(Acknowledge|Assign to me)\\s*$/i').locator('visible=true').last
                    if await ack_btn.is_visible(timeout=3000):
                        is_unacknowledged = True
                            
                    if is_unacknowledged:
                        if age_minutes >= 5:
                            console.print(f"[Tab 1] [red]Incident is {int(age_minutes)} minutes old (>= 5 mins)! Skipping Acknowledgment.[/red]")
                            if metric_callback: metric_callback('ignored', 1)
                        else:
                            console.print("[Tab 1] [yellow]Unacknowledged incident detected! Silencing SLA clock...[/yellow]")
                            await ack_btn.click(force=True)
                            await asyncio.sleep(0.1) # Instant acknowledgement, no waiting for slow UI banners!
                            if metric_callback: metric_callback('acked', 1)
                            
                    # Add to acked_ids
                    acked_ids.add(unique_id)
                    await close_side_panel(page)
                    processed_any = True
                    
                except Exception as inner_e:
                    err_str = str(inner_e).lower()
                    if "execution context was destroyed" in err_str or "detached" in err_str or "target closed" in err_str or "not attached" in err_str:
                        break # Break inner loop, instantly fetch fresh rows
                    else:
                        raise # Bubble up to outer
                        
            if not processed_any:
                # Force the dashboard to fetch new incidents if a Refresh button exists!
                try:
                    refresh_btn = page.locator('button:has-text("Refresh"), button[title*="Refresh"], .lucide-refresh-cw').locator('visible=true').first
                    if await refresh_btn.is_visible(timeout=100):
                        await refresh_btn.click(force=True)
                except Exception:
                    pass
                await asyncio.sleep(1.0) # Scan every 1 second
                
        except Exception as e:
            if "execution context was destroyed" not in str(e).lower():
                console.print(f"[Tab 1 Error] {e}")
            await asyncio.sleep(2)

# ── Thread 2: The Filler (Tab 2) ────────────────────────────────────────────
async def fill_worker(page):
    """Scans for acknowledged incidents and fills them, prompting user safely if unknown."""
    console.print("[Tab 2] [cyan]Tab 2 (Background Filler) is online.[/cyan]")
    while True:
        try:
            rows = await get_incident_rows(page)
            if not rows:
                await asyncio.sleep(5)
                continue
                
            processed_any = False
            for r in rows:
                try:
                    text = await r.inner_text()
                    text = text.strip()
                    if not text: continue
                    
                    parts = text.split()
                    if not parts: continue
                    first = parts[0]
                    
                    unique_id = first
                    match = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', text)
                    if match:
                        unique_id = f"{first}_{match.group(1)}"
                    else:
                        unique_id = text[:60]
                    
                    if unique_id in filled_ids or unique_id in temporarily_skipped_ids:
                        continue
                    
                    # Open modal
                    await r.click(force=True)
                    await asyncio.sleep(0.5)
                
                    try:
                        await page.locator('label:has-text("Priority"), :text("Priority")').last.wait_for(state="visible", timeout=8000)
                    except Exception:
                        pass
                    
                    # We no longer explicitly skip unacknowledged incidents. 
                    # If the dashboard allows editing them without acknowledging, the bot will successfully fill them!
                        
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
                                temporarily_skipped_ids.add(unique_id)
                                await close_side_panel(page)
                                
                                if unknown_incident_callback:
                                    unknown_incident_callback(characteristic_key)
                                    
                                continue
                                
                        # Start filling
                        console.print(f"[Tab 2] [cyan]Applying data for '{characteristic_key}'...[/cyan]")
                    
                        fill_data = {
                            "priority": matched_data.get("priority", "Low"),
                            "rc_description": matched_data["rc_description"],
                            "rc_category": matched_data["rc_category"],
                            "rc_responsibility": matched_data["rc_responsibility"]
                        }
                    
                        for field_key, field_value in fill_data.items():
                            if field_value and len(str(field_value)) > 2 and "required if" not in str(field_value).lower() and "alllowmediumhigh" not in str(field_value).lower():
                                try:
                                    lbl_map = {
                                        "priority": "Priority",
                                        "rc_description": "Root Cause Description",
                                        "rc_category": "Root Cause Category",
                                        "rc_responsibility": "Root Cause Responsibility"
                                    }
                                    lbl = lbl_map[field_key]
                                
                                    if field_key == "priority":
                                        label_loc = page.locator(':text("Priority")').locator('visible=true').last
                                        if not await label_loc.is_visible(timeout=1000):
                                            continue
                                        
                                        parent_block = label_loc.locator("xpath=..")
                                        edit_selectors = 'button:has-text("Edit"), a:has-text("Edit"), :text-is("Edit"), button:has(svg.lucide-pen-square)'
                                        edit_btn = parent_block.locator(edit_selectors).locator('visible=true').first
                                        if not await edit_btn.is_visible(timeout=500):
                                            parent_block = label_loc.locator("xpath=../..")
                                            edit_btn = parent_block.locator(edit_selectors).locator('visible=true').first
                                            if not await edit_btn.is_visible(timeout=500):
                                                parent_block = label_loc.locator("xpath=../../..")
                                                edit_btn = parent_block.locator(edit_selectors).locator('visible=true').first
                                    else:
                                        parent_block = page.locator(f'div:has(label:has-text("{lbl}"))').last
                                        if not await parent_block.is_visible(timeout=1000):
                                            continue
                                        edit_btn = parent_block.locator('button:has-text("Edit")').first
                                    
                                    if await edit_btn.is_visible(timeout=1000):
                                        await edit_btn.click(force=True)
                                        await asyncio.sleep(1.0)
                                    else:
                                        if field_key == "priority":
                                            await parent_block.click(force=True)
                                            await asyncio.sleep(1.0)
                                        else:
                                            continue
                                        
                                    input_loc = parent_block.locator('input:not([type="checkbox"]):not([type="radio"]), textarea, select, .selected-option-class').first
                                    if await input_loc.is_visible(timeout=2000):
                                        tag = await input_loc.evaluate("el => el.tagName.toLowerCase()")
                                        if tag == "select" or "selected-option-class" in await input_loc.evaluate("el => el.className"):
                                            dropdown_btn = parent_block.locator('button[aria-haspopup="listbox"], button:has(svg.lucide-chevron-down), .lucide-chevron-down').first
                                            if await dropdown_btn.is_visible(timeout=1000):
                                                await dropdown_btn.click(force=True)
                                                await asyncio.sleep(1.0)
                                                option = page.locator(f'[role="option"]:has-text("{field_value}")').first
                                                if await option.is_visible(timeout=1000):
                                                    await option.click(force=True)
                                                    await asyncio.sleep(0.5)
                                                else:
                                                    await page.keyboard.press("Escape")
                                        else:
                                            await input_loc.fill(str(field_value))
                                            await asyncio.sleep(0.5)
                                        
                                        save_btn = parent_block.locator('button:has-text("Save"), :text-is("Save")').first
                                        if await save_btn.is_visible(timeout=1500):
                                            await save_btn.click(force=True)
                                            await asyncio.sleep(1.5)
                                except Exception as e:
                                    pass
                                
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
                if not moved_next:
                    console.print("[Tab 2] [dim]Queue exhausted. Resting for 30 seconds before next sweep...[/dim]")
                    await asyncio.sleep(30)
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
