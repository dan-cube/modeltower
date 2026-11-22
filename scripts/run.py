"""Launcher used by the self-contained app build. It imports clusterctl instead of running the file directly:
the system Python 3.9 (the one that comes with Apple's command line tools) fails to read clusterctl.py as a
script ("Non-UTF-8 code"), while importing it works on every supported version."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.argv[0] = os.path.join(HERE, 'clusterctl.py')
import clusterctl  # noqa: E402

clusterctl.main()
