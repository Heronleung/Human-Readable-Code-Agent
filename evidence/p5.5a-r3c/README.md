# P5.5a-r3c — a bound runner image under the normal coding-agent baseline

This directory is the bounded evidence for preparing **one** runner image that a
later, separately-authorized task can use to run the already-designed,
network-disabled candidate syntax check.

**Result: `built`, re-pinned, and ready once the pin was updated.** The rebuild
succeeded and produced a record that is honest about the one thing that did not
reproduce: with identical content, the image's *reported* identity moved, and it
moves on every rebuild.

**This is networked setup evidence.** It is not a zero-egress proof, not a proof
of the purpose of any request, and not a candidate-validation success.

| File | What it is |
|---|---|
| `setup-record-9f1925445330e137.json` | the build: one `docker build`, its observations and its identity |
| `setup-record-27d3b7220711c465.json` | a later read-only `inspect`: the same artifact after the re-pin, `ready_for_validation_planning` |

The names are derived from each record's own bytes, so a run that observed
something different writes a second file rather than replacing this one.

## Authority actually exercised

| | |
|---|---|
| Local | repository inspection, three new modules, focused tests, this record, one local commit |
| Docker (anonymous) | `version`, `buildx version`, `buildx ls`, three `buildx imagetools inspect` resolutions of the pinned base per run, two `docker build`, `image inspect`, `ps -a`, `images` |
| Remote | none: no login, no token, no push, no PR, no remote builder, no cache import/export, no Scout, no provenance/SBOM upload, no provider call, no deployment |

The base reference, the Dockerfile path, the runner tag and the pinned digest are
code-owned constants in [`src/hrca/runner_image_policy.py`](../../src/hrca/runner_image_policy.py).
The path that carries them out is
[`src/hrca/runner_image_setup.py`](../../src/hrca/runner_image_setup.py), reached
from the operator CLI
[`src/hrca/runner_image_setup_cli.py`](../../src/hrca/runner_image_setup_cli.py).
No desktop action and no protocol action was added: the desktop cannot prepare,
see or trigger a rebuild.

Reproduce either record with:

```bash
uv run python -m hrca.runner_image_setup_cli build  --evidence-base evidence/p5.5a-r3c
uv run python -m hrca.runner_image_setup_cli inspect --evidence-base evidence/p5.5a-r3c
```

`inspect` is the same path with the build left out. Both write only one record
beneath the evidence base they are given, and nothing else.

## Identities, by digest

Every value below is from the records, which were produced by the runs rather
than typed into this file.

| Identity | Value |
|---|---|
| Base requested | `python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea` |
| Base, as the engine resolved it | `docker.io/library/python:3.12-slim@sha256:78387bc…` — the index digest equals the digest requested, or the setup refuses |
| Platform | `linux/amd64`, from the engine's own report (`Os: linux`, `Arch: amd64`) |
| Platform manifest | `sha256:2fe5997d249a808b8eeea52c58a1dbffbba28754dc11699ef5c029f2d818ce79` — exactly one entry of 16 for this platform |
| Base config | `sha256:ec7d6c95cd3692a2e2d228a8b1ca74e4025b54121fcc4c5da6f09cfa473315ad` |
| Base layers | 4 `diff_ids`, beginning `sha256:411a8667…` |
| Runner image | `hrca-runner:v1` → `sha256:0809a47a00fcce555500b02d6645b68a565ad2a8299416bd9aa02f459ebaf258` |
| Runner digest, before P5.5a-r3c | `sha256:0ae0f7f5c31a4378a03f35c158d7c07989bcd3f1fcc64148e914ef363cbf2c48` |
| Runner layers | 8 `diff_ids`; the first 4 equal the base's, in order — the lineage check |

Client, daemon and builder identities, as the engine reported them: client
`28.4.0`, server `28.4.0` (`Docker Desktop 4.46.0`), buildx
`v0.28.0-desktop.1`, builder `default` (driver `docker`), BuildKit `v0.24.0`.

Tag-only and mismatched input fails closed: a base reference with no digest is
refused as mutable, a digest that is not the pinned one as a mismatch, a
same-named different image by name, and a registry outside the allowed set as an
unexpected destination. Every one of those refusals happens **before any Docker
command is dispatched** — `contact.dispatched_operations` is empty in those
records, which the focused tests assert.

