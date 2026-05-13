"""Coding-CLI delegation: Claude Code, Codex, OpenCode wrappers.

These are the "implementation muscles" the cofounder uses to write code.
Architecturally distinct from the Inference Pool (which routes the cofounder's
own LLM reasoning calls). Per ARCHITECTURE.md:

| Pool                    | Used for                                    |
|-------------------------|---------------------------------------------|
| Inference Pool          | Agent reasoning (CEO planning, CMO copy)    |
| Coding Delegation Pool  | CTO hands code work to a CLI                |

Auth is whatever the local CLI already has — Mike's Claude Code login,
Mike's ChatGPT for Codex, or any API key the binary picks up from its
own env. Multi-account is possible via per-account Docker containers
when one operator wants parallelism across plans.
"""
from __future__ import annotations

from korpha.delegation.claude_code import ClaudeCodeCLI
from korpha.delegation.codex import CodexCLI
from korpha.delegation.types import (
    DelegationBudgetExceeded,
    DelegationError,
    DelegationRequest,
    DelegationResponse,
    DelegationTimeout,
)

__all__ = [
    "ClaudeCodeCLI",
    "CodexCLI",
    "DelegationBudgetExceeded",
    "DelegationError",
    "DelegationRequest",
    "DelegationResponse",
    "DelegationTimeout",
]
