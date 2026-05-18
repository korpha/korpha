#!/usr/bin/env python3
"""Push the curated first-party skills list to the AIgenteur hub.

Usage::

    FIRST_PARTY_SEED_SECRET=... python tools/seed_firstparty_hub.py

Optional env vars:
  HUB_BASE_URL  default: https://skills.aigenteur.com
  DRY_RUN       set to anything truthy to print the payload + skip POST

The endpoint at /api/v1/seed/first-party is idempotent — re-running
updates existing rows in place. Safe to run on every release.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Make korpha importable when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from korpha.skills._publish_list import PUBLISHABLE_SKILLS


def main() -> int:
    secret = os.getenv("FIRST_PARTY_SEED_SECRET", "").strip()
    if not secret:
        print(
            "ERROR: FIRST_PARTY_SEED_SECRET is required. Set it from "
            "the VPS .env (or wherever the hub is hosted) so the "
            "endpoint accepts the push.",
            file=sys.stderr,
        )
        return 2

    base_url = os.getenv("HUB_BASE_URL", "https://skills.aigenteur.com").rstrip("/")
    url = f"{base_url}/api/v1/seed/first-party"
    body = {"skills": PUBLISHABLE_SKILLS}

    if os.getenv("DRY_RUN"):
        print(f"DRY_RUN: would POST to {url} with payload:")
        print(json.dumps(body, indent=2)[:4000])
        print(f"\n(total {len(PUBLISHABLE_SKILLS)} skills)")
        return 0

    print(f"Pushing {len(PUBLISHABLE_SKILLS)} skills → {url}")
    try:
        r = httpx.post(
            url,
            headers={"X-Seed-Secret": secret, "Content-Type": "application/json"},
            json=body,
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        print(f"NETWORK ERROR: {exc}", file=sys.stderr)
        return 1

    if r.status_code != 200:
        print(f"FAILED [{r.status_code}]: {r.text}", file=sys.stderr)
        return 1

    result = r.json()
    print(f"OK — created={len(result['created'])} updated={len(result['updated'])}")
    if result["created"]:
        print(f"  new: {', '.join(result['created'])}")
    if result["updated"]:
        print(f"  updated: {', '.join(result['updated'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
