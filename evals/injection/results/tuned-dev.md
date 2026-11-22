# Injection eval: tuned prompt on the dev set

gpt-4o-mini, temperature 0.0, 3 runs over 26 cases, 2026-11-22T02:35:53Z. Cost $0.004838 (28275 in / 994 out tokens).

Mean over runs (min–max):

| Direction | Precision | Recall | False-positive rate |
|---|---|---|---|
| input | 1.00 (1.00–1.00) | 1.00 (1.00–1.00) | 0.00 (0.00–0.00) |
| tool_response | 1.00 (1.00–1.00) | 1.00 (1.00–1.00) | 0.00 (0.00–0.00) |
| output | 1.00 (1.00–1.00) | 1.00 (1.00–1.00) | 0.00 (0.00–0.00) |
| overall | 1.00 (1.00–1.00) | 1.00 (1.00–1.00) | 0.00 (0.00–0.00) |

Per run (overall): TP 12 FP 0 FN 0 TN 14, TP 12 FP 0 FN 0 TN 14, TP 12 FP 0 FN 0 TN 14

No case changed verdict between runs.
