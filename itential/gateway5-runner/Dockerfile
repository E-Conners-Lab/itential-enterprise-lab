# Gateway 5 runner node on glibc (ADR 0038). The stock automation-gateway5 image is Alpine
# (musl, Python 3.14): Cisco pyATS/Genie publishes manylinux wheels for Python <= 3.13 only, so
# Genie can never install into that image's isolated environments. iagctl is a static Go
# binary, so the runner is the same binary on Debian with Python 3.12; the gateway server keeps
# the stock image and hands every execution to this runner (GATEWAY_SERVER_DISTRIBUTED_EXECUTION).
# Built on the VM by ansible/playbooks/itential.yml from the two pinned images in versions.yaml.
ARG GATEWAY5_IMAGE
ARG PYTHON_IMAGE
FROM ${GATEWAY5_IMAGE} AS gateway
FROM ${PYTHON_IMAGE}
COPY --from=gateway /usr/local/bin/iagctl /usr/local/bin/iagctl
# The netsdk services (send-command, send-config, ...) are pex executables that look for
# /usr/bin/python3; the venv cache, task workspaces and logs must be writable by the runner user.
RUN ln -s /usr/local/bin/python3 /usr/bin/python3 \
 && useradd -u 1001 -m itential \
 && mkdir -p /var/lib/gateway /etc/gateway /var/log/gateway /var/cache/gateway \
 && chown -R itential:itential /var/lib/gateway /etc/gateway /var/log/gateway /var/cache/gateway
USER itential
ENTRYPOINT ["iagctl", "runner"]
