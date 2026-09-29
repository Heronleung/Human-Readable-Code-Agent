# P5.5a-r3d — Fresh bound runner-image setup: blocked, with a bounded honest result

**Result: `refused`.** The setup could not be performed, because the container
daemon is not running on this host. The refusal is the setup's own, not an
inference: it is recorded in
[`setup-record-d90a87945340fbb3.json`](setup-record-d90a87945340fbb3.json) with
the bounded reason `the container daemon could not be reached`, and **no build
was dispatched**.

**This is not candidate validation, not approval, not adoption, and not a
promotion of any local commit to accepted source.** P5.5a setup remains
unaccepted pending a successful run.

## Preflight, and the blocker

| | |
|---|---|
| Branch / HEAD | `feat/m4.1-offline-developer-memory` / `62f897c` |
| Accepted baseline | `38e6c52` — an ancestor, and still the only accepted Phase 5 baseline. `8ee4371` and `62f897c` are local and unaccepted |
| Worktree | clean except `?? hrca-df8baf7.bundle` — never inspected, hashed, staged, copied or altered |
| `/usr/bin/docker` | a **dangling symlink** → the Docker Desktop CLI mount is absent |
| `/mnt/wsl/docker-desktop` | **absent** (the mount exists only while Docker Desktop runs) |
| `wsl -l -v` | `Ubuntu` Running · **`docker-desktop` Stopped** |
| Windows client binary | present and installed — the client exists, the **backend does not** |

Docker Desktop is stopped. That is the whole blocker: the client is installed,
the engine is not running, and no daemon-side state can be read or written.

**No configuration was changed to work around it.** Starting Docker Desktop,
enabling WSL integration, switching to the Windows client binary, or standing up
a VM are all outside this task's authority, and the last is explicitly optional
higher-assurance infrastructure rather than an MVP prerequisite. The task stops
here and names the unblocking action instead.

## The approved setup suite

```bash
uv run python -m hrca.setup_verification_cli
→ 4 modules, 187 tests, guard armed by hrca.setup_verification, nothing attempted
→ Ran 187 tests in 3.667s — OK — RC 0
```

No discovery, no broad suite, no candidate or container integration module
selected, and the guard's own record shows nothing was attempted. The count rose
from 183 to 187 in this task because four focused tests were added around the
claim correction described below.

## The bounded setup attempt

```bash
uv run python -m hrca.runner_image_setup_cli build --evidence-base evidence/p5.5a-r3d
→ refused: the container daemon could not be reached          RC 2    ~1s
```

What the record establishes, and what it does not:

| Fact | Value |
|---|---|
| `outcome` / `refusal` | `refused` / `the container daemon could not be reached` |
| `build.dispatched` | `false` — **no image build was attempted** |
| `base` | `null` — the base was never resolved, so no base, platform-manifest, config or layer identity exists for this run |
| `contact.dispatched_operations` | `images`, `ps`, `version` — the three read-only client calls it tried, and nothing else. No `buildx imagetools inspect`, no `build` |
| `contact.observed_classes` | `[]` — no contact class is claimed |
| `contact.host_names_observed` | `false` — no host name was read from anywhere |
| `contact.unexpected_hits` | `[]`, **and flagged as unread** — an empty list here means "no output was read", never "nothing was contacted" |
| `readiness` | `not_ready`, same bounded reason |
| Images / containers | `non_mutation.readable: false` — **unavailable, not zero**. With the daemon stopped these counts cannot be read, and the record says so rather than reporting an empty set |

**Identities that could not be established in this run**, stated plainly: base
index, platform manifest, base config, base `OnBuild`, and the local runner
image digest. The code-owned pins are unchanged from the P5.5a-r3c run
([`../p5.5a-r3c/`](../p5.5a-r3c/)), and their agreement with whatever image is
now in the local store is **unverified** — the store could not be read at all.

## OnBuild

