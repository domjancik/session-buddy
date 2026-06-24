import threading

from session_search.resume import PreparedResumeCommand
from session_search.terminal import EmbeddedTerminalProcess


def test_embedded_terminal_process_streams_output() -> None:
    output: list[str] = []
    exited = threading.Event()
    exit_codes: list[int | None] = []
    command = PreparedResumeCommand(
        provider="test",
        session_id="echo",
        cwd="",
        argv=["/bin/echo", "hello-pane"],
        warnings=[],
    )

    process = EmbeddedTerminalProcess(
        command,
        on_output=output.append,
        on_exit=lambda code: (exit_codes.append(code), exited.set()),
    )

    process.start()

    assert exited.wait(timeout=5)
    assert exit_codes == [0]
    assert "hello-pane" in "".join(output)