Identity is compared by *identity*, not by spelling: the canonical
`docker.io/library/python:3.12-slim@sha256:…` — the form these records publish
as `requested_canonical` — is the pinned base written the way the engine writes
it, and both the base gate and the Dockerfile gate accept it.

## The build, and what it actually contacted

| Step | Outcome |
|---|---|
| `load build definition` / `load metadata` for the pinned base | `DONE 1.9s` on the first build — the base manifest was **revalidated against the registry**, not answered from a local cache |
| `[1/5] FROM …@sha256:78387bc…` | `resolve … 0.0s done` |
| `[2/5]`–`[5/5]` (`WORKDIR`, three `COPY`) | `CACHED` |
| `exporting to image` | manifest, config and an **attestation manifest** exported; named to `docker.io/library/hrca-runner:v1` |
| context transfer | `#4 transferring context: 18.08kB` — the dockerfile (1.33 kB) and the three files the Dockerfile copies |

**Cache versus acquisition, stated precisely.** No layer was downloaded or
extracted in either run: `build.acquired_steps` is empty and
`build.cache_state` is `cache_hit`. What *was* acquired is metadata — the base
index, its platform manifest and its config, resolved anonymously — and what was
transferred is local build context, counted separately under
`build.context_steps` so that a context transfer can never be read as a registry
pull. A cache hit on layers still required the metadata resolution, which is why
"cached" here is not "offline".

### Contact: what was observed, and what was not

| | |
|---|---|
| Observed (build) | base index manifest resolution; base platform manifest resolution; base image config resolution; base layer **cache lookup** at build time |
| Observed (inspect) | the same three resolutions, and deliberately no layer class: no build ran |
| Host names **the builder itself printed** | `docker.io` — and nothing else; `contact.host_names_observed` says whether any were read at all, so an empty `unexpected_hits` cannot be misread as "nothing was contacted" |
| Unexpected hits | none |
| Declared, **not observed** | the Docker Hub registry manifest API, the token endpoint the official client uses for an anonymous pull token, and the content endpoints it serves manifests and blobs from (including the redirect target named for blob delivery) |

The observed classes are built from what each run actually reached, so a run that
dispatched nothing claims none, and a build that pulled no layer claims a cache
lookup rather than an acquisition.

The declaration is the official client's documented behaviour, not a capture. No
destination, port, path, query string or payload was observed, and this setup
does not claim that a host name in a build log is a destination that was
contacted. What remains unobserved is listed explicitly in the record:
destinations; whether any request carried a token or consulted a credential
helper; which declared endpoint class served which artifact; and whether the
engine also contacted something outside the declared classes.

The reason the gap cannot be closed here is the environment, not the effort: the
`docker` client on this host is a Windows binary and the daemon and BuildKit run
in the Docker Desktop VM, so both sides of the traffic are outside this process's
view. No packet capture, proxy log or firewall log was consulted, and none of
those was reconfigured.

## Credentials: constructed absence, not a promise

Every Docker command runs with `DOCKER_CONFIG` pointing at a fresh empty
directory created for the run, and with every credential-bearing ambient variable
dropped from the child environment.

| | |
|---|---|
| `login_dispatched` | `false` |
| Auth entries available to the run | `0` |
| Credential helper available to the run | `false` |
| Ambient client configuration used | `false` |
| Credential-bearing variables present in the ambient environment | none |
| Variable names dropped unconditionally | all eight (`DOCKER_AUTH_CONFIG`, `DOCKER_USERNAME`, `DOCKER_PASSWORD`, `DOCKER_TOKEN`, `DOCKER_PAT`, `REGISTRY_AUTH_FILE`, `REGISTRY_USERNAME`, `REGISTRY_PASSWORD`) |

The removal is read back from the environment the children actually received, not
asserted from the ambient mapping, so the claim and the behaviour cannot drift
apart; and `isolated_directory_empty_at_creation` is an observation of the
directory this setup created rather than a constant. A run that dispatched
nothing reports `null` for the effective auth state and says why, instead of
reporting a zero it never established.

