# Gotchas

## .numbers write can corrupt cells
`numbers-parser`'s `save()` has been observed to mangle untouched cells (e.g. turning a JSON
value's straight quotes into smart quotes, breaking parsing). Mitigation lives in
`tools/results_writer.py`: it backs up the file, re-reads the case-definition region after
saving, and **restores from backup + raises `ResultsWriteError` if anything changed** (backup
auto-deleted on success). Don't bypass this guard.

## .env uses `:` separators
The provided `.env` uses `key:value` instead of `key=value`, which python-dotenv ignores.
`config._read_env_file` handles both. Do **not** rewrite `.env` programmatically — it's an
untracked secrets file and the harness blocks editing it.

## Parser must skip the results block
Results blocks are appended into the same sheet. `tools/suite.py` stops reading cases at the
first row whose `test_id` cell starts with `RESULTS` (`RESULTS_MARKER`). Keep that marker.

## Tests stay offline
All HTTP is mocked with `httpx.MockTransport`. `numbers-parser` write tests use `.xlsx`
fixtures (the writer supports both). Don't add tests that hit the network or the real `.env`.

## Sheet is user-edited
`api_test_suite_sample.numbers` is edited by the user between runs (flags, expected strings,
even invalid JSON with smart quotes). Don't assert its exact contents in tests — only stable
structural facts.
