"""The Production Line: turn a finished manuscript into shippable artifacts.

`stoner ship` reads the completed `manuscript/` and writes into a new
`export/` project dir: a typeset trade-paperback PDF, a dependency-free
EPUB3, a Shunn submission DOCX, model-drafted synopsis / query-letter /
cover-brief markdown, and a multi-voice table-read audiobook draft.

Two rules run through everything here (see the plan, invariants 2/3/8/9):
deterministic converters may *gate* (readiness blockers refuse unless
`--allow-incomplete`) and *reproduce* (EPUB is byte-identical); model-drafted
artifacts are the human's drafts, never machine-edited after the fact. Heavy
document deps (reportlab, python-docx) live behind the `export` extra with
lazy imports so every format degrades independently; EPUB and audio ride on
the standard library and core httpx and never degrade.
"""
