# oob-gw: the only path between 10.100.0.0/14 and the home LAN (ADR 0004).
# net0 on vmbr0 (home LAN, static below the DHCP pool), net1 on vmbr1 (OOB, 10.100.0.1).
# Guest configuration (nftables NAT, unbound, chrony) is Ansible's job, not cloud-init's.

resource "proxmox_virtual_environment_vm" "oob_gw" {
  node_name   = var.node_name
  vm_id       = 200
  name        = "oob-gw"
  description = "OOB gateway + NAT + forwarding resolver + NTP (PID S1, ADR 0004)"
  tags        = ["phase-2", "oob"]
  on_boot     = true
  started     = true

  clone {
    vm_id = proxmox_virtual_environment_vm.template["ubuntu-2404"].vm_id
    full  = true
  }

  machine       = "q35"
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
    type = "l26"
  }

  disk {
    datastore_id = "local-lvm"
    interface    = "scsi0"
    size         = 16 # must equal the template disk; a clone cannot shrink
    discard      = "on"
    iothread     = true
  }

  network_device {
    bridge = "vmbr0"
    model  = "virtio"
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
      servers = var.home_dns
    }

    ip_config {
      ipv4 {
        address = var.oob_gw_lan_address
        gateway = var.oob_gw_lan_gateway
      }
    }

    ip_config {
      ipv4 {
        address = var.oob_gw_oob_address
      }
    }

    user_account {
      username = "ubuntu"
      keys     = [var.ssh_public_key]
    }
  }

  lifecycle {
    # VM 200 was imported after an interrupted apply; an imported VM carries no clone
    # provenance, and bpg would otherwise force a replacement. Provider-documented pattern.
    ignore_changes = [clone]
  }

  depends_on = [proxmox_virtual_environment_vm.template]
}

output "oob_gw" {
  value = {
    vm_id = proxmox_virtual_environment_vm.oob_gw.vm_id
    lan   = var.oob_gw_lan_address
    oob   = var.oob_gw_oob_address
    macs  = proxmox_virtual_environment_vm.oob_gw.mac_addresses
  }
}

output "templates" {
  value = { for k, t in proxmox_virtual_environment_vm.template : k => t.vm_id }
}
