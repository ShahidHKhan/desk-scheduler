"""
Batch ingestion: run ingest() over every submission file in a folder.

Collects successes and failures per file instead of crashing the whole
batch on one bad submission - failures (unparseable file, missing vision
API key, etc.) and per-submission warnings (unfilled name, out-of-range
hours, etc.) are meant for human review before anything gets linked to
the roster.

Usage, from the repo root:
    python -m scripts.batch_ingest path/to/submissions

Or from code:
    from scripts.batch_ingest import ingest_folder
    result = ingest_folder("/path/to/submissions")
    result.submissions  # list[AvailabilitySubmission] that parsed
    result.failures     # list[(path, error message)] that didn't
"""

from dataclasses import dataclass, field
from pathlib import Path

from scheduler.ingest.router import ingest
from scheduler.ingest.schema import AvailabilitySubmission

SUPPORTED_SUFFIXES = {".xlsx", ".pdf"}


@dataclass
class BatchResult:
    submissions: list[AvailabilitySubmission] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)  # (path, error message)


def ingest_folder(folder: str) -> BatchResult:
    """Ingest every supported file in `folder` (non-recursive).

    Files with unsupported extensions are silently skipped. Any error
    raised while ingesting a given file - a bad parse, a missing vision
    API key, whatever - is caught and recorded as a failure so the rest
    of the batch still runs.
    """
    result = BatchResult()

    for path in sorted(Path(folder).iterdir()):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        try:
            result.submissions.append(ingest(str(path)))
        except Exception as e:
            result.failures.append((str(path), str(e)))

    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        sys.exit("usage: python -m scripts.batch_ingest <folder>")
    folder = sys.argv[1]
    result = ingest_folder(folder)

    print(f"Ingested {len(result.submissions)} submission(s), {len(result.failures)} failure(s)")
    for sub in result.submissions:
        print(f"  OK   {sub.source_file} -> {sub.name!r} ({sub.parser_used})")
        for w in sub.warnings:
            print(f"         ! {w}")
    for path, err in result.failures:
        print(f"  FAIL {path}: {err}")
