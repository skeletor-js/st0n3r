"""The Verisimilitude Engine: a sourced fact locker plus web research.

`canon/facts/` is a first-class canon artifact -- one markdown file per
checkable real-world fact, frontmatter carrying the hard fields (name, claim,
source_url, confidence, status) and the body carrying verbatim quotes and
human notes. This package owns:

- ``locker`` -- pure assemble/parse/diff/apply over the locker (never calls a
  provider), mirroring ``canon/archivist``; feeds facts into the writer's
  context pack and into the sweep pass.
- ``webfetch`` -- an opt-in, ledgered, capped URL fetcher usable as an agent
  tool (the degradation path when no native web search exists).
- ``research`` -- the ``facts research`` pipeline: opt-in gate, capability
  selection, researcher call, tolerant parse, diff, dry-run/apply.
- ``sweep`` -- the advisory ``verisimilitude`` review pass.

Web access is explicit opt-in (``facts.enabled`` defaults False), refused in
autonomous book mode even when enabled, and every network action is ledgered
under the ``facts.*`` prefix.
"""
