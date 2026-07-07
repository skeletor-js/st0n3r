# The web UI

```
$ stoner ui
INFO:     Uvicorn running on http://127.0.0.1:8377 (Press CTRL+C to quit)
```

`stoner ui` serves a local dashboard for the project in the current directory. It needs the `ui` extra (`pip install 'st0n3r[ui]'`); if that's missing, the command says so and exits.

It's a single static page plus a small JSON API — no build step, no CDN, no external requests. Every view reads straight from disk on each request, so edits made in your editor, by the CLI, or by an agent mid-run show up on the next refresh. The UI is read-mostly: the one thing it writes is finding statuses during triage.

The dashboard is built on the Hearth design system: a warm paper light theme and a warm charcoal dark theme. It follows your system preference by default; the sidebar's theme toggle overrides that, and the choice persists in the browser across visits.

## Panels

The sidebar rail has five views.

### Manuscript

The chapter list (number, title, status, POV, words) on the left; select a chapter to read it rendered on the right, with frontmatter shown as a small table.

This is also where the **slop heatmap** lives. Hit **Run slop** and the chapter is scored in place:

- a score card shows the 0–100 score and verdict band, with a horizontal bar per analyzer subscore;
- every finding with a locatable span is highlighted directly in the prose, tinted by severity — blue for info, amber for minor, orange for major, red for critical.

A page that reads mostly clean with two red streaks tells you where to spend the next hour; a page that's amber all over says the problem is systemic, not local. Findings are listed under the text with line numbers and issues. What the categories mean is covered in [The slop detector](slop.md).

### Canon

Browse the story bible: premise, style, characters, world entries, timeline, threads. Selecting an entry shows its frontmatter (the hard facts the archivist diffs) above the rendered body. Editing still happens in your editor — the UI is for reading and cross-checking, e.g. eyeballing a character sheet against the chapter that just contradicted it.

### Reviews

Every saved report from `.stoner/reviews/` — both `stoner review` reports and slop reports saved with `--save` — newest first, tagged by chapter and kind.

Open a review report and you get the **findings triage table**: severity, category, quote, issue, and status for each finding, with **Accept** and **Dismiss** buttons. Statuses are written back into the report file itself, which is exactly what `stoner revise` reads — accept the findings you agree with here, then run `stoner revise N` in the terminal. The full flow is in [Review & revise](review.md#the-revise-flow).

### Book

Progress of an [autonomous run](autonomous.md), rendered from `.stoner/book-state.json`. If no run has happened yet it says so and points you at `stoner book`; otherwise you get:

- the run's current phase (drafting / reviewing / done) and when the state last changed;
- a per-chapter checklist — done chapters with word count, slop score, review status, and revision cycles; the chapter being drafted marked **current**; the rest pending;
- the history of whole-book review rounds, each with its major-finding count.

Since the state file is saved before every model call, refreshing this panel during a run is a live progress view — and after a Ctrl-C it shows exactly where a resumed run will pick up.

### Ledger

The action log as a table: time, action, target, session, detail. Useful for answering "what just touched my canon?" without leaving the browser. See [Concepts](concepts.md#the-ledger).

## Local-only by default

The server binds to `127.0.0.1:8377` — reachable only from your own machine. There is no authentication layer, so treat the flags accordingly:

```bash
stoner ui --port 9000          # different port, still local-only
stoner ui --host 0.0.0.0       # exposes the project (and triage writes) to your network
```

Only widen the host on a network you trust, or put a reverse proxy with auth in front. The UI itself never phones home; the only network traffic it generates is between your browser and the local server. Model calls are not made from the UI at all — drafting, reviewing, and revising stay in the CLI.
