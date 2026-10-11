# Workflow guides

What each workflow does from start to end, for someone who is about to run one or is looking at a long job in the task bar and wants to know what it is doing. Every page is generated from the workflow's own document by `itential/workflows/describe.py`: the steps are the ones `build.py` names, the lines are the tasks' canvas labels, the arrows are the transitions. A test keeps the pages equal to the documents.

| Workflow | Steps | Tasks | Waits for a person |
|---|---|---|---|
| [Rotate AWS VPN Key](rotate-aws-vpn-key.md) | 12 | 106 | 1 |

The pilot covers Rotate AWS VPN Key. The other workflows get a page as their builders in `build.py` name their steps (`step(...)`): the renderer needs nothing else.
