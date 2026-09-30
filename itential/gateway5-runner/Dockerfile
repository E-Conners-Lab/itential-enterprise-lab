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
# Terraform for terraform-run (ADR 0068): the pinned release, refused unless its SHA-256 matches versions.yaml. The
# slim base has no curl or unzip, so Python fetches and unpacks it. amd64: the Gateway image is amd64 only.
# CHECKPOINT_DISABLE stops Terraform's version check calling home on every run.
ARG TERRAFORM_VERSION
ARG TERRAFORM_SHA256
ENV CHECKPOINT_DISABLE=1
RUN python3 -c "import hashlib, io, os, sys, urllib.request, zipfile; \
v, want = sys.argv[1], sys.argv[2]; \
data = urllib.request.urlopen(f'https://releases.hashicorp.com/terraform/{v}/terraform_{v}_linux_amd64.zip', timeout=300).read(); \
got = hashlib.sha256(data).hexdigest(); \
sys.exit(f'terraform {v}: sha256 {got} is not the pinned {want}') if got != want else None; \
zipfile.ZipFile(io.BytesIO(data)).extract('terraform', '/usr/local/bin'); \
os.chmod('/usr/local/bin/terraform', 0o755)" "$TERRAFORM_VERSION" "$TERRAFORM_SHA256" \
 && terraform version
# boto3 for terraform-run's plan files (S3, the state bucket's CMK), every wheel checked against its pinned hash
COPY requirements-terraform-run.txt /tmp/requirements-terraform-run.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements-terraform-run.txt \
 && rm /tmp/requirements-terraform-run.txt
# netmiko for the lab-edge services (ADR 0068 step 6): the router sessions, every wheel checked against its pinned hash
COPY requirements-lab-edge.txt /tmp/requirements-lab-edge.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements-lab-edge.txt \
 && rm /tmp/requirements-lab-edge.txt \
 && python3 -c "import netmiko; print('netmiko', netmiko.__version__)"
# GitHub's pinned host keys, where the runner's Gateway looks for them (~/.gateway.d/known_hosts, measured): without
# them the SSH clone of terraform-run's repository stops at "knownhosts: key is unknown"
# (the directory first: a COPY --chmod=600 would give the directory it creates the same mode, with no execute bit)
RUN install -d -o itential -g itential -m 700 /home/itential/.gateway.d
COPY --chown=itential:itential --chmod=600 known_hosts /home/itential/.gateway.d/known_hosts
USER itential
ENTRYPOINT ["iagctl", "runner"]
