"""Keep the command prompt and unfinished input visible below asynchronous ROS logs."""
import sys
from functools import partial

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.application.current import set_app


class CommandConsole:
    def __init__(self):
        self.session = None

    def read(self):
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return input('명령> ')
        if self.session is None:
            self.session = PromptSession('명령> ')
        return self.session.prompt()

    def output(self, callback):
        app = self.session.app if self.session is not None else None
        if app is None or not app.is_running or app.loop is None:
            callback()
            return

        def show():
            if not app.is_running:
                callback()
                return
            with set_app(app):
                run_in_terminal(callback)
        try:
            app.loop.call_soon_threadsafe(show)
        except RuntimeError:
            callback()

    def write(self, message):
        self.output(partial(print, message, flush=True))

    def close(self):
        app = self.session.app if self.session is not None else None
        if app is not None and app.is_running and app.loop is not None:
            def finish():
                if app.is_running and not app.is_done:
                    app.exit(exception=EOFError())
            try:
                app.loop.call_soon_threadsafe(finish)
            except RuntimeError:
                pass


console = CommandConsole()


class PromptLogger:
    def __init__(self, logger):
        self.logger = logger

    def info(self, message):
        console.output(lambda: self.logger.info(message))

    def warning(self, message):
        console.output(lambda: self.logger.warning(message))

    def error(self, message):
        console.output(lambda: self.logger.error(message))
