"""Hidden terminal input, exercised through a real pseudo-terminal.

Each test runs the input code in a child process whose controlling terminal
is a PTY, and answers the bracketed-paste query and types or pastes bytes
like a terminal emulator would. These tests need no real terminal and run
headless; they do not prove that a particular emulator behaves this way.
"""

import json
import os
import pty
import select
import signal
import sys
import time

import pytest

PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
VALUE_PROMPT = b"Value: "
QUERY = b"\x1b[?2004$p"
PASTE_ON = b"\x1b[?2004;1$y"
SAVE_PROMPT = b"Save? "

CHILD = r"""
import json, os, sys, termios
from secretops.terminal_input import (
    HiddenTerminal, InputCancelled, InputRejected, TerminalUnsupported,
)
result = {}
fd = os.open("/dev/tty", os.O_RDWR)
before = termios.tcgetattr(fd)
try:
    with HiddenTerminal(support_wait=float(sys.argv[2])) as terminal:
        result["value"] = terminal.read_hidden("Value: ")
        result["confirmed"] = terminal.confirm("Save? ")
except InputRejected as error:
    result["rejected"] = str(error)
except InputCancelled:
    result["cancelled"] = True
except TerminalUnsupported as error:
    result["unsupported"] = str(error)
result["restored"] = termios.tcgetattr(fd) == before
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(result, handle)
"""


