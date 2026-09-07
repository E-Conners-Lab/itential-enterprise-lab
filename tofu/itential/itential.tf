# One Docker host for the itential-dev-stack (Platform, MongoDB, Redis, Gateway 5, Gateway 4,
# MCP), single-homed on vmbr1 (ADR 0030, ADR 0035). Cloned from the Ubuntu template; guest
# configuration is ansible/playbooks/itential.yml.

resource "proxmox_virtual_environment_vm" "itential" {
  node_name   = var.node_name
  vm_id       = var.vm.vm_id
  name        = "itential"
  description = "Itential Platform + Automation Gateways + MCP as containers (PID S4, ADR 0035)"
  tags        = ["phase-5", "itential"]
  on_boot     = true
  started     = true

  clone {
    vm_id = var.template_vm_id
    full  = true
  }

  machine       = "q35"
  scsi_hardware = "virtio-scsi-single"

  agent {
    enabled = true
  }

  cpu {
    type  = "host"
    cores = var.vm.cores
  }

  memory {
    dedicated = var.vm.memory_mb
  }

  operating_system {
    type = "l26"
  }

  disk {
    datastore_id = "local-lvm"
    interface    = "scsi0"
    size         = var.vm.disk_gb
    discard      = "on"
    iothread     = true
  }

  network_device {
    bridge = "vmbr1"
    model  = "virtio"
  }

  serial_device {}

  vga {
    type = "serial0"
  }

  initialization {
    datastore_id = "local-lvm"
    interface    = "ide2"

    dns {
      domain  = "lab.internal"
      servers = var.dns_servers
    }

    ip_config {
      ipv4 {
        address = var.vm.ip
        gateway = var.gateway
      }
    }

    user_account {
      username = "ubuntu"
      keys     = [var.ssh_public_key]
    }
  }
}

output "itential" {
  value = { vm_id = proxmox_virtual_environment_vm.itential.vm_id, ip = var.vm.ip }
}
