# The Writers' Room

A one-shot review forgets everything the moment it finishes. The Writers' Room is a *persistent* roster of editor personas that keep notebooks: a running opinion and a list of open items that survive across sessions, so an editor can flag "the middle still doesn't earn its length" in one pass and re-check that exact concern in the next. It is **advisory only** — the room never gates.

```bash
stoner room session 3            # run the roster over chapter 3
stoner room session --book       # a whole-book milestone session
stoner room notebook "Line Editor"   # one editor's running opinion + open items
stoner room comments 3           # a chapter's margin comments; resolve/dismiss by id
stoner room comment 3 --quote "..." --text "..."   # pin a margin comment to a span
stoner room status               # open comments and persisting flags across the project
```

## The roster

The default roster is four editors, each a persona wrapped around a set of existing [review passes](review.md):

| Editor | Cares about | Runs passes |
|---|---|---|
| Developmental Editor | structure, momentum, whether scenes earn their place | `pacing`, `logic` |
| Line Editor | prose-level tells, ruthless cutting | `line`, `adversarial` |
| Continuity Pedant | canon, timeline, who knew what when | `continuity` |
| First Reader | the chapter as a fresh reader meets it | `grade` |

You reshape the roster in `stoner.yaml`: each editor is a `{name, persona, passes}` entry. Unknown pass names are *soft* — they warn and skip at session time rather than failing the load, so a roster stays usable even if a pass was renamed or belongs to a feature you haven't turned on. That's what lets the room reference opt-in passes like `interiority` or `verisimilitude` when those subsystems have state, and quietly skip them when they don't.

## What a session does

`stoner room session <chapter>` runs each editor's passes, then does the things a one-shot review can't:

- **re-locates prior flags** — an open item from a previous session is checked against the current text to see whether the rewrite addressed it;
- **cross-examines** — each editor gets one turn to push on its own findings;
- **updates the notebook** — a running opinion (capped at `opinion_cap_chars`) plus the open/resolved item lists (bounded by `max_open_items` / `max_resolved_items`).

`--book` runs a whole-book milestone session instead of a single chapter. The cost is bounded and predictable: **one call per editor per assigned pass, one cross-examination per editor, plus at most one re-locate fallback and one comment follow-up.** The roster size is the cost knob.

## Margin comments

`stoner room comment <chapter> --quote "<verbatim span>" --text "<comment>"` pins a comment to a span, anchored by the quote so it survives edits that don't touch that text. `stoner room comments <chapter>` lists them and lets you resolve or dismiss by id; `stoner room status` rolls up open comments and persisting flags across the whole project. Comments and notebooks are read in the [UI](ui.md) Writers' Room panel.

Dismissing an item silences that notebook thread — the editor won't keep re-raising a concern you've explicitly waved off.

## Configuration

```yaml
room:
  editors: [...]              # the roster; each {name, persona, passes}
  model: ""                   # empty resolves against the reviewer role; set to run cheaper
  opinion_cap_chars: 2000     # cap on an editor's running-opinion length
  digest_chars: 3000          # cap on the notebook digest fed back into a session
  max_open_items: 50
  max_resolved_items: 20
  llm_relocate: true          # use the model to re-locate prior flags in changed text
```

Editors resolve against `room.model`, falling back to the `reviewer` role.
