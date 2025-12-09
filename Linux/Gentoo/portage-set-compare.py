#!/usr/bin/python3

from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Iterable
from pathlib import Path
import sys

from tabulate import tabulate


def get_paths(argv: list[str]) -> list[tuple[Path, int]]:
    match argv:
        case [_, a, b]:
            source_a = Path(a)
            source_b = Path(b)
        case _:
            raise ValueError("expected 2 folder paths as arguments")

    if not source_a.is_dir():
        raise ValueError(f"{source_a} is not a directory")

    if not source_b.is_dir():
        raise ValueError(f"{source_b} is not a directory")

    paths: list[tuple[Path, int]] = []
    for source, mask in ((source_a, 0b01), (source_b, 0b10)):
        for path_to_file in source.iterdir():
            if path_to_file.is_file(follow_symlinks=False):
                paths.append((path_to_file, mask))

    return paths


def process_file(job: tuple[Path, int]) -> dict[str, int]:
    path, mask = job
    entries: dict[str, int] = {}
    with path.open(encoding="utf-8", errors="replace", mode="rt") as file:
        stream = (xs for yz in file if (xs := yz.split("#", 1)[0].strip()))
        for line in stream:
            entries[line] = mask

    return entries


def merge_dicts(dicts: Iterable[dict[str, int]]) -> dict[str, int]:
    entries: dict[str, int] = {}
    for dic in dicts:
        for line, mask in dic.items():
            entries[line] = entries.get(line, 0) | mask

    return entries


def print_table(entries: dict[str, int]) -> None:
    table = [
        (line, "✔" * (mask & 1), "✔" * ((mask & 2) >> 1))
        for line, mask in sorted(entries.items())
    ]
    print(
        tabulate(
            table,
            headers=["ENTRY", "A", "B"],
            tablefmt="pretty",
            stralign="left",
        )
    )


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv

    paths = get_paths(argv)
    with ThreadPoolExecutor() as executor:
        futures = [executor.submit(process_file, job) for job in paths]
        dicts = (f.result() for f in as_completed(futures))
        merged = merge_dicts(dicts)

    print_table(merged)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, PermissionError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
