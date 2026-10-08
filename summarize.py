"""Turn results/results.jsonl into results/results.csv + a table on the run page."""
import csv
import json
import os

src = "results/results.jsonl"
rows = []
if os.path.exists(src):
    rows = [json.loads(line) for line in open(src, encoding="utf-8") if line.strip()]

fields = ["hospital", "state", "status", "mrf_url", "confidence",
          "verified_by_download", "warning", "note"]
with open("results/results.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)

found = sum(1 for r in rows if r.get("mrf_url"))
lines = [f"### Found {found} of {len(rows)} files", "",
         "| Hospital | Confidence | File URL |", "|---|---|---|"]
for r in rows:
    lines.append(f"| {r.get('hospital')} | {r.get('confidence')} | "
                 f"{r.get('mrf_url') or 'not found'} |")
text = "\n".join(lines) + "\n"
print(text)
summary = os.environ.get("GITHUB_STEP_SUMMARY")
if summary:
    with open(summary, "a", encoding="utf-8") as f:
        f.write(text)
