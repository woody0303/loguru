import json
import multiprocessing
import random
import re
import sys
import threading
import traceback

import ansimarkup
from better_exceptions_fork import ExceptionFormatter


class HackyInt(int):

    rand = "{:0>32}".format(random.randrange(10**31))

    def __str__(self):
        return self.rand

    def __eq__(self, other):
        return False


class StrRecord(str):
    __slots__ = ('record', )


class Handler:

    def __init__(self, *, writer, stopper, levelno, format_, filter_, colored, structured, enhanced, wrapped, queued, colors=[]):
        self.writer = writer
        self.stopper = stopper
        self.levelno = levelno
        self.format = format_
        self.filter = filter_
        self.colored = colored
        self.structured = structured
        self.enhanced = enhanced
        self.wrapped = wrapped
        self.queued = queued
        self.decolorized_format = self.decolorize(format_)
        self.precolorized_formats = {}
        self.lock = threading.Lock()
        self.queue = None
        self.thread = None
        self.exception_formatter = ExceptionFormatter(colored=colored)

        if colored:
            for color in colors:
                self.update_format(color)

        if queued:
            self.queue = multiprocessing.SimpleQueue()
            self.thread = threading.Thread(target=self.queued_writer, daemon=True)
            self.thread.start()

    @staticmethod
    def serialize(text, record):
        exception = record['exception']
        if exception:
            etype, value, _ = record['exception']
            exception = ''.join(traceback.format_exception_only(etype, value))

        serializable = {
            'text': text,
            'record': {
                'elapsed': record['elapsed'].total_seconds(),
                'exception': exception,
                'extra': record['extra'],
                'file': dict(name=record['file'].name, path=record['file'].path),
                'function': record['function'],
                'level': dict(icon=record['level'].icon, name=record['level'].name, no=record['level'].no),
                'line': record['line'],
                'message': record['message'],
                'module': record['module'],
                'name': record['name'],
                'process': dict(id=record['process'].id, name=record['process'].name),
                'thread': dict(id=record['thread'].id, name=record['thread'].name),
                'time': record['time'].float_timestamp,
            }
        }

        return json.dumps(serializable, default=str)

    @staticmethod
    def make_ansimarkup(color):
        color = ansimarkup.parse(color)
        custom_markup = dict(level=color, lvl=color)
        am = ansimarkup.AnsiMarkup(tags=custom_markup)
        return am

    @staticmethod
    def decolorize(format_):
        am = Handler.make_ansimarkup('')
        return am.strip(format_)

    @staticmethod
    def colorize(format_, color):
        am = Handler.make_ansimarkup(color)
        return am.parse(format_)

    def update_format(self, color):
        if not self.colored or color in self.precolorized_formats:
            return
        self.precolorized_formats[color] = self.colorize(self.format, color)

    def handle_error(self, record=None):
        if not self.wrapped:
            raise

        if not sys.stderr:
            return

        ex_type, ex, tb = sys.exc_info()

        try:
            sys.stderr.write('--- Logging error in Loguru ---\n')
            sys.stderr.write('Record was: ')
            try:
                sys.stderr.write(str(record))
            except Exception:
                sys.stderr.write('/!\\ Unprintable record /!\\')
            sys.stderr.write('\n')
            traceback.print_exception(ex_type, ex, tb, None, sys.stderr)
            sys.stderr.write('--- End of logging error ---\n')
        except OSError:
            pass
        finally:
            del ex_type, ex, tb

    def emit(self, record, level_color=None, ansi_message=False):
        try:
            if self.levelno > record['level'].no:
                return

            if self.filter is not None:
                if not self.filter(record):
                    return

            if ansi_message:
                preformatted_message = self.format.format_map(record)

                if self.colored:
                    formatted = self.colorize(preformatted_message, level_color)
                else:
                    formatted = self.decolorize(preformatted_message)
            else:
                if self.colored:
                    precomputed_format = self.precolorized_formats[level_color]
                else:
                    precomputed_format = self.decolorized_format

                formatted = precomputed_format.format_map(record)

            formatted += '\n'

            exception = record['exception']

            if exception:
                hacky_int = None
                ex_type, ex, tb = exception
                tb_ = tb

                while tb_:
                    if tb_.__is_caught_point__:
                        hacky_int = HackyInt(tb_.tb_lineno)
                        tb_.tb_lineno = hacky_int
                        break
                    tb_ = tb_.tb_next

                if self.enhanced:
                    formatted_exc = self.exception_formatter.format_exception(ex_type, ex, tb)
                else:
                    formatted_exc = traceback.format_exception(ex_type, ex, tb)

                formatted_exc = ''.join(formatted_exc)

                reg = r'(?:^\S*(Traceback \(most recent call last\):)\S*$|^\S*  \S*File.*\D(%s)\D.*$)' % str(hacky_int)
                matches = re.finditer(reg, formatted_exc, flags=re.M)

                tb_match = None

                for match in matches:
                    tb, line = match.groups()
                    if tb is not None:
                        tb_match = match
                    if line is not None:
                        s, e = match.span(2)
                        formatted_exc = formatted_exc[:s] + str(int(hacky_int)) + formatted_exc[e:]
                        s = match.start(0)
                        formatted_exc = formatted_exc[:s] + formatted_exc[s:].replace(" ", ">", 1)
                        if tb_match is not None:
                            old = "Traceback (most recent call last):"
                            new = "Traceback (most recent call last, catch point marked):"
                            s = tb_match.start(0)
                            formatted_exc = formatted_exc[:s] + formatted_exc[s:].replace(old, new, 1)
                        break

                formatted += formatted_exc

                if self.queued:
                    record['exception'] = (ex_type, ex, None)  # tb are not pickable

            if self.structured:
                formatted = self.serialize(formatted, record) + '\n'

            message = StrRecord(formatted)
            message.record = record

            with self.lock:
                if self.queued:
                    self.queue.put(message)
                else:
                    self.writer(message)

        except Exception:
            self.handle_error(record)

    def queued_writer(self):
        message = None
        queue = self.queue
        try:
            while 1:
                message = queue.get()
                if message is None:
                    break
                self.writer(message)
        except Exception:
            if message and hasattr(message, 'record'):
                message = message.record
            self.handle_error(message)

    def stop(self):
        if self.queued:
            self.queue.put(None)
            self.thread.join()
        self.stopper()
