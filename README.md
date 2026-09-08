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


## License

MIT. See `LICENSE`.
