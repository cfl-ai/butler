"""统一文件存储 / 日志存储 / 版本备份 / 项目回溯测试。

计划书依据：三、P0-1「统一文件存储、日志存储、版本备份、项目回溯机制」。
"""

from __future__ import annotations

import pytest

from ai_butler.core.storage import (
    SCOPE_BACKUPS,
    SCOPE_LOGS,
    SCOPE_VAULT,
    SCOPE_WORKSPACE,
    BackupManager,
    FileStore,
    StorageIsolationError,
)


def _store(settings, workdir):
    return FileStore(workdir / "storage", settings=settings)


# ------------------------------ 统一文件存储 ------------------------------ #
def test_put_get_list_delete_roundtrip(settings, workdir):
    store = _store(settings, workdir)
    store.put_text(SCOPE_WORKSPACE, ["a.txt"], "hello")
    assert store.get_bytes(SCOPE_WORKSPACE, ["a.txt"]) == b"hello"
    assert store.exists(SCOPE_WORKSPACE, ["a.txt"])
    assert "a.txt" in store.list_names(SCOPE_WORKSPACE)
    assert store.delete(SCOPE_WORKSPACE, ["a.txt"]) is True


def test_path_traversal_is_refused(settings, workdir):
    """路径穿越、分隔符注入、绝对路径一律拒绝。"""
    store = _store(settings, workdir)
    for bad in ("..", "../escape", "a/b", "a\\b", "", ".", "C:evil"):
        with pytest.raises(StorageIsolationError):
            store.path(SCOPE_WORKSPACE, bad)


def test_vault_scope_requires_user_id(settings, workdir):
    store = _store(settings, workdir)
    with pytest.raises(StorageIsolationError) as ei:
        store.scope_dir(SCOPE_VAULT, user_id=None)
    assert "必须携带 user_id" in str(ei.value)


def test_global_scope_rejects_user_id(settings, workdir):
    """计划书：公共域无用户 ID。"""
    store = _store(settings, workdir)
    with pytest.raises(StorageIsolationError):
        store.scope_dir(SCOPE_WORKSPACE, user_id="u-1")


def test_vault_files_are_not_cross_visible(settings, workdir):
    store = _store(settings, workdir)
    store.put_text(SCOPE_VAULT, ["secret.txt"], "A 的数据", user_id="user-A")
    assert store.exists(SCOPE_VAULT, ["secret.txt"], user_id="user-A")
    assert not store.exists(SCOPE_VAULT, ["secret.txt"], user_id="user-B")


def test_log_storage_path_is_prepared(settings, workdir):
    store = _store(settings, workdir)
    path = store.log_path()
    assert path.parent.is_dir() and path.name == "aibutler.log"
    assert SCOPE_LOGS in str(path)


# --------------------------- 版本备份 + 项目回溯 --------------------------- #
def test_backup_and_restore_roundtrip(settings, workdir):
    store = _store(settings, workdir)
    store.put_text(SCOPE_WORKSPACE, ["page.md"], "v1 内容")
    mgr = BackupManager(store)

    manifest = mgr.create(label="p0", sources=[SCOPE_WORKSPACE])
    assert manifest.total_bytes > 0 and manifest.files
    assert mgr.verify(manifest.version) == []

    # 破坏现场后回溯
    store.put_text(SCOPE_WORKSPACE, ["page.md"], "被改坏了")
    assert store.get_bytes(SCOPE_WORKSPACE, ["page.md"]) == "被改坏了".encode()

    restored = mgr.restore(manifest.version)
    assert restored == 1
    assert store.get_bytes(SCOPE_WORKSPACE, ["page.md"]) == "v1 内容".encode()


def test_backup_versions_are_listed_and_ordered(settings, workdir):
    store = _store(settings, workdir)
    store.put_text(SCOPE_WORKSPACE, ["a.txt"], "1")
    mgr = BackupManager(store)
    first = mgr.create(label="one", sources=[SCOPE_WORKSPACE])
    store.put_text(SCOPE_WORKSPACE, ["b.txt"], "2")
    second = mgr.create(label="two", sources=[SCOPE_WORKSPACE])

    versions = mgr.list_versions()
    assert {v.version for v in versions} == {first.version, second.version}
    assert second.total_bytes >= first.total_bytes


def test_verify_detects_tampered_backup(settings, workdir):
    store = _store(settings, workdir)
    store.put_text(SCOPE_WORKSPACE, ["a.txt"], "原始")
    mgr = BackupManager(store)
    manifest = mgr.create(label="x", sources=[SCOPE_WORKSPACE])

    tampered = (workdir / "storage" / SCOPE_BACKUPS / manifest.version / "tree"
                / SCOPE_WORKSPACE / "a.txt")
    tampered.write_text("被篡改", encoding="utf-8")
    assert any("内容不一致" in p for p in mgr.verify(manifest.version))


def test_backup_of_one_vault_never_includes_another(settings, workdir):
    """跨账号隔离同样适用于备份。"""
    store = _store(settings, workdir)
    store.put_text(SCOPE_VAULT, ["mine.txt"], "A 的私有", user_id="user-A")
    store.put_text(SCOPE_VAULT, ["other.txt"], "B 的私有", user_id="user-B")

    manifest = BackupManager(store).create(label="vaultA", sources=["vault:user-A"])
    names = {f["relpath"] for f in manifest.files}
    assert names == {"mine.txt"}
    assert all("other" not in n for n in names)


def test_restore_unknown_version_is_refused(settings, workdir):
    mgr = BackupManager(_store(settings, workdir))
    with pytest.raises(FileNotFoundError):
        mgr.restore("不存在")
