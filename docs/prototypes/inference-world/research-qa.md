# Local research workbench validation

Validated on 2026-09-07 using the Codex in-app browser against the Lab service.
Interactive checks used `localhost:8773`, keeping test records separate from the
operator's `127.0.0.1:8773` browser storage. No model, source command, or real
experiment was invoked. Existing Lab identities and room data were preserved.

## Automated checks

- Python: `uv run pytest -q` — 411 passed, 11 subtests passed.
- Frontend: `node --test tests/web/*.test.mjs` — 58 passed, including 24 new
  research-state tests.
- `uv run ruff check .` and `uv run mypy src/euboulia` passed.
- JavaScript syntax checks and `uv build` passed.

The state tests exercise immutable nested branch snapshots, discussion ownership,
source validation, measurement provenance, comparison eligibility, zero baselines,
lower-is-better metrics, execution budgets and queue ownership, pause/resume,
interruption without replay, cancellation, memory evidence and verdict history,
per-study experiment numbering, JSON measurement import, backup validation, and
HTML escaping. The Lab static-page regression also checks the new default assets
and preserved historical `team.html` route.

## Browser walkthrough

| Flow | Verified result |
| --- | --- |
| Default entry | Research direction composer and recent studies open without Lab login. |
| Evidence-led discussion | Selecting a P95 bar attaches its experiment to the discussion draft. Saving the discussion and creating a child preserve the reference and hypothesis. |
| Nested branches | A third-level branch created from a source inherits its parent experiment, source, and referenced memory. |
| Bounded demo | A three-round demo can pause and resume. It returns after two rounds when the guardrail is exceeded, retaining its results and discussion draft. |
| Refresh recovery | Reloading during a demo marks R05 interrupted, with no measurement and no automatic replay. The branch remains available for further work. |
| Concurrent tabs | Editing a draft in a second tab makes the older tab stop saving and display an export-and-refresh notice. |
| Search | Searching for a phrase returns matching branches, discussions, and memories; selecting the result opens its branch. |
| Comparison and memory | Same-condition records can create an evidence-linked memory draft. A contested verdict retains its reason and history, and the memory can be referenced by a branch. |
| External reference | A paper URL, date/version, relevance, and applicability can be saved as an unverified clue and used to create a child. No automatic paper retrieval occurs. |
| New study | A custom direction with two resource references and fixed evaluation conditions starts without fabricated measurements. |
| Measurement import | The native file chooser reads a JSON fixture into a reviewable form. Saving it and entering a second branch result produce a same-condition comparison of 200 versus 220 items/s, shown as 10%. Both remain user-entered, unverified records. |
| Escaping | Script-like study titles and HTML-like result summaries display as literal text. |
| Backup | The backup preview exposes complete JSON. Copying it yielded a parseable snapshot with two studies, nine branches, eight experiments, and two memories. A separate seed backup passed count preview and explicit replacement confirmation. |
| Responsive layout | Desktop checks at 1265×714 and 1365×900; mobile checks at 390×844. The mobile document had no horizontal overflow, and the budget dialog fit the viewport. |
| Appearance | Neutral, graphite, cream, and sage themes rendered with legible states and controls. |

No browser console errors were reported during the walkthrough. New experiment
labels were made unique across each study after the comparison check exposed
ambiguous per-branch numbering.

## Known prototype limits

- The workbench does not call a model or execute source code. Demo measurements
  and generated responses use explicit, deterministic examples; free discussion
  saves user input only.
- User-entered artifacts are references, not files read or measurements verified
  by this interface. Research branches are records, not Git checkouts.
- The in-app browser did not confirm a download from the original automatic
  download action. Export now presents a visible download link, full JSON preview,
  and a verified copy fallback; native file-download completion is not asserted.
- State and drafts are local to the browser origin. Backup is explicit, and
  restoring a valid backup replaces that origin's workbench records after
  confirmation.
