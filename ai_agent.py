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

    client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)

    # Extract unique categories and responsibilities from memory to guide the AI
    known_categories = set()
    known_responsibilities = set()
    for data in memory_dict.values():
        cat = data.get("rc_category")
        resp = data.get("rc_responsibility")
        if cat and cat != "-":
            known_categories.add(cat)
        if resp and resp != "-":
            known_responsibilities.add(resp)

    custom_rules = ""
    try:
        rules_path = os.path.join(os.path.dirname(__file__), "ai_rules.txt")
        if os.path.exists(rules_path):
            with open(rules_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if (
                    content
                    and not content.startswith("#")
                    or len(content.split("\n")) > 5
                ):
                    # Filter out purely commented files
                    real_lines = [
                        line
                        for line in content.split("\n")
                        if not line.strip().startswith("#")
                    ]
                    if real_lines:
                        custom_rules = "\n\nUser-Defined Rules:\n" + "\n".join(
                            real_lines
                        )
    except Exception:
        pass

    system_prompt = f"""You are an autonomous IT Incident Management AI.
Your job is to read an incident key/name and predict the most appropriate Root Cause Description, Category, Responsibility, and Priority.

Rules:
1. Always return a raw JSON object containing exactly these keys: `priority`, `rc_category`, `rc_responsibility`, `rc_description`. Do NOT return any markdown wrapping.
2. `priority` must be exactly one of: "Low", "Medium", "High", "Critical". (Default to Low if unsure).
3. If you do not know the category/responsibility, pick the most logical guess from the lists below.
4. Try to keep `rc_description` under 100 characters.

Known Categories:
{list(known_categories)}

Known Responsibilities:
{list(known_responsibilities)}{custom_rules}
"""

    user_prompt = (
        f"Please analyze and predict values for the incident key: '{incident_key}'"
    )

    try:
        response = await client.chat.completions.create(
            model="google/gemini-2.5-flash",  # Fast, cheap, and very smart JSON generator
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=250,
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
