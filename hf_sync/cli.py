"""hf-sync command-line interface."""

from __future__ import annotations

import argparse
import logging
import sys

from tqdm.auto import tqdm

from hf_sync.providers import get_provider
from hf_sync.sync import run_sync
from hf_sync.uri import parse_repo_ref


class TqdmLoggingHandler(logging.Handler):
    """A logging handler that routes records through ``tqdm.write()``.

    Plain ``logging.StreamHandler`` writes directly to the stream, which can
    collide with an active tqdm progress bar's carriage-return redraw and
    corrupt the terminal (missing newlines, log text mixed into the bar).
    ``tqdm.write()`` coordinates with tqdm's internal lock and always clears
    active bars before writing a full line, then redraws them -- this makes
    it safe to use from any thread, including provider code that isn't aware
    of the progress bars at all.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            tqdm.write(msg, file=sys.stderr)
        except Exception:
            self.handleError(record)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hf-sync",
        description="Sync models/datasets directly between Hugging Face Hub and ModelScope.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_p = subparsers.add_parser(
        "sync",
        help="Sync a repo from source to target, e.g. 'hf-sync sync hf:<repo> ms:<repo>'",
    )
    sync_p.add_argument("source", help="Source repo ref, e.g. hf:<namespace>/<repo> or ms:<namespace>/<repo>")
    sync_p.add_argument("target", help="Target repo ref, e.g. hf:<namespace>/<repo> or ms:<namespace>/<repo>")
    sync_p.add_argument(
        "--repo-type",
        choices=["model", "dataset"],
        default="model",
        help="Type of repo to sync. Default: model",
    )
    sync_p.add_argument("--hf-token", default=None, help="Hugging Face access token")
    sync_p.add_argument("--ms-token", default=None, help="ModelScope access token (SDK token)")
    sync_p.add_argument("--revision", default=None, help="Source revision/branch (default: main for hf, master for ms)")
    sync_p.add_argument(
        "--dst-revision",
        default=None,
        help="Target revision/branch (default: same convention as target platform)",
    )
    sync_p.add_argument(
        "--commit-message",
        default="Sync via hf-sync",
        help="Commit message to use on the target repo",
    )
    sync_p.add_argument("--force", action="store_true", help="Re-upload all files, ignoring incremental diff")
    sync_p.add_argument(
        "--delete",
        action="store_true",
        help="Delete files present in the target but not in the source, to make the target an exact mirror",
    )
    sync_p.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=5,
        help="Number of files to transfer concurrently (parallel download+upload pipelines). Default: 5",
    )
    sync_p.add_argument("--dry-run", action="store_true", help="Only print what would be synced")
    sync_p.add_argument("--private", action="store_true", help="Create the target repo as private if it needs creating")
    sync_p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Assume 'yes' to the overwrite/create confirmation prompt (non-interactive)",
    )
    sync_p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")

    return parser


def _default_revision(platform: str) -> str:
    return "main" if platform == "hf" else "master"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    handler = TqdmLoggingHandler()
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        handlers=[handler],
    )

    if args.command == "sync":
        src_ref = parse_repo_ref(args.source)
        dst_ref = parse_repo_ref(args.target)

        tokens = {"hf": args.hf_token, "ms": args.ms_token}
        src_provider = get_provider(src_ref.platform, tokens[src_ref.platform])
        dst_provider = get_provider(dst_ref.platform, tokens[dst_ref.platform])

        revision = args.revision or _default_revision(src_ref.platform)
        dst_revision = args.dst_revision or _default_revision(dst_ref.platform)

        try:
            run_sync(
                src_ref=src_ref,
                dst_ref=dst_ref,
                src=src_provider,
                dst=dst_provider,
                repo_type=args.repo_type,
                revision=revision,
                dst_revision=dst_revision,
                commit_message=args.commit_message,
                force=args.force,
                dry_run=args.dry_run,
                private=args.private,
                assume_yes=args.yes,
                delete=args.delete,
                concurrency=args.concurrency,
            )
        except Exception as exc:  # surface a clean error instead of a traceback
            logging.error("hf-sync failed: %s", exc)
            return 1
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
