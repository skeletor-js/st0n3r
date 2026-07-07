"""Post-refactor verification: deterministic integrity gate + advisory model
checks.

`verify_refactor` runs the U5 integrity checker first (findings with source
`drafts:integrity`; these are the only ones allowed to fail a command),
then -- when model verification is enabled and a provider is available --
a targeted continuity review plus an archivist re-sync in preview mode
(`auto=False`: conflicts surface, canon is never auto-overwritten) for each
affected chapter. Model findings are advisory only, and any provider
failure degrades to a note rather than failing the refactor, mirroring
`review/runner.py`'s degrade-never-abort discipline: the deterministic
checks already ran.

Everything is aggregated into one saved report under `.stoner/reviews/`
(kind `drafts-verify`) and a `drafts.verify` ledger entry.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from ..ledger import Ledger
from ..project import WritingProject
from ..providers.base import Provider, ProviderError
from ..types import Finding, Severity, Usage
from .renumber import check_integrity, has_gating_findings

VERIFY_SOURCE = "drafts:verify"


@dataclass
class VerifyResult:
    """Aggregated outcome of one verification run."""

    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    report_path: str = ""

    @property
    def integrity_failed(self) -> bool:
        """True when deterministic checks found gating (major+) problems."""
        return has_gating_findings(self.findings)


def _model_verify_chapter(
    project: WritingProject,
    ch: int,
    provider: Provider | None,
    model: str | None,
    result: VerifyResult,
) -> None:
    """Continuity review + archivist preview for one chapter. Never raises
    except ProviderError (handled by the caller as a skip-everything note)."""
    from ..pipelines.write import run_archive
    from ..review.runner import run_review

    report = run_review(project, ch, passes=["continuity"], model=model, provider=provider)
    result.usage += report.usage
    result.findings.extend(report.findings)
    try:
        archive = run_archive(project, ch, model=model, provider=provider, auto=False)
        for c in archive.conflicts:
            result.findings.append(
                Finding(
                    source=VERIFY_SOURCE,
                    severity=Severity.minor,
                    category="canon-conflict",
                    issue=(
                        f"ch-{ch:02d}: {c.entity}.{c.field}: "
                        f"canon={c.canon_value!r} new={c.new_value!r} — resolve by hand"
                    ),
                )
            )
    except ProviderError:
        raise
    except Exception as e:  # noqa: BLE001 - archivist parse failures are advisory, never abort
        result.notes.append(f"archivist re-sync skipped for ch-{ch:02d}: {e}")


def verify_refactor(
    project: WritingProject,
    affected: list[int],
    provider: Provider | None = None,
    model: str | None = None,
    run_model: bool = True,
) -> VerifyResult:
    """Verify the project after a refactor (or standalone via `drafts verify`).

    Deterministic integrity findings may gate (the CLI exits nonzero on
    `integrity_failed`); model findings and notes are advisory only.
    """
    result = VerifyResult()
    result.findings.extend(check_integrity(project))

    if run_model and project.config.archaeology.verify_after_refactor and affected:
        try:
            for ch in affected:
                _model_verify_chapter(project, ch, provider, model, result)
        except ProviderError as e:
            result.notes.append(f"model verification skipped: {e}")

    ts = int(time.time())
    rel = f".stoner/reviews/drafts-verify-{ts}.json"
    payload = {
        "kind": "drafts-verify",
        "affected": affected,
        "findings": [f.model_dump() for f in result.findings],
        "notes": result.notes,
        "usage": result.usage.model_dump(),
        "created_at": time.time(),
    }
    result.report_path = str(project.write(rel, json.dumps(payload, indent=2, ensure_ascii=False)))

    Ledger(project.root).append(
        "drafts.verify",
        target=rel,
        affected=affected,
        findings=len(result.findings),
        integrity_failed=result.integrity_failed,
        notes=len(result.notes),
    )
    return result
