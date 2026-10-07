# AC Foundation

AC Foundation provides neutral, reusable infrastructure for agentic products.
It does not define a research or learning workflow and ships no agent-host
Plugin or Skill.

Packages:

- `ac-jobs`: durable runs, artifacts, recovery, work groups, and cooperative stop.
- `ac-llm`: provider-neutral model execution and structured output.
- `ac-document`: document ingestion, parsing, search, and rich-document contracts.
- `ac-proposer-reviewer`: typed proposal and review orchestration.

All packages require Python 3.11 or newer. Install distributions directly or
let a product launcher create a private runtime. Public CLIs are `ac-jobs`,
`ac-llm`, `ac-document`, and `ac-proposer-reviewer`.

## Development

```bash
python -m pytest --import-mode=importlib packages/*/tests tests
scripts/build-packages.sh
```

Generated files, test runs, and caches belong under ignored `local/` paths.
See `AGENTS.md` for repository rules.

## Release

All Foundation distributions share one repository version. Prepare a release
with:

```bash
scripts/release-ac-foundation.sh VERSION
```

Product repositories depend on Foundation with `>=2,<3`; runtime source locks
pin an exact full Git commit SHA.

The canonical product bootstrap is `runtime/ac_runtime.py`. Its explicit
`AC_INSTALL_SOURCE=mixed` mode uses a local checkout only for sources whose
`local_root_env` is provided; other sources install from their locked Git SHA.
For example, a product may set `AC_PRODUCT_REPO_ROOT` without requiring a
Foundation checkout. An explicit invalid or empty root is an error, not a Git
fallback. Each mixed source records its mode and, when local, root/content hash
in runtime identity; local edits select a new private runtime.

Existing `auto` (local only with all valid roots), `local` (all roots required),
and `git` modes are unchanged. Product-generated copies must be synchronized
with this canonical file and their generated-source checksum manifests.

An explicit `--requirements <file>` before `setup`, `doctor`, `run` or `script`
selects an optional environment. The file contains plain exact external pins
such as `numpy==2.3.5`. Its normalized requirements enter the runtime fingerprint
and installation alongside the locked source packages; the base environment
is preserved. Source-owned packages cannot be overridden. Use the same option
for subsequent commands. Without it, the existing base fingerprint is unchanged.


### Interrupted runtime installation

Bootstrap layout v2 uses POSIX `flock` on a permanent lock inode. Kernel
ownership is authoritative; hostnames, PIDs, namespace-visible process lists,
and elapsed time are not takeover criteria. Installer subprocesses inherit the
lock descriptor. Closing the coordinator does not unlock a surviving child;
`setup --retry` waits and reports `lock_occupied`/`lock_wait_timeout` rather than
starting a competing writer. Lock acquisition errors report
`lock_ownership_unverifiable` and stop. The lock file must not be removed.

Each installation uses a separate retained `attempts/<id>/venv`. Only a
completed attempt is published through the stable `venv` link and an exact
matching success marker. This also isolates a surviving grandchild that closes
inherited descriptors. Absolute console-script shebangs continue to point at
the original attempt directory. Incomplete environments cannot satisfy doctor
readiness. Logs stream into each attempt as commands run; failed/interrupted
attempts and their state remain available after retry. A ready v2 runtime has a
read-only fast path and repeated setup does not reinstall it.

Layout v2 deliberately does not reuse or mutate v1 runtimes, whose directory
locks cannot safely cooperate with the new protocol. Updating the generated
bootstrap selects a separate v2 runtime automatically, preserving old evidence
without manual lock deletion. This is an internal layout version, not a package
release. Current installation support requires Linux/macOS and a filesystem
that reliably implements shared advisory `flock`; Windows and remote filesystem
locking semantics are not certified. Different PID namespaces do not supply
process-liveness evidence. Tests cover shared local inodes with simulated
foreign namespace owner records; deployment-specific mount semantics still need
verification. See [flock semantics](https://man7.org/linux/man-pages/man2/flock.2.html).

The installer preserves explicit `UV_CACHE_DIR` and `PIP_CACHE_DIR`. Otherwise
it selects private `cache/uv` and `cache/pip` paths under that runtime. Temporary
storage uses the first configured `TMPDIR`, `TEMP`, or `TMP`, otherwise private
`tmp`; all installer subprocess temp variables use that selected directory.
The selected launcher Python is passed to uv and automatic Python downloads are
disabled. `HOME`, system directory permissions and shell profiles are unchanged.
Doctor reports these paths with read-only access estimates and probes an
existing lock without creating it. Setup performs actual write probes and
reports the exact failing path/OS error; unrelated dependency/network errors
retain their original classification and logs.

After an external platform approval cancellation, retain the platform message
separately. The bootstrap cannot infer user intent or repair that platform's
approval system. Run doctor to inspect lock availability and the last attempt;
then use the original `setup --retry` entry point. An occupied lock means wait
for or inspect the existing installer. Do not delete its lock, blindly loop
retries, or escalate permissions to bypass a platform decision.


## License

MIT. See `LICENSE`.
