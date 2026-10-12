from __future__ import annotations

import json
import logging

from openai import AsyncOpenAI

logger = logging.getLogger("uvicorn.error")

EXTRACTION_FIELDS = [
    "ModuleName",
    "MenuName",
    "Caption",
    "SectionName",
    "UnderName",
    "TypeName",
    "NatureName",
    "SortOrder",
    "StatusName",
    "FromVersion",
    "ToVersion",
]

EXTRACTION_PROMPT = """You extract menu-creation details from a user's message for the GoodBooks Menu Creation Agent.

Return ONLY a JSON object with exactly these keys, nothing else:
{
  "ModuleName": "",
  "MenuName": "",
  "Caption": "",
  "SectionName": "",
  "UnderName": "",
  "TypeName": "",
  "NatureName": "",
  "SortOrder": "",
  "StatusName": "",
  "FromVersion": "",
  "ToVersion": ""
}

RULES:
- Extract ONLY what the user explicitly states in this message. Never guess, infer, or default a value.
- Keep values as the user wrote them (e.g. "framework", "Master", "2") -- do not resolve them against any picklist, do not invent ids.
- MenuName is the name of the menu being created, e.g. "menu name test" -> MenuName="test". Do not confuse it with ModuleName.
- Caption is the caption/description text for the menu, e.g. "the caption is test form" -> Caption="test form". Do not confuse it with MenuName even if the wording or value looks similar.
- SortOrder, FromVersion, ToVersion are numbers if mentioned, as plain strings (e.g. "2").
- If a field isn't mentioned, its value is "" (empty string). Never omit a key.
- Output must be valid JSON only -- no markdown, no code fences, no explanation.

EXAMPLE:
User: "i want to create a menu for Frame Work module with the menu name test and the caption is test form and the sort order is 2"
Output:
{
  "ModuleName": "Frame Work",
  "MenuName": "test",
  "Caption": "test form",
  "SectionName": "",
  "UnderName": "",
  "TypeName": "",
  "NatureName": "",
  "SortOrder": "2",
  "StatusName": "",
  "FromVersion": "",
  "ToVersion": ""
}
"""


async def extract_known_fields(openai_client: AsyncOpenAI, model: str, message: str) -> dict:
    try:
        resp = await openai_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": EXTRACTION_PROMPT},
                {"role": "user", "content": message},
            ],
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
    except Exception as e:
        logger.warning("[EXTRACTION] failed, skipping pre-fill for this message: %s", e)
        return {k: "" for k in EXTRACTION_FIELDS}

    return {k: str(data.get(k, "") or "").strip() for k in EXTRACTION_FIELDS}


def merge_known_slots(known_slots: dict, newly_extracted: dict) -> dict:
    merged = dict(known_slots)
    for key, value in newly_extracted.items():
        if value:
            merged[key] = value
    return merged


def format_known_slots_reminder(known_slots: dict) -> str | None:
    present = {k: v for k, v in known_slots.items() if v}
    if not present:
        return None

    lines = [
        "The user has already provided the following information earlier in this "
        "conversation. Do NOT ask for these again. Resolve each one against the "
        "matching tool (e.g. call list_modules and match ModuleName against the "
        "real module names, call list_sections and match SectionName, etc.) and "
        "treat that step as already answered. If a value does not clearly match "
        "any option returned by the tool, ask the user to pick from the real "
        "options instead of guessing. Then continue with the next step that is "
        "still missing, following the normal step order.",
        "",
        "Known so far:",
    ]
    label_map = {
        "ModuleName": "Module",
        "MenuName": "Menu Name",
        "Caption": "Caption",
        "SectionName": "Section",
        "UnderName": "Under (Parent Menu)",
        "TypeName": "Type",
        "NatureName": "Nature",
        "SortOrder": "Sort Order",
        "StatusName": "Status",
        "FromVersion": "From Version",
        "ToVersion": "To Version",
    }
    for key, value in present.items():
        lines.append(f"- {label_map.get(key, key)}: {value}")

    return "\n".join(lines)