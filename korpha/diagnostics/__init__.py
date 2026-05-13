"""Operator-facing diagnostics: ``doctor`` checks + log persistence.

Two pieces of the support story:

  - :mod:`korpha.diagnostics.doctor` — runs config/DB/provider/
    plugin/security probes and emits a checklist. Mike installs
    Korpha, hits something weird, runs ``korpha doctor`` —
    that's the first-line tool.

  - :mod:`korpha.diagnostics.logs` — installs a JSONL file
    handler at ``~/.korpha/logs/korpha.log`` (rotating size
    cap) so ``korpha logs`` has something to tail. Stderr stays
    on too — operators running ``korpha server`` foreground
    keep seeing live output.
"""
from korpha.diagnostics.doctor import (
    Check,
    CheckResult,
    DoctorReport,
    run_doctor,
)
from korpha.diagnostics.logs import (
    DEFAULT_LOG_PATH,
    install_file_handler,
    iter_log_records,
    tail_log,
)

__all__ = [
    "Check",
    "CheckResult",
    "DEFAULT_LOG_PATH",
    "DoctorReport",
    "install_file_handler",
    "iter_log_records",
    "run_doctor",
    "tail_log",
]
