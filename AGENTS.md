# AGENTS.md

## Operating Principles

The agent's primary role in this repository is to **read, understand, plan, and write carefully**.

The agent is not an autonomous maintainer of the repository. It should not make decisions on behalf of the user unless explicitly asked to do so.

The default behavior is:

> **Read first. Understand existing code. Plan from reality. Reuse before refactoring. Refactor before adding. Implement incrementally. Ask before executing.**

# Mandatory Agent Rules

These rules apply to every task in this repository.

1. Check for an exisiting plan and its progress with the user,
   usually the changes, features and steps are planned. They
   are to be executed step by step and the steps must not be 
   something extraordinarily complex, atomic changes if you will.

   If there is no plan, then the plan must be written down
   and agreed with the user. Who is responsible for reviewing it.

2. Dont implement an entire feature or phase at once.
   These contain steps to follow.
   Implement ONE step at a time and STOP after that step.
   If there is no step or feature. Go back to (1.) so that
   it is implemented.

3. Running read commands on the CLI/Shell is allowed.
   Writing commands need permission from the user.

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

7. Preserve existing architecture and patterns unless the task
   requires changing them.

8. Feedback and observations are welcome, but keep them separate
   from implementation. Do not implement optional improvements
   without permission.

9.  TIf a plan becomes incorrect while implementing:
   STOP, explain the discovery, update the plan, and wait.

10. When in doubt, STOP AND ASK.