The ambient client configuration at `~/.docker/config.json` **does** hold one
stored auth entry and **does** configure a credential helper. Both are disclosed
as booleans. Neither the entry's contents nor the helper's name is read, recorded
or invoked, and the isolated run was in no position to use either — a client
cannot read an entry or call a helper that is not in the directory it was told to
use. A platform-specific client configuration outside the path this process can
compute is **not** visible to it; that limit is stated in the record rather than
papered over.

## OnBuild

Both images were inspected, and both are clear.

| Image | `OnBuild` | Verdict |
|---|---|---|
| Base config (from the registry) | absent | clear |
| Runner image (local) | `null` | clear |

An absent or empty value is clear; a non-empty value refuses further readiness
with `the image declares OnBuild triggers`; a value that cannot be read — a
missing `config` section, a section that is not a mapping, or a type that is
neither a string nor a list of strings — refuses with `the image's OnBuild state
could not be read`. A base whose OnBuild is non-empty or unverifiable is refused
**before the build is dispatched**; a runner image whose OnBuild is non-empty is
built, recorded, and reported as not ready.

## Before and after: what changed, and what did not

| Group | Unchanged |
|---|---|
| Git HEAD, working-tree status, `diff HEAD` | yes |
| Candidate roots (`candidate_fixtures/`, content digest) | yes |
| Containers (all, `--no-trunc`) | yes |
| Images other than the runner tag | yes |

The run took the same observation before and after the build and compared them
group by group; `non_mutation.all_unchanged` is `true`. The runner image itself is
excluded from the image comparison by exact tag — an image merely *named like* it
is still compared — because it is the one artifact this task is allowed to
change, and it did change.

What the notes claim is exactly what the code establishes, and no more. Each
named candidate root is **observed by content digest**, with the file count and a
truncation flag recorded beside the digest so a digest over a prefix cannot be
mistaken for a digest of the whole. The build context **is** the repository root,
which is why the builder's own context lines are kept in the record: the
transferred dockerfile and the three copied files are readable there, and the
untracked bundle was not among them. What this record still cannot verify, and
says so, is the content of any untracked file: Git's view of one is its name
alone, and no file is ever read, hashed or staged by this setup. The builder's
own cache is not an image and does not appear in the image list, so a change to
it is outside what these observations cover — another limit the record states
rather than implies.

Container observations deliberately omit the human uptime string `docker ps`
prints: it moves with the wall clock, so comparing it would report a change
nobody made. The record is deterministic for one environment, and says that too.

## The re-pin, and what a rebuild actually reproduces

This is the finding worth reading twice.

Two rebuilds from the same pinned base, with the same Dockerfile, produced:

| | Build 1 | Build 2 |
|---|---|---|
| Platform manifest | `sha256:0ba2c00a…` | `sha256:0ba2c00a…` — identical |
| Image config | `sha256:89b3c9d9…` | `sha256:89b3c9d9…` — identical |
| Layer `diff_ids` | the same 8 | the same 8 — identical |
| `Created`, `Cmd`, `User`, `WorkingDirectory`, `OnBuild` | — | identical |
| **Attestation manifest** | `sha256:45440a2b…` | `sha256:15407aad…` — **differs** |
| **Tag identity** (`docker image inspect .Id`) | `sha256:f6b3752c…` | `sha256:0809a47a…` — **differs** |

The content is reproducible. The identity the runner binds — the index digest
BuildKit names the tag to — is **not**, because the export attaches a fresh
provenance attestation each time. So a rebuild always leaves the pin behind: the
first run here reported `not_ready` with `the recorded runner image digest is not
the pinned digest`, and refused to re-pin itself, because a verdict that silently
rewrites its own input is worthless.

The pin was therefore updated in the four places it lives —
`hrca.container_runner.RUNNER_IMAGE_DIGEST`,
`hrca.validation_policy.CANDIDATE_IMAGE_DIGEST`,
`hrca.runner_image_policy.RUNNER_IMAGE_DIGEST` and the two `image_digest` values
in `fixtures/validation/manifest.json` — and a later read-only `inspect` reports
`ready_for_validation_planning`, which is the second record in this directory.
The record of each build carries both the pin it was checked against and the
identity it produced, so the transition is auditable rather than asserted.

