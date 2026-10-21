"""Hidden secret entry on the controlling terminal.

The value is read from /dev/tty with echo off, never from stdin, arguments,
or the environment. Bracketed paste mode is enabled so a paste arrives
between markers: a pasted value is consumed through its end marker as one
unit, and a paste that contains a line break is rejected as a whole instead
of being cut at the first line. Paste data is never taken as confirmation.

Typing and an unmarked paste cannot be told apart, so a terminal without
bracketed paste could cut a multiline paste at its first line and feed the
rest to the confirmation prompt. Entry is therefore refused unless the
terminal confirms that bracketed paste is on (DECRQM report for mode 2004)
before the value is requested. No answer within the wait counts as no.
"""

import os
import re
import select
import termios
import time
from typing import Self

ESC = 0x1B
PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
ENABLE_PASTE = b"\x1b[?2004h"
DISABLE_PASTE = b"\x1b[?2004l"
QUERY_PASTE_MODE = b"\x1b[?2004$p"
PASTE_MODE_REPORT = re.compile(rb"\x1b\[\?2004;(\d)\$y")
PASTE_MODE_ON = {b"1", b"3"}  # set, permanently set
SUPPORT_WAIT_SECONDS = 1.5
CONFIRM_CHARACTERS = frozenset(b"yesnoYESNO")
CANCEL_BYTES = {0x03, 0x04, 0x1A}  # Ctrl+C, Ctrl+D, Ctrl+Z
ERASE_BYTES = {0x7F, 0x08}
KILL_LINE = 0x15  # Ctrl+U
ENTER_BYTES = {0x0D, 0x0A}
MAX_INPUT_BYTES = 64 * 1024


class TerminalUnavailable(Exception):
    """No controlling terminal for secure input."""


class TerminalUnsupported(TerminalUnavailable):
    """The terminal did not confirm bracketed paste; no value was requested."""


class InputCancelled(Exception):
    """The human cancelled; nothing was submitted."""


class InputRejected(Exception):
    """The input cannot be used. The message never contains it."""


