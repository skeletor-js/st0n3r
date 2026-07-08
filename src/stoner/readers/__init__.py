"""Reader Simulation at Scale (src/stoner/readers/).

A synthetic focus group: dozens of distinct personas reading the manuscript
chapter-by-chapter with persistent per-reader state, logging span-anchored
attention events (bored, confused, reread, hooked) that aggregate
*arithmetically* into a whole-book attention heatmap, plus blind pairwise
benchmarking against user-supplied public-domain comps.

This is an extend-not-replace subsystem: `review.passes.panel` stays the cheap
single-call persona pass inside `stoner review`; readers scales the same idea
with persistence and aggregation. Design constraints (from the plan): personas
emit span-anchored *events* only, never numeric scores; every heatmap/bench
number is Python arithmetic; per-call context is one chapter plus a capped
canon slice plus k compact reader states, never the whole manuscript; state is
saved before every model call and is `.bak`-recoverable and resumable.
"""

from __future__ import annotations

from .personas import Persona, PersonaError, load_personas, select_roster

__all__ = ["Persona", "PersonaError", "load_personas", "select_roster"]
