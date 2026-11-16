# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's
[private vulnerability reporting](../../security/advisories/new) for this repository ("Report a vulnerability"
on the Security tab). Include steps to reproduce and the AirPy version.

You can expect an acknowledgement within a few days. This is a hobby project, so fixes are best‑effort.

## Scope and design notes

- The **phone sharing page** is HTTP on the local network (or Tailscale), protected by a random secret in the
  URL. It is not meant for the open internet.
- The **extension API** (`127.0.0.1`) is intentionally minimal and read‑only; web pages must not be able to read
  it (no CORS headers, `Origin` and `Host` are validated).
- Uploaded file names are sanitised and confined to the receive folder.

Interesting areas for review: the upload handler (`airpy/share.py`), the local API guard
(`airpy/localapi.py`) and the extension's content script.
