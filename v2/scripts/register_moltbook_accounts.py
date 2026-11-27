#!/usr/bin/env python3
"""Create the five Moltbook agent accounts the bridge needs and store their API keys in .env.

Accounts / env variables (see bridge/bridge_service.py):
    bridge        -> MOLTBOOK_API_KEY                 (reads posts, kickoff + consensus comments)
    financeguard  -> MOLTBOOK_API_KEY_FINANCEGUARD    (posts that agent's reply)
    careermax     -> MOLTBOOK_API_KEY_CAREERMAX
    studentlife   -> MOLTBOOK_API_KEY_STUDENTLIFE
    riskanalyst   -> MOLTBOOK_API_KEY_RISKANALYST

What it does:
  * POSTs to https://www.moltbook.com/api/v1/agents/register once per missing account
  * writes each returned api_key straight into the .env file (never overwrites an existing key)
  * prints a masked key plus the claim URL and verification code for each new account

What it CANNOT do: each account must still be CLAIMED BY A HUMAN (you) - open the claim URL,
verify your email, and post the verification tweet. Until then the key will not work.
Check progress any time with:  python scripts/register_moltbook_accounts.py --status

API keys are only ever sent to www.moltbook.com. They are never printed in full.

Usage (from the repo root):
    python scripts/register_moltbook_accounts.py --prefix myteam --dry-run
    python scripts/register_moltbook_accounts.py --prefix myteam
    python scripts/register_moltbook_accounts.py --status
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "https://www.moltbook.com/api/v1"
REPO_ROOT = Path(__file__).resolve().parent.parent

ACCOUNTS = {
    "bridge": ("MOLTBOOK_API_KEY", "Bridge account: reads topics and posts the consensus summary"),
    "financeguard": ("MOLTBOOK_API_KEY_FINANCEGUARD", "FinanceGuard: financial feasibility and budget lens"),
    "careermax": ("MOLTBOOK_API_KEY_CAREERMAX", "CareerMax: career outcomes and skills lens"),
    "studentlife": ("MOLTBOOK_API_KEY_STUDENTLIFE", "StudentLife: campus experience and wellbeing lens"),
    "riskanalyst": ("MOLTBOOK_API_KEY_RISKANALYST", "RiskAnalyst: downside risk and contingency lens"),
}


# --------------------------------------------------------------------------- .env helpers
def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$", line)
            if m:
                env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return env


def set_env(path: Path, key: str, value: str) -> None:
    """Set KEY=value in the .env file, keeping every other line untouched."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, done = [], False
    for line in lines:
        if re.match(rf"^\s*{re.escape(key)}\s*=", line):
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")


def mask(key: str) -> str:
    return key[:10] + "..." + key[-3:] if len(key) > 16 else "***"


# --------------------------------------------------------------------------- HTTP
def api(method: str, path: str, body: dict | None = None, key: str | None = None, timeout: int = 30):
    url = f"{BASE_URL}{path}"
    assert url.startswith("https://www.moltbook.com/"), "refusing to send anything elsewhere"
    headers = {"Content-Type": "application/json", "User-Agent": "ioc-moltbook-poc-setup/1.0"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw[:300]}


# --------------------------------------------------------------------------- commands
def cmd_status(env_path: Path) -> int:
    env = read_env(env_path)
    print(f"Checking claim status from {env_path}\n")
    problems = 0
    for role, (var, _) in ACCOUNTS.items():
        key = env.get(var, "")
        if not key:
            print(f"  {role:13s} {var:32s} no key in .env")
            problems += 1
            continue
        code, data = api("GET", "/agents/status", key=key)
        status = (data or {}).get("status", data) if isinstance(data, dict) else data
        ok = code == 200 and status == "claimed"
        problems += 0 if ok else 1
        print(f"  {role:13s} {var:32s} HTTP {code}  {status}")
    print("\nAll accounts claimed." if not problems else "\nSome accounts are missing or not claimed yet.")
    return 0 if not problems else 1


def cmd_register(args, env_path: Path) -> int:
    env = read_env(env_path)
    todo = [r for r in ACCOUNTS if r in (args.only or ACCOUNTS) and not env.get(ACCOUNTS[r][0])]
    skipped = [r for r in ACCOUNTS if r in (args.only or ACCOUNTS) and env.get(ACCOUNTS[r][0])]
    for r in skipped:
        print(f"  skip {r}: {ACCOUNTS[r][0]} already set in {env_path.name} (never overwritten)")
    if not todo:
        print("Nothing to register.")
        return 0

    names = {r: f"{args.prefix}-{r}" for r in todo}
    print(f"\nThis will create {len(todo)} NEW Moltbook account(s) at {BASE_URL}:")
    for r in todo:
        print(f"  - {names[r]:30s} -> {ACCOUNTS[r][0]}")
    print(f"API keys will be written to {env_path}. You must then claim each account yourself")
    print("(claim URL, email verification, verification tweet).")
    if args.dry_run:
        print("\n[dry-run] no requests were sent.")
        return 0
    if not args.yes and input("\nCreate these accounts? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Aborted.")
        return 1

    claims_file = REPO_ROOT / ".moltbook_claims.txt"
    failures = 0
    for i, role in enumerate(todo):
        var, description = ACCOUNTS[role]
        if i:
            time.sleep(2)
        code, data = api("POST", "/agents/register", {"name": names[role], "description": description})
        agent = (data or {}).get("agent") if isinstance(data, dict) else None
        if code >= 400 or not agent or not agent.get("api_key"):
            failures += 1
            print(f"\n[FAILED] {role} ({names[role]}): HTTP {code}: {json.dumps(data)[:300]}")
            continue
        set_env(env_path, var, agent["api_key"])  # save first so the key can never be lost
        claim = agent.get("claim_url", "?")
        vcode = agent.get("verification_code", "?")
        with claims_file.open("a", encoding="utf-8") as f:
            f.write(f"{names[role]}\n  claim_url: {claim}\n  verification_code: {vcode}\n\n")
        print(f"\n[ok] {role}: account '{names[role]}' created, key saved as {var} ({mask(agent['api_key'])})")
        print(f"     claim URL         : {claim}")
        print(f"     verification code : {vcode}")

    print(f"\nClaim details were also saved to {claims_file.name} (git-ignored).")
    print("NEXT: open each claim URL, verify your email, post the verification tweet.")
    print("Then run:  python scripts/register_moltbook_accounts.py --status")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Create the Moltbook accounts used by the bridge")
    ap.add_argument("--prefix", help="unique name prefix, e.g. 'myteam' -> myteam-bridge, myteam-financeguard ...")
    ap.add_argument("--only", nargs="+", choices=list(ACCOUNTS), help="only these accounts")
    ap.add_argument("--env-file", default=str(REPO_ROOT / ".env"), help="env file to store the keys in")
    ap.add_argument("--status", action="store_true", help="check which accounts are claimed")
    ap.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    ap.add_argument("--dry-run", action="store_true", help="show what would be created, send nothing")
    args = ap.parse_args()
    env_path = Path(args.env_file)

    if args.status:
        return cmd_status(env_path)
    if not args.prefix:
        ap.error("--prefix is required (names must be unique on Moltbook)")
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,30}", args.prefix):
        ap.error("--prefix may only contain letters, digits, '-' and '_' (2-30 chars)")
    return cmd_register(args, env_path)


if __name__ == "__main__":
    sys.exit(main())
