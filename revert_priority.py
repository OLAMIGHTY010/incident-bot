import os

with open('incident_bot.py', 'r', encoding='utf-8') as f:
    content = f.read()

search_loop = '''                        lbl = labels[field_key]
                        
                        # --- PRIORITY DIRECT BUTTON CLICK ---
                        if field_key == "priority":
                            val = str(field_value).strip()
                            if val and val != "All":
                                # Try to find a clickable button for the priority value
                                btn_loc = page.locator(f'div[role="dialog"] button:has-text("{val}"), aside button:has-text("{val}"), .modal button:has-text("{val}")').locator('visible=true').first
                                if not btn_loc.is_visible(timeout=500):
                                    btn_loc = page.locator(f'button:has-text("{val}"), [data-priority="{val.lower()}"], [aria-label*="{val}" i], li:has-text("{val}"), div[role="option"]:has-text("{val}")').locator('visible=true').first
                                
                                if btn_loc.is_visible(timeout=1000):
                                    btn_loc.click()
                                    time.sleep(1)
                                    console.print(f"  [green]✓[/green] Directly clicked Priority option: {val}")
                                else:
                                    # Try clicking the label first if it's a dropdown
                                    label_loc = page.locator('aside :text-is("Priority"), aside :text-is("Incident Priority"), div[role="dialog"] :text-is("Priority")').locator('visible=true').first
                                    if not label_loc.is_visible(timeout=500):
                                        label_loc = page.locator('p:text-is("Priority"), label:text-is("Priority"), span:text-is("Priority")').locator('visible=true').last
                                        
                                    if label_loc.is_visible(timeout=500):
                                        label_loc.click()
                                        time.sleep(1)
                                        btn_loc = page.locator(f'text="{val}"').locator('visible=true').first
                                        if btn_loc.is_visible(timeout=1000):
                                            btn_loc.click()
                                            console.print(f"  [green]✓[/green] Clicked Priority dropdown option: {val}")
                                        else:
                                            console.print(f"  [dim]DEBUG: Could not find Priority option for '{val}' even after clicking label![/dim]")
                                    else:
                                        console.print(f"  [dim]DEBUG: Could not find Priority button or label for '{val}' on the screen![/dim]")
                            continue
                        # --- END PRIORITY ---
                        
                        label_loc = page.locator(f'p:has-text("{lbl}")').first'''

# Let's completely strip out Priority from fill_data and labels!
search_fill = '''                    fill_data = {
                        "priority": matched_data.get("priority", "Low"),
                        "rc_description": matched_data["rc_description"],
                        "rc_category": matched_data["rc_category"],
                        "rc_responsibility": matched_data["rc_responsibility"]
                    }'''
new_fill = '''                    fill_data = {
                        "rc_description": matched_data["rc_description"],
                        "rc_category": matched_data["rc_category"],
                        "rc_responsibility": matched_data["rc_responsibility"]
                    }'''
if search_fill in content:
    content = content.replace(search_fill, new_fill)

search_labels = '''                        labels = {
                            "priority": "Priority",
                            "rc_description": "Root Cause Description",
                            "rc_category": "Root Cause Category",
                            "rc_responsibility": "Root Cause Responsibility"
                        }'''
new_labels = '''                        labels = {
                            "rc_description": "Root Cause Description",
                            "rc_category": "Root Cause Category",
                            "rc_responsibility": "Root Cause Responsibility"
                        }'''
if search_labels in content:
    content = content.replace(search_labels, new_labels)

if search_loop in content:
    content = content.replace(search_loop, '                        lbl = labels[field_key]\n                        label_loc = page.locator(f\'p:has-text("{lbl}")\').first')

with open('incident_bot.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("SUCCESS: Reverted Priority logic.")
