<!--
ship_cover.md — user prompt for `stoner ship blurbs` (cover-brief artifact).

Filled by ship/blurbs.py via render_prompt. Placeholders:
  {title}, {author}, {word_count}, {logline}, {comps}, {promise},
  {canon}, {memory}, {threads}

No manuscript prose is present — only canon and rolling summaries
(invariant 4). This brief is designer-facing text, NOT an image.
-->

Write a cover-design brief for a book designer working on "{title}" by
{author}. This is a brief the designer reads — do not describe generating an
image, and do not output image data.

Cover these sections:

- **Mood & tone**: the feeling the cover should give a browser, grounded in
  the genre and comps below.
- **Key imagery**: two or three concrete visual motifs pulled from the
  story's world and setting (objects, places, textures) that could anchor a
  cover — no spoilers required, evocation over plot.
- **Palette cues**: a color direction that fits the mood.
- **Comp covers**: what the covers of the comp titles below tend to look
  like, and where this book should sit relative to them.
- **Typography feel**: a direction for the title treatment.

## Logline
{logline}

## Genre & Comps
{comps}

## Promise to the Reader
{promise}

## Story summaries (in order)
{memory}

## Canon
{canon}

Write only the brief, as markdown with the section headings above.
