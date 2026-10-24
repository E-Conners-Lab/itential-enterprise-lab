# Workflow guides

What each workflow does from start to end, for someone who is about to run one or is looking at a long job in the task bar and wants to know what it is doing. Every page is generated from the workflow's own document by `itential/workflows/describe.py`: the steps are the ones `build.py` names, the lines are the tasks' canvas labels, the arrows are the transitions. A test keeps the pages equal to the documents.

| Workflow | Steps | Tasks | Waits for a person |
|---|---|---|---|
| [Count Devices in NetBox](count-devices-in-netbox.md) | 1 | 1 | 0 |
| [Get Device Software Version](get-device-software-version.md) | 4 | 9 | 0 |
| [Add Branch VLAN](add-branch-vlan.md) | 12 | 94 | 1 |
| [Run Show Command on a Device](run-show-command-on-a-device.md) | 5 | 21 | 0 |
| [Run Show Command on All Devices](run-show-command-on-all-devices.md) | 6 | 19 | 0 |
| [Push Configuration with Approval](push-configuration-with-approval.md) | 8 | 28 | 1 |
| [Push Configuration with Revert Timer](push-configuration-with-revert-timer.md) | 8 | 53 | 1 |
| [Deploy AWS VPN](deploy-aws-vpn.md) | 10 | 64 | 1 |
| [Verify AWS VPN](verify-aws-vpn.md) | 7 | 36 | 0 |
| [Hand Off AWS VPN](hand-off-aws-vpn.md) | 14 | 121 | 1 |
| [Tear Down AWS VPN](tear-down-aws-vpn.md) | 11 | 93 | 2 |
| [Check AWS Drift](check-aws-drift.md) | 3 | 11 | 1 |
| [Get AWS VPN Status](get-aws-vpn-status.md) | 2 | 8 | 0 |
| [Diagnose AWS VPN Outage](diagnose-aws-vpn-outage.md) | 12 | 144 | 2 |
| [Diagnose Fabric BGP Outage](diagnose-fabric-bgp-outage.md) | 12 | 116 | 2 |
| [Break Fabric BGP](break-fabric-bgp.md) | 12 | 70 | 2 |
| [Drill Batfish Gate](drill-batfish-gate.md) | 8 | 44 | 0 |
| [Rotate AWS VPN Key](rotate-aws-vpn-key.md) | 12 | 106 | 1 |
| [Rotate AWS VPN Key Monthly](rotate-aws-vpn-key-monthly.md) | 13 | 110 | 1 |
| [Tear Down Expired AWS VPN](tear-down-expired-aws-vpn.md) | 11 | 78 | 1 |
| [Run Nightly Compliance Check](run-nightly-compliance-check.md) | 1 | 3 | 0 |
| [Back Up All Device Configs](back-up-all-device-configs.md) | 1 | 4 | 0 |
| [Remove Branch VLAN](remove-branch-vlan.md) | 9 | 50 | 0 |
| [Summarize Compliance Results](summarize-compliance-results.md) | 6 | 38 | 0 |
| [List Devices from NetBox](list-devices-from-netbox.md) | 3 | 8 | 0 |
| [Describe Workflow](describe-workflow.md) | 5 | 18 | 1 |
| [Describe Job](describe-job.md) | 5 | 18 | 1 |
| [Describe for an Agent](describe-for-an-agent.md) | 4 | 15 | 0 |
| [Ask Platform Guide](ask-platform-guide.md) | 4 | 15 | 1 |
| [Run Show Command on a Scratch Device](run-show-command-on-a-scratch-device.md) | 5 | 14 | 0 |
| [Get RESTCONF Data from a Scratch Device](get-restconf-data-from-a-scratch-device.md) | 5 | 15 | 0 |
| [Push Configuration to a Scratch Device with Approval](push-configuration-to-a-scratch-device-with-approval.md) | 8 | 40 | 1 |
| [Restore Baseline on a Scratch Device](restore-baseline-on-a-scratch-device.md) | 5 | 21 | 0 |

A workflow is listed here as soon as versions.yaml names it: its builder in `build.py` names the steps (`step(...)`), and the renderer needs nothing else.
