"""Diffing and transfer orchestration between two providers."""

from __future__ import annotations

import concurrent.futures
import contextlib
import io
import logging
import queue
import threading
from dataclasses import dataclass
from typing import List

from tqdm.auto import tqdm

from hf_sync.progress import ProgressStream, set_current_position
from hf_sync.providers.base import FileMeta, Provider
from hf_sync.uri import RepoRef

logger = logging.getLogger("hf_sync")


def _confirm(prompt: str) -> bool:
    """Prompt the user for confirmation. Default answer is 'no'."""
    reply = input(f"{prompt} [y/N]: ").strip().lower()
    return reply == "y"


@dataclass
class Plan:
    to_sync: List[FileMeta]
    unchanged: List[FileMeta]
    extra_in_target: List[FileMeta]


def build_plan(src_files: List[FileMeta], dst_files: List[FileMeta], force: bool) -> Plan:
    dst_by_path = {f.path: f for f in dst_files}
    src_paths = {f.path for f in src_files}

    to_sync: List[FileMeta] = []
    unchanged: List[FileMeta] = []

    for f in src_files:
        existing = dst_by_path.get(f.path)
        if force or existing is None:
            to_sync.append(f)
            continue
        if f.sha256 and existing.sha256:
            if f.sha256 != existing.sha256:
                to_sync.append(f)
            else:
                unchanged.append(f)
        elif f.size != existing.size:
            to_sync.append(f)
        else:
            # Best-effort: same path + same size, no hash available on one
            # side. Treated as already-synced to keep incremental syncs cheap.
            unchanged.append(f)

    extra_in_target = [f for f in dst_files if f.path not in src_paths]
    return Plan(to_sync=to_sync, unchanged=unchanged, extra_in_target=extra_in_target)


