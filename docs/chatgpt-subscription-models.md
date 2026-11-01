### The ChatGPT subscription model list is ours to maintain

The `chatgpt/` provider authenticates with a ChatGPT Plus or Pro plan by OAuth
device flow instead of an API key, and it reaches **Codex**, not the ChatGPT web
app. Everything about the agent's provider catalogue is read live from LiteLLM
precisely so a package bump brings new models with it. This provider is the one
exception, and it needs a person.

**The symptom, if you do not know this.** LiteLLM's registry lags the backend:
at 1.104.0 its bundled map carries fourteen `chatgpt/*` entries, newest the
`gpt-5.6` variants, while the backend serves more. The map LiteLLM fetches from
GitHub at import is usually ahead of the bundled one, so check both. A model
absent from that registry has no `mode`, so LiteLLM routes it through the
chat-completions bridge instead of `/v1/responses`. The request never reaches
the API: it lands on a Cloudflare interstitial and returns
`403 Enable JavaScript and cookies to continue`. That reads like a network
problem, an account problem or a bot block, and is none of them. Registering the
entry with `mode: responses` is the entire fix.

**The distinction that makes a candidate testable.** The backend refuses a model
it does not serve in plain words:

```
{"detail":"The 'gpt-5.6' model is not supported when using Codex with a ChatGPT account."}
```

So a clean 400 naming the model means **not available**; a 403 HTML page means
**not registered**, and is your bug, not OpenAI's. Anything else, read it.

**Adding a model.** `services/agent/chatgpt_models.py` holds the supplement,
registered into LiteLLM by `catalog._build()` and by `builder.build_model()`.
Both are needed: a run resolving a stored row never touches the catalogue. It is
deliberately NOT done in `chatgpt_oauth.ensure_ready()`, which runs inside a
request and must import no LiteLLM and do no network work -- a hook placed there
was caught by `test_the_gate_does_no_network_work`. To add a name, verify it
first against a real
subscription rather than guessing, because the set is not derivable from a
pattern -- `gpt-5.6` is refused while `gpt-5.6-sol`, `-luna` and `-terra` all
work:

```python
litellm.register_model({"chatgpt/<name>": {"litellm_provider": "chatgpt", "mode": "responses"}})
litellm.responses(model="chatgpt/<name>", input=[{"role": "user", "content": "ok"}], stream=True)
```

Then add it to `SUPPLEMENTAL` with its context window.

**Three rules for that file.**

- **Never write cost keys.** A plan turn has no per-token price, and
  `catalog.estimate_cost` returning None is what makes the usage badge report
  tokens and no cost. Reporting `$0.00` claims the turn was free when it
  consumed plan quota; falling back to the API price is worse.
- **Never overwrite a LiteLLM entry, and test the provider, not the name.**
  Eight of these models share a bare name with an OpenAI API model, so
  `"gpt-5.6-sol" in litellm.model_cost` is True because of *OpenAI's* entry. A
  guard written that way skips every model it exists to add. Match on
  `litellm_provider == "chatgpt"`.
- **Fail quietly.** The supplement is a convenience; a LiteLLM whose registry
  has a different shape should cost these models, not a working agent.

**Availability is per plan, not per provider.** These are the models the backend
serves; which a given account may use is between the operator and OpenAI. The
catalogue is advisory, and the model test on the config page is what answers for
one account. That test **streams**, deliberately: LiteLLM's non-streaming reader
for this provider raises `Unknown items in responses API response: []` on a
reply that streams back perfectly (upstream #26179, open; its fix #27562 was
closed unmerged), and the agent only ever runs `stream=True` anyway.

Delete the file when LiteLLM ships these names. Its entries win automatically.

**Strategy logic changes must get a layman summary in that strategy's STRATEGY-LOG.md**
(e.g. `signal_engine/strategy_library/intraday/breaking-trade/STRATEGY-LOG.md`) — any change to
entry, exit, stop-loss, take-profit, or filters that decide which signals become trades.
Dated entry (`### YYYY-MM-DD HH:MM — Title`, from `git log --format="%ad" --date=format:"%Y-%m-%d %H:%M"`
if already committed), five fields, one to two sentences each, no financial/technical jargon
(define a term in one clause if it can't be avoided):

- **What changed**
- **Entry** — does this change how or when a position opens?
- **Exit (SL)** — does this change the stop-loss?
- **Exit (TP)** — does this change how profit is taken?
- **Consideration** — anything a trader should watch for or weigh

Say "Not affected" for a field the change doesn't touch — don't omit it. A fuller technical
explanation (file/function names, formulas, edge cases) goes in a trailing **NOTE:** block, kept
out of the five fields above.

**All strategy and analysis timestamps are IST — this platform trades only Indian NSE/BSE
markets, never anything else.** Convert any external timestamp (broker/data API responses, raw
epoch seconds, an ISO datetime carrying its own offset) to IST the moment it is parsed, at the
boundary where it enters the codebase — never assume an API already returns IST, and never
compare a value that might not be IST against a naive-IST constant (session cutoffs, market-hour
windows, `IB_START`/`IB_END`-style literals) without converting first. Prefer
`signal_engine/timeutils.py`'s `IST` timezone object over redefining the offset locally. This is
not theoretical: `validate.fetch_bars()` silently left OpenAlgo's epoch-second bar timestamps in
UTC instead of shifting to IST, and every BreakingTrade trade-confirmation check compared
against them — so the confirmation step was structurally incapable of succeeding, for every
symbol, every day, from when the strategy was built until the bug was found (2026-09-08). No
exception, no error, nothing to search logs for — it just never worked, silently. See
`signal_engine/strategy_library/intraday/breaking-trade/STRATEGY-LOG.md`'s 2026-09-08 13:52 entry.

