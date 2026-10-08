# Hospital MRF finder (GitHub Actions)

1. Put your hospitals in `hospitals.csv` (only `name` is required; `state`, `city`, `url` help).
2. Commit it. The workflow runs by itself and writes `results/results.csv`.
3. Or run it by hand: **Actions -> Find hospital MRF files -> Run workflow**
   (fill in one hospital, or leave empty to run the whole CSV).

Results: `results/results.csv` in the repo, the table on the run page, and the
`mrf-results` artifact (includes the debug log).

A URL appears only when the file is proven to belong to that hospital;
otherwise `mrf_url` is empty.

Optional secrets (Settings -> Secrets and variables -> Actions):
`ANTHROPIC_API_KEY` (last-resort Claude search), `BRAVE_API_KEY` (better search),
`MRF_PROXY` (a proxy, if a site blocks GitHub's servers).
