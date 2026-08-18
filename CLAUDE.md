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

### Writing style

Documentation prose follows Simplified Technical English principles:

- One idea per sentence. Keep sentences short (about 20–25 words) and in
  active voice, simple present tense.
- Use the same word for the same thing throughout; no elegant variation.
- Prefer plain declarative sentences over em-dash chains, parenthetical
  asides, and inverted constructions. An em dash is fine for a single
  clarifying aside; never to pack several ideas into one sentence.
- No rhetorical flourishes or marketing language ("the payoff", "for free",
  "strictly better", "that's the whole thing"). State the fact and stop.
- Prefer selectivity over compression: drop what the reader doesn't need
  rather than squeezing everything into dense sentences.
- Guide pages explain meaning and when to use something; exact signatures,
  options, and defaults live in the reference pages.

## AI policy

This project follows the [Open Home Foundation AI Policy](AI_POLICY.md).
Autonomous contributions are not accepted: a human must review, understand,
and be able to explain every change before it is submitted. Do not open
issues or pull requests autonomously, and do not post comments on behalf of
a user without their review.
