### Bumping openalgo-charts also updates the chart-indicator skill

The skill documents a specific build. `reference/api.md` carries the full export
index and `pitfalls.md` carries the built-in ids a custom module can shadow, so
a version bump that touches neither leaves the skill describing a library that
is no longer installed. **Upgrading the pin and updating the skill are one
change, not two.**

```sh
cd frontend && npm install openalgo-charts@<version> --save-exact
node .claude/skills/chart-indicator/generate-api-index.mjs   # regenerates the index
node .claude/skills/chart-indicator/coverage.mjs             # must print COVERAGE COMPLETE
node scripts/generate-drawing-tools.mjs                       # rail glyphs and names, from the installed draw tier
node scripts/generate-chrome-icons.mjs                        # toolbar and rail chrome glyphs
```

The two generators write `src/lib/trading/drawingToolMetadata.ts` and
`src/lib/trading/chromeIcons.ts`; CI runs both with `--check`, so a bump that
changes a glyph and skips them fails the build. Never edit either file by hand,
and never run a formatter over them: `frontend/biome.json` leaves them out, because
the check compares the generator's exact output and a reformatted file fails it.

A bump that changes what /trading loads also runs the browser harness that lives
in the openalgo-charts repo: `node scripts/check-openalgo-compat.mjs --frontend
<worktree>/frontend --browser <chromium|firefox|webkit> --navigation true` (14
checks). It needs a linked `git worktree` of this repo with `node_modules`
copied, not linked. On Windows copy it with `robocopy <src> <dst> /E /XJ`: without
`/XJ` robocopy follows the junctions inside `node_modules` and copies the same
trees over and over (9 GB instead of 730 MB), which once filled the system drive.

Then read the upstream changelog for the range you skipped and update the prose
by hand: **Recent changes worth knowing** in `SKILL.md`, the *What arrived
after* table in `api.md`, and the id-collision list in `pitfalls.md` if the
registry grew. The generator only owns the export index; nothing generates the
teaching.

The `chart-indicator-skill` CI job runs both checks, so a stale skill fails the
build. It exists because both scripts were already in the repo and nothing ran
them: the index sat on 1.8.1 advertising "337 names" while `/trading` shipped
2.1.5 with 363, and the eleven studies added in 1.8.3 were absent from the
reference an indicator author reads.

