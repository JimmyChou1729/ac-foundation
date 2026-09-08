from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "ac_runtime_bootstrap", ROOT / "runtime/ac_runtime.py"
)
assert SPEC is not None and SPEC.loader is not None
RUNTIME = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNTIME
SPEC.loader.exec_module(RUNTIME)


def _lock(commit: str = "a" * 40) -> dict[str, object]:
    return {
        "schema_version": "ac.runtime_sources.v2",
        "profile": "test-product",
        "sources": [
            {
                "id": "foundation",
                "repository": "https://github.com/example/foundation.git",
                "commit": commit,
                "packages": ["ac-jobs"],
                "tools": ["ac-jobs"],
                "local_root_env": "AC_FOUNDATION_REPO_ROOT",
            }
        ],
        "environment_defaults": {},
    }


def test_source_lock_requires_full_sha(tmp_path: Path) -> None:
    path = tmp_path / "runtime-sources.json"
    path.write_text(json.dumps(_lock()), encoding="utf-8")
    lock = RUNTIME.load_lock(path)

    assert lock.profile == "test-product"
    assert lock.tools == ("ac-jobs",)

    path.write_text(json.dumps(_lock("abc123")), encoding="utf-8")
    with pytest.raises(RUNTIME.RuntimeConfigError, match="full Git SHA"):
        RUNTIME.load_lock(path)


def test_source_lock_rejects_duplicate_package_ownership(tmp_path: Path) -> None:
    document = _lock()
    document["sources"].append(
        {
            "id": "product",
            "repository": "https://github.com/example/product.git",
            "commit": "b" * 40,
            "packages": ["ac-jobs"],
            "tools": ["product-tool"],
            "local_root_env": "AC_PRODUCT_REPO_ROOT",
        }
    )
    path = tmp_path / "runtime-sources.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RUNTIME.RuntimeConfigError, match="one owning source"):
        RUNTIME.load_lock(path)


