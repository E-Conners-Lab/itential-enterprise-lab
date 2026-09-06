variable "node_name" {
  description = "Proxmox node that hosts everything (single-host lab)"
  type        = string
  default     = "homelab"
}

variable "ssh_public_key" {
  description = "Workstation public key installed on every cloud-init VM (elliot@mac-mini ed25519)"
  type        = string
}

variable "ubuntu_import_file" {
  description = "Ubuntu 24.04 cloud image as staged by the phase-2 host play (manifest 2.9)"
  type        = string
  default     = "local:import/noble-24.04-20260826.qcow2"
}

variable "rocky_import_file" {
  description = "Rocky 9.8 GenericCloud image as staged by the phase-2 host play (manifest 3.1)"
  type        = string
  default     = "local:import/rocky-9.8-20260525.qcow2"
}

variable "oob_gw_lan_address" {
  description = "oob-gw home-LAN leg, CIDR. Must be below the router DHCP pool (topology/ipam.yaml home_lan)"
  type        = string
  default     = "192.168.68.120/22"
}

variable "oob_gw_lan_gateway" {
  type    = string
  default = "192.168.68.1"
}

variable "oob_gw_oob_address" {
  description = "oob-gw OOB leg, CIDR (topology/ipam.yaml: oob-gw)"
  type        = string
  default     = "10.100.0.1/24"
}

variable "home_dns" {
  description = "Resolver oob-gw itself uses until phase 6 (the home router)"
  type        = list(string)
  default     = ["192.168.68.1"]
}
