"""Run the three scrapers concurrently. Use --workers 1 for sequential fallback."""

import argparse
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent
SCRAPERS = [
    ("Jio", "scrape_jio_browserbase.py"),
    ("Hathway", "scrape_hathway_browserbase.py"),
    ("ACT", "scrape_act_browserbase.py"),
]


def run(provider, filename, output_dir, full=False):
    started = time.perf_counter()
    path = BASE_DIR / filename
    log_path = output_dir / (provider.lower() + ".log")
    if not path.is_file():
        return provider, 1, 0, "Missing script: " + filename
    print(f"Starting {provider}…", flush=True)
    try:
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, "-u", str(path)]
                + (["--all-tabs"] if full and provider == "Jio" else []),
                cwd=BASE_DIR,
                stdout=log,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
            )
            try:
                code = process.wait(timeout=240)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                code = 124
        return provider, code, time.perf_counter() - started, str(log_path)
    except OSError as e:
        return provider, 1, time.perf_counter() - started, str(e)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, choices=[1, 2, 3], default=3)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Explore recognized Jio plan tabs; slower and requires compatible page selectors",
    )
    args = parser.parse_args()
    # Prevent two runner processes from overwriting one provider's snapshot concurrently.
    import os

    lock = BASE_DIR / ".scrapers-running.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        print(
            "A run is already active. If an earlier run crashed, remove .scrapers-running.lock after confirming it stopped."
        )
        return 1
    os.close(fd)
    try:
        started = time.perf_counter()
        output_dir = (
            BASE_DIR / "logs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        )
        output_dir.mkdir(parents=True)
        results = []
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [
                executor.submit(run, provider, filename, output_dir, args.full)
                for provider, filename in SCRAPERS
            ]
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                print(
                    f'{result[0]}: {"SUCCESS" if result[1] == 0 else "FAILED"} | {result[2]:.2f}s | {result[3]}',
                    flush=True,
                )
        print(f"Total wall time: {time.perf_counter() - started:.2f}s")
        return 0 if all(r[1] == 0 for r in results) else 1
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
