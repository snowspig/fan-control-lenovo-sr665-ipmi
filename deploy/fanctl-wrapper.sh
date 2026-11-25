#!/bin/sh
# fanctl: run the SR665 fan controller installed under /opt/fan-control.
PYTHONPATH=/opt/fan-control/src exec /usr/bin/python3 -m sr665_fanctl "$@"
