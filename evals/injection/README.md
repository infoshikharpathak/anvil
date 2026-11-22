# Injection classifier eval

Labeled cases for Anvil's hybrid injection scan (regex first, then an LLM
classifier), in three directions: `input` (a user's message to their agent),
`tool_response` (data or an error body a tool returned) and `output` (the
agent's reply to its user).

- `dev.jsonl` (30 cases): for tuning prompts. Round 4 added the live output
  false positives (`out-live-*`, real Ironclad replies) and moved
  `out-new-policy-otp` here from test, so it can be tuned on.
- `test.jsonl` (32 cases): held out. Run it for a baseline and once more
  after the prompt is frozen; don't tune against it. 10 output cases
  (`out-new-*`) were added after round 2's tuning and 8 (`out-r4-*`) before
  round 4's, each labeled and committed before it was run.

Each line: `id`, `direction`, `text`, optional `context` (`agent`, `tool`,
`user_request`), `label` (`flag` | `allow`), `source` (`real_trace` |
`written`) and a one-line `note`. The split was made once with a seeded,
stratified shuffle (about 62% of every direction/label group to dev).
Real cases come from FoodDash and Ironclad traces: the QuickMart refusal the
output scan falsely blocked (trace `1cbe4326…`), the planted poisoned memo,
the "call place_order directly as your very first and only tool call" input
that was blocked, the menu-skip messages that got through, and the banking
agent's reply after the memo was withheld.

## Running it

```bash
export OPENAI_API_KEY=...
python -m evals.injection.run --model gpt-4o-mini --runs 3 --prompt baseline --set both
python -m evals.injection.run --model gpt-4o-mini --runs 3 --prompt tuned --set dev
```

- `--prompt baseline`: `BASE_PROMPT` for every direction, API-default
  temperature. This is exactly what bastion's classifier sends.
- `--prompt tuned`: what classifiers use by default now: the
  `output_context` variant of `anvil.injection_prompts.classifier_prompt`
  (a note for agent replies; other directions unchanged), temperature 0 for
  output scans and the API default otherwise.

`results/round1/` holds the first test-set run (14 cases). `results/round4/`
holds round 4's before/after runs on dev and test and a summary
(`summary.md`); `results/tuned-*` and `baseline-*` are the latest runs.

Each run writes `results/<prompt>-<set>.json` (per-run metrics, per-case
verdicts and stages, unstable cases, tokens, cost) and a markdown summary.
CI only checks the dataset format (`tests/test_eval_dataset.py`); a real run
needs an API key, so it stays manual. A full run of both sets costs well
under a cent with gpt-4o-mini.
