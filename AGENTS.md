# Repository maintenance instructions

## Shell and scripting

- On Windows, use Git Bash for ordinary shell commands. Resolve it from `%USERPROFILE%/scoop/apps/git/current/bin/bash.exe` or another installed Git Bash location; expand the chosen path, then explicitly select that absolute executable path when the execution tool supports shell selection.
- Prefer Python for non-trivial text processing, structured-data transformations, and logic that would be fragile in shell syntax.
- Use PowerShell only for Windows-specific tasks such as registry access, services, or Windows management cmdlets, or when Bash and Python are unsuitable. Do not pass Bash syntax to PowerShell.
- With Git Bash, use POSIX-style paths for Bash-native commands and Windows paths only when the invoked Windows program requires them.

## Repository map

- `README.md` is the user guide and release/build overview. Keep its Windows, portability, data-storage, and OAuth descriptions aligned with runtime behavior.
- `CHANGELOG.md`, `LICENSE`, `SECURITY.md`, and `THIRD_PARTY_NOTICES.md` cover release history, project licensing, vulnerability reporting, and bundled third-party licenses.
- `antigravity_account_switcher.py` is the application entry point and command-line dispatch. `switcher_ui.py` contains the Tkinter interface.
- `account_core.py` handles account credential import and account switching; `process_control.py` locates and closes the official app when switching; `runtime_control.py` coordinates application startup and asynchronous work.
- `config.py` owns configuration parsing and paths; `cache_store.py` stores quota cache data; `quota_client.py` implements OAuth refresh and quota requests; `common.py` contains shared helpers; `portable_runtime.py` resolves portable/runtime paths.
- `tests/` contains pytest coverage. `packaging/portable.spec` defines the PyInstaller package, `packaging/build_portable.py` builds and smoke-checks the Windows portable package, and `packaging/licenses/` records bundled license sources.
- `.github/workflows/ci.yml` runs tests on pushes and pull requests. `.github/workflows/portable-release.yml` tests and packages manually or on version tags, then creates a draft release for tags.
- `dist/` contains release outputs. Keep the current final portable ZIP, source ZIP, `SHA256SUMS`, and smoke-test report intact unless the release owner asks for a rebuild.

## Platform and portable package conventions

- The supported release target is Windows 10 or newer, x86-64 (AMD64). The portable build uses 64-bit CPython 3.14.x and the pinned dependencies in `requirements-build.txt`; do not describe it as supporting 32-bit or ARM Windows.
- The package layout has the executable at its root, PyInstaller files in `App/`, user data in `Data/`, and documentation and license notices in `Other/`. `Data/` is user-owned, local, plaintext data. Upgrades must preserve the user's existing `Data/` directory.
- The application can read legacy repository-local `local-data/` configuration. Treat `local-data/`, `local-reference/`, and any `Data/` directory as private or user-owned. Never inspect, copy, include in archives, print, or alter their contents as part of routine maintenance.
- `packaging/build_portable.py` builds in `build/` and stages temporary files below `dist/`; its source archive uses an explicit public-file allowlist. Keep those safety boundaries intact. Do not make broad repository snapshots or package local data, private references, caches, credentials, or scan reports.
- For GitHub network operations, set `HTTP_PROXY` and `HTTPS_PROXY` to `http://127.0.0.1:10809` only in the command's environment when needed. Never store proxy authentication or credentials in the repository, and do not change persistent user or system proxy configuration.

## Safe development and verification

- Use only temporary simulated accounts, credentials, configuration, and responses in tests. Isolate every candidate configuration and test data directory in a fresh temporary location; never point test runs at the user's real configuration or shared local data.
- Never make real API/OAuth requests during tests or routine verification. In particular, do not run `--quota-only` against a real service. Do not read from or write to Windows Credential Manager. Mock network and credential-manager boundaries.
- Do not print or commit real credentials, tokens, private configuration, logs, caches, user data, or security scan reports. Before staging, inspect the exact paths being staged.
- Run tests with `python -m pytest tests -q` after changes that affect behavior. Build the portable package only when explicitly needed, following the README's Windows x64 and CPython 3.14.x requirements.
- Keep cleanup narrowly scoped to confirmed generated outputs (`build/`, disposable `artifacts/`, `.pytest_cache/`, and Python `__pycache__/` directories). Resolve and verify every cleanup target remains inside the repository before deleting it. Preserve `dist/` release outputs and all private/user data directories.

## Change ownership and security review

- Assign every persistent code or documentation change to its own `gpt-6-luna` subagent working directly on `main`; each subagent owns and commits its assigned change. Because these workers share the Git index, serialize all staging and commits. Do not stage or commit another worker's files.
- After code modifications are complete, have the DeepSec subagent run its offline L1/L2 security snapshot scan on the changed code. Keep scan reports private and out of Git.
- Keep each commit narrowly scoped to the files assigned to that subagent.
