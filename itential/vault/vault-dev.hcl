# The dev tier's Vault (ADR 0065 decision 9): one Raft node on itential-dev, TLS with the VM's own lab-CA
# certificate (itential-host.yml issues it for itential-dev.lab.internal and 10.100.0.65), dev secrets only.
# Production runs the k3s chart instead (ADR 0026); only this file is dev-specific.
ui = true

# Raft on the image's own vault-owned directory; mlock is off, as HashiCorp recommends for integrated storage
disable_mlock = true
storage "raft" {
  path    = "/vault/file"
  node_id = "vault-dev"
}

listener "tcp" {
  address       = "0.0.0.0:8200"
  tls_cert_file = "/vault/tls/cert.pem"
  tls_key_file  = "/vault/tls/key.pem"
}

api_addr     = "https://10.100.0.65:8200"
cluster_addr = "https://127.0.0.1:8201"
