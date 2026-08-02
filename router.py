"""
Entry point for ingestion: routes a submitted file to the right parser.

    from router import ingest
    submission = ingest("/path/to/ShahidKhan_Spring2026S.xlsx")

_vision_fallback() calls Gemini (Google AI Studio) to read scanned/
image-based PDF submissions that pdf_parser.py's text-layer extraction
can't handle. Needs GEMINI_API_KEY set (see .env.example). Untested
against a real scanned submission (we only have text-layer PDF samples
so far) - if Gemini's output doesn't parse cleanly, this fails loudly
rather than guessing. See pdf_parser.py's design note for the two real
samples this pipeline was built against.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

from pdf_parser import NeedsVisionFallback, parse_pdf
from schema import DAYS, TIME_SLOTS, AvailabilitySubmission, hours_range_warning
from xlsx_parser import parse_xlsx

load_dotenv()  # reads .env into os.environ (GEMINI_API_KEY, etc.)


def ingest(path: str) -> AvailabilitySubmission:
    suffix = Path(path).suffix.lower()

    if suffix == ".xlsx":
        return parse_xlsx(path)

    if suffix == ".pdf":
        try:
            return parse_pdf(path)
        except NeedsVisionFallback as e:
            return _vision_fallback(path, reason=e.reason)

    raise ValueError(f"Unsupported file type: {suffix} ({path})")


_VISION_PROMPT = f"""\
This image is one page of a service-desk availability schedule. It has a
header with the person's name and total hours wanted, and a grid with a
"Time" column of 30-minute slots down the left and day-of-week columns
(Mon, Tue, Wed, Thu, Fri, Sat, Sun) across the top. A cell marked with the
person's initials (or any mark) means they're available in that slot;
"Closed" or "Tech Only" or a blank cell means not available.

Return ONLY a JSON object, no markdown fences, with this exact shape:
{{
  "name": string or null if not filled in,
  "hours_requested": integer or null if not filled in,
  "availability": {{
    "Mon": [array of {len(TIME_SLOTS)} booleans, one per time slot in order],
    "Tue": [...], "Wed": [...], "Thu": [...], "Fri": [...], "Sat": [...], "Sun": [...]
  }},
  "notes": [array of strings - anything you're unsure about or couldn't read clearly]
}}

The time slots in order are: {TIME_SLOTS}

If you cannot read the grid with reasonable confidence, set "availability"
to null instead of guessing - do not fabricate marks.
"""


def _vision_fallback(path: str, reason: str) -> AvailabilitySubmission:
    """Extract a submission from a scanned/image PDF via Gemini vision.

    Deliberately strict: if the API key is missing, the response isn't
    valid JSON, doesn't match the expected shape, or Gemini itself says it
    couldn't read the grid, this raises instead of returning partial or
    fabricated data. A schedule built on fabricated availability is a
    worse failure mode than one that stops and asks for help.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            f"{path} needs vision-based extraction ({reason}), but "
            f"GEMINI_API_KEY is not set. Add it to .env (see .env.example)."
        )

    from google import genai
    from google.genai import types

    with open(path, "rb") as f:
        pdf_bytes = f.read()

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
            _VISION_PROMPT,
        ],
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )

    try:
        data = json.loads(response.text)
    except (json.JSONDecodeError, TypeError) as e:
        raise RuntimeError(
            f"{path}: Gemini response wasn't valid JSON, refusing to guess: {e}"
        ) from e

    if not data.get("availability"):
        raise RuntimeError(
            f"{path}: Gemini couldn't read the grid confidently "
            f"(notes: {data.get('notes')}), refusing to guess."
        )

    availability = data["availability"]
    warnings = list(data.get("notes") or [])
    for day in DAYS:
        flags = availability.get(day)
        if not isinstance(flags, list) or len(flags) != len(TIME_SLOTS):
            raise RuntimeError(
                f"{path}: Gemini's availability for {day!r} doesn't match "
                f"the expected {len(TIME_SLOTS)}-slot shape: {flags!r}"
            )
        availability[day] = [bool(v) for v in flags]

    name = data.get("name") or "UNKNOWN"
    if name == "UNKNOWN":
        warnings.append("Name field looks unfilled (Gemini found no name)")

    hours_requested = data.get("hours_requested")
    if hours_requested is None:
        warnings.append("Hours-requested field looks unfilled (Gemini found no value)")
    else:
        range_warning = hours_range_warning(hours_requested)
        if range_warning:
            warnings.append(range_warning)

    return AvailabilitySubmission(
        name=name,
        hours_requested=hours_requested,
        availability=availability,
        source_file=path,
        parser_used="pdf_vision",
        warnings=warnings,
    )
