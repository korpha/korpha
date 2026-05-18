"""Post-write verification — file-mutation tracking + semantic
diagnostics that run AFTER a write tool lands.

Closes the agent-hallucination loop where Claude/GPT/Grok claim
"I added the function" but the diff didn't apply, or the file
parsed but the type checker would have caught a typo.

Two layers, ship-together:

  1. ``FileMutationTracker`` — context manager wrapped around an
     agent turn; observes every Write/Edit/NotebookEdit call and
     emits a footer enumerating files touched + line delta. Cheap,
     no external deps. Catches "did the write actually land".

  2. ``run_lsp_diagnostics`` — invokes language-server-equivalent
     CLIs (pyright for Python, tsserver-shim via tsc for TypeScript,
     yamllint for YAML) on each written file and surfaces semantic
     errors the syntax-only delta_lint misses. Skips silently when
     the tool isn't installed — degrades to syntax-only.

The footer + diagnostics get appended to the agent's turn output as
a system-level message. The director loop sees them and can self-
correct on the next iteration.

Mirrors Hermes PRs #24168 (LSP) + #24498 (mutation footer).
"""
from korpha.post_write.diagnostics import (
    DiagnosticIssue,
    DiagnosticResult,
    run_lsp_diagnostics,
)
from korpha.post_write.mutation_tracker import (
    FileMutation,
    FileMutationTracker,
    render_mutation_footer,
)

__all__ = [
    "DiagnosticIssue",
    "DiagnosticResult",
    "FileMutation",
    "FileMutationTracker",
    "render_mutation_footer",
    "run_lsp_diagnostics",
]