def run_sync(
    src_ref: RepoRef,
    dst_ref: RepoRef,
    src: Provider,
    dst: Provider,
    repo_type: str,
    revision: str,
    dst_revision: str,
    commit_message: str,
    force: bool = False,
    dry_run: bool = False,
    private: bool = False,
    assume_yes: bool = False,
    delete: bool = False,
    concurrency: int = 5,
) -> Plan:
    logger.info("Listing files on source %s ...", src_ref)
    src_files = src.list_files(src_ref.repo_id, repo_type, revision)
    if not src_files:
        raise RuntimeError(f"Source repo {src_ref} has no files (or does not exist / revision not found).")

    logger.info("Listing files on target %s ...", dst_ref)
    dst_exists = dst.repo_exists(dst_ref.repo_id, repo_type)
    dst_files = dst.list_files(dst_ref.repo_id, repo_type, dst_revision) if dst_exists else []

    plan = build_plan(src_files, dst_files, force=force)

    to_delete = plan.extra_in_target if delete else []

    logger.info(
        "Plan: source has %d file(s) total -- %d already up-to-date on target (skipped), "
        "%d remaining to sync (upload/overwrite); %d extra file(s) exist only on target (%s).",
        len(src_files),
        len(plan.unchanged),
        len(plan.to_sync),
        len(plan.extra_in_target),
        f"will delete {len(to_delete)}" if delete else "not deleted",
    )

    if dry_run:
        for f in plan.to_sync:
            logger.info("[dry-run] would sync: %s (%d bytes)", f.path, f.size)
        for f in to_delete:
            logger.info("[dry-run] would delete: %s", f.path)
        return plan

    if not plan.to_sync and not to_delete:
        logger.info("没有需要同步或删除的文件，退出。")
        return plan

    delete_note = (
        f"，并删除目标仓库中多余的 {len(to_delete)} 个文件（--delete，确保两端完全一致）"
        if delete
        else "（不会删除目标仓库中多余的文件）"
    )
    progress_note = f"源仓库共 {len(src_files)} 个文件，其中 {len(plan.unchanged)} 个已存在于目标且内容一致（跳过）"
    if dst_exists:
        prompt = (
            f"目标仓库 {dst_ref} 已存在，{progress_note}，本次同步将向其中新增/覆盖剩余 "
            f"{len(plan.to_sync)} 个文件{delete_note}。是否继续？"
        )
    else:
        prompt = (
            f"目标仓库 {dst_ref} 不存在，{progress_note}，本次同步将创建该仓库并写入 "
            f"{len(plan.to_sync)} 个文件{delete_note}。是否继续？"
        )
    if not assume_yes:
        if not _confirm(prompt):
            logger.info("用户取消，未执行任何写入操作。")
            return plan
    else:
        logger.info(prompt)

    if not dst_exists:
        logger.info("Target repo %s does not exist, creating it ...", dst_ref)
        dst.ensure_repo(dst_ref.repo_id, repo_type, private=private)

    concurrency = max(1, concurrency)
    total = len(plan.to_sync)
    total_bytes = sum(f.size for f in plan.to_sync)
    logger.info("Syncing %d file(s) (%s total) with up to %d concurrent transfer(s) ...",
                total, f"{total_bytes:,}", concurrency)

    # Install a tqdm lock so that bar redraws from multiple threads don't
    # interleave and corrupt the terminal output. The logging handler
    # (TqdmLoggingHandler, installed in cli.py) routes all log records
    # through tqdm.write(), which also respects this lock -- so log lines
    # from this function *and* from provider code (e.g. ms_provider.py's
    # "Uploading ..." messages) are always printed as clean, complete lines
    # above the active progress bars instead of colliding with them.
    if concurrency > 1:
        tqdm.set_lock(threading.Lock())

    def _sync_one(f: FileMeta, index: int, position: int | None = None) -> None:
        logger.info("[%d/%d] syncing %s (%s bytes) ...", index, total, f.path, f"{f.size:,}")
        stream = src.open_read_stream(src_ref.repo_id, repo_type, revision, f.path)
        progress_stream = ProgressStream(
            stream, total=f.size, desc=f"[{index}/{total}] ↓ {f.path}",
            position=position,
        )
        # ModelScope SDK requires io.BufferedIOBase, but ProgressStream extends
        # io.RawIOBase. Wrap it in BufferedReader to satisfy the type check.
        buffered_stream = io.BufferedReader(progress_stream)
        # Record this worker thread's row position so that provider upload
        # code (e.g. ModelScope's SDK, which creates its own tqdm bar
        # internally for large-file uploads) can reuse the same row instead
        # of defaulting to row 0 and colliding with other concurrent bars.
        set_current_position(position)
        try:
            dst.upload(
                dst_ref.repo_id,
                repo_type,
                dst_revision,
                f.path,
                buffered_stream,
                f.size,
                commit_message,
            )
        finally:
            buffered_stream.close()
        logger.info("[%d/%d] done %s", index, total, f.path)

    # Suppress SDK print statements (e.g. ModelScope's "Committing file to
    # ...") that go to stdout and would interleave with the tqdm progress
    # bar on stderr without a separating newline. This must wrap the whole
    # concurrent section (rather than each individual transfer) since
    # redirect_stdout mutates global process state (sys.stdout) and is not
    # safe to enter/exit concurrently from multiple threads.
    with contextlib.redirect_stdout(io.StringIO()):
        if concurrency == 1:
            for i, f in enumerate(plan.to_sync, start=1):
                _sync_one(f, i)
        else:
            # Each concurrent transfer gets its own tqdm row position
            # (0..concurrency-1). A queue manages the pool of positions so
            # that as one transfer finishes, its row is reused by the next.
            positions: "queue.Queue[int]" = queue.Queue()
            for p in range(concurrency):
                positions.put(p)

            def _run(f: FileMeta, index: int) -> None:
                position = positions.get()
                try:
                    _sync_one(f, index, position)
                finally:
                    positions.put(position)

            errors: List[BaseException] = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {
                    executor.submit(_run, f, i): f for i, f in enumerate(plan.to_sync, start=1)
                }
                for future in concurrent.futures.as_completed(futures):
                    f = futures[future]
                    try:
                        future.result()
                    except Exception as exc:  # noqa: BLE001
                        logger.error("同步文件 %s 失败：%s", f.path, exc)
                        errors.append(exc)

            if errors:
                raise RuntimeError(
                    f"{len(errors)}/{total} 个文件同步失败，详情见上方日志。已成功的文件下次运行会自动跳过。"
                ) from errors[0]

    if to_delete:
        logger.info("Deleting %d extra file(s) from target %s ...", len(to_delete), dst_ref)
        dst.delete_files(
            dst_ref.repo_id,
            repo_type,
            dst_revision,
            [f.path for f in to_delete],
            commit_message,
        )

    logger.info("Done. Synced %d file(s), deleted %d file(s).", len(plan.to_sync), len(to_delete))
    return plan
