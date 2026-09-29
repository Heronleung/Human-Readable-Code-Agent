# Reviewed isolated-runner image definition (P4.3).
#
# Build once, from the repository root, with a fixed tag matching
# hrca.container_runner.RUNNER_IMAGE:
#
#     docker build -f packaging/runner/Dockerfile -t hrca-runner:v1 .
#
# The base is pinned by manifest digest, not by tag. A floating tag makes the
# image unreproducible and lets a rebuild silently swap the interpreter out
# from under a pinned runner digest, which would leave the evidence describing
# a base nobody reviewed.
#
# The image contains only the standalone trusted handler module and the runner
# entrypoints — no source tree, no credentials, no network tools are expected.
# The container runtime applies the remaining hardening (--network none,
# --user, --security-opt no-new-privileges, --cap-drop ALL, resource limits)
# at run time via hrca.container_runner.ContainerRunner.build_command.

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

# A fixed non-root user is the image default; the runner also passes
# --user 65534:65534 for defense in depth.
USER 65534:65534

WORKDIR /app

COPY src/hrca/runtime_handlers.py /app/runtime_handlers.py
COPY packaging/runner/runner_main.py /app/runner_main.py
COPY packaging/runner/runner_syntax.py /app/runner_syntax.py
