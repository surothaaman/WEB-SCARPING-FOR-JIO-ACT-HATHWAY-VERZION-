import json
import statistics
import time
from urllib.request import urlopen

API_URL = "http://127.0.0.1:8000/plans"
RUNS = 10


def request_plans():
    start = time.perf_counter()

    with urlopen(API_URL, timeout=30) as response:
        body = response.read()
        status = response.status

    elapsed_ms = (time.perf_counter() - start) * 1000
    data = json.loads(body)

    return status, elapsed_ms, data


def main():
    print("Testing:", API_URL)

    # Report the first request separately.
    status, elapsed_ms, data = request_plans()
    print(f"\nFirst request: {elapsed_ms:.2f} ms")
    print("HTTP status:", status)
    print("Plans returned:", data.get("total"))

    timings = []

    for index in range(1, RUNS + 1):
        status, elapsed_ms, data = request_plans()
        timings.append(elapsed_ms)

        print(
            f"Run {index:02d}: {elapsed_ms:.2f} ms | "
            f"HTTP {status} | Plans: {data.get('total')}"
        )

    print("\nRepeated-request results:")
    print(f"Average: {statistics.mean(timings):.2f} ms")
    print(f"Median:  {statistics.median(timings):.2f} ms")
    print(f"Fastest: {min(timings):.2f} ms")
    print(f"Slowest: {max(timings):.2f} ms")


if __name__ == "__main__":
    main()