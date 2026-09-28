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

- `origin` is the private Gitea development remote and the only development source of truth. CI and internal issues run there.
- `github` is a public, read-only mirror. Do not develop or accept divergent commits there.
- Local `main` tracks `origin/main`. Push normal work to `origin`; GitHub synchronization is performed only by the mirror automation after Linux and Windows GPU CI pass for the same SHA.
- Use a Gitea server-side push mirror with `sync_on_commit` disabled. The CI gate requests a mirror sync only after both suites pass; GitHub credentials must remain in Gitea and must not be exposed to runners.
- Public releases use `vX.Y.Z` tags and GitHub Releases. Internal `npm-v*` tags only trigger Verdaccio publication and are not public repository releases.
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
