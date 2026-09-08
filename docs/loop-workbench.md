# Loop workbench · v0.5

A local interaction prototype for iterative work: define a direction, propose a
change, produce an artifact, evaluate it, discuss the evidence, and continue from
any earlier node. Code optimization is one use case; prompts, answers, documents,
designs, and other artifacts use the same record model.

## Branch and preview

The independent development base is `molou/loop-workbench`, initially cut from
`a76b1b7`. This feature lives on `molou/loop-evolution-ui`; its PR targets the new
base. It does not update `molou/inference-world-design` or PR #27.

The default prototype route is `/prototypes/inference-world/`. The previous
research workbench remains at `research.html`, with its original independent
browser storage. Historical room and connector views remain at `team.html`.

## Product model

- **Loop:** terminal objective, artifact type, evaluation conditions, criteria,
  collaboration mode, role names, current accepted node, and version history.
- **Node:** a plan, parent, immutable evaluation contract, role discussions,
  execution snapshot, artifact, evaluation, and human decision. Children preserve
  lineage; updating the Loop contract affects new nodes, not old evidence.
- **Criteria:** numeric metrics, 0–100 human/simulated ratings, or pass/fail/unknown
  checks. Necessary conditions are conjunctive. A higher score cannot compensate
  for a failed necessary check. Required numeric criteria need a threshold.
- **Discussion:** node-scoped or Loop-wide, with an addressed role and explicit
  intent. A proposed steering change remains pending until the user applies it to
  the next iteration or creates another branch. Queued instructions are visible
  and become part of the new execution snapshot exactly once.
- **Knowledge:** external references retain provenance, dates/versions, and
  relevance. Memories retain evidence and applicability; counterexamples remain
  in the verdict history, and cross-condition reuse prompts revalidation.

The main surface combines a selectable evolution tree, status/stage progress,
an artifact/decision inspector, and a persistent discussion panel. The tree has
zoom, fit, current-path emphasis, and native scrolling. Results retain both
accepted and rejected paths. The trend separates provenance and evaluation
contracts rather than pooling incompatible measurements. On narrow screens, the
discussion panel becomes an always-available drawer.

## Two collaboration modes

Single-role loops can continue through several bounded iterations. The
adversarial mode adds proposal, critique by the other champion, rebuttal, and
coordinator selection. A revised plan must repeat the discussion; a new iteration
returns to debate rather than inheriting its parent's selection. Selection only
admits a plan to validation, while accepting its result remains a separate human
decision governed by necessary conditions.

Researcher, champion, executor, monitor, coordinator, and auditor labels express
different responsibilities in the prototype. They do not start real processes or
provide a runtime authorization boundary. Role names can be configured.

## References and interpretation

- [Weco steerability](https://docs.weco.ai/using-weco/steerability) and
  [dashboard conversation](https://docs.weco.ai/using-weco/claude-in-dashboard):
  continue from a selected node with a new instruction and budget; keep the tree
  and conversation available during execution. This prototype implements the
  interaction pattern without integrating Weco.
- [AMMO, Figure 1 and sections 4.1–4.4](https://amazon-science.github.io/ammo/AMMO_arXiv_Paper.pdf):
  separate proposal, implementation, challenge, and acceptance; keep state and
  evidence outside transient discussion. Its champions propose, critique and
  rebut before selection. Its production acceptance contract is domain-specific;
  this prototype adapts the separation into generic necessary criteria rather
  than claiming to implement AMMO's full runtime or empirical results.

## Boundaries

All Agent replies and execution in this version are deterministic simulations,
labelled in the interface. Free text can be saved, answered with contextual
simulation, and turned into an explicit steering action; it is not interpreted
by a language model. No model, shell command, remote machine, or automatic web
retrieval is invoked. Arbitrary artifact text and paths are never executed or
opened. User-entered evaluation is unverified.

Iteration and time budgets, pause/resume, and refresh interruption are functional
prototype state transitions. Only one active exploration per Loop is admitted;
different Loops may simulate concurrently. Refresh interrupts running and paused
jobs, retains queued instructions, and does not automatically replay work.

The stage template is fixed; this is not yet an arbitrary workflow editor.
Research nodes do not create Git branches or worktrees. Production model adapters,
artifact isolation, streaming execution, and independent auditing remain future
integration work.