**Not inspected, because nothing was reachable to inspect.** The gate is not
skipped: readiness is refused for a more fundamental reason (no daemon), and the
policy's OnBuild rule — non-empty, unreadable *or unverifiable* refuses
readiness — is consistent with that refusal. A run in which the base resolves
but `OnBuild` cannot be read is a separate, already-implemented refusal path
covered by the focused tests; it simply was not the path this attempt took.

## Credentials

| | |
|---|---|
| `login_dispatched` | `false` |
| Auth entries available to the run | `0` |
| Credential helper available to the run | `false` |
| Ambient client configuration used | `false` |

Every command the attempt made ran with `DOCKER_CONFIG` pointed at a fresh empty
directory and every credential-bearing ambient variable dropped. No login, no
token, no private registry and no credential value was read, and the setup never
reached a registry because it never got past the client identity check.

## Network: what this record claims, and what it cannot

**Destination-level observation of the Docker Desktop / daemon / BuildKit path is
unavailable on this environment.** That is the accepted P5.5r3c2 gate result
(`network_observation_unavailable`), and it is unchanged by this task.

This record therefore makes **no** destination claim of any kind: no egress
statement, no endpoint list, no unexpected-destination set, no exact-purpose
proof, no daemon-network claim. In this run there was additionally nothing to
observe — the daemon was down and no registry operation was dispatched.

**A correction made in this task, and why.** The first record this task produced
carried `claim.networked_setup_evidence: true` for a run that reached nothing —
the claim was a fixed constant describing the artifact's *category*. That is a
stronger claim than the run supports, so it is now computed from what the run
actually dispatched: the setup's only registry-reaching operations are a
resolution and a build, and the claim is true only if one of them was
dispatched. The pre-fix record was discarded rather than edited (its filename
covered a different byte sequence, so nothing was overwritten), and four focused
tests cover the converted claim: a refused base, an unreachable daemon, an
`inspect` that did resolve the base, and a build.

## Before / after protected state

| | Before | After |
|---|---|---|
| Git HEAD | `62f897c` | `62f897c` |
| `git status` | `?? hrca-df8baf7.bundle` | the same, plus this task's own four source/test edits and this evidence directory |
| `candidate_fixtures/` digest | `9e98e246c5b72b819f3e593ec5545592…` | **identical** |
| Containers / images | not readable (daemon stopped) | not readable (daemon stopped) — unchanged in the only sense verifiable here |
| Validation stores | none created | none created |
| Bundle | untouched | untouched |

No container was created, started, mounted or executed. No image was built,
pulled, tagged or removed. No candidate root was created, mounted or read beyond
its content digest. No plan, attempt or validation result was created. No
credential was used. No push, PR, remote builder, cache service or provider call
was made.

## Remaining limitations, and the unblocking action

1. **The rebuild is blocked until the container daemon is running.** The
   operator's action is to start Docker Desktop (and, if it is not already
   configured, the WSL integration for this distro) — a configuration change this
   task is not authorized to make.
2. **Destination-level network observation remains unavailable**, so a future
   successful setup will again be evidence of the mainstream coding-agent kind:
   bounded, anonymous, digest-bound — and explicitly not a destination-complete
   or zero-egress proof.
3. **The runner image digest is expected to move on every rebuild** (P5.5a-r3c
   established that BuildKit's attestation manifest varies while the platform
   manifest and config do not), so a successful rebuild will require a re-pin.
   Making that identity reproducible — for instance by exporting a
   single-platform manifest with attestations disabled — remains a decision for
   the reviewer, because it changes how the accepted artifact is produced.
4. **One local commit accompanies this evidence** (the claim correction and its
   tests). It does not promote `8ee4371` or `62f897c` to accepted source, and it
   does not make P5.5a accepted.

**P5.5a setup/rebuild, candidate validation and P5.5b remain unauthorized, and
adoption remains unauthorized.** No rebuild is authorized under current evidence:
the environment cannot currently support one, and the acceptance criteria it
would have to satisfy include a bound runner image that does not yet exist.