def test_runtime_environment_reports_product_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "runtime-sources.json"
    document = _lock()
    document["environment_defaults"] = {
        "PRODUCT_CACHE": "{cwd}/.product/cache"
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    lock = RUNTIME.load_lock(path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AC_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AC_RUNTIME_HOME", raising=False)
    monkeypatch.delenv("AC_DOCUMENT_CACHE", raising=False)

    environment = RUNTIME._runtime_environment(lock, None)

    assert set(environment) == {
        "AC_HOME",
        "AC_RUNTIME_HOME",
        "AC_DOCUMENT_CACHE",
        "PRODUCT_CACHE",
    }
    assert environment["AC_DOCUMENT_CACHE"] == str(
        tmp_path / ".ac/cache/ac-document"
    )
    assert environment["PRODUCT_CACHE"] == str(tmp_path / ".product/cache")


def test_source_lock_rejects_repository_userinfo(tmp_path: Path) -> None:
    document = _lock()
    document["sources"][0]["repository"] = (  # type: ignore[index]
        "https://secret@example.com/foundation.git"
    )
    path = tmp_path / "runtime-sources.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RUNTIME.RuntimeConfigError, match="HTTPS Git URL"):
        RUNTIME.load_lock(path)


def test_logged_command_failure_does_not_expose_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Failed:
        returncode = 1
        stdout = ""
        stderr = "https://token@example.com/repository.git failed"

    monkeypatch.setattr(RUNTIME.subprocess, "run", lambda *args, **kwargs: Failed())
    log = tmp_path / "install.log"

    with pytest.raises(RuntimeError, match="exit status 1") as raised:
        RUNTIME._run_logged(
            ["git", "https://token@example.com/repository.git"], log
        )

    assert "token" not in str(raised.value)
    assert "token" not in log.read_text(encoding="utf-8")


def test_install_creates_console_scripts_at_their_final_venv_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "runtime-sources.json"
    lock_path.write_text(json.dumps(_lock()), encoding="utf-8")
    lock = RUNTIME.load_lock(lock_path)
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    foundation = tmp_path / "foundation"
    constraints = tmp_path / "missing-constraints.txt"

    def fake_run(command: list[str], _log_path: Path) -> None:
        if command[1:3] == ["-m", "venv"]:
            venv = Path(command[-1])
            (venv / "bin").mkdir(parents=True)
            (venv / "bin/python").write_text("", encoding="utf-8")
            return
        python = Path(command[0])
        (python.parent / "ac-jobs").write_text(
            f"#!{python}\n", encoding="utf-8"
        )

    monkeypatch.setattr(RUNTIME.shutil, "which", lambda _name: None)
    monkeypatch.setattr(RUNTIME, "_run_logged", fake_run)

    RUNTIME._install(
        runtime_dir,
        lock,
        "local",
        {"foundation": foundation},
        constraints,
        "f" * 64,
        {"mode": "local"},
    )

    tool = runtime_dir / "venv/bin/ac-jobs"
    assert tool.read_text(encoding="utf-8") == (
        f"#!{runtime_dir / 'venv/bin/python'}\n"
    )


def test_python_script_command_uses_private_runtime(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    script = tmp_path / "workflow.py"
    script.write_text("print('ok')\n", encoding="utf-8")

    executable, command = RUNTIME._python_script_command(
        runtime, str(script), ["--json"]
    )

    python = runtime / "venv/bin/python"
    assert executable == python
    assert command == [str(python), str(script), "--json"]


def test_python_script_command_rejects_missing_script(tmp_path: Path) -> None:
    with pytest.raises(RUNTIME.RuntimeConfigError, match="does not exist"):
        RUNTIME._python_script_command(
            tmp_path / "runtime", str(tmp_path / "missing.py"), []
        )


def test_symlinked_private_console_scripts_keep_the_literal_venv_path(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "actual-runtime"
    bin_dir = runtime / "venv/bin"
    bin_dir.mkdir(parents=True)
    linked_runtime = tmp_path / "linked-runtime"
    linked_runtime.symlink_to(runtime, target_is_directory=True)
    python = bin_dir / "python"
    python.symlink_to(sys.executable)

    sibling_target = tmp_path / "sibling.py"
    sibling_target.write_text(
        f"#!{linked_runtime / 'venv/bin/python'}\nprint('sibling')\n",
        encoding="utf-8",
    )
    parent_target = tmp_path / "parent.py"
    parent_target.write_text(
        (
            f"#!{linked_runtime / 'venv/bin/python'}\n"
            "import subprocess\n"
            "print(subprocess.check_output(['ac-sibling'], text=True).strip())\n"
        ),
        encoding="utf-8",
    )
    os.chmod(sibling_target, 0o755)
    os.chmod(parent_target, 0o755)
    (bin_dir / "ac-sibling").symlink_to(sibling_target)
    (bin_dir / "ac-parent").symlink_to(parent_target)

    environment = RUNTIME._private_runtime_environment(
        linked_runtime, {"PATH": "/usr/bin"}
    )

    assert environment["PATH"].split(os.pathsep)[0] == str(
        linked_runtime / "venv/bin"
    )
    completed = subprocess.run(
        [str(linked_runtime / "venv/bin/ac-parent")],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == "sibling\n"


def test_private_runtime_uses_scripts_directory_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(RUNTIME.os, "name", "nt")

    assert RUNTIME._venv_bin_dir(runtime) == runtime / "venv/Scripts"
    assert RUNTIME._venv_python(runtime) == runtime / "venv/Scripts/python.exe"
    assert RUNTIME._venv_tool(runtime, "ac-jobs") == (
        runtime / "venv/Scripts/ac-jobs.exe"
    )


def _mixed_lock_and_product(tmp_path):
    document = _lock()
    document['sources'].append({
        'id':'product', 'repository':'https://github.com/example/product.git',
        'commit':'b'*40, 'packages':['product-package'], 'tools':['product-tool'],
        'local_root_env':'AC_PRODUCT_REPO_ROOT',
    })
    path = tmp_path/'runtime-sources.json'
    path.write_text(json.dumps(document))
    product = tmp_path/'product'
    package = product/'packages/product-package'
    package.mkdir(parents=True)
    (package/'pyproject.toml').write_text('[project]\nname="product-package"\nversion="1"\n')
    return path, RUNTIME.load_lock(path), product


def test_mixed_sources_preserve_locked_git_and_hash_local_content(tmp_path, monkeypatch):
    path, lock, product = _mixed_lock_and_product(tmp_path)
    monkeypatch.setenv('AC_INSTALL_SOURCE','mixed')
    monkeypatch.setenv('AC_PRODUCT_REPO_ROOT',str(product))
    monkeypatch.delenv('AC_FOUNDATION_REPO_ROOT',raising=False)
    mode,roots = RUNTIME._source_selection(lock)
    assert mode == 'mixed' and roots == {'product':product}
    assert RUNTIME._requirements(lock,mode,roots) == [
        'ac-jobs @ git+https://github.com/example/foundation.git@'+'a'*40+'#subdirectory=packages/ac-jobs',
        str(product/'packages/product-package'),
    ]
    constraints = tmp_path/'absent-constraints'
    first,identity = RUNTIME._fingerprint(path,lock,mode,roots,constraints)
    assert identity['sources'][0]['mode'] == 'git'
    assert 'root' not in identity['sources'][0] and 'content_sha256' not in identity['sources'][0]
    assert identity['sources'][1]['mode'] == 'local'
    assert identity['sources'][1]['root'] == str(product)
    (product/'packages/product-package/module.py').write_text('value = 1\n')
    second,_ = RUNTIME._fingerprint(path,lock,mode,roots,constraints)
    assert first != second


@pytest.mark.parametrize('bad', ['', 'missing'])
def test_mixed_explicit_invalid_root_fails_instead_of_using_git(tmp_path, monkeypatch, bad):
    _,lock,_ = _mixed_lock_and_product(tmp_path)
    monkeypatch.setenv('AC_INSTALL_SOURCE','mixed')
    monkeypatch.setenv('AC_PRODUCT_REPO_ROOT',str(tmp_path/bad) if bad else '')
    monkeypatch.delenv('AC_FOUNDATION_REPO_ROOT',raising=False)
    with pytest.raises(RUNTIME.RuntimeConfigError, match='AC_PRODUCT_REPO_ROOT'):
        RUNTIME._source_selection(lock)


def test_mixed_empty_roots_are_git_and_environment_handles_partial_roots(tmp_path, monkeypatch):
    path,lock,product = _mixed_lock_and_product(tmp_path)
    monkeypatch.setenv('AC_INSTALL_SOURCE','mixed')
    for name in ('AC_PRODUCT_REPO_ROOT','AC_FOUNDATION_REPO_ROOT','AC_DOCUMENT_CACHE','AC_RUNTIME_HOME'):
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv('AC_HOME',str(tmp_path/'home'))
    monkeypatch.chdir(product)
    mode,roots = RUNTIME._source_selection(lock)
    assert roots == {} and mode == 'mixed'
    assert all('git+' in req for req in RUNTIME._requirements(lock,mode,roots))
    _,identity = RUNTIME._fingerprint(path,lock,mode,roots,tmp_path/'constraints')
    assert all(source['mode'] == 'git' for source in identity['sources'])
    assert RUNTIME._runtime_environment(lock,roots)['AC_DOCUMENT_CACHE'] == str(product/'.ac/cache/ac-document')
    monkeypatch.delenv('AC_DOCUMENT_CACHE')
    assert RUNTIME._runtime_environment(lock,{'product':product})['AC_DOCUMENT_CACHE'] == str(product/'local/cache/ac-document')


@pytest.mark.parametrize('mode',['auto','local','git'])
def test_original_source_modes_keep_complete_root_semantics(tmp_path, monkeypatch, mode):
    path,lock,product = _mixed_lock_and_product(tmp_path)
    monkeypatch.setenv('AC_INSTALL_SOURCE',mode)
    monkeypatch.setenv('AC_PRODUCT_REPO_ROOT',str(product))
    monkeypatch.delenv('AC_FOUNDATION_REPO_ROOT',raising=False)
    if mode == 'local':
        with pytest.raises(RUNTIME.RuntimeConfigError, match='complete roots'):
            RUNTIME._source_selection(lock)
    else:
        assert RUNTIME._source_selection(lock) == ('git',None)
    foundation = tmp_path/'foundation'
    package = foundation/'packages/ac-jobs'
    package.mkdir(parents=True)
    (package/'pyproject.toml').write_text('[project]\nname="ac-jobs"\n')
    monkeypatch.setenv('AC_FOUNDATION_REPO_ROOT',str(foundation))
    selected,roots = RUNTIME._source_selection(lock)
    assert selected == ('git' if mode == 'git' else 'local')
    _,identity = RUNTIME._fingerprint(path,lock,selected,roots,tmp_path/'constraints')
    assert all('mode' not in source for source in identity['sources'])
    if selected == 'local':
        assert set(roots) == {'foundation','product'}
        assert all('git+' not in req for req in RUNTIME._requirements(lock,selected,roots))


def test_mixed_allows_explicit_foundation_development_override(tmp_path, monkeypatch):
    path,lock,product = _mixed_lock_and_product(tmp_path)
    foundation = tmp_path/'foundation'
    (foundation/'packages/ac-jobs').mkdir(parents=True)
    (foundation/'packages/ac-jobs/pyproject.toml').write_text('[project]\nname="ac-jobs"\n')
    monkeypatch.setenv('AC_INSTALL_SOURCE','mixed')
    monkeypatch.setenv('AC_PRODUCT_REPO_ROOT',str(product))
    monkeypatch.setenv('AC_FOUNDATION_REPO_ROOT',str(foundation))
    mode,roots = RUNTIME._source_selection(lock)
    assert mode == 'mixed' and roots == {'foundation':foundation, 'product':product}
    assert RUNTIME._requirements(lock,mode,roots) == [
        str(foundation/'packages/ac-jobs'), str(product/'packages/product-package')]
    _,identity = RUNTIME._fingerprint(path,lock,mode,roots,tmp_path/'constraints')
    assert all(source['mode'] == 'local' and source['content_sha256'] for source in identity['sources'])
