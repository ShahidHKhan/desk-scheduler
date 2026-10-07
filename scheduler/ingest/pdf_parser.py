"""
Parses a submitted PDF into an AvailabilitySubmission.

Real submissions arrive in two PDF flavors. Some are scanned/rasterized
pages where pdfplumber can't extract any positioned words at all. Others
are digital-native exports of the schedule template (~150 positioned
words, real vector gridlines, exact coordinates), which _reconstruct_grid()
below is calibrated against. This module handles either:
  1. Try to extract words WITH their (x, y) positions via pdfplumber.
  2. If that yields enough positioned words to confidently reconstruct
     the day/time grid, do so - this is the deterministic, cheap path.
  3. If extraction fails outright, or yields too few positioned words to
     reconstruct the grid with any confidence, raise NeedsVisionFallback
     instead of guessing. The caller (router.py) decides what to do next.

CONFIDENCE THRESHOLD: MIN_POSITIONED_WORDS = 20. Calibrated on one sample
of each kind, which land nowhere near each other (~0 words for the scan
vs. ~150 for the digital export), so 20 is a conservative cutoff. Revisit
if a borderline case ever shows up.
"""

from dataclasses import dataclass

from scheduler.ingest.schema import (
    DAYS,
    TIME_SLOTS,
    AvailabilitySubmission,
    hours_range_warning,
    is_available_value,
    parse_hours,
)

MIN_POSITIONED_WORDS = 20  # see confidence threshold note above


