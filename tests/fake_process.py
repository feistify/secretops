"""Test doubles for the GitHub adapter's Popen factory.

FakeFactory records each launch (arguments and keyword arguments, including
the exact environment) and returns a FakeProcess, which records the stdin
input and timeout passed to communicate. Launch and communication failures
are separate, so tests can cover both sides of the launch boundary.
"""

import subprocess


class FakeProcess:
    def __init__(self, *, returncode=0, stdout="", communicate_error=None, hang=False):
        self.returncode = returncode
        self._stdout = stdout
        self._communicate_error = communicate_error
        self._hang = hang
        self.inputs = []
        self.timeouts = []
        self.killed = False
        self.stdin = self.stdout = self.stderr = None

    def communicate(self, input=None, timeout=None):
        self.inputs.append(input)
        self.timeouts.append(timeout)
        if self._hang and not self.killed:
            raise subprocess.TimeoutExpired("gh", timeout, output="partial output")
        if self._communicate_error is not None and not self.killed:
            raise self._communicate_error
        if self.killed:
            self.returncode = -9
            return "", ""
        return self._stdout, ""

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self.returncode


class FakeFactory:
    def __init__(self, *processes, launch_error=None):
        self.processes = list(processes) or [FakeProcess()]
        self.launch_error = launch_error
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.launch_error is not None:
            raise self.launch_error
        return self.processes[min(len(self.calls), len(self.processes)) - 1]

    @property
    def process(self):
        return self.processes[0]
