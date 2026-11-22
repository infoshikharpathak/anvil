"""Output-scan eval on real agent replies.

labels.jsonl holds replies the real agents wrote (sampled by the scenario
harnesses in Ironclad and FoodDash, see README.md), each labeled ok,
ai_instruction, data_leak or unrequested_action, and split into dev and test
by scenario. Three setups are measured, each on the live hybrid path (regex
first, then the LLM):

- base:  BASE_PROMPT, yes/no; anything detected is blocked.
- tuned: round 4's yes/no output prompt (output_binary); detected = blocked.
- new:   anvil.output_guard first (blocks), then the labeled prompt
         (output_context): ai_instruction / data_leak block,
         unrequested_action flags (delivered with a warning).

    python -m evals.replies.run --runs 3 --set test

Per reply the majority outcome over runs counts (block, flag or pass).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import Counter
from pathlib import Path

import httpx

from anvil.output_guard import otp_guard
from evals.injection.run import VARIANTS, classify

HERE = Path(__file__).resolve().parent
LABELS = ("ok", "ai_instruction", "data_leak", "unrequested_action")
SETUPS = {"base": "baseline", "tuned": "tuned", "new": "labeled"}


def load(split: str, file: str = "labels") -> list[dict]:
    """labels.jsonl (real replies, split dev/test) or synthetic.jsonl (the
    synthetic attack set: no split, measured on its own, never tuned on)."""
    rows = [json.loads(line) for line in (HERE / f"{file}.jsonl").read_text().splitlines() if line.strip()]
    for r in rows:
        r.setdefault("scenario", r["id"])
    return [r for r in rows if split == "all" or r.get("split") == split]


async def outcome(client, row: dict, setup: str, model: str) -> tuple[str, str | None, tuple[int, int]]:
    """(block|flag|pass, which check, tokens) for one reply under *setup*."""
    if setup == "new":
        reason = otp_guard(row["text"])
        if reason:
            return "block", "otp_guard", (0, 0)
    case = {"id": row["id"], "direction": "output", "text": row["text"], "context": row["context"]}
    v = await classify(client, case, VARIANTS[SETUPS[setup]], model)
    if not v["flagged"]:
        return "pass", None, v["tokens"]
    if setup == "new" and v.get("action") == "flag":
        return "flag", v.get("label"), v["tokens"]
    return "block", v.get("label") or v["stage"], v["tokens"]


async def run(split: str, setup: str, model: str, runs: int, file: str = "labels") -> dict:
    rows = load(split, file)
    sem = asyncio.Semaphore(8)
    headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
    tokens = [0, 0]
    async with httpx.AsyncClient(timeout=60, headers=headers) as client:
        async def one(row):
            async with sem:
                outs = [await outcome(client, row, setup, model) for _ in range(runs)]
            for _, _, t in outs:
                tokens[0] += t[0]
                tokens[1] += t[1]
            votes = Counter(o for o, _, _ in outs)
            final = votes.most_common(1)[0][0]
            return {"id": row["id"], "scenario": row["scenario"], "label": row["label"], "outcome": final,
                    "votes": dict(votes), "checks": [c for _, c, _ in outs], "text": row["text"]}
        results = await asyncio.gather(*(one(r) for r in rows))
    table = {}
    for label in LABELS:
        rs = [r for r in results if r["label"] == label]
        table[label] = {"n": len(rs), **{o: sum(r["outcome"] == o for r in rs) for o in ("block", "flag", "pass")}}
    try:
        from agent_forge.pricing import estimate_cost  # noqa: PLC0415  (optional dependency)
        cost = estimate_cost(model, *tokens)
    except ImportError:
        cost = round((tokens[0] * 0.15 + tokens[1] * 0.60) / 1_000_000, 6) if model == "gpt-4o-mini" else None
    return {"split": split, "setup": setup, "model": model, "runs": runs, "n": len(rows), "table": table,
            "results": results, "tokens": tokens, "cost_usd": cost,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def summary_line(r: dict) -> dict:
    t = r["table"]
    attacks = t["ai_instruction"]["n"] + t["data_leak"]["n"]
    return {
        "legit_blocked": f"{t['ok']['block']}/{t['ok']['n']}",
        "legit_flagged": f"{t['ok']['flag']}/{t['ok']['n']}",
        "attacks_missed": f"{t['ai_instruction']['pass'] + t['ai_instruction']['flag'] + t['data_leak']['pass'] + t['data_leak']['flag']}/{attacks}",
        "unrequested_caught": f"{t['unrequested_action']['flag'] + t['unrequested_action']['block']}/{t['unrequested_action']['n']}",
        "unrequested_blocked": f"{t['unrequested_action']['block']}/{t['unrequested_action']['n']}",
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--set", choices=["dev", "test", "all"], default="dev")
    p.add_argument("--setups", nargs="+", default=list(SETUPS))
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--model", default="gpt-4o-mini")
    p.add_argument("--out", default=str(HERE / "results"))
    p.add_argument("--file", choices=["labels", "synthetic"], default="labels")
    args = p.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for setup in args.setups:
        r = asyncio.run(run(args.set, setup, args.model, args.runs, args.file))
        stem = f"{setup}-{args.set}" if args.file == "labels" else f"synthetic-{setup}"
        (out / f"{stem}.json").write_text(json.dumps(r, indent=1))
        print(setup, args.set, json.dumps(r["table"]), json.dumps(summary_line(r)), f"${r['cost_usd']}")


if __name__ == "__main__":
    main()
