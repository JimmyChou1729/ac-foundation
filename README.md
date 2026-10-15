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

Choose the next `X.Y.Z` version and prepare the shared package version
from a clean task branch:

```bash
scripts/release-ac-foundation.sh X.Y.Z
```

Include the version updates in the release PR and merge it with a merge commit.

When a PR changes `VERSION` and merges into `main`, CI validates the release
and automatically creates an annotated `vX.Y.Z` tag after all checks pass.
Other PRs do not create tags. If tagging fails, rerun the failed CI run.
Published tags are immutable.

## License

MIT. See `LICENSE`.
