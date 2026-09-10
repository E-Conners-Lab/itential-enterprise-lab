terraform {
  required_version = ">= 1.12.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "0.112.0"
    }
  }
}

# Credentials from the environment only (PROXMOX_VE_ENDPOINT, PROXMOX_VE_API_TOKEN,
# PROXMOX_VE_INSECURE), the same token-only model as every other root module (ADR 0029).
provider "proxmox" {}
