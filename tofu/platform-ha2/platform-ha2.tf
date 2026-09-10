# The production Itential environment in Itential's HA2 shape (PID S11, ADR 0053): one VM per component,
# every one of them cloned from the Ubuntu template and single-homed on vmbr1 (ADR 0030). The VM list, its
# sizes and its addresses are NOT written here: they come from itential/ha2/versions.yaml, which
# tests/test_platform_ha2.py holds to docs/resource-budget.md and topology/ipam.yaml. Guest configuration is
# ansible/playbooks/platform-ha2-*.yml.

locals {
  ha2 = yamldecode(file("${path.module}/${var.oracle}"))
  vms = { for v in local.ha2.vms : v.name => v }
  # one short sentence per role for the Proxmox description, so the host inventory reads like the design
  role_note = {
    loadbalancer = "nginx in front of the Platform nodes; serves ${local.ha2.service_name}.${local.ha2.domain}"
    platform     = "Itential Platform node (container from the Itential registry)"
    mongodb      = "MongoDB member of replica set ${local.ha2.mongodb.replica_set}"
    redis        = "Redis with a Sentinel monitoring ${local.ha2.redis.master_name}"
    gateway      = "Itential Gateway 5 cluster (gateway5, etcd, runner)"
    tools        = "MCP server and the in-lab Ollama (not Itential components)"
  }
}

resource "proxmox_virtual_environment_vm" "ha2" {
  for_each = local.vms

  node_name   = var.node_name
  vm_id       = each.value.vm_id
  name        = each.key
  description = "${local.role_note[each.value.role]} (PID S11, ADR 0053)"
  tags        = ["phase-8", "itential-ha2", each.value.role]
  on_boot     = true
  started     = true

  clone {
    vm_id = var.template_vm_id
    full  = true
  }

  machine       = "q35"
  scsi_hardware = "virtio-scsi-single"

  # The Ubuntu template carries no qemu-guest-agent (ansible/playbooks/platform-ha2-hosts.yml installs it), so
  # the provider would wait its full default of 15 minutes per VM on a first apply. Two minutes is enough once
  # the agent is there, and a first apply proceeds instead of stalling (lab-build-lessons, 2026-09-06).
  agent {
    enabled = true
    timeout = "2m"
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
      domain  = local.ha2.domain
      servers = var.dns_servers
    }

    ip_config {
      ipv4 {
        address = "${each.value.ip}/24"
        gateway = var.gateway
      }
    }

    user_account {
      username = "ubuntu"
      keys     = [var.ssh_public_key]
    }
  }
}

output "ha2" {
  value = { for name, vm in proxmox_virtual_environment_vm.ha2 : name => { vm_id = vm.vm_id, ip = local.vms[name].ip, role = local.vms[name].role } }
}