class NeedsVisionFallback(Exception):
    """Raised when the PDF doesn't have a usable positioned-text layer."""

    def __init__(self, path: str, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


@dataclass
class _PositionedWord:
    text: str
    x: float
    y: float


def parse_pdf(path: str) -> AvailabilitySubmission:
    words = _extract_positioned_words(path)

    if len(words) < MIN_POSITIONED_WORDS:
        raise NeedsVisionFallback(
            path,
            f"only {len(words)} positioned words extracted "
            f"(need >= {MIN_POSITIONED_WORDS}) - likely an image-based page",
        )

    return _reconstruct_grid(path, words)


def _extract_positioned_words(path: str) -> list[_PositionedWord]:
    """Try to get words with real (x, y) coordinates from the PDF's text layer.

    Returns an empty list (never raises) if the PDF can't be opened or has
    no text layer at all - that's a normal "this is a scan" outcome, not
    an error, and gets handled by the word-count check in parse_pdf().
    """
    try:
        import pdfplumber
    except ImportError as e:
        raise RuntimeError("pdfplumber is required: pip install pdfplumber") from e

    try:
        with pdfplumber.open(path) as pdf:
            page = pdf.pages[0]
            raw_words = page.extract_words()
    except Exception:
        # Malformed / non-standard PDF, or genuinely no text layer.
        return []

    return [
        _PositionedWord(text=w["text"], x=w["x0"], y=w["top"])
        for w in raw_words
    ]


def _reconstruct_grid(path: str, words: list[_PositionedWord]) -> AvailabilitySubmission:
    """Cluster positioned words into the day/time grid by coordinates.

    Calibrated against a digital-native PDF export of the same schedule
    template xlsx_parser.py reads. That PDF's vector gridlines gave exact day-column boundaries
    (via page.rects), and its time-label column gave exact row y0
    positions - both hardcoded below, mirroring how xlsx_parser.py
    hardcodes DAY_COLUMNS/FIRST_SLOT_ROW for its own fixed template.

    Cell text can be split across multiple words by pdfplumber (e.g. the
    "Tech Only" label extracts as two separate words, "Tech" and "Only").
    Evaluating a word like "Tech" alone through is_available_value() would
    wrongly read as an availability mark, since only the full string
    "tech only" is excluded. So words are first grouped by which
    (day, slot) cell they fall in and joined, then evaluated once per
    cell - exactly mirroring how xlsx_parser.py reads one whole cell
    value at a time.
    """
    name = "UNKNOWN"
    hours_requested = None
    warnings: list[str] = []

    name_words = [
        w.text for w in words
        if 115 <= w.x <= 300 and 50 <= w.y <= 70
        and w.text.strip("*").strip().lower() not in ("", "your", "name")
    ]
    name = " ".join(name_words).strip()
    if not name:
        name = "UNKNOWN"
        warnings.append("Name field looks unfilled (still has placeholder text)")

    hours_words = [
        w.text for w in words
        if 300 <= w.x <= 420 and 40 <= w.y <= 70
        and w.text.strip("*").strip().lower() not in ("", "total", "wanted", "hrs.", "hrs")
    ]
    hours_text = " ".join(hours_words).strip()
    if hours_text:
        hours_requested = parse_hours(hours_text)
        if hours_requested is None:
            warnings.append(f"Could not parse hours_requested from header text {hours_text!r}")
        else:
            range_warning = hours_range_warning(hours_requested)
            if range_warning:
                warnings.append(range_warning)
    else:
        warnings.append("Hours-requested field looks unfilled (still has placeholder text)")

    # (day, slot_index) -> list of word texts, in reading order
    cells: dict[tuple[str, int], list[_PositionedWord]] = {}
    for w in words:
        day = _match_day_column(w.x)
        if day is None:
            continue
        slot = _match_slot_row(w.y)
        if slot is None:
            continue
        cells.setdefault((day, slot), []).append(w)

    availability: dict[str, list[bool]] = {day: [False] * len(TIME_SLOTS) for day in DAYS}
    for (day, slot), cell_words in cells.items():
        cell_text = " ".join(w.text for w in sorted(cell_words, key=lambda w: w.x))
        availability[day][slot] = is_available_value(cell_text)

    return AvailabilitySubmission(
        name=name,
        hours_requested=hours_requested,
        availability=availability,
        source_file=path,
        parser_used="pdf_text",
        warnings=warnings,
    )


# Day-column x-ranges, read off the PDF's vector gridlines (page.rects)
# in the calibration PDF - each ~60pt wide, left-to-right
# Mon through Sun.
DAY_X_RANGES: dict[str, tuple[float, float]] = {
    "Mon": (116.0, 176.8),
    "Tue": (176.8, 236.9),
    "Wed": (236.9, 296.9),
    "Thu": (296.9, 356.9),
    "Fri": (356.9, 416.9),
    "Sat": (416.9, 476.9),
    "Sun": (476.9, 538.3),
}

# Exact y0 of each time-slot row's label in the Time column (e.g. "8:00",
# "8:30", ...), read off the same PDF. 25 entries, one per TIME_SLOTS
# entry, 08:00 through 20:00.
SLOT_Y_POSITIONS: list[float] = [
    110.0, 121.8, 134.3, 146.0, 158.5, 170.3, 182.8, 194.5,
    207.0, 218.8, 231.2, 243.0, 255.5, 267.3, 279.8, 291.5,
    304.0, 315.8, 328.2, 340.0, 352.5, 364.2, 376.7, 388.5, 401.0,
]

# How far (in points) a mark's y0 may drift from its row's label y0 and
# still count as belonging to that row. Observed drift in the calibration
# sample was <=1.6pt; row spacing is ~12.1pt, so this leaves a wide safety
# margin while still rejecting words from the day-header/hour-range rows
# above the grid (12+pt away).
SLOT_Y_TOLERANCE = 6.0


def _match_day_column(x: float) -> str | None:
    for day, (x_min, x_max) in DAY_X_RANGES.items():
        if x_min <= x <= x_max:
            return day
    return None


def _match_slot_row(y: float) -> int | None:
    best_slot, best_dist = None, SLOT_Y_TOLERANCE
    for i, slot_y in enumerate(SLOT_Y_POSITIONS):
        dist = abs(y - slot_y)
        if dist <= best_dist:
            best_slot, best_dist = i, dist
    return best_slot


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        sys.exit("usage: python -m scheduler.ingest.pdf_parser <submission.pdf>")
    target = sys.argv[1]
    try:
        result = parse_pdf(target)
        print(result)
    except NeedsVisionFallback as e:
        print(f"Needs vision fallback: {e.reason}")
