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

## Runtime

Product launchers use `runtime/ac_runtime.py` to install pinned sources into
private environments. See the [runtime guide](runtime/README.md) for source
overrides, optional dependencies and interrupted-install recovery.

## Development

```bash
python -m pytest --import-mode=importlib packages/*/tests tests
scripts/build-packages.sh
```

Generated files, test runs, and caches belong under ignored `local/` paths.
See `AGENTS.md` for repository rules.

## Release

All packages share one version. Prepare an update with:

```bash
scripts/release-ac-foundation.sh X.Y.Z
```

Commit the changes through a PR. After it passes CI and merges into `main`,
create an annotated `vX.Y.Z` tag on the merged commit. Published tags are immutable.

## License

MIT. See `LICENSE`.
