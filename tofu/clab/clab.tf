# The Containerlab host for the dev topology (ADR 0063; PID S10.6), single-homed on vmbr1 (ADR 0030). Cloned
# from the Ubuntu template like tofu/itential; guest configuration is ansible/playbooks/clab-host.yml.
# CPU type host is required, not a tuning choice: the vrnetlab C8000v nodes are QEMU VMs inside containers
# and need vmx exposed for nested KVM (/dev/kvm in the guest, S10.6).

resource "proxmox_virtual_environment_vm" "clab" {
  node_name   = var.node_name
  vm_id       = var.vm.vm_id
  name        = var.vm.name
  description = "Containerlab dev topology: 2 C8000v + 2 vEOS (vrnetlab) for the itential-dev stack (PID S10, ADR 0063)"
  tags        = ["phase-12", "clab"]
  on_boot     = true
  started     = true

  clone {
    vm_id = var.template_vm_id
    full  = true
  }

  machine       = "q35"
  scsi_hardware = "virtio-scsi-single"

  # The Ubuntu template carries no qemu-guest-agent (clab-host.yml installs it), so the provider would wait its
  # full default of 15 minutes on a first apply and on every refresh until then - measured 15m21s on
  # 2026-09-16. Two minutes, as tofu/platform-ha2 (lab-build-lessons, 2026-09-06).
  agent {
    enabled = true
    timeout = "2m"
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
        address = "${var.vm.ip}/24"
        gateway = var.gateway
      }
    }

    user_account {
      username = "ubuntu"
      keys     = [var.ssh_public_key]
    }
  }
}

output "clab" {
  value = { vm_id = proxmox_virtual_environment_vm.clab.vm_id, ip = var.vm.ip }
}
