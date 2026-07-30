# AGENTS.md

## Ground rules

- Follow the toolchain and conventions established in the repository. Do not assume a language or introduce foundational dependencies without agreement.
- Before handing work back, run the relevant formatting, lint, and test checks, or explain why you could not.
- Finish the work you start; ask before dropping scope or leaving TODOs.
- Coordinate major trade-offs with the user; do not unilaterally defer functionality.
- Keep edits ASCII unless the file already uses extended characters.

## Project

Mend is a video cleanup project. The language, toolchain, and architecture are not yet decided. Keep early changes flexible without building speculative abstractions.

## Complexity budget

YAGNI and KISS: build only what the current task requires; when two approaches work, take the simpler one.

Production LOC should be flat or negative; tests may grow freely. Before any fix, identify the invariant that makes the bug impossible and what existing code becomes redundant if it is enforced. Prefer deletion and stronger invariants over additive patches. Do not add dependencies, modules, public APIs, configuration flags, workers, caches, or abstraction layers unless they clearly reduce total complexity. Avoid helper sprawl: do not extract single-use helpers unless they represent a real domain concept. Do not add configuration to avoid making a design decision. For non-trivial work, report the production LOC delta, new public surface, and what was removed or simplified.

## Documentation

Keep `README.md` focused on user-facing setup and usage. Keep non-obvious rationale near the code it constrains. Do not add design documents, proposals, or ADRs unless explicitly requested.
