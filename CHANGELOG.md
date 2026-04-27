# Changelog

## 2026-04-27

- Add Daily Activities view: per-day, per-project bulleted activity summaries inferred by Haiku via the local `claude` CLI
- Summaries are inferred lazily on demand: clicking a day fans out one `/api/cell-summary` request per project so they stream in parallel
- Cache invalidated by sha256 hash of the day's user prompts
- Day-row cost matches the sum of per-cell costs (turn-based attribution; sessions that span multiple days no longer pile onto their last day)
- Recent Sessions table now paginates the full filtered list (50 per page) instead of capping at 20
- Click a session row to expand inline activity bullets summarizing what happened in that session (cached via new `session_summaries` table)
- New env var: `SUMMARY_MODEL` (default: `haiku`)
- New `daily_summaries` and `session_summaries` tables (auto-created via `CREATE TABLE IF NOT EXISTS`)

## 2026-04-26

- Add "Setup for non-technical users (macOS)" section to README
- Add Custom date range picker (calendar-based, additive to preset buttons)
- Add in-place scan progress in terminal (TTY) and periodic logging (non-TTY)
- Add stale-data banner to dashboard when last scan is older than 24 hours
- New `scan_meta` table tracks `last_scan_at` (auto-created via CREATE TABLE IF NOT EXISTS)

## 2026-04-09

- Fix token counts inflated ~2x by deduplicating streaming events that share the same message ID
- Fix session cost totals that were inflated when sessions spanned multiple JSONL files
- Fix pricing to match current Anthropic API rates (Opus $5/$25, Sonnet $3/$15, Haiku $1/$5)
- Add CI test suite (84 tests) and GitHub Actions workflow running on every PR
- Add sortable columns to Sessions, Cost by Model, and new Cost by Project tables
- Add CSV export for Sessions and Projects (all filtered data, not just top 20)
- Add Rescan button to dashboard for full database rebuild
- Add Xcode project directory support and `--projects-dir` CLI option
- Non-Anthropic models (gemma, glm, etc.) no longer incorrectly charged at Sonnet rates
- CLI and dashboard now both compute costs per-turn for consistent results
