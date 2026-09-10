variable "node_name" {
  type    = string
  default = "homelab"
}

variable "ssh_public_key" {
  description = "Workstation public key installed by cloud-init (elliot@mac-mini ed25519)"
  type        = string
}

variable "template_vm_id" {
  description = "tpl-ubuntu-2404 from tofu/oob; the container path is OS-neutral (ADR 0053)"
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

variable "oracle" {
  description = "Path to itential/ha2/versions.yaml, the single oracle for the HA2 topology (ADR 0053)"
  type        = string
  default     = "../../itential/ha2/versions.yaml"
}
