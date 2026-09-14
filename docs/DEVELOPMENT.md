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

To start an installed Catalog instance with diagnostics enabled, run this from its instance folder in PowerShell:

```powershell
$env:CATALOG2_DIAGNOSTICS = "1"
& ".\Start Catalog.bat"
```

Browse the folders to be measured, then stop the Catalog with `Ctrl+C` so the diagnostic summary is finalized. Diagnostic files are written under `Catalog_Output\_state\diagnostics\`.

```powershell
python tools/summarize_diagnostics.py "C:\path\to\Catalog_Output\_state\diagnostics" --latest 1
```

Summarizes the latest diagnostic session, including individual `/api/folders` request breakdowns. The path may also point to one `*_summary.json` file or one `.jsonl` events file.

To load and compare more sessions from a diagnostics directory:

```powershell
python tools/summarize_diagnostics.py PATH --latest N
```

`PATH` is required and `--latest N` defaults to `2`; values below `1` are treated as `1`.
