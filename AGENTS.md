# AGENTS.md

## Ground rules

- Do not run `git commit` or `git push` unless explicitly asked.
- Follow the toolchain and conventions established in the repository. Do not assume a language or introduce foundational dependencies without agreement.
- Before handing work back, run the relevant formatting, lint, and test checks, or explain why you could not.
- Finish the work you start; ask before dropping scope or leaving TODOs.
- Coordinate major trade-offs with the user; do not unilaterally defer functionality.
- Keep edits ASCII unless the file already uses extended characters.

## Project

Mend is a video cleanup project. The language, toolchain, and architecture are not yet decided. Keep early changes flexible without building speculative abstractions.

## Complexity budget

Prefer simple, direct solutions. Build only what the current task requires. Avoid unnecessary dependencies, configuration, abstractions, and compatibility layers. When two approaches work, choose the one with less code and fewer concepts.

## Documentation

Keep `README.md` focused on user-facing setup and usage. Keep non-obvious rationale near the code it constrains. Do not add design documents, proposals, or ADRs unless explicitly requested.
