# Binance Square GitHub Actions

This repo can run Binance Square auto posting from GitHub Actions.

Setup:

1. Push this folder to a private GitHub repository.
2. In GitHub, add repository secret `BINANCE_SQUARE_OPENAPI_KEY`.
3. Enable Actions for the repository.
4. The workflow runs every 24 minutes and posts at most one unposted draft each run.

Notes:

- API keys must only be stored in GitHub Secrets.
- The workflow keeps state in `square_github_state.json`.
- `square_drafts.jsonl` and `square_post_log.jsonl` are updated and committed back by the workflow.
- Old draft/log artifacts are cleaned every 48 hours.
- GitHub scheduled workflows are not exact to the minute; occasional delays are normal.
