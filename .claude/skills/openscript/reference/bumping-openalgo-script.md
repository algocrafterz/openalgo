### Bumping openalgo-script also updates the openscript skill

The same rule as the chart above, for the same reason: `reference/library.md`
carries all 350 names with their warmups and marks the 95 that are **planned and
not implemented**, so a bump that leaves it behind has an author reading a page
about a compiler that is no longer installed. The marking is the part that
matters most, because reaching for a planned name is refused at the call with
`OS2020` and nothing warns first.

```sh
cd frontend && npm install openalgo-script@<version> --save-exact
node .claude/skills/openscript/generate-reference.mjs    # rewrites the name table
node .claude/skills/openscript/coverage.mjs              # must print COVERAGE COMPLETE
node .claude/skills/openscript/check-pitfalls.mjs        # must print PITFALLS VERIFIED
```

The third is specific to this skill. `reference/pitfalls.md` teaches by naming
diagnostic codes, and an author trusts a code; the script compiles both halves
of every entry, so the wrong spelling must still produce the code named and the
fix offered must still come out clean. It also holds the page and the script to
the same set of codes, so neither drifts alone.

The `openscript-skill` CI job runs all three. The generator owns the name table
and nothing generates the teaching: after a bump, read the upstream changelog
and update the prose in `SKILL.md`, `pitfalls.md` and `strategies.md` by hand,
particularly wherever they say a name is planned. A version that implements one
turns three pages stale at once.

Two built-in pages exercise the streaming stack end to end: **`/websocket/test`**
(market data; `/20`, `/30`, `/50` variants request those depth levels) and
**`/websocket/order`** (account-level order/trade update stream). Use them to
verify a broker feed rather than writing a throwaway client.

**React routes do not all have Flask routes**, and that is fine — the `app.py`
404 handler falls through to `serve_react_app()`, so React Router handles them.
The reason to still register a route in `blueprints/react_app.py` is that
unregistered paths hit `Error404Tracker` for *unauthenticated* visitors and
count toward an IP ban.

