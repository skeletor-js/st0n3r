<!--
ship_synopsis.md — user prompt for `stoner ship blurbs` (synopsis artifact).

Filled by ship/blurbs.py via render_prompt. Placeholders:
  {title}       - resolved book title
  {author}      - book author (may be empty)
  {word_count}  - total manuscript word count
  {logline}     - premise Logline section
  {comps}       - premise Genre & Comps section
  {promise}     - premise Promise to the Reader section
  {canon}       - canon context pack (premise/style/threads/characters/world)
  {memory}      - book-so-far plus every chapter summary in order
  {threads}     - open and resolved plot threads

This context deliberately contains NO manuscript prose — the book is
represented only by canon and rolling summaries (invariant 4).
-->

Draft a complete synopsis for the novel "{title}" ({word_count} words) by
{author}.

A synopsis tells the whole story in present tense, including the ending —
it is not back-cover copy. Cover the central conflict, the protagonist's
arc, the major turns, and how it resolves. One to two pages. Name the
characters who matter and leave out the ones who don't.

## Logline
{logline}

## Genre & Comps
{comps}

## Promise to the Reader
{promise}

## What happens in the book (summaries, in order)
{memory}

## Open and resolved threads
{threads}

## Canon
{canon}

Write only the synopsis, as markdown. Do not add commentary, headings like
"Synopsis:", or notes to the author.
