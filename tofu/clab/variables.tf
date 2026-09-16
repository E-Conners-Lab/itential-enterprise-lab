variable "node_name" {
  type    = string
  default = "homelab"
}

variable "ssh_public_key" {
  description = "Workstation public key installed by cloud-init (elliot@mac-mini ed25519)"
  type        = string
}

variable "template_vm_id" {
  description = "tpl-ubuntu-2404 from tofu/oob (Ubuntu, like tofu/itential)"
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

# Sizing is docs/resource-budget.md section 2 and clab/versions.yaml vm (tests/test_clab.py holds them
# together); verify/test-12a-clab-dev.sh S10.6 measures the result on the VM.
variable "vm" {
  type = object({ name = string, vm_id = number, ip = string, cores = number, memory_mb = number, disk_gb = number })
  default = {
    name      = "clab"
    vm_id     = 230
    ip        = "10.100.0.224"
    cores     = 8
    memory_mb = 16384
    disk_gb   = 60
  }
}
