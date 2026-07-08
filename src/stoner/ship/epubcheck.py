"""Optional EPUB-spec validation via the external `epubcheck` tool (R9).

`ship epub` is byte-reproducible but never checked against the EPUB spec. This
wires in the W3C `epubcheck` command-line tool as a strictly optional, clearly
gated post-write validation: an author who opts in (via `--validate` or
`ship.epubcheck`) gets the generated `.epub` run through epubcheck; an author
who has not installed it gets an informational skip, never an error.

epubcheck is a Java tool, so it must never become a hard dependency. Discovery
is `ship.epubcheck_path` (explicit) then `epubcheck` on PATH; a miss returns an
`unavailable` result the caller reports as a skip. When the tool *is* present
and validation was requested, a spec FAILURE is a real, deterministic failure
the command surfaces (and may exit nonzero on) -- the same discipline the
readiness gate follows. Output is parsed from epubcheck's plain ERROR/WARNING/
FATAL lines, keyed off its exit code (0 = valid, warnings allowed).
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..config import StonerConfig
from ..ledger import Ledger
from ..project import WritingProject

#: A subprocess runner (injectable in tests); defaults to `subprocess.run`.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass
class EpubcheckResult:
    """Outcome of an (attempted) epubcheck run.

    `available` is False when the tool could not be found -- the caller reports
    `message` as an informational skip and never fails. When `available`, `ok`
    reflects epubcheck's exit code (0 = spec-valid; warnings do not fail).
    """

    available: bool
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    executable: str = ""
    message: str = ""


def find_epubcheck(config: StonerConfig) -> str | None:
    """Locate the epubcheck executable: explicit config path, then PATH.

    Returns the resolved command, or None when nothing is found (a miss is a
    skip, never an error). An explicit `ship.epubcheck_path` that does not
    resolve also returns None rather than raising.
    """
    explicit = config.ship.epubcheck_path.strip()
    if explicit:
        found = shutil.which(explicit)
        if found:
            return found
        return explicit if Path(explicit).is_file() else None
    return shutil.which("epubcheck")


def _parse_messages(output: str) -> tuple[list[str], list[str]]:
    """Split epubcheck's plain output into (errors, warnings) by line prefix."""
    errors: list[str] = []
    warnings: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(("ERROR", "FATAL")):
            errors.append(stripped)
        elif stripped.startswith("WARNING"):
            warnings.append(stripped)
    return errors, warnings


def validate_epub(
    project: WritingProject,
    epub_path: Path,
    *,
    runner: Runner | None = None,
) -> EpubcheckResult:
    """Run epubcheck on `epub_path` if the tool is available; ledger the run.

    Returns an `unavailable` result (no ledger entry, no failure) when
    epubcheck is not installed. When it runs, parses ERROR/WARNING/FATAL lines,
    keys `ok` off the exit code, and writes a `ship.epubcheck` ledger entry.
    """
    run = runner or subprocess.run
    executable = find_epubcheck(project.config)
    if executable is None:
        return EpubcheckResult(
            available=False,
            message=(
                "epubcheck not found -- skipping EPUB validation. Install "
                "epubcheck (or set ship.epubcheck_path) to enable it."
            ),
        )
    try:
        proc = run([executable, str(epub_path)], capture_output=True, text=True)
    except OSError as e:
        return EpubcheckResult(
            available=False,
            message=f"epubcheck ({executable}) could not be run: {e}. Skipping validation.",
        )
    errors, warnings = _parse_messages((proc.stderr or "") + (proc.stdout or ""))
    result = EpubcheckResult(
        available=True,
        ok=proc.returncode == 0,
        errors=errors,
        warnings=warnings,
        executable=executable,
    )
    Ledger(project.root).append(
        "ship.epubcheck",
        target=str(epub_path.relative_to(project.root)),
        ok=result.ok,
        errors=len(errors),
        warnings=len(warnings),
    )
    return result
