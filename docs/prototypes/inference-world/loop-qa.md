# Loop workbench v0.5 acceptance record

Validated on 2026-09-08 using the local static preview in the Codex in-app browser.
Interactive test data used the `localhost` origin, separate from the user's
`127.0.0.1` preview data. Agent replies and execution were explicitly simulated.

## Browser checks

| Flow                        | Observed result                                                                                                                                                                                                                   |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Entry and lineage           | Default route opens without Lab authentication. The answer example shows a baseline, accepted branch, rejected higher-scoring branch and nested proposals. Selecting nodes changes the inspector and discussion context.          |
| Live steering               | Started the code example, paused it, submitted a next-round instruction, explicitly queued it and resumed. The second node contains that instruction in its plan, directives and execution snapshot; the first node is unchanged. |
| Discussion during execution | A monitor-directed challenge was accepted while execution continued. The second round failed a necessary condition and returned to discussion; the accepted baseline was not automatically replaced.                              |
| Adversarial proposal        | Entered a concrete critique and rebuttal on the answer example, then selected validation. Both roles' text remains in the discussion and the candidate becomes ready.                                                             |
| Draft persistence           | Entered an unsent node discussion draft on mobile, reloaded, and reopened discussion. The exact draft remained. Escape closes the mobile discussion drawer.                                                                       |
| Non-code task               | Created a document Loop with a rubric and required completeness check, without a repository or directory. It began with no fabricated evaluation.                                                                                 |
| Evaluation import           | Native JSON chooser filled the review form; saving showed the rating, checklist and `user-entered / unverified` provenance. Literal HTML in artifact text remained text, with no image element or script dialog.                  |
| Backup                      | Export contained three Loops and nine nodes from the exercise. Clipboard copy parsed as JSON. Import preview showed the seed backup's two Loops, seven nodes and nineteen messages; explicit confirmation restored it.            |
| Responsive layout           | At 390 × 844, document scroll width equalled client width. The evolution graph scrolls within its own viewport, and discussion opens as a separate drawer with a visible composer. Temporary viewport override was reset.         |
| Visual hierarchy            | Inspected the warm-green and graphite appearances. The discussion composer remains visible at desktop height after adjusting panel height; status, selected path, evidence and primary actions remain distinct.                   |
| Runtime errors              | No browser console errors were recorded during these checks.                                                                                                                                                                      |

## Automated checks

- `node --test tests/web/*.test.mjs`: 81 passing tests, including 23 new Loop
  state tests for generic contracts, nested branches, debate sequencing, human
  acceptance, queued steering, budgets, interruptions, provenance, knowledge,
  layout and backup validation.
- `uv run pytest -q`: 411 passing tests and 11 passing subtests, including static
  serving of both the new entry and retained prototype assets.
- Ruff and mypy pass; JavaScript syntax checks pass. Wheel build includes the
  new Loop assets and the previous research entry.

This validates the local interaction prototype. It does not validate a real model,
experiment process, arbitrary workflow executor, remote connection, web retrieval
or independent runtime auditor.
