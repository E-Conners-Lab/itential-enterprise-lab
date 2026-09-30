# The AWS VPN box's twin for the clab dev topology (clab/versions.yaml aws_twin). The same Ubuntu release and strongSwan
# packages as the AWS box's user data (cloud-devops-pipeline terraform/modules/vpn/user_data.sh.tftpl), plus iptables
# and iproute2. One image, two roles: twin.sh (the strongSwan end) and nat.sh (the 1:1 NAT in front of it, the EIP).
# Built on the clab VM by clab-host.yml; BASE is aws_twin.image.base, pinned by digest.
ARG BASE
FROM ${BASE}
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends strongswan strongswan-swanctl iptables iproute2 \
 && rm -rf /var/lib/apt/lists/* \
 && swanctl --version
COPY charon-lab.conf /etc/strongswan.d/charon-lab.conf
COPY --chmod=0755 twin.sh nat.sh /usr/local/sbin/
