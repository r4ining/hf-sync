"""Diffing and transfer orchestration between two providers."""

from __future__ import annotations

import contextlib
import io
import logging
from dataclasses import dataclass
from typing import List

from hf_sync.progress import ProgressStream
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

    for i, f in enumerate(plan.to_sync, start=1):
        logger.info("[%d/%d] syncing %s (%s bytes) ...", i, len(plan.to_sync), f.path, f"{f.size:,}")
        stream = src.open_read_stream(src_ref.repo_id, repo_type, revision, f.path)
        progress_stream = ProgressStream(stream, total=f.size, desc=f"[{i}/{len(plan.to_sync)}] ↓ {f.path}")
        # ModelScope SDK requires io.BufferedIOBase, but ProgressStream extends
        # io.RawIOBase. Wrap it in BufferedReader to satisfy the type check.
        buffered_stream = io.BufferedReader(progress_stream)
        try:
            # Suppress SDK print statements (e.g. ModelScope's "Committing
            # file to ...") that go to stdout and would interleave with the
            # tqdm progress bar on stderr without a separating newline.
            with contextlib.redirect_stdout(io.StringIO()):
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
