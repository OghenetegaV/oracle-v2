"""Oracle — AI Layout Generator (single floor)

Purpose:
    Builds the prompt from parsed architectural geometry and engineer notes, calls Claude (with
    retries and JSON-fence stripping) and saves the proposed structural layout as
    output_json/ga_output.json (columns, beams, slabs).

Role in Oracle:
    Legacy AI layout step. Claude currently proposes the layout; under the V2 principle it
    should only ever propose, with the engineer deciding.

Dependencies:
    anthropic (imported inside call_claude); config; oracle_log.

Consumers:
    oracle_wizard, oracle_pipeline.

Status:
    Legacy / Transitional.

Migration:
    Retained. Later, its output becomes proposed elements/decisions in oracle.core and the
    prompt is rendered from the project's decisions and design basis.
"""

import json
import os
from pathlib import Path
from config import OUTPUT_JSON_DIR, INPUT_DIR, get_api_key
from oracle_log import log_event, LOG_PATH

ANTHROPIC_API_KEY = get_api_key()

if not ANTHROPIC_API_KEY:
    print("⚠️  ANTHROPIC_API_KEY not set. Get it from: https://console.anthropic.com/")
    print("   Either set it as an environment variable, or run oracle_wizard.py, "
          "which will ask for it once and save it locally.")


def _strip_json_fences(text):
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()

def load_parsed_geometry(json_file):
    """Load parsed DXF geometry"""
    path = OUTPUT_JSON_DIR / json_file
    with open(path, 'r') as f:
        return json.load(f)

def generate_ga_prompt(geometry, storey_height_m=3.0, slab_thickness_hint_mm=(150, 200),
                        engineer_notes=None, element_notes=None):
    """Create Claude prompt for GA generation"""
    thickness_lo, thickness_hi = slab_thickness_hint_mm
    notes_block = f"\nADDITIONAL REQUIREMENTS FROM THE ENGINEER (follow these, they override defaults below):\n{engineer_notes}\n" if engineer_notes else ""
    if element_notes:
        notes_block += (
            "\nPER-ELEMENT NOTES FROM THE ENGINEER (these reference names from a previous "
            "layout attempt -- keep using the same name for that element if it still applies "
            "to the same position/role):\n" +
            "\n".join(f"- {name}: {note}" for name, note in element_notes.items()) + "\n"
        )

    # Format geometry for Claude
    walls_summary = f"{len(geometry['walls'])} wall segments"
    columns_summary = f"{len(geometry['columns'])} columns"
    gridlines_summary = f"{len(geometry['gridlines'])} gridlines"
    
    walls_text = "\n".join([
        f"  Wall {i}: ({w['start'][0]}, {w['start'][1]}) → ({w['end'][0]}, {w['end'][1]})"
        for i, w in enumerate(geometry['walls'])
    ])
    
    columns_text = "\n".join([
        f"  Column {i}: center ({c['center'][0]}, {c['center'][1]})"
        for i, c in enumerate(geometry['columns'])
    ])
    
    gridlines_text = "\n".join([
        f"  Gridline {i}: ({g['start'][0]}, {g['start'][1]}) → ({g['end'][0]}, {g['end'][1]})"
        for i, g in enumerate(geometry['gridlines'])
    ])
    
    prompt = f"""You are a structural engineer creating a General Arrangement (GA) drawing.

ARCHITECTURAL GEOMETRY EXTRACTED FROM DRAWING:
Walls ({walls_summary}):
{walls_text}

Columns ({columns_summary}):
{columns_text}

Gridlines ({gridlines_summary}):
{gridlines_text}
{notes_block}
TASK: Generate a structural general arrangement with:
1. Optimally placed structural columns (use grid intersections as guidance)
2. Beams connecting columns (longest reasonable spans first)
3. Slabs on each storey
4. Dimensions, labels (C1, C2, B1, B2, S1, S2, etc.)

CONSTRAINTS:
- Column spacing should respect the architectural walls
- Beams should not intersect walls (except at bearing points)
- Slab thickness should be reasonable for a 5m × 5m bay (assume {thickness_lo}-{thickness_hi}mm)
- Assume {storey_height_m}m storey height
- All units in meters

OUTPUT: Provide the GA as a structured JSON with:
{{
  "columns": [
    {{"name": "C1", "x": 0, "y": 0}},
    {{"name": "C2", "x": 5, "y": 0}},
    {{"name": "C3", "x": 10, "y": 0}},
    {{"name": "C4", "x": 0, "y": 5}},
    {{"name": "C5", "x": 5, "y": 5}},
    {{"name": "C6", "x": 10, "y": 5}},
    {{"name": "C7", "x": 0, "y": 10}},
    {{"name": "C8", "x": 5, "y": 10}},
    {{"name": "C9", "x": 10, "y": 10}}
  ],
  "beams": [
    {{"name": "B1", "start_col": "C1", "end_col": "C2", "depth_mm": 500}},
    {{"name": "B2", "start_col": "C2", "end_col": "C3", "depth_mm": 500}},
    ...
  ],
  "slabs": [
    {{"name": "S1", "thickness_mm": 150, "vertices": [[0,0], [5,0], [5,5], [0,5]]}}
  ],
  "summary": "Brief description of the GA"
}}

Be concise. Output ONLY valid JSON, no preamble."""
    
    return prompt

