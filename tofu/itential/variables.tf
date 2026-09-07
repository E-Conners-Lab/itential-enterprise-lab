variable "node_name" {
  type    = string
  default = "homelab"
}

variable "ssh_public_key" {
  description = "Workstation public key installed by cloud-init (elliot@mac-mini ed25519)"
  type        = string
}

variable "template_vm_id" {
  description = "tpl-ubuntu-2404 from tofu/oob (ADR 0035: Ubuntu, not the Rocky template)"
  type        = number
  default     = 9000
}

variable "gateway" {
  type    = string
  default = "10.100.0.1"
}

variable "dns_servers" {
  type    = list(string)
  default = ["10.100.0.1"]
}

# Sizing is docs/resource-budget.md section 2 and itential/versions.yaml (tests/test_itential.py
# holds all three together); verify/test-05-itential.sh S4.6 measures the result after 24 h.
variable "vm" {
  type = object({ vm_id = number, ip = string, cores = number, memory_mb = number, disk_gb = number })
  default = {
    vm_id     = 205
    ip        = "10.100.0.65/24"
    cores     = 8
    memory_mb = 24576
    disk_gb   = 160
  }
}
