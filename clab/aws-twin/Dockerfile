# The AWS VPN box's twin for the clab dev topology (clab/versions.yaml aws_twin). The same Ubuntu release and strongSwan
# install as the AWS box's user data (cloud-devops-pipeline terraform/modules/vpn/user_data.sh.tftpl: recommends
# included, which brings libstrongswan-standard-plugins - the gcm and openssl plugins behind aes256gcm16 and ecp256),
# plus iptables and iproute2. One image, two roles: twin.sh (the strongSwan end) and nat.sh (the 1:1 NAT, the EIP).
# Built on the clab VM by clab-host.yml; BASE is aws_twin.image.base, pinned by digest.
ARG BASE
FROM ${BASE}
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y strongswan strongswan-swanctl libstrongswan-standard-plugins iptables iproute2 \
 && rm -rf /var/lib/apt/lists/* \
 && swanctl --version \
 && test -f /usr/lib/ipsec/plugins/libstrongswan-gcm.so \
 && test -f /usr/lib/ipsec/plugins/libstrongswan-openssl.so
COPY charon-lab.conf /etc/strongswan.d/charon-lab.conf
COPY --chmod=0755 twin.sh nat.sh /usr/local/sbin/