**Not fixed here, and why.** A build that exported a single-platform manifest —
for instance with attestations disabled — would make the tag identity
reproducible, and the earlier P5.5a-r2 image may have been exactly that. Doing it
would change how the accepted runner artifact is produced, which is a change to
the P4.3/P5.5a artifact definition rather than to this setup path, so it is
recorded here as a recommendation for the next task and not done unilaterally.
Until then the rule is the one the fixture note already states: a rebuild
requires a re-pin, and a run bound to a stale pin refuses.

## Corrections made after review

Two things in the first pass of this work were wrong, and both were fixed before
the records here were produced:

* the acquisition classification counted the client's own **context transfer**
  as a registry acquisition, which told a reader that layers were pulled when
  none were. Context transfer, registry acquisition and cache hits are now three
  separate answers, and `cache_state` is computed from layers alone;
* the Dockerfile base check compared the `FROM` line as **text** against the
  caller's spelling, so passing back the canonical reference this record itself
  publishes was refused with the false claim that the reviewed Dockerfile builds
  from something else. It now compares canonical name and digest.

Both are covered by focused tests, and the first record (which predated the fix)
was discarded rather than edited; the two rebuilds disclosed above are what
replaced it.

## Deviations, and one that matters

**A candidate container ran during this session, and this record says so.** A
baseline `uv run python -m unittest discover -s tests` was launched at the start
of the session, before the environment had been inspected, and it reached
`tests/test_candidate_syntax_integration.py` — the accepted P5.5a-r2 live test,
which mounts a fixture candidate in a real container. The daemon's own event log
records it: between `20:03:26` and `20:03:37` on 2026-09-21, five `hrca-run-…`
containers and one `hrca-unrelated-…` container were created from `hrca-runner:v1`
(then the `0ae0f7f5…` identity), two of them started and ran to completion, all of
them destroyed, with three mounts named per run (`/in`, `/out`, and a
`candidate-…/files` directory at `/candidate`).

That was the repository's accepted test, not this task's setup path: the setup
path dispatched no container, mounted nothing and executed nothing. It created no
plan, attempt or result — the test passes no evidence base, so nothing was
persisted — it approved and adopted nothing, and it changed no source. The run
was terminated once identified. **No candidate ran under P5.5a-r3c authority, and
none will**, which is why the two live-container modules were excluded from the
suite run reported below and why the candidate path was not exercised against any
of the identity values above.

Read that as a disclosure of a real gap in this session's discipline: the
repository's documented test command dispatches containers on a host where the
daemon is reachable, and running it "as a baseline" is not a neutral act.

## What was not run, and why

| Not run | Why |
|---|---|
| `tests/test_candidate_syntax_integration.py` | it mounts a candidate in a real container; that is candidate execution, and P5.5b remains unauthorized |
| `tests/test_rule_delta_docker_integration.py` | it executes the package handlers in a real container; also outside this task's authority |
| The candidate path against any identity here | preparing an image is not validating one; the identities are recorded and bound, and nothing about them was exercised |
| A build with attestations disabled | it would change the accepted artifact definition; recorded above as a recommendation instead |

The suite was run with both live-container modules excluded — 2,726 cases
discovered, of which the 576 belonging to the modules this change touches and
their neighbours pass in 27 seconds. Full excluding runs were also attempted
three times and none finished on this host: the first two were stopped when
their own bounded window expired, and the third — a verbose run started only to
identify which module is slow — was stopped by the session harness under memory
pressure. Every one of them was CPU-bound, each reached roughly a third of the
cases, and none reported a failure or an error before it stopped; an unmodified
checkout behaves the same way, so the cost is this host's, not this change's.
So the honest statement about the whole suite here is "unfinished", not
"passing".

## What this record is not

- it is **not** a zero-egress proof: the build reached the official registry
- it is **not** an exact-purpose proof: no destination was captured, and the
  declared endpoint classes do not establish what any request was for
- it is **not** a candidate-validation success: no candidate was mounted or
  executed by this setup, and no plan, attempt or result was created
- it is **not** an approval, an adoption or an application: `approved`,
  `adopted`, `applied`, `source_modified` and `remote_pushed` are all `false` in
  the records, and P5.5b remains unauthorized
