# Contributing to AirPy

Thanks for helping! AirPy is small and Windows‑only, so changes are easy to review. Please open an issue
first for larger features.

## Setup

```bash
git clone <your fork>
cd airpy
py -3.14 -m venv .venv          # 3.12-3.14 work
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m airpy    # run from source
```

## Checks (CI runs the same)

```bash
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m pytest
```

The tests need no audio device, speaker or network access beyond `127.0.0.1`.

## Testing against real hardware

- **Never play audible test audio on someone else's speakers.** Automated checks should send digital silence
  (an empty `Timeline`) and must not change speaker volume. The existing tests in `tests/` follow this.
- When you report results, say which receiver (model, firmware) and Windows/Python versions you used.

## Pull requests

- Keep them focused; add or update a test when you change behaviour.
- Follow the existing style (`ruff` enforces most of it). Comments and strings are English.
- User-visible strings live next to the code (mostly `airpy/ui/`, `airpy/web/*.html`, `extension/popup.*`)
  and are English only for now. **An i18n layer and translations are very welcome.**
- Don't commit personal data (IP addresses, device names, tokens, logs). Redact them in issues too.

## Ideas that would help

Other UI languages (an i18n layer) · tested reports for Apple TV and third‑party AirPlay speakers · password‑protected
receivers · a packaged installer (PyInstaller/MSIX) · Chrome Web Store / Edge Add‑ons packaging for the extension.
