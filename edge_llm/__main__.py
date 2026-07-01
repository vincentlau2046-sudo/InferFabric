#!/usr/bin/env python3
"""Allow `python3 -m edge_llm` invocation."""

import sys
import os

# Ensure the package parent is on the path
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if here not in sys.path:
    sys.path.insert(0, here)

# Import and run CLI main
import edge_llm.cli as cli
cli.main()
