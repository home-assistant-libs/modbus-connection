# Instructions for Claude Code

## Documentation

- Every behavior change requires a matching documentation update. When you change
  behavior, arguments, return values, errors, or public API, update the docs in
  the same change: the `README.md` and the relevant pages under
  `docs/src/content/docs/`. A change is not complete until the docs reflect it.
- A bug fix does not. A fix makes the code match what the docs already promise,
  so it lands as source and tests only — no `README.md`, no pages under
  `docs/src/content/docs/`. This holds even where the docs never mentioned the
  broken behavior: do not add a line saying it works now, and do not document the
  limitation instead of fixing it.

## AI policy

This project follows the [Open Home Foundation AI Policy](AI_POLICY.md).
Autonomous contributions are not accepted: a human must review, understand,
and be able to explain every change before it is submitted. Do not open
issues or pull requests autonomously, and do not post comments on behalf of
a user without their review.
