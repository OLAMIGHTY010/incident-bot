import os
import json
from openai import AsyncOpenAI
from dotenv import load_dotenv
from rich.console import Console

console = Console()
load_dotenv()

async def predict_incident_fields(incident_key: str, memory_dict: dict) -> dict:
    """
    Uses OpenRouter to predict the dropdown fields for an unknown incident.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if api_key:
        api_key = api_key.replace("\\n", "").strip()
        
    if not api_key:
        return None, "OPENROUTER_API_KEY is not set in .env file."
        
    client = AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key
    )
    
    # Extract unique categories and responsibilities from memory to guide the AI
    known_categories = set()
    known_responsibilities = set()
    for data in memory_dict.values():
        cat = data.get("rc_category")
        resp = data.get("rc_responsibility")
        if cat and cat != "-": known_categories.add(cat)
        if resp and resp != "-": known_responsibilities.add(resp)
        
    system_prompt = f"""You are an autonomous IT Incident Management AI.
Your job is to read an incident key/name and predict the most appropriate Root Cause Description, Category, Responsibility, and Priority.

Rules:
1. Always return a raw JSON object containing exactly these keys: `priority`, `rc_category`, `rc_responsibility`, `rc_description`. Do NOT return any markdown wrapping.
2. `priority` must be exactly one of: "Low", "Medium", "High", "Critical". (Default to Low if unsure).
3. Choose the most logical `rc_category` from this known list: {list(known_categories)}. If none fit perfectly, invent a highly professional IT category.
4. Choose the most logical `rc_responsibility` from this known list: {list(known_responsibilities)}. If none fit perfectly, invent a highly professional IT responsibility (e.g. "Network Team", "DevOps", "Database Admin").
5. As an SRE, your `rc_description` MUST be a highly concise technical summary, strictly limited to 1-2 lines max. (E.g. if the name is 'document-api-timeout', the description could be 'Intermittent timeouts observed on the document API service.')
"""

    user_prompt = f"Please analyze and predict values for the incident key: '{incident_key}'"
    
    try:
        response = await client.chat.completions.create(
            model="google/gemini-2.5-flash", # Fast, cheap, and very smart JSON generator
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"},
            max_tokens=250
        )
        
        raw_output = response.choices[0].message.content
        predicted_data = json.loads(raw_output)
        
        # Ensure fallback defaults just in case
        for k in ["priority", "rc_category", "rc_responsibility", "rc_description"]:
            if k not in predicted_data:
                predicted_data[k] = "-"
                
        return predicted_data, None
        
    except Exception as e:
        return None, str(e)
