"""Skills security scanner.

Adapted from Hermes Agent (MIT, Nous Research) — ``hermes/tools/skills_guard.py``.
The threat-pattern set, install-policy matrix, and trust-level shape are
direct ports with attribution. Korpha extends with cofounder-protocol
detection (skills declaring side-effects must surface them honestly).

Trust levels:

  - ``builtin``  — ships with Korpha. Never scanned, always trusted.
  - ``trusted``  — known-good upstream sources (openai/skills, anthropics/skills,
                   nousresearch/hermes-agent, openclaw/clawhub).
                   Caution allowed; dangerous blocked.
  - ``community``— everything else (user submissions, third-party GitHub repos).
                   Any caution-or-higher = blocked unless ``--force``.
  - ``agent-created`` — skills the cofounder authored at runtime. Dangerous
                       findings prompt a re-author cycle, not a hard block.

Install policy matrix (verdict → decision per trust level):

  trust       | safe   | caution | dangerous
  builtin     | allow  | allow   | allow
  trusted     | allow  | allow   | block
  community   | allow  | block   | block
  agent-created| allow | allow   | ask
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Trust + policy
# ---------------------------------------------------------------------------

TRUSTED_REPOS: frozenset[str] = frozenset({
    "openai/skills",
    "anthropics/skills",
    "NousResearch/hermes-agent",
    "openclaw/clawhub",
    "Korpha/korpha",
    "Korpha/skills-hub",
})


# Install decision per (trust_level, verdict). 'ask' means caller must
# prompt the user explicitly before installing.
INSTALL_POLICY: dict[str, tuple[str, str, str]] = {
    #                  safe      caution    dangerous
    "builtin":       ("allow",  "allow",   "allow"),
    "trusted":       ("allow",  "allow",   "block"),
    "community":     ("allow",  "block",   "block"),
    "agent-created": ("allow",  "allow",   "ask"),
}

VERDICT_INDEX: dict[str, int] = {"safe": 0, "caution": 1, "dangerous": 2}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """One pattern hit on one line of one file."""

    pattern_id: str
    severity: str       # "critical" | "high" | "medium" | "low"
    category: str       # "exfiltration" | "injection" | "destructive" | etc.
    file: str
    line: int
    match: str
    description: str


@dataclass
class ScanResult:
    """Aggregated scan output for a skill directory."""

    skill_name: str
    source: str
    trust_level: str    # "builtin" | "trusted" | "community" | "agent-created"
    verdict: str        # "safe" | "caution" | "dangerous"
    findings: list[Finding] = field(default_factory=list)
    scanned_at: str = ""
    summary: str = ""


# ---------------------------------------------------------------------------
# Threat patterns (regex, id, severity, category, description)
# Ported from Hermes skills_guard.py THREAT_PATTERNS — same shape, same
# names. We trim a handful of Hermes-specific rules and add a few
# cofounder-protocol-aware ones.
# ---------------------------------------------------------------------------

THREAT_PATTERNS: list[tuple[str, str, str, str, str]] = [
    # ── Exfiltration: shell commands leaking secrets ──
    (r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)',
     "env_exfil_curl", "critical", "exfiltration",
     "curl command interpolating secret environment variable"),
    (r'wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)',
     "env_exfil_wget", "critical", "exfiltration",
     "wget command interpolating secret environment variable"),
    (r'fetch\s*\([^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|API)',
     "env_exfil_fetch", "critical", "exfiltration",
     "fetch() call interpolating secret environment variable"),
    (r'httpx?\.(get|post|put|patch)\s*\([^\n]*(KEY|TOKEN|SECRET|PASSWORD)',
     "env_exfil_httpx", "critical", "exfiltration",
     "HTTP library call with secret variable"),
    (r'requests\.(get|post|put|patch)\s*\([^\n]*(KEY|TOKEN|SECRET|PASSWORD)',
     "env_exfil_requests", "critical", "exfiltration",
     "requests library call with secret variable"),

    # ── Exfiltration: reading credential stores ──
    (r'base64[^\n]*env',
     "encoded_exfil", "high", "exfiltration",
     "base64 encoding combined with environment access"),
    (r'\$HOME/\.ssh|~/\.ssh',
     "ssh_dir_access", "high", "exfiltration",
     "references user SSH directory"),
    (r'\$HOME/\.aws|~/\.aws',
     "aws_dir_access", "high", "exfiltration",
     "references user AWS credentials directory"),
    (r'\$HOME/\.gnupg|~/\.gnupg',
     "gpg_dir_access", "high", "exfiltration",
     "references user GPG keyring"),
    (r'\$HOME/\.kube|~/\.kube',
     "kube_dir_access", "high", "exfiltration",
     "references Kubernetes config directory"),
    (r'\$HOME/\.docker|~/\.docker',
     "docker_dir_access", "high", "exfiltration",
     "references Docker config (may contain registry creds)"),
    (r'\$HOME/\.korpha/\.env|~/\.korpha/\.env',
     "korpha_env_access", "critical", "exfiltration",
     "directly references Korpha secrets file"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)',
     "read_secrets_file", "critical", "exfiltration",
     "reads known secrets file"),

    # ── Exfiltration: programmatic env access ──
    (r'printenv|env\s*\|',
     "dump_all_env", "high", "exfiltration",
     "dumps all environment variables"),
    (r'os\.environ\b(?!\s*\.get\s*\(\s*["\']PATH)',
     "python_os_environ", "high", "exfiltration",
     "accesses os.environ (potential env dump)"),
    (r'os\.getenv\s*\(\s*[^\)]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)',
     "python_getenv_secret", "critical", "exfiltration",
     "reads secret via os.getenv()"),
    (r'process\.env\[',
     "node_process_env", "high", "exfiltration",
     "accesses process.env (Node.js environment)"),

    # ── Exfiltration: DNS staging + markdown link injection ──
    (r'\b(dig|nslookup|host)\s+[^\n]*\$',
     "dns_exfil", "critical", "exfiltration",
     "DNS lookup with variable interpolation (possible DNS exfiltration)"),
    (r'!\[.*\]\(https?://[^)]*\$\{?',
     "md_image_exfil", "high", "exfiltration",
     "markdown image URL with variable interpolation"),

    # ── Prompt injection ──
    (r'ignore\s+(?:\w+\s+)*(previous|all|above|prior)\s+instructions',
     "prompt_injection_ignore", "critical", "injection",
     "prompt injection: ignore previous instructions"),
    (r'you\s+are\s+(?:\w+\s+)*now\s+',
     "role_hijack", "high", "injection",
     "attempts to override the agent's role"),
    (r'do\s+not\s+(?:\w+\s+)*tell\s+(?:\w+\s+)*the\s+user',
     "deception_hide", "critical", "injection",
     "instructs agent to hide information from user"),
    (r'system\s+prompt\s+override',
     "sys_prompt_override", "critical", "injection",
     "attempts to override the system prompt"),
    (r'pretend\s+(?:\w+\s+)*(you\s+are|to\s+be)\s+',
     "role_pretend", "high", "injection",
     "attempts to make the agent assume a different identity"),
    (r'disregard\s+(?:\w+\s+)*(your|all|any)\s+(?:\w+\s+)*(instructions|rules|guidelines)',
     "disregard_rules", "critical", "injection",
     "instructs agent to disregard its rules"),
    (r'output\s+(?:\w+\s+)*(system|initial)\s+prompt',
     "leak_system_prompt", "high", "injection",
     "attempts to extract the system prompt"),
    (r'<!--[^>]*(?:ignore|override|system|secret|hidden)[^>]*-->',
     "html_comment_injection", "high", "injection",
     "hidden instructions in HTML comments"),

    # ── Destructive operations ──
    (r'rm\s+-rf\s+/',
     "destructive_root_rm", "critical", "destructive",
     "recursive delete from root"),
    (r'rm\s+(-[^\s]*)?r.*\$HOME|\brmdir\s+.*\$HOME',
     "destructive_home_rm", "critical", "destructive",
     "recursive delete targeting home directory"),
    (r'chmod\s+777',
     "insecure_perms", "medium", "destructive",
     "sets world-writable permissions"),
    (r'>\s*/etc/',
     "system_overwrite", "critical", "destructive",
     "overwrites system configuration file"),
    (r'\bmkfs\b',
     "format_filesystem", "critical", "destructive",
     "formats a filesystem"),
    (r'\bdd\s+.*if=.*of=/dev/',
     "disk_overwrite", "critical", "destructive",
     "raw disk write operation"),
    (r'shutil\.rmtree\s*\(\s*[\"\'/]',
     "python_rmtree", "high", "destructive",
     "Python rmtree on absolute or root-relative path"),

    # ── Persistence ──
    (r'\bcrontab\b',
     "persistence_cron", "medium", "persistence",
     "modifies cron jobs"),
    (r'\.(bashrc|zshrc|profile|bash_profile|bash_login|zprofile|zlogin)\b',
     "shell_rc_mod", "medium", "persistence",
     "references shell startup file"),
    (r'authorized_keys',
     "ssh_backdoor", "critical", "persistence",
     "modifies SSH authorized keys"),
    (r'systemd.*\.service|systemctl\s+(enable|start)',
     "systemd_service", "medium", "persistence",
     "references or enables systemd service"),
    (r'/etc/sudoers|visudo',
     "sudoers_mod", "critical", "persistence",
     "modifies sudoers (privilege escalation)"),

    # ── Network: reverse shells / tunnels ──
    (r'\bnc\s+-[lp]|ncat\s+-[lp]|\bsocat\b',
     "reverse_shell", "critical", "network",
     "potential reverse shell listener"),
    (r'\bngrok\b|\blocaltunnel\b|\bserveo\b|\bcloudflared\b',
     "tunnel_service", "high", "network",
     "uses tunneling service for external access"),
    (r'/bin/(ba)?sh\s+-i\s+.*>/dev/tcp/',
     "bash_reverse_shell", "critical", "network",
     "bash interactive reverse shell via /dev/tcp"),
    (r'webhook\.site|requestbin\.com|pipedream\.net|hookbin\.com',
     "exfil_service", "high", "network",
     "references known data exfiltration / webhook testing service"),

    # ── Obfuscation ──
    (r'base64\s+(-d|--decode)\s*\|',
     "base64_decode_pipe", "high", "obfuscation",
     "base64 decodes and pipes to execution"),
    (r'\beval\s*\(\s*["\']',
     "eval_string", "high", "obfuscation",
     "eval() with string argument"),
    (r'\bexec\s*\(\s*["\']',
     "exec_string", "high", "obfuscation",
     "exec() with string argument"),
    (r'echo\s+[^\n]*\|\s*(bash|sh|python|perl|ruby|node)',
     "echo_pipe_exec", "critical", "obfuscation",
     "echo piped to interpreter for execution"),

    # ── Path traversal + system access ──
    (r'\.\./\.\./\.\.',
     "path_traversal_deep", "high", "traversal",
     "deep relative path traversal (3+ levels up)"),
    (r'/etc/passwd|/etc/shadow',
     "system_passwd_access", "critical", "traversal",
     "references system password files"),

    # ── Crypto mining ──
    (r'xmrig|stratum\+tcp|monero|coinhive|cryptonight',
     "crypto_mining", "critical", "mining",
     "cryptocurrency mining reference"),

    # ── Supply chain: curl|sh patterns ──
    (r'curl\s+[^\n]*\|\s*(ba)?sh',
     "curl_pipe_shell", "critical", "supply_chain",
     "curl piped to shell (download-and-execute)"),
    (r'wget\s+[^\n]*-O\s*-\s*\|\s*(ba)?sh',
     "wget_pipe_shell", "critical", "supply_chain",
     "wget piped to shell (download-and-execute)"),
    (r'curl\s+[^\n]*\|\s*python',
     "curl_pipe_python", "critical", "supply_chain",
     "curl piped to Python interpreter"),

    # ── Cofounder Protocol awareness (Korpha-specific additions) ──
    # Skills shouldn't fire side-effects without going through the
    # approval gate. These flag attempts to bypass it.
    (r'korpha\.approvals\.bypass|skip_approval\s*=\s*True',
     "approval_bypass", "critical", "approval_bypass",
     "skill attempts to bypass the approval gate"),
    (r'\bResendEmailNotifier\(\)\.send\(',
     "direct_email_send", "high", "approval_bypass",
     "skill calls Resend directly — should route through outreach.send_cold_email"),
    (r'\bStripeClient\(.*\)\..*payment',
     "direct_stripe_charge", "critical", "approval_bypass",
     "skill calls Stripe directly — should route through commerce.create_payment_link"),
]


# Invisible-character detection — agents have been tricked by
# zero-width unicode characters smuggled into prompts. Scan for them.
INVISIBLE_CHARS: tuple[str, ...] = (
    "​",  # zero-width space
    "‌",  # zero-width non-joiner
    "‍",  # zero-width joiner
    "‎",  # left-to-right mark
    "‏",  # right-to-left mark
    "‪",  # left-to-right embedding
    "‫",  # right-to-left embedding
    "‬",  # pop directional formatting
    "‭",  # left-to-right override
    "‮",  # right-to-left override
    "⁦",  # left-to-right isolate
    "⁧",  # right-to-left isolate
    "⁨",  # first-strong isolate
    "⁩",  # pop directional isolate
    "﻿",  # zero-width no-break space (BOM)
)


# Files we actually scan (skip binaries, archives, etc.)
SCANNABLE_EXTENSIONS: frozenset[str] = frozenset({
    ".md", ".markdown",
    ".py", ".pyi",
    ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs",
    ".sh", ".bash", ".zsh",
    ".yaml", ".yml", ".toml", ".json",
    ".rb", ".pl", ".php",
    ".go", ".rs", ".java", ".kt", ".swift",
    ".html", ".htm", ".xml",
    ".sql",
    ".txt",
})


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def scan_file(file_path: Path, rel_path: str = "") -> list[Finding]:
    """Scan one file. Returns findings (deduplicated per pattern per line)."""
    if not rel_path:
        rel_path = file_path.name

    if file_path.suffix.lower() not in SCANNABLE_EXTENSIONS and file_path.name != "SKILL.md":
        return []

    try:
        content = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    findings: list[Finding] = []
    lines = content.split("\n")
    seen: set[tuple[str, int]] = set()

    # Regex pattern matching
    for pattern, pid, severity, category, description in THREAT_PATTERNS:
        for i, line in enumerate(lines, start=1):
            if (pid, i) in seen:
                continue
            if re.search(pattern, line, re.IGNORECASE):
                seen.add((pid, i))
                matched = line.strip()
                if len(matched) > 120:
                    matched = matched[:117] + "..."
                findings.append(Finding(
                    pattern_id=pid,
                    severity=severity,
                    category=category,
                    file=rel_path,
                    line=i,
                    match=matched,
                    description=description,
                ))

    # Invisible unicode detection
    for i, line in enumerate(lines, start=1):
        for char in INVISIBLE_CHARS:
            if char in line:
                try:
                    char_name = unicodedata.name(char)
                except ValueError:
                    char_name = "UNKNOWN"
                findings.append(Finding(
                    pattern_id="invisible_unicode",
                    severity="high",
                    category="injection",
                    file=rel_path,
                    line=i,
                    match=f"U+{ord(char):04X} ({char_name})",
                    description=(
                        f"invisible unicode character {char_name} "
                        "(possible text hiding / injection)"
                    ),
                ))
                break  # one finding per line

    return findings


def scan_skill(skill_path: Path, source: str = "community") -> ScanResult:
    """Scan an entire skill directory and return verdict + findings."""
    skill_name = skill_path.name
    trust_level = _resolve_trust_level(source)

    all_findings: list[Finding] = []
    if skill_path.is_dir():
        for f in skill_path.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(skill_path))
                all_findings.extend(scan_file(f, rel))
    elif skill_path.is_file():
        all_findings.extend(scan_file(skill_path, skill_path.name))

    verdict = _determine_verdict(all_findings)
    summary = (
        f"{skill_name} ({source}/{trust_level}) → {verdict.upper()} "
        f"with {len(all_findings)} finding(s)"
    )
    return ScanResult(
        skill_name=skill_name,
        source=source,
        trust_level=trust_level,
        verdict=verdict,
        findings=all_findings,
        scanned_at=datetime.now(UTC).isoformat(),
        summary=summary,
    )


def should_allow_install(
    result: ScanResult, *, force: bool = False
) -> tuple[bool | None, str]:
    """Decide install policy from scan result + trust level.

    Returns (decision, reason):
      decision = True  → install
      decision = None  → ask user (interactive confirmation needed)
      decision = False → block (caller may pass ``force=True`` to override)
    """
    policy = INSTALL_POLICY.get(result.trust_level, INSTALL_POLICY["community"])
    vi = VERDICT_INDEX.get(result.verdict, 2)
    decision = policy[vi]

    if decision == "allow":
        return True, f"Allowed ({result.trust_level} source, {result.verdict} verdict)"
    if force:
        return True, (
            f"Force-installed despite {result.verdict} verdict "
            f"({len(result.findings)} findings)"
        )
    if decision == "ask":
        return None, (
            f"Requires confirmation ({result.trust_level} + {result.verdict}, "
            f"{len(result.findings)} findings)"
        )
    return False, (
        f"Blocked ({result.trust_level} + {result.verdict}, "
        f"{len(result.findings)} findings). Pass force=True to override."
    )


def format_scan_report(result: ScanResult) -> str:
    """Multi-line human-readable report — suitable for CLI or dashboard."""
    lines = [
        f"Scan: {result.skill_name}  source={result.source}  "
        f"trust={result.trust_level}  verdict={result.verdict.upper()}"
    ]
    if result.findings:
        sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        sorted_findings = sorted(
            result.findings, key=lambda f: sev_order.get(f.severity, 4)
        )
        for f in sorted_findings:
            sev = f.severity.upper().ljust(8)
            cat = f.category.ljust(14)
            loc = f"{f.file}:{f.line}".ljust(30)
            lines.append(f"  {sev} {cat} {loc} \"{f.match[:60]}\"")
        lines.append("")

    decision, reason = should_allow_install(result)
    status = (
        "ALLOWED" if decision is True
        else "NEEDS CONFIRMATION" if decision is None
        else "BLOCKED"
    )
    lines.append(f"Decision: {status} — {reason}")
    return "\n".join(lines)


def content_hash(skill_path: Path) -> str:
    """SHA-256 of all files (for dedup across re-uploads / forks)."""
    h = hashlib.sha256()
    if skill_path.is_file():
        h.update(skill_path.read_bytes())
        return h.hexdigest()
    if not skill_path.is_dir():
        return h.hexdigest()
    for f in sorted(skill_path.rglob("*")):
        if f.is_file():
            try:
                h.update(str(f.relative_to(skill_path)).encode())
                h.update(f.read_bytes())
            except OSError:
                continue
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_trust_level(source: str) -> str:
    """Map a source identifier to a trust level."""
    if source == "builtin" or source.startswith("builtin:"):
        return "builtin"
    if source == "agent-created":
        return "agent-created"
    # Strip provider prefix (e.g. "github:openai/skills" → "openai/skills")
    bare = source.split(":", 1)[1] if ":" in source else source
    if bare in TRUSTED_REPOS:
        return "trusted"
    # Path-prefix match for nested repos (e.g. "openai/skills/foo")
    for repo in TRUSTED_REPOS:
        if bare.startswith(repo + "/"):
            return "trusted"
    return "community"


def _determine_verdict(findings: list[Finding]) -> str:
    """Reduce findings to safe / caution / dangerous."""
    if not findings:
        return "safe"
    severities = {f.severity for f in findings}
    if "critical" in severities:
        return "dangerous"
    if "high" in severities or "medium" in severities:
        return "caution"
    return "safe"


__all__ = [
    "INSTALL_POLICY",
    "INVISIBLE_CHARS",
    "SCANNABLE_EXTENSIONS",
    "THREAT_PATTERNS",
    "TRUSTED_REPOS",
    "VERDICT_INDEX",
    "Finding",
    "ScanResult",
    "content_hash",
    "format_scan_report",
    "scan_file",
    "scan_skill",
    "should_allow_install",
]
