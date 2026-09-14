# Development commands

Run these commands from the project root. This file is the permanent reference for project development commands.

## Automated tests

```powershell
python -m unittest discover -s tests -v
```

Runs all automated tests found in `tests/` using verbose output.

## Translation validation

```powershell
python tools/check_i18n_translations.py
```

Validates the locale registry and translation JSON files, including key parity, placeholders, duplicate keys, and frontend i18n structure; the tool has no additional command-line options.

## Backend message contract

```powershell
python tools/check_backend_message_contract.py
```

Validates the canonical backend message envelope and checks that legacy message-contract debt has not increased beyond the stored baseline.

To print the current transition-debt inventory even when validation passes:

```powershell
python tools/check_backend_message_contract.py --inventory
```

## Diagnostic summaries

```powershell
python tools/summarize_diagnostics.py PATH
```

Summarizes diagnostic sessions from a diagnostics directory, a `*_summary.json` file, or a `.jsonl` events file; when a directory is supplied, it shows the latest two sessions by default and compares them.

To choose how many latest sessions are loaded from a directory:

```powershell
python tools/summarize_diagnostics.py PATH --latest N
```

`PATH` is required and `--latest N` defaults to `2`; values below `1` are treated as `1`.
