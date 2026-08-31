# Security Policy

Euboulia generates and may execute benchmark commands near expensive inference
systems. Treat command construction, endpoint access, environment handling, and
evidence integrity as security-sensitive behavior.

## Supported versions

Security fixes target the latest published release and the current `main`
branch. Older releases receive best-effort support and may require upgrading.
Until the project publishes stable releases, test reports against the latest
commit when possible.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use the
repository's GitHub **Security** tab and choose **Report a vulnerability** to
open a private security advisory.

Include, when available:

- the affected version or commit;
- operating system, Python version, and framework involved;
- a minimal reproduction without credentials or sensitive prompts;
- expected and actual behavior;
- security impact and the boundary crossed; and
- any known workaround or mitigation.

Do not include API tokens, proprietary model data, production endpoints, or
customer prompts. Maintainers will acknowledge and triage reports on a
best-effort basis, coordinate a fix and disclosure where appropriate, and
credit reporters who wish to be credited. This project does not promise a
specific response or remediation SLA.

## Security boundary

The MVP is a planner, benchmark-client runner, result parser, gate evaluator,
and evidence ledger. Execution requires an explicit `--execute` flag. It does
not edit source or server configuration, apply a candidate patch, start,
restart, kill, deploy, or promote an inference service.

Examples of in-scope security issues include:

- command or argument injection;
- approval or dry-run bypass;
- artifact path traversal, unsafe overwrite, or symlink attacks;
- leakage of environment secrets or prompt content;
- unintended endpoint access or server-side request forgery;
- killing or altering a process Euboulia did not start; and
- tampering that makes recorded evidence differ from the executed experiment.

Framework vulnerabilities that are independent of Euboulia should be reported
to the SGLang or vLLM maintainers. Benchmark variance, disappointing throughput,
or an unsupported tuning claim is not by itself a security vulnerability,
although correctness or integrity failures may be.

## Safe handling

Reproduce against a disposable local endpoint where possible. Do not probe a
third-party or production service without authorization. Give maintainers a
reasonable opportunity to investigate before public disclosure, and coordinate
publication when a fix is ready.