def call_claude(prompt, max_attempts=3, max_tokens=8000):
    """Call Claude API to generate GA. Automatically retries with a corrective
    follow-up if the reply isn't valid JSON -- truncation or a formatting slip
    is the single most common failure mode for structured LLM output, and not
    worth surfacing to the user on the first occurrence. Every failed
    attempt's FULL raw response is logged to logs/oracle.log (not just a
    short preview), so a persistent failure can actually be diagnosed.
    Returns raw text that is already confirmed to parse as JSON -- by the
    time save_ga() sees it, re-parsing is just a formality."""
    import anthropic

    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "No Claude API key is set up yet. Run oracle_wizard.py and enter one "
            "when asked, or set the ANTHROPIC_API_KEY environment variable."
        )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=120.0)
    messages = [{"role": "user", "content": prompt}]
    last_error = None

    for attempt in range(1, max_attempts + 1):
        print(f"Calling Claude for GA generation (attempt {attempt}/{max_attempts})...")
        try:
            message = client.messages.create(model="claude-sonnet-5", max_tokens=max_tokens, messages=messages)
        except Exception as e:
            log_event("ga.api_error", f"attempt {attempt}/{max_attempts}: {e}")
            raise RuntimeError(f"Couldn't reach Claude for the layout: {e}") from e

        text = next((b.text for b in message.content if hasattr(b, "text") and b.text), None)
        if not text:
            last_error = f"empty reply (stop_reason={message.stop_reason!r})"
            log_event("ga.empty_reply", f"attempt {attempt}/{max_attempts}: stop_reason={message.stop_reason!r}")
            messages += [
                {"role": "assistant", "content": "(empty reply)"},
                {"role": "user", "content": "That reply had no content. Please resend the complete JSON layout."},
            ]
            continue

        cleaned = _strip_json_fences(text)
        try:
            json.loads(cleaned)
            return text
        except json.JSONDecodeError as e:
            last_error = e
            log_event(
                "ga.invalid_json",
                f"attempt {attempt}/{max_attempts}, stop_reason={message.stop_reason!r}, error={e}\n"
                f"FULL RAW RESPONSE:\n{text}",
            )
            if attempt == max_attempts:
                break
            hint = (
                " Your reply was cut off before it finished -- keep the summary text brief this time "
                "so the full JSON fits, and make sure every string is properly closed."
                if message.stop_reason == "max_tokens" else
                " Check for things like an unescaped quote or a stray line break inside a string value."
            )
            messages += [
                {"role": "assistant", "content": text},
                {"role": "user", "content": f"That wasn't valid JSON ({e}).{hint} Resend the COMPLETE, "
                                             "valid JSON object only -- no explanation, no markdown fences."},
            ]

    raise RuntimeError(
        f"Claude's layout reply still wasn't valid JSON after {max_attempts} attempts ({last_error}). "
        f"The full raw response from every attempt was logged to {LOG_PATH} -- ask about it in "
        "\"Ask Claude\", or open that file directly."
    )

def save_ga(ga_json, output_file="ga_output.json"):
    """Save GA to JSON file. Raises on failure instead of printing and returning
    None, so the real cause is visible under the GUI wizard too."""
    output_path = OUTPUT_JSON_DIR / output_file

    # Strip markdown code fences if present
    if ga_json.startswith("```json"):
        ga_json = ga_json[7:]  # Remove ```json
    if ga_json.startswith("```"):
        ga_json = ga_json[3:]  # Remove ```
    if ga_json.endswith("```"):
        ga_json = ga_json[:-3]  # Remove trailing ```

    ga_json = ga_json.strip()

    try:
        ga_data = json.loads(ga_json)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Claude's layout reply wasn't valid JSON ({e}). First 300 chars:\n{ga_json[:300]}"
        ) from e

    with open(output_path, 'w') as f:
        json.dump(ga_data, f, indent=2)

    print(f"✓ GA saved to: {output_path}")
    return ga_data

def main():
    print("=== Oracle Phase 2: General Arrangement Generation ===\n")
    
    # Load parsed geometry
    geometry = load_parsed_geometry("test_floor_parsed.json")
    print(f"✓ Loaded geometry: {len(geometry['walls'])} walls, {len(geometry['columns'])} cols, {len(geometry['gridlines'])} gridlines")
    
    # Generate prompt
    prompt = generate_ga_prompt(geometry)
    print(f"\nPrompt length: {len(prompt)} chars")
    
    # Call Claude
    ga_response = call_claude(prompt)
    
    if not ga_response:
        print("❌ Failed to get response from Claude")
        return None
    
    # Save GA
    ga_data = save_ga(ga_response)
    
    if ga_data:
        print(f"\n✓ GA Generated:")
        print(f"  Columns: {len(ga_data.get('columns', []))}")
        print(f"  Beams: {len(ga_data.get('beams', []))}")
        print(f"  Slabs: {len(ga_data.get('slabs', []))}")
        print(f"  Summary: {ga_data.get('summary', 'N/A')}")
        return ga_data
    else:
        print("❌ GA generation failed")
        return None

if __name__ == "__main__":
    main()