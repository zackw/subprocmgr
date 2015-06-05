# subprocmgr

This is a semi-compatible replacement for the `subprocess` module.
It solves a headache when writing a Python program that runs a bunch
of subprocesses and consumes their output: There is no way to
`select()` for the termination of a specific process, and therefore no
reliable way to know when to call `wait()`.

Presently see [`subprocmgr.py`](subprocmgr.py) for documentation.
