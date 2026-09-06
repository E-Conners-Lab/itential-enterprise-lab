terraform {
  required_version = ">= 1.12.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "0.112.0"
    }
  }
}

# Credentials come only from the environment (.env via make):
#   PROXMOX_VE_ENDPOINT, PROXMOX_VE_API_TOKEN, PROXMOX_VE_INSECURE=true (self-signed host cert).
# No SSH block: this phase uses no snippets, so the API token is all tofu needs (ADR 0029).
provider "proxmox" {}
