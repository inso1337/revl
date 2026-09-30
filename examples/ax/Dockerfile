# The task image for examples/ax: AX's default runner image, plus revl and the
# cordis-py runtime that `revl mcp serve` boots compositions on.
#
#   docker build --build-arg REVL_REF=<revl commit sha> \
#       -t <your registry>/revl-ax-task examples/ax
#
# Push it where your cluster can pull from, then put the pushed digest in
# ax.yaml's Task.spec.image. Read "Licensing" in README.md before you publish
# the image: it contains AGPL-3.0-only code.

# The default runner image that google/ax pins in examples/task.yaml at commit
# 9edb7b1b0ca55f71670da02674b5cd6adeb49b7e. It keeps /usr/local/bin/ax-task-runner
# as the entrypoint, which the AX controller always runs, and ships Python 3.12.
FROM gcr.io/ax-substrate/ate-images/ax-task-runner@sha256:3a0dea6ad8b55278685db58aca6e37dc4ba04056831d45bef3aaeafdca43cac6

# revl has no release that carries this example yet: pin a commit.
ARG REVL_REF
# The cordis-py revision revl's test suite runs against
# (CORDIS_PY_PIN in backends/python/setup.sh). Install it from that fork by
# URL: the PyPI package named `cordis` is a different project.
ARG CORDIS_PY_REF=1c5e6f17abf538bf01012f9d72ce0cfa978d91b3

RUN test -n "${REVL_REF}" || { echo "pass --build-arg REVL_REF=<revl commit sha>" >&2; exit 1; }
RUN python3 -m pip install --no-cache-dir \
        "revl @ git+https://github.com/inso1337/revl@${REVL_REF}" \
        "cordis @ git+https://github.com/inso1337/cordis-py@${CORDIS_PY_REF}" \
        watchdog \
    && revl --help > /dev/null
