# Three k3s server nodes (embedded etcd), all schedulable, on vmbr1 only (single-homed by design,
# ADR 0030). Cloned from the Ubuntu template; guest config is ansible/playbooks/k3s-cluster.yml.

resource "proxmox_virtual_environment_vm" "k3s" {
  for_each = var.nodes

  node_name   = var.node_name
  vm_id       = each.value.vm_id
  name        = each.key
  description = "k3s server node (PID S2, ADR 0021)"
  tags        = ["phase-3", "k3s"]
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
    cores = each.value.cores
  }

  memory {
    dedicated = each.value.memory_mb
  }

  operating_system {
    type = "l26"
  }

  disk {
    datastore_id = "local-lvm"
    interface    = "scsi0"
    size         = each.value.disk_gb
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
        address = each.value.ip
        gateway = var.gateway
      }
    }

    user_account {
      username = "ubuntu"
      keys     = [var.ssh_public_key]
    }
  }
}

output "nodes" {
  value = { for k, v in proxmox_virtual_environment_vm.k3s : k => { vm_id = v.vm_id, ip = var.nodes[k].ip } }
}
