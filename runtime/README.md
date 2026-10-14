# Private runtime

`ac_runtime.py` is the canonical bootstrap used by product launchers. Products
supply their source lock and dependency constraints, and keep generated copies
in sync with this file and its checksum.

## Source selection

Set `AC_INSTALL_SOURCE` to choose how locked sources are installed:

| Mode | Sources |
| --- | --- |
| `git` | Exact Git revisions from the source lock. |
| `local` | Local checkouts for every source; all configured roots are required. |
| `auto` | Local checkouts when all roots are valid; otherwise locked Git sources. |
| `mixed` | Local checkouts for explicitly supplied roots, locked Git sources for the rest. |

Root variables are named in each source's `local_root_env`, typically
`AC_PRODUCT_REPO_ROOT` and `AC_FOUNDATION_REPO_ROOT`. An invalid explicit root
in mixed mode is an error. Local content hashes enter runtime identity, so
source edits select a new environment.

## Optional dependencies

Place exact external package pins, such as `numpy==2.3.5`, in a requirements
file and pass `--requirements <file>` before `setup`, `doctor`, `run` or
`script`. Use the same option on subsequent commands. These dependencies select
a separate runtime and cannot override source-owned packages.

## Installation and recovery

`doctor` reports readiness, selected paths and installation status without
installing dependencies. After correcting a failed installation's cause, use
`setup --retry`. Failed attempts retain their state and logs; command errors
include a short excerpt and the full log path.

Each attempt installs into its own environment. Only a completed attempt with
a matching success marker becomes the active runtime. Repeating setup on a
ready runtime is a no-op. Layout v2 uses a separate directory from v1.

Installation ownership uses POSIX `flock`. Surviving installer subprocesses
retain the lock after their coordinator exits. Retry waits for ownership;
never delete the lock file or infer ownership from PID existence or lock age. Supported systems
are Linux/macOS on filesystems with reliable shared advisory locks.

Explicit `UV_CACHE_DIR` and `PIP_CACHE_DIR` settings take precedence over private
runtime caches. Temporary storage uses the first configured `TMPDIR`, `TEMP`
or `TMP`, otherwise the runtime's private directory. Setup verifies writability
and reports the failing path and OS error.

For a platform approval interruption, retain the platform message and inspect
installation status before retrying. The platform's cancellation does not
establish whether the installer has stopped.
