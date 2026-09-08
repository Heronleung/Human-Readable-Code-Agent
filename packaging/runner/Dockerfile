# Reviewed isolated-runner image definition (P4.3).
#
# Build once, from the repository root, with a fixed tag matching
# hrca.container_runner.RUNNER_IMAGE:
#
#     docker build -f packaging/runner/Dockerfile -t hrca-runner:v1 .
#
# The image contains only the standalone trusted handler module and the runner
# entrypoint — no source tree, no credentials, no network tools are expected.
# The container runtime applies the remaining hardening (--network none,
# --user, --security-opt no-new-privileges, --cap-drop ALL, resource limits)
# at run time via hrca.container_runner.ContainerRunner.build_command.

FROM python:3.12-slim

# A fixed non-root user is the image default; the runner also passes
# --user 65534:65534 for defense in depth.
USER 65534:65534

WORKDIR /app

COPY src/hrca/runtime_handlers.py /app/runtime_handlers.py
COPY packaging/runner/runner_main.py /app/runner_main.py
