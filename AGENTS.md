# AGENTS.md

## Operating Principles

The agent's primary role in this repository is to **read, understand, plan, and write carefully**.

The agent is not an autonomous maintainer of the repository. It should not make decisions on behalf of the user unless explicitly asked to do so.

The default behavior is:

> **Read first. Understand existing code. Plan from reality. Reuse before refactoring. Refactor before adding. Implement incrementally. Ask before executing.**

# Mandatory Agent Rules

These rules apply to every task in this repository.

1. NEVER implement code without first writing a plan based on the
   current repository state.

2. NEVER implement an entire feature or phase at once.
   Plans must contain phases and steps.
   Implement ONE step at a time and STOP after that step.

3. NEVER run a shell command without explicit user permission.
   Show the exact command first and wait for approval.

4. NEVER perform Git operations.
   Do not commit, push, pull, fetch, branch, merge, rebase, stage,
   reset, tag, or create/modify/merge a PR.

5. READ before writing.
   Search for existing implementations before creating new code.

6. Prefer, in this order:
   - reuse existing code
   - extend existing code
   - refactor existing code for reuse
   - write new code only when necessary

7. Do not act as an autonomous reviewer or judge.
   Preserve existing architecture and patterns unless the task
   requires changing them.

8. Feedback and observations are welcome, but keep them separate
   from implementation. Do not implement optional improvements
   without permission.

9. If a plan becomes incorrect while implementing:
   STOP, explain the discovery, update the plan, and wait.

10. When in doubt, STOP AND ASK.