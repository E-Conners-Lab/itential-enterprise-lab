"""The canvas search is off for the suite (build.LAYOUT_SEARCH): it is the slow part of every build and only the
committed JSON needs it. The tests that compare a build against the committed files turn it back on for their own
module (tests/test_verify_aws_vpn.py). `python itential/workflows/build.py` is unaffected: it runs with the default."""

import os

os.environ.setdefault("LAB_WORKFLOW_LAYOUT", "off")
