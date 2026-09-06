# Cloud-init templates. Built once from the images the host play staged into local:import/.
# Cloning is what every later phase does; the templates themselves never run.

locals {
  templates = {
    ubuntu-2404 = { vm_id = 9000, file = var.ubuntu_import_file, os = "l26", desc = "Ubuntu 24.04 noble cloud image serial 20260826 (ADR 0018)" }
    rocky-9     = { vm_id = 9001, file = var.rocky_import_file, os = "l26", desc = "Rocky Linux 9.8 GenericCloud 20260525.0 (ADR 0020), Itential VMs only" }
  }
}

resource "proxmox_virtual_environment_vm" "template" {
  for_each = local.templates

  node_name   = var.node_name
  vm_id       = each.value.vm_id
  name        = "tpl-${each.key}"
  description = each.value.desc
  tags        = ["template", "phase-2"]
  template    = true
  started     = false
  on_boot     = false

  machine       = "q35"
  bios          = "seabios"
  scsi_hardware = "virtio-scsi-single"

  agent {
    enabled = true
  }

  cpu {
    type  = "host"
    cores = 1
  }

  memory {
    dedicated = 1024
  }

  operating_system {
    type = each.value.os
  }

  disk {
    datastore_id = "local-lvm"
    import_from  = each.value.file
    interface    = "scsi0"
    size         = 16
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

    user_account {
      username = each.key == "rocky-9" ? "rocky" : "ubuntu"
      keys     = [var.ssh_public_key]
    }
  }

  lifecycle {
    ignore_changes = [initialization[0].ip_config, initialization[0].dns]
  }
}