class HiddenTerminal:
    """The controlling terminal in no-echo, byte-at-a-time mode.

    Terminal settings and paste mode are restored on every exit.
    """

    def __init__(self, path: str = "/dev/tty", support_wait: float = SUPPORT_WAIT_SECONDS):
        self._path = path
        self._support_wait = support_wait
        self._fd: int | None = None
        self._saved = None
        self._pending = bytearray()

    def __enter__(self) -> Self:
        try:
            self._fd = os.open(self._path, os.O_RDWR | os.O_NOCTTY)
        except OSError as error:
            raise TerminalUnavailable("no terminal is available for hidden input") from error
        try:
            self._saved = termios.tcgetattr(self._fd)
            attributes = termios.tcgetattr(self._fd)
            attributes[0] &= ~(termios.ICRNL | termios.INLCR | termios.IXON)
            attributes[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
            attributes[6][termios.VMIN] = 1
            attributes[6][termios.VTIME] = 0
            termios.tcsetattr(self._fd, termios.TCSAFLUSH, attributes)
            self.write(ENABLE_PASTE)
        except (OSError, termios.error) as error:
            self._restore()
            raise TerminalUnavailable("the terminal does not support hidden input") from error
        try:
            self._require_paste_mode()
        except BaseException:
            self._restore()
            raise
        return self

    def _require_paste_mode(self) -> None:
        """Ask the terminal whether bracketed paste is on; refuse otherwise.

        The wait only decides support: no answer means unsupported. Anything
        typed before the value prompt is discarded with the answer.
        """
        self.write(QUERY_PASTE_MODE)
        received = bytearray()
        deadline = time.monotonic() + self._support_wait
        while True:
            match = PASTE_MODE_REPORT.search(received)
            if match:
                break
            remaining = deadline - time.monotonic()
            ready = remaining > 0 and select.select([self._fd], [], [], remaining)[0]
            if not ready:
                raise TerminalUnsupported(
                    "this terminal did not confirm bracketed paste, which secret entry "
                    "requires so that a multiline paste cannot be cut or shown"
                )
            try:
                chunk = os.read(self._fd, 4096)
            except OSError as error:
                raise InputCancelled("the terminal was closed") from error
            if not chunk:
                raise InputCancelled("the terminal was closed")
            received.extend(chunk)
        if match.group(1) not in PASTE_MODE_ON:
            raise TerminalUnsupported(
                "this terminal reports that bracketed paste is off, which secret entry requires"
            )
        self.discard_pending()

    def __exit__(self, *_exc) -> None:
        self._restore()

    def _restore(self) -> None:
        if self._fd is None:
            return
        try:
            os.write(self._fd, DISABLE_PASTE)
        except OSError:
            pass
        if self._saved is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSAFLUSH, self._saved)
            except termios.error:
                pass
        os.close(self._fd)
        self._fd = None

    def write(self, data: bytes) -> None:
        os.write(self._fd, data)

    def print(self, text: str = "") -> None:
        self.write(text.replace("\n", "\r\n").encode("utf-8") + b"\r\n")

    def discard_pending(self) -> None:
        """Drop input that arrived but was not read yet."""
        self._pending.clear()
        termios.tcflush(self._fd, termios.TCIFLUSH)

    def _byte(self) -> int:
        if not self._pending:
            try:
                chunk = os.read(self._fd, 4096)
            except OSError as error:
                raise InputCancelled("the terminal was closed") from error
            if not chunk:
                raise InputCancelled("the terminal was closed")
            self._pending.extend(chunk)
        return self._pending.pop(0)

    def _inner_byte(self) -> int:
        # Inside an escape sequence or a paste, Ctrl+C, Ctrl+D, and Ctrl+Z
        # still cancel, so a missing end marker cannot hold the prompt.
        byte = self._byte()
        if byte in CANCEL_BYTES:
            raise InputCancelled("cancelled")
        return byte

    def _escape_sequence(self) -> bytes:
        """Read the rest of a sequence after ESC and return it whole."""
        sequence = bytearray([ESC])
        following = self._inner_byte()
        sequence.append(following)
        if following != ord("["):
            return bytes(sequence)
        while True:
            byte = self._inner_byte()
            sequence.append(byte)
            if 0x40 <= byte <= 0x7E or len(sequence) > 32:
                return bytes(sequence)

    def _paste_body(self) -> bytes:
        """Read a marked paste through its end marker."""
        body = bytearray()
        while not body.endswith(PASTE_END):
            body.append(self._inner_byte())
            if len(body) > MAX_INPUT_BYTES + len(PASTE_END):
                self._drain_paste()
                raise InputRejected("the pasted value is too long")
        return bytes(body[: -len(PASTE_END)])

    def _drain_paste(self) -> None:
        tail = bytearray()
        while not tail.endswith(PASTE_END):
            tail.append(self._inner_byte())
            del tail[: -len(PASTE_END)]

    def read_hidden(self, prompt: str) -> str:
        """Read one hidden value. Enter submits; Ctrl+C or Ctrl+D cancels."""
        self.write(prompt.encode("utf-8"))
        value = bytearray()
        try:
            while True:
                byte = self._byte()
                if byte in CANCEL_BYTES:
                    raise InputCancelled("cancelled")
                if byte in ENTER_BYTES:
                    break
                if byte in ERASE_BYTES:
                    _erase_last_character(value)
                    continue
                if byte == KILL_LINE:
                    value.clear()
                    continue
                if byte == ESC:
                    sequence = self._escape_sequence()
                    if sequence == PASTE_START:
                        pasted = self._paste_body()
                        if b"\r" in pasted or b"\n" in pasted:
                            raise InputRejected(
                                "multiline values are not supported; nothing was saved"
                            )
                        value.extend(pasted)
                    # Other keys (arrows, function keys) do not edit the value.
                    continue
                if byte < 0x20:
                    # Typed control keys are not part of the value.
                    continue
                value.append(byte)
                if len(value) > MAX_INPUT_BYTES:
                    raise InputRejected("the value is too long")
        finally:
            self.write(b"\r\n")
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise InputRejected("the value is not valid UTF-8 text") from error

    def confirm(self, prompt: str) -> bool:
        """Ask a yes/no question that only typed input can answer.

        Input still waiting from the value step is discarded first. A paste
        at this prompt is consumed and counts as no. Only the letters of
        yes/no are echoed; any other character makes the answer no and is
        not shown.
        """
        self.discard_pending()
        self.write(prompt.encode("utf-8"))
        answer = bytearray()
        pasted = False
        other = False
        try:
            while True:
                byte = self._byte()
                if byte in CANCEL_BYTES:
                    return False
                if byte in ENTER_BYTES:
                    break
                if byte in ERASE_BYTES:
                    if answer:
                        answer.pop()
                        self.write(b"\b \b")
                    continue
                if byte == ESC:
                    if self._escape_sequence() == PASTE_START:
                        self._drain_paste()
                        pasted = True
                    continue
                if byte in CONFIRM_CHARACTERS and len(answer) < 3:
                    answer.append(byte)
                    self.write(bytes([byte]))
                elif byte >= 0x20:
                    # Includes a fourth letter: "yess" is not an answer.
                    other = True
        finally:
            self.write(b"\r\n")
        if pasted or other:
            return False
        return answer.decode("ascii").strip().lower() in {"y", "yes"}


def _erase_last_character(value: bytearray) -> None:
    # Remove one whole UTF-8 character: continuation bytes, then its lead byte.
    while value and 0x80 <= value[-1] <= 0xBF:
        value.pop()
    if value:
        value.pop()
