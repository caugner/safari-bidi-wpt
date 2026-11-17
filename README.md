# Safari WebDriver BiDi WPT runner

Reruns a subset of the web-platform-tests WebDriver BiDi tests against Safari stable through
`/System/Cryptexes/App/usr/bin/safaridriver`, to measure how much window focus changes during
the earlier full run affected its results.

> Status: experimental. The setup dry run (`--dry-run`) has been verified. A full test run from this layout has not been verified yet.

## Prerequisites

- Remote automation is enabled for Safari stable (`safaridriver --enable` was run once).
- Safari stable only. The script does not start Safari Technology Preview or change any setting.
- Optional: a WebKit checkout, passed with `--webkit PATH`, to use its test copy instead.
- Leave the Mac idle during the run: do not switch windows, type, or use Safari.
- Ports 4460 (safaridriver), 8802 and 9444 (wptserve) are free.
- Network access on the first run, for the two sparse clones and the pinned pytest packages in `venv/`.

## Where the tests come from

By default, `run.py` reads the WPT revision that WebKit pins and fetches that commit:

1. Sparse shallow clone of WebKit into `deps/webkit/`, fetching only `importer.json`, `config.json`, and `pytest.ini`. This reads the pin from WebKit's `main` branch.
2. Sparse shallow clone of the pinned WPT commit into `deps/web-platform-tests/`, fetching `webdriver`, `tools`, `resources`, `docs`, and `wpt.py`.

Both clones are gitignored, and re-running refreshes them. Each results directory has a `sources.json` with the WebKit commit and WPT revision that were used.

Pass `--webkit PATH` to use a WebKit checkout instead. Its tests, tools, and `wpt.py` are used as-is, and nothing is fetched.

WebKit's copy and upstream WPT at the pinned commit are not identical. For the 54 files in `files.txt`, 53 match and `webdriver/tests/bidi/browsing_context/handle_user_prompt/handle_user_prompt.py` differs. WebKit's copy also has some files that upstream doesn't, such as `digital_credentials` tests. Use `--webkit` if you need WebKit's exact test set.

## Run

```sh
cd safari-bidi-wpt
python3 run.py --dry-run   # check the setup, start and stop wptserve and safaridriver, run no tests
python3 run.py             # run the files in files.txt
python3 compare.py results-<timestamp>
```

`run.py` prints one line per test file with pass/fail/error/skip counts and writes JUnit XML,
pytest output, `driver.log` and `session-errors.log` to `results-<timestamp>/`. Ctrl-C stops
safaridriver, wptserve and the automation Safari it started.

`compare.py` prints per-file and per-directory deltas and the tests whose result changed. Pass a baseline results directory as the second argument to compare two runs.

## Files

- `files.txt`: the test files to run, one per line. Edit freely; `#` lines are ignored.
- `wdspec.json`: the WDSPEC config template. `run.py` fills in the scratch path.
- `plugins/sessionlog.py`: pytest plugin that logs session start failures and session ids.
- `work/`: scratch copy of the tests, certificates and logs. Safe to delete.
- `venv/`: pytest 8.4.2, pytest-asyncio 0.19.0, pytest-timeout 2.4.0, websockets 12.0.

## Notes

- Each test file runs in its own pytest process with a fresh safaridriver, a 25 second timeout
  per test, and a 15 minute cap per file.
- Counts are per test: a test that fails and then errors in teardown counts once, as failed.
