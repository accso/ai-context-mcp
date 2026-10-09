---
apply: always
---

# AI agent workflow discipline

Applies to every session, always. Governs *how* the agent works, not
domain-specific code content (see the other rule files for that).

## 1. Plan before implementing

Before writing or modifying any code, produce a short implementation plan:
what will change, which files, in what order, and where the natural
commit/PR boundaries are. Wait for explicit confirmation before starting
implementation. Do not silently expand scope mid-implementation — if the
plan needs to change, say so and get confirmation before continuing.

## 2. Verify version-specific reality before acting, don't rely on memorized patterns

Training data mixes multiple versions of every library/framework. Before
touching a part of the stack, check the actual state in *this* repo
first — installed version (`package.json`/lockfile), current official
docs/changelog if uncertain — rather than defaulting to the most common
pattern in training data. Specifically check for:

- Known issues/breaking changes in the currently installed version.
- What's deprecated or should no longer be used, and its current
  replacement.
- Where a library/framework already replaces a language-native concept
  (e.g. a state library replacing ad-hoc Context+useState, a
  micro-frontend framework's own primitives replacing generic
  routing/event patterns) — default to the library's own idiom instead of
  the generic pattern trained on most.

If genuinely uncertain about current best practice for something
version-sensitive, say so explicitly rather than presenting a guess with
unwarranted confidence.

## 3. Small, isolated, reviewable increments

Each implementation step from the plan should be its own commit/PR-sized
unit. Do not bundle unrelated cleanup (e.g. "also removed some now
redundant code") into a feature change — propose it separately so it can
be reviewed and rolled back independently.

## 4. Tight verify-and-correct loop

After each change, run the relevant checks (tests/lint/build) before
moving to the next step, rather than batching many changes and checking
only at the end. React to failures immediately — fix and re-verify before
continuing, don't defer failures to a later cleanup pass.

## 5. Periodically propose reusable skills from the session

At natural checkpoints — end of a feature, end of a plan, or when a
workflow/terminal sequence repeats within the session — pause and ask:
"did this session use a workflow, script, or terminal sequence that
recurred or was fiddly enough to be worth turning into a reusable skill
(with a backing script)?" If yes, propose it explicitly rather than
letting the pattern silently repeat next time. Treat this as building a
persistent, versioned library of verified project skills over time, not
one-off note-taking.

## 6. Use minimal, relevant context

Don't pull in or reference more of the repo than the current task
actually needs. Prefer the scoped rule files (file-pattern/model-decision
triggered) over dumping unrelated context — excess irrelevant context
increases the risk of distraction and incorrect suggestions.
