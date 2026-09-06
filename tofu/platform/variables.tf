variable "node_name" {
  type    = string
  default = "homelab"
}

variable "ssh_public_key" {
  description = "Workstation public key installed by cloud-init (elliot@mac-mini ed25519)"
  type        = string
}

variable "template_vm_id" {
  description = "tpl-ubuntu-2404 from tofu/oob"
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

# Sizing is docs/resource-budget.md section 2; verify/test-03-platform.sh S2.6 checks the host
# against that file, so a change here must be a change there too.
variable "nodes" {
  type = map(object({ vm_id = number, ip = string, cores = number, memory_mb = number, disk_gb = number }))
  default = {
    k3s-01 = { vm_id = 201, ip = "10.100.0.16/24", cores = 4, memory_mb = 12288, disk_gb = 80 }
    k3s-02 = { vm_id = 202, ip = "10.100.0.17/24", cores = 4, memory_mb = 12288, disk_gb = 80 }
    k3s-03 = { vm_id = 203, ip = "10.100.0.18/24", cores = 4, memory_mb = 12288, disk_gb = 80 }
  }
}
