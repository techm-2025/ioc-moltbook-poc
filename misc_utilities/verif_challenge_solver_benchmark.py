#!/usr/bin/env python3
"""
Moltbook Math Challenge Solver Benchmark Harness
Evaluates the success rate and accuracy of the math challenge solver across 100 API verification cycles.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http import HTTPStatus
import json
import os
import random
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3

urllib3.disable_warnings()

# ---------------------------------------------------------------------------
# 1. SOLVER IMPORT & FALLBACK MATRIX
# ---------------------------------------------------------------------------
try:
    from solver import calculate_answer as _ext_calculate_answer
except Exception:
    _ext_calculate_answer = None


def solve_challenge_legacy(text: str) -> float:
    """Fallback regex matrix if solver.py is not present."""
    clean_text = re.sub(r"[^a-zA-Z0-9*+\-/\s]", "", text).lower()
    number_map = {
        "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
        "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
        "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
        "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    }

    found = []
    for m in re.finditer(r"\d+", clean_text):
        found.append((m.start(), int(m.group())))

    temp = clean_text
    for word, val in sorted(number_map.items(), key=lambda x: len(x[0]), reverse=True):
        pattern = r"\b" + r"\s*".join(re.escape(c) for c in word) + r"\b"
        for m in re.finditer(pattern, temp):
            found.append((m.start(), val))
            temp = temp[:m.start()] + " " * (m.end() - m.start()) + temp[m.end():]

    sorted_found = [v for _, v in sorted(found)]
    merged = []
    i = 0
    while i < len(sorted_found):
        curr = sorted_found[i]
        if curr in [20, 30, 40, 50, 60, 70, 80, 90] and i + 1 < len(sorted_found):
            next_val = sorted_found[i + 1]
            if 1 <= next_val <= 9:
                merged.append(curr + next_val)
                i += 2
                continue
        merged.append(curr)
        i += 1

    if not merged:
        return 0.0

    if any(k in clean_text for k in ["/", "divide", "divided", "over", "split"]):
        return float(merged[0] / merged[1]) if len(merged) >= 2 and merged[1] != 0 else float(merged[0])
    elif any(k in clean_text for k in ["*", "times", "multiply", "multiplied"]):
        return float(merged[0] * merged[1]) if len(merged) >= 2 else float(merged[0])
    elif any(k in clean_text for k in ["-", "minus", "subtract", "subtracted", "less"]):
        return float(merged[0] - merged[1]) if len(merged) >= 2 else float(merged[0])
    else:
        return float(sum(merged))


def solve_challenge(text: str) -> Tuple[float, str]:
    """Attempts solver.py first, falling back to legacy regex if needed."""
    if _ext_calculate_answer is not None:
        try:
            ans = _ext_calculate_answer(text)
            if ans is not None:
                return float(ans), "solver.py (OSS)"
        except Exception as exc:
            pass
    return solve_challenge_legacy(text), "Legacy Regex Matrix"


# ---------------------------------------------------------------------------
# 2. MOLTBOOK BENCHMARK CLIENT
# ---------------------------------------------------------------------------
class MoltbookBenchmarkClient:
    RETRYABLE_STATUS = {
        HTTPStatus.TOO_MANY_REQUESTS,
        HTTPStatus.REQUEST_TIMEOUT,
        HTTPStatus.INTERNAL_SERVER_ERROR,
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.SERVICE_UNAVAILABLE,
        HTTPStatus.GATEWAY_TIMEOUT,
    }

    def __init__(self, api_key: str, base_url: str = "https://www.moltbook.com/api/v1"):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def request(
        self, method: str, path: str, payload: Optional[Dict[str, Any]] = None, max_retries: int = 5
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        for attempt in range(max_retries):
            try:
                res = requests.request(
                    method, url, headers=self.headers, json=payload, timeout=30, verify=False
                )
                if res.status_code < 400:
                    return res.json()

                if res.status_code in self.RETRYABLE_STATUS:
                    delay = (2.0 ** attempt) + random.uniform(1.0, 2.5)
                    print(f"   ⚠️ [HTTP {res.status_code}] Rate limited or busy. Retrying in {delay:.2f}s...")
                    time.sleep(delay)
                    continue

                # Return payload for 400 validation responses
                try:
                    return res.json()
                except Exception:
                    raise RuntimeError(f"HTTP {res.status_code}: {res.text}")

            except requests.RequestException as e:
                if attempt == max_retries - 1:
                    raise
                time.sleep(2.0)

        raise RuntimeError(f"Failed {method} {path} after {max_retries} retries.")

    def ensure_anchor_post(self, submolt_name: str) -> str:
        """Finds or creates a dedicated anchor post to attach benchmark comments to."""
        try:
            res = self.request("GET", f"/submolts/{submolt_name}/feed?sort=new&limit=10")
            posts = res.get("posts", [])
            for p in posts:
                if "Benchmark" in p.get("title", ""):
                    return str(p["id"])
                if p.get("id"):
                    return str(p["id"])
        except Exception:
            pass

        # Create new anchor post if none found
        res = self.request(
            "POST",
            "/posts",
            {
                "submolt_name": submolt_name,
                "title": f"Math Solver Benchmark Anchor Post - {int(time.time())}",
                "content": "Automated evaluation anchor post for testing Moltbook verification challenges.",
            },
        )
        post_id = res.get("post", {}).get("id")
        if not post_id:
            raise RuntimeError(f"Failed to resolve anchor post ID: {res}")
        return str(post_id)


# ---------------------------------------------------------------------------
# 3. BENCHMARK RUNNER
# ---------------------------------------------------------------------------
def run_benchmark(
    api_key: str,
    target_count: int = 100,
    submolt_name: str = "secure-dev-logic",
    base_url: str = "https://www.moltbook.com/api/v1",
    delay_between_runs: float = 3.0,
    output_json: str = "benchmark_results.json",
):
    print("=" * 80)
    print("🚀 MOLTBOOK MATH SOLVER BENCHMARK HARNESS")
    print("=" * 80)
    print(f" Target Run Count : {target_count} challenges")
    print(f" Target SubMolt   : {submolt_name}")
    print(f" Base API URL     : {base_url}")
    print(f" Rate-Limit Delay : {delay_between_runs:.2f}s (with adaptive jitter)")
    print(f" Active Solver    : {'solver.py (OSS)' if _ext_calculate_answer else 'Legacy Regex Matrix (Fallback)'}")
    print("=" * 80 + "\n")

    client = MoltbookBenchmarkClient(api_key=api_key, base_url=base_url)

    print("📌 Resolving benchmark anchor post...")
    anchor_post_id = client.ensure_anchor_post(submolt_name)
    print(f"✅ Anchor Post ID set: {anchor_post_id}\n")

    logs: List[Dict[str, Any]] = []
    success_count = 0
    failure_count = 0
    total_solve_time = 0.0
    error_categories: Dict[str, int] = {}

    start_timestamp = datetime.now(timezone.utc).isoformat()

    for i in range(1, target_count + 1):
        print(f"[{i:03d}/{target_count}] Initiating challenge cycle...")
        cycle_start = time.time()

        try:
            # 1. Generate challenge by posting a comment
            comment_res = client.request(
                "POST",
                f"/posts/{anchor_post_id}/comments",
                {"content": f"Benchmark evaluation probe iteration {i}/{target_count}"},
            )

            comment_data = comment_res.get("comment", {})
            verification = comment_data.get("verification") or comment_res.get("verification")

            if not verification or "verification_code" not in verification:
                print(f"   ⚠️ No verification payload returned on iteration {i}. Response: {comment_res}")
                failure_count += 1
                error_categories["missing_verification_payload"] = error_categories.get("missing_verification_payload", 0) + 1
                time.sleep(delay_between_runs)
                continue

            v_code = verification.get("verification_code")
            c_text = verification.get("challenge_text", "")

            # 2. Run solver
            t0 = time.time()
            ans, solver_used = solve_challenge(c_text)
            solve_duration = time.time() - t0
            total_solve_time += solve_duration

            ans_str = f"{ans:.2f}"

            # 3. Submit verification answer
            verify_res = client.request(
                "POST",
                "/verify",
                {"verification_code": v_code, "answer": ans_str},
            )

            is_success = verify_res.get("success") is True or verify_res.get("status") == "verified"
            cycle_duration = time.time() - cycle_start

            record = {
                "iteration": i,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "challenge_text": c_text,
                "calculated_answer": ans_str,
                "solver_engine": solver_used,
                "solve_duration_sec": round(solve_duration, 4),
                "total_duration_sec": round(cycle_duration, 4),
                "success": is_success,
                "response": verify_res,
            }
            logs.append(record)

            if is_success:
                success_count += 1
                print(
                    f"   ✅ SUCCESS | Challenge: '{c_text[:50]}...' => Answer: {ans_str} "
                    f"| Time: {solve_duration*1000:.1f}ms"
                )
            else:
                failure_count += 1
                hint = verify_res.get("hint") or verify_res.get("message") or str(verify_res)
                print(f"   ❌ FAILED  | Challenge: '{c_text[:50]}...' => Answer: {ans_str} | Reason: {hint}")
                error_categories[str(hint)] = error_categories.get(str(hint), 0) + 1

        except Exception as exc:
            failure_count += 1
            err_str = f"Exception: {exc}"
            print(f"   ❌ ERROR   | Iteration {i} crashed: {err_str}")
            error_categories[err_str] = error_categories.get(err_str, 0) + 1
            logs.append({"iteration": i, "success": False, "error": err_str})

        # 4. Adaptive delay between attempts to respect API rate limits
        jittered_delay = delay_between_runs + random.uniform(0.2, 0.8)
        time.sleep(jittered_delay)

    # ---------------------------------------------------------------------------
    # 4. FINAL STATISTICAL REPORT
    # ---------------------------------------------------------------------------
    completed_runs = success_count + failure_count
    success_rate = (success_count / completed_runs * 100) if completed_runs > 0 else 0.0
    avg_solve_ms = (total_solve_time / completed_runs * 1000) if completed_runs > 0 else 0.0

    report = {
        "benchmark_info": {
            "start_time": start_timestamp,
            "end_time": datetime.now(timezone.utc).isoformat(),
            "target_runs": target_count,
            "completed_runs": completed_runs,
            "submolt": submolt_name,
            "primary_solver_active": _ext_calculate_answer is not None,
        },
        "metrics": {
            "successes": success_count,
            "failures": failure_count,
            "success_rate_percent": round(success_rate, 2),
            "avg_solve_time_ms": round(avg_solve_ms, 2),
        },
        "error_breakdown": error_categories,
        "run_logs": logs,
    }

    # Save telemetry JSON
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print("\n" + "=" * 80)
    print("📊 BENCHMARK EXECUTIVE SUMMARY")
    print("=" * 80)
    print(f" Total Runs Executed   : {completed_runs}/{target_count}")
    print(f" Successful Solves     : {success_count}")
    print(f" Failed Solves         : {failure_count}")
    print(f" Overall Success Rate  : {success_rate:.2f}%")
    print(f" Avg Solver Speed      : {avg_solve_ms:.2f} ms / challenge")
    print("-" * 80)
    print(" Error Breakdown:")
    if error_categories:
        for err, count in error_categories.items():
            print(f"   • {err}: {count} occurrences")
    else:
        print("   • None (100% Pass Rate)")
    print("=" * 80)
    print(f"💾 Full telemetry report saved to: {output_json}\n")


# ---------------------------------------------------------------------------
# 5. CLI ENTRYPOINT
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Moltbook Math Solver 100-Run Benchmark Harness")
    parser.add_argument(
        "--api-key",
        default=os.getenv("MOLTBOOK_API_KEY", ""),
        help="Moltbook API Key (defaults to MOLTBOOK_API_KEY env var)",
    )
    parser.add_argument(
        "--runs", type=int, default=100, help="Number of benchmark verification iterations (default: 100)"
    )
    parser.add_argument(
        "--submolt", default="secure-dev-logic", help="SubMolt name to target for benchmark anchor post"
    )
    parser.add_argument(
        "--delay", type=float, default=2.5, help="Delay in seconds between iterations (default: 2.5)"
    )
    parser.add_argument(
        "--output", default="benchmark_results.json", help="Path to save output JSON telemetry"
    )

    args = parser.parse_args()

    if not args.api_key:
        print("❌ Error: MOLTBOOK_API_KEY is required. Provide via --api-key or environment variable.")
        sys.exit(1)

    run_benchmark(
        api_key=args.api_key,
        target_count=args.runs,
        submolt_name=args.submolt,
        delay_between_runs=args.delay,
        output_json=args.output,
    )