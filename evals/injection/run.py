"""Injection-classifier eval: precision, recall and false-positive rate of
Anvil's hybrid scan (regex first, then an LLM classifier) on labeled cases.

    python -m evals.injection.run --model gpt-4o-mini --runs 3 [--prompt baseline|tuned] [--set dev|test|both]

Needs OPENAI_API_KEY (the classifier is called directly over HTTP, like
bastion's ProviderClassifier). Writes results/<prompt>-<set>.json and .md.
See evals/injection/README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from anvil.injection_prompts import BASE_PROMPT, OUTPUT_TEMPERATURE, classifier_prompt, parse_verdict
from anvil.validators import scan_for_injection

HERE = Path(__file__).resolve().parent
DIRECTIONS = ("input", "tool_response", "output")
LABELS = ("flag", "allow")
SOURCES = ("real_trace", "written")
REQUIRED = ("id", "direction", "text", "label", "source", "note")

# USD per 1M tokens (input, output), from https://developers.openai.com/api/docs/pricing, checked 2026-11-22.
PRICES = {"gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.50, 10.00)}


@dataclass
class Variant:
    """baseline: the base prompt everywhere, API-default temperature (bastion
    before output_context). tuned: what classifiers use by default now, the
    output_context prompt, at OUTPUT_TEMPERATURE for output scans only."""

    name: str

    def system_prompt(self, case: dict) -> str:
        if self.name == "baseline":
            return BASE_PROMPT
        variant = "output_context" if self.name == "labeled" else "output_binary"
        return classifier_prompt(case["direction"], case.get("context"), variant=variant)

    def temperature(self, case: dict) -> float | None:  # None: the API default
        if self.name in ("tuned", "labeled") and case["direction"] == "output":
            return OUTPUT_TEMPERATURE
        return None


# tuned: round 4's yes/no output prompt (output_binary). labeled: the
# four-label output prompt (output_context, the default now); a case counts
# as flagged if it is blocked or flagged.
VARIANTS = {"baseline": Variant("baseline"), "tuned": Variant("tuned"), "labeled": Variant("labeled")}


def load(name: str) -> list[dict]:
    rows = [json.loads(line) for line in (HERE / f"{name}.jsonl").read_text().splitlines() if line.strip()]
    for row in rows:
        problems = validate_case(row)
        if problems:
            raise ValueError(f"{name}.jsonl case {row.get('id')}: {'; '.join(problems)}")
    return rows


def validate_case(row: dict) -> list[str]:
    problems = [f"missing {k}" for k in REQUIRED if not row.get(k)]
    if row.get("direction") not in DIRECTIONS:
        problems.append(f"direction must be one of {DIRECTIONS}")
    if row.get("label") not in LABELS:
        problems.append(f"label must be one of {LABELS}")
    if row.get("source") not in SOURCES:
        problems.append(f"source must be one of {SOURCES}")
    if "context" in row and not isinstance(row["context"], dict):
        problems.append("context must be an object")
    extra = set(row) - {*REQUIRED, "context"}
    if extra:
        problems.append(f"unknown fields {sorted(extra)}")
    return problems


async def classify(client: httpx.AsyncClient, case: dict, variant: Variant, model: str) -> dict:
    """One case through the hybrid scan. Returns verdict, stage and tokens."""
    regex = scan_for_injection(case["text"])
    if regex.detected:
        return {"flagged": True, "stage": "regex", "pattern": regex.matched_pattern, "tokens": (0, 0)}
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": variant.system_prompt(case)},
                     {"role": "user", "content": case["text"][:4000]}],
    }
    temperature = variant.temperature(case)
    if temperature is not None:
        body["temperature"] = temperature
    for attempt in range(4):
        resp = await client.post("https://api.openai.com/v1/chat/completions", json=body)
        if resp.status_code == 429 or resp.status_code >= 500:
            await asyncio.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        break
    data = resp.json()
    usage = data.get("usage") or {}
    tokens = (usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
    content = data["choices"][0]["message"]["content"] or ""
    try:
        verdict = parse_verdict(content)
    except ValueError:
        # Unreadable: Anvil would fall back to the regex scan, which passed.
        return {"flagged": False, "stage": "unparsed", "pattern": None, "tokens": tokens}
    return {"flagged": verdict.detected, "stage": "llm", "pattern": verdict.matched_pattern,
            "label": verdict.label, "action": verdict.action if verdict.detected else None, "tokens": tokens}


def metrics(cases: list[dict], flagged: dict[str, bool]) -> dict:
    out = {}
    for direction in (*DIRECTIONS, "overall"):
        rows = [c for c in cases if direction == "overall" or c["direction"] == direction]
        tp = sum(1 for c in rows if c["label"] == "flag" and flagged[c["id"]])
        fn = sum(1 for c in rows if c["label"] == "flag" and not flagged[c["id"]])
        fp = sum(1 for c in rows if c["label"] == "allow" and flagged[c["id"]])
        tn = sum(1 for c in rows if c["label"] == "allow" and not flagged[c["id"]])
        out[direction] = {
            "n": len(rows), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(tp / (tp + fp), 3) if tp + fp else None,
            "recall": round(tp / (tp + fn), 3) if tp + fn else None,
            "fpr": round(fp / (fp + tn), 3) if fp + tn else None,
        }
    return out


async def run(set_name: str, variant: Variant, model: str, runs: int, concurrency: int = 8) -> dict:
    cases = load(set_name)
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("OPENAI_API_KEY is not set")
    sem = asyncio.Semaphore(concurrency)
    tokens_in = tokens_out = 0
    per_run: list[dict[str, dict]] = []
    async with httpx.AsyncClient(timeout=60, headers={"Authorization": f"Bearer {key}"}) as client:
        for _ in range(runs):
            async def one(case):
                async with sem:
                    return case["id"], await classify(client, case, variant, model)
            results = dict(await asyncio.gather(*(one(c) for c in cases)))
            for r in results.values():
                tokens_in += r["tokens"][0]
                tokens_out += r["tokens"][1]
            per_run.append(results)

    run_metrics = [metrics(cases, {cid: r["flagged"] for cid, r in res.items()}) for res in per_run]
    unstable = []
    for c in cases:
        verdicts = [res[c["id"]]["flagged"] for res in per_run]
        if len(set(verdicts)) > 1:
            unstable.append({"id": c["id"], "label": c["label"], "flagged_in_runs": sum(verdicts), "runs": runs})
    spread = {}
    for direction in (*DIRECTIONS, "overall"):
        spread[direction] = {}
        for m in ("precision", "recall", "fpr"):
            values = [rm[direction][m] for rm in run_metrics if rm[direction][m] is not None]
            spread[direction][m] = {
                "mean": round(statistics.mean(values), 3) if values else None,
                "min": min(values) if values else None, "max": max(values) if values else None,
            }
    rates = PRICES.get(model)
    cost = round((tokens_in * rates[0] + tokens_out * rates[1]) / 1e6, 6) if rates else None
    cases_out = []
    for c in cases:
        verdicts = [res[c["id"]] for res in per_run]
        cases_out.append({"id": c["id"], "direction": c["direction"], "label": c["label"],
                          "flagged_in_runs": sum(v["flagged"] for v in verdicts),
                          "stages": sorted({v["stage"] for v in verdicts}),
                          "patterns": sorted({v["pattern"] for v in verdicts if v["pattern"]})})
    return {
        "set": set_name, "prompt": variant.name,
        "temperature": "0 for output, API default otherwise" if variant.name == "tuned" else "API default",
        "model": model, "runs": runs,
        "cases": len(cases), "run_metrics": run_metrics, "spread": spread, "unstable_cases": unstable,
        "case_results": cases_out, "tokens": {"input": tokens_in, "output": tokens_out}, "cost_usd": cost,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _fmt(x) -> str:
    return "—" if x is None else f"{x:.2f}"


def summary_md(result: dict) -> str:
    lines = [f"# Injection eval: {result['prompt']} prompt on the {result['set']} set",
             "",
             f"{result['model']}, temperature {result['temperature']}, "
             f"{result['runs']} runs over {result['cases']} cases, {result['finished_at']}. "
             f"Cost ${result['cost_usd']} ({result['tokens']['input']} in / {result['tokens']['output']} out tokens).",
             "",
             "Mean over runs (min–max):",
             "",
             "| Direction | Precision | Recall | False-positive rate |",
             "|---|---|---|---|"]
    for d, s in result["spread"].items():
        cells = [f"{_fmt(s[m]['mean'])} ({_fmt(s[m]['min'])}–{_fmt(s[m]['max'])})" for m in ("precision", "recall", "fpr")]
        lines.append(f"| {d} | " + " | ".join(cells) + " |")
    lines += ["", "Per run (overall): " + ", ".join(
        f"TP {m['overall']['tp']} FP {m['overall']['fp']} FN {m['overall']['fn']} TN {m['overall']['tn']}"
        for m in result["run_metrics"])]
    if result["unstable_cases"]:
        lines += ["", "Cases whose verdict changed between runs:", ""]
        lines += [f"- `{u['id']}` ({u['label']}): flagged in {u['flagged_in_runs']}/{u['runs']} runs"
                  for u in result["unstable_cases"]]
    else:
        lines += ["", "No case changed verdict between runs."]
    wrong = [c for c in result["case_results"]
             if (c["label"] == "flag") != (c["flagged_in_runs"] * 2 > result["runs"])]
    if wrong:
        lines += ["", "Misclassified (majority of runs):", ""]
        lines += [f"- `{c['id']}` ({c['direction']}, {c['label']}): flagged {c['flagged_in_runs']}/{result['runs']}"
                  + (f", pattern {c['patterns'][0]!r}" if c["patterns"] else "") for c in wrong]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m evals.injection.run")
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--prompt", choices=sorted(VARIANTS), default="tuned")
    parser.add_argument("--set", choices=["dev", "test", "both"], default="dev")
    parser.add_argument("--out", default=str(HERE / "results"))
    args = parser.parse_args(argv)
    variant = VARIANTS[args.prompt]
    total = 0.0
    for set_name in (["dev", "test"] if args.set == "both" else [args.set]):
        result = asyncio.run(run(set_name, variant, args.model, args.runs))
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        stem = f"{args.prompt}-{set_name}"
        (out / f"{stem}.json").write_text(json.dumps(result, indent=2) + "\n")
        (out / f"{stem}.md").write_text(summary_md(result))
        o = result["spread"]["overall"]
        print(f"{stem}: precision {_fmt(o['precision']['mean'])}, recall {_fmt(o['recall']['mean'])}, "
              f"FPR {_fmt(o['fpr']['mean'])}, unstable {len(result['unstable_cases'])}, cost ${result['cost_usd']}")
        total += result["cost_usd"] or 0
    print(f"total cost ${round(total, 6)}")


if __name__ == "__main__":
    main()
