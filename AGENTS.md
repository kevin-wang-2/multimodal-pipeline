# Repository working agreement

## Product direction

- This is an application-oriented multimodal infrastructure project, not a sequence of open-ended model research projects.
- Keep audio and image development moving in parallel: first complete both automatic triage workflows, then establish a small useful capability group for each modality, and only then deepen complex understanding.
- A research spike ends with evidence for integrate, reject, or defer. Do not treat research notes as an implemented capability.
- Keep scope to the current issue. Do not add compatibility paths, feature flags, or speculative abstractions unless the protocol explicitly requires them.

## Architecture boundaries

- `protocol/schemas/` is the language-neutral contract source. Change schemas before Python or TypeScript implementations and regenerate types.
- A is the Python compute node and owns queues, cache, media fetching, engine processes, and jobs. Engines run out of process.
- B is a stateless coordination role with Python and TypeScript implementations. Do not move durable job or media state into B.
- C is the caller-side TypeScript client or host adapter. It must not depend on whether B is bound or remote.
- `triage.audio` and `triage.image` are automatic task types; explicit capabilities use the same job, media-handle, cache, and registry system.
- Preflight emits task-independent atomic facts, explicit gaps, and available capabilities. It does not guess user intent or silently turn generated descriptions into facts.

## Planning and issues

- `docs/实施计划.md` is the roadmap source. Gitea milestones and issues reflect execution status.
- Milestones are layered: dual-modal triage, basic capability groups, complex understanding, then budget and feedback upgrades.
- One issue must have one independently closable outcome. Split research from implementation and split work that belongs to different milestones.
- Before closing an issue, reread its acceptance checklist and report completed, incomplete, and evidence separately.

## Git and hosting

- `origin` is the private Gitea promotion and deployment authority. Internal development, trusted Linux/GPU validation, deployment, and issues run there.
- `github` is the public contribution intake. External pull requests run secret-free hosted Linux CI, then a maintainer explicitly imports the exact head commit into a Gitea pull request; never merge a GitHub pull request directly.
- Gitea pull requests require Linux unit, Windows GPU smoke, and code-owner approval. A merged `main` is deployed to the Windows node only after both suites pass for the same SHA.
- GitHub `main` is fast-forwarded from Gitea only after Gitea unit, GPU smoke, and deployment all succeed. Divergence is an error; synchronization must never force-push `main`.
- Local `main` tracks `origin/main`. Normal internal work goes through a Gitea pull request; do not push directly to either protected `main`.
- Public releases use `vX.Y.Z` tags and GitHub Releases. Internal `npm-v*` tags trigger Verdaccio publication and may be mirrored as ordinary Git refs, but they do not create GitHub Releases and are not public project versions.
- Do not commit machine names, credentials, tokens, model weights, private media, or local deployment configuration. Machine-specific instructions belong in ignored `AGENTS.md.local`.

## TypeScript packages and publication

- The pnpm workspace is under `ts/`; there is no root npm workspace.
- Published packages are `@mmp/protocol`, `@mmp/broker`, `@mmp/client`, and `@mmp/openclaw`.
- `@mmp/*` installs from the scoped Verdaccio registry configured in `ts/.npmrc`; reads are anonymous and publishing requires the Gitea secret.
- Build and test all workspace packages before publishing. Never retry unchanged package versions as a batch; publish only versions that do not already exist.

## Verification

- Run contract tests on both Python and TypeScript sides after protocol changes.
- Run targeted tests first, then the relevant full suite. Model changes require the Windows GPU smoke workflow; TypeScript and protocol changes require Linux unit CI.
- Preserve explicit degradation behavior. Network or environment failures are not code verdicts, and a completed workflow is not success until its conclusion is checked.