class PtySession:
    def __init__(self, tmp_path, support_wait=5.0):
        self.result_path = tmp_path / "result.json"
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - child
            os.execv(
                sys.executable,
                [sys.executable, "-c", CHILD, str(self.result_path), str(support_wait)],
            )
        self.pid = pid
        self.fd = fd
        self.output = bytearray()

    def read_until(self, marker: bytes, timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        while marker not in self.output:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"timed out waiting for {marker!r}: {bytes(self.output)!r}"
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if ready:
                try:
                    chunk = os.read(self.fd, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    raise AssertionError(f"child exited before {marker!r}: {bytes(self.output)!r}")
                self.output.extend(chunk)

    def send(self, data: bytes) -> None:
        os.write(self.fd, data)

    def finish(self, timeout: float = 10) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            pid, _ = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                break
            assert time.monotonic() < deadline, "child did not exit"
            ready, _, _ = select.select([self.fd], [], [], 0.05)
            if ready:
                try:
                    self.output.extend(os.read(self.fd, 4096))
                except OSError:
                    pass
        os.close(self.fd)
        return json.loads(self.result_path.read_text(encoding="utf-8"))

    def still_waiting(self, seconds: float = 0.5) -> bool:
        time.sleep(seconds)
        pid, _ = os.waitpid(self.pid, os.WNOHANG)
        return pid == 0

    def kill(self) -> None:
        try:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
        os.close(self.fd)


@pytest.fixture
def session(tmp_path):
    started = PtySession(tmp_path)
    started.read_until(QUERY)
    started.send(PASTE_ON)
    started.read_until(VALUE_PROMPT)
    return started


def paste(data: bytes) -> bytes:
    return PASTE_START + data + PASTE_END


def test_typed_value_is_hidden_and_terminal_is_restored(session):
    session.send(b"typed-value\r")
    session.read_until(SAVE_PROMPT)
    session.send(b"y\r")

    result = session.finish()

    assert result == {"value": "typed-value", "confirmed": True, "restored": True}
    assert b"typed-value" not in session.output
    assert b"\x1b[?2004h" in session.output
    assert session.output.rstrip().endswith(b"\x1b[?2004l") or b"\x1b[?2004l" in session.output


def test_single_line_paste_is_accepted(session):
    session.send(paste(b"pasted value") + b"\r")
    session.read_until(SAVE_PROMPT)
    session.send(b"yes\r")

    assert session.finish() == {"value": "pasted value", "confirmed": True, "restored": True}


def test_multiline_paste_is_rejected_as_a_whole(session):
    session.send(paste(b"first line\nsecond line"))

    result = session.finish()

    assert "multiline" in result["rejected"]
    assert "value" not in result and "confirmed" not in result
    assert result["restored"] is True
    assert SAVE_PROMPT not in session.output


def test_pasted_second_line_y_never_confirms(session):
    session.send(paste(b"secret-part\ny\r"))

    result = session.finish()

    assert "multiline" in result["rejected"]
    assert "confirmed" not in result
    assert SAVE_PROMPT not in session.output


def test_delayed_and_chunked_multiline_paste_is_rejected(session):
    for chunk in (b"\x1b[20", b"0~first", b" line", b"\r", b"second", b"\x1b[2", b"01~"):
        session.send(chunk)
        time.sleep(0.15)

    result = session.finish()

    assert "multiline" in result["rejected"]
    assert SAVE_PROMPT not in session.output


def test_chunked_single_line_paste_is_reassembled(session):
    for chunk in (b"\x1b[20", b"0~abc", b"def\x1b[2", b"01~", b"\r"):
        session.send(chunk)
        time.sleep(0.15)
    session.read_until(SAVE_PROMPT)
    session.send(b"y\r")

    assert session.finish()["value"] == "abcdef"


def test_incomplete_paste_never_reaches_confirmation_and_ctrl_c_cancels(session):
    session.send(PASTE_START + b"no end marker\ry\r")

    try:
        assert session.still_waiting()
        assert SAVE_PROMPT not in session.output
        session.send(b"\x03")
        result = session.finish()
    except BaseException:
        session.kill()
        raise

    assert result == {"cancelled": True, "restored": True}


def test_ctrl_c_inside_an_escape_sequence_cancels(session):
    session.send(b"ab\x1b[1;")
    assert session.still_waiting(0.3)
    session.send(b"\x03")

    assert session.finish() == {"cancelled": True, "restored": True}


def test_paste_at_confirmation_counts_as_no(session):
    session.send(b"typed\r")
    session.read_until(SAVE_PROMPT)
    session.send(paste(b"y") + b"\r")

    assert session.finish() == {"value": "typed", "confirmed": False, "restored": True}


def test_input_typed_ahead_of_the_confirmation_is_discarded(session):
    # An unmarked "value\ny\n" arriving at once must not answer Save?.
    session.send(b"typed\ry\r")
    session.read_until(SAVE_PROMPT)
    session.send(b"n\r")

    assert session.finish()["confirmed"] is False


@pytest.mark.parametrize("key", [b"\x03", b"\x04"], ids=["ctrl-c", "ctrl-d"])
def test_cancel_keys_cancel_and_restore(session, key):
    session.send(b"partial" + key)

    assert session.finish() == {"cancelled": True, "restored": True}


def test_ctrl_c_at_confirmation_is_no(session):
    session.send(b"typed\r")
    session.read_until(SAVE_PROMPT)
    session.send(b"\x03")

    assert session.finish()["confirmed"] is False


def test_backspace_and_kill_line_edit_typed_input(session):
    session.send("wrong\x15abé\x7f\x7fc\r".encode())
    session.read_until(SAVE_PROMPT)
    session.send(b"y\r")

    assert session.finish()["value"] == "ac"


def test_arrow_keys_do_not_edit_the_value(session):
    session.send(b"ab\x1b[Dc\r")
    session.read_until(SAVE_PROMPT)
    session.send(b"y\r")

    assert session.finish()["value"] == "abc"


def test_unicode_and_spaces_are_kept(session):
    session.send(paste("  ü 中 ".encode()) + b"\r")
    session.read_until(SAVE_PROMPT)
    session.send(b"y\r")

    assert session.finish()["value"] == "  ü 中 "


def test_other_characters_at_confirmation_are_not_shown_and_mean_no(session):
    session.send(b"typed\r")
    session.read_until(SAVE_PROMPT)
    session.send(b"q7y\r")

    result = session.finish()

    assert result["confirmed"] is False
    after_prompt = bytes(session.output).split(SAVE_PROMPT, 1)[1]
    assert b"q" not in after_prompt and b"7" not in after_prompt


# Terminals that do not confirm bracketed paste are refused before the prompt


def test_terminal_without_an_answer_is_refused_before_the_prompt(tmp_path):
    started = PtySession(tmp_path, support_wait=0.5)
    started.read_until(QUERY)

    result = started.finish()

    assert "did not confirm bracketed paste" in result["unsupported"]
    assert result["restored"] is True
    assert VALUE_PROMPT not in started.output


@pytest.mark.parametrize("reply", [b"\x1b[?2004;0$y", b"\x1b[?2004;2$y", b"\x1b[?2004;4$y"])
def test_terminal_reporting_paste_off_is_refused(tmp_path, reply):
    started = PtySession(tmp_path)
    started.read_until(QUERY)
    started.send(reply)

    result = started.finish()

    assert "bracketed paste is off" in result["unsupported"]
    assert VALUE_PROMPT not in started.output


def test_unmarked_multiline_paste_in_unsupported_terminal_is_never_read(tmp_path):
    # The reviewer's case: no paste markers. Entry stops before any prompt,
    # so nothing is taken as a value or a confirmation, and nothing is echoed.
    started = PtySession(tmp_path, support_wait=0.5)
    started.read_until(QUERY)
    started.send(b"first-line\nsecond-line\ny\n")

    result = started.finish()

    assert "unsupported" in result
    assert b"second-line" not in started.output
    assert VALUE_PROMPT not in started.output and SAVE_PROMPT not in started.output


def test_input_typed_before_the_prompt_is_discarded(tmp_path):
    started = PtySession(tmp_path)
    started.read_until(QUERY)
    started.send(b"early" + PASTE_ON)
    started.read_until(VALUE_PROMPT)
    started.send(b"typed\r")
    started.read_until(SAVE_PROMPT)
    started.send(b"y\r")

    assert started.finish()["value"] == "typed"


def test_closed_terminal_cancels(tmp_path):
    started = PtySession(tmp_path)
    started.read_until(QUERY)
    started.send(PASTE_ON)
    started.read_until(VALUE_PROMPT)
    started.send(PASTE_START + b"abc")
    os.close(started.fd)
    try:
        _, status = os.waitpid(started.pid, 0)
    except ChildProcessError:
        return
    # Either SIGHUP ended the child, or it reported a cancellation.
    if os.WIFEXITED(status) and started.result_path.exists():
        result = json.loads(started.result_path.read_text(encoding="utf-8"))
        assert result.get("cancelled") is True
        assert "confirmed" not in result
