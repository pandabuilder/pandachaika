# -*- coding: utf-8 -*-
import logging
from logging.handlers import RotatingFileHandler, SMTPHandler

import cherrypy
from cherrypy import _cplogging, _cperror
from django.http import HttpResponseServerError


class HTTPLogger(_cplogging.LogManager):

    def __init__(self, app, crawler_settings):
        _cplogging.LogManager.__init__(
            self, id(self), cherrypy.log.logger_root)
        self.app = app
        max_bytes = getattr(self, "rot_maxBytes", 10000000)
        backup_count = getattr(self, "rot_backupCount", 1000)

        # Make a new RotatingFileHandler for the error log.
        file_name = getattr(self, "rot_error_file", "error.log")
        h = RotatingFileHandler(file_name, 'a', max_bytes, backup_count)
        h.setLevel(logging.DEBUG)
        h.setFormatter(_cplogging.logfmt)
        self.error_log.addHandler(h)

        if crawler_settings.webserver.write_access_log:
            # Make a new RotatingFileHandler for the access log.
            file_name = getattr(self, "rot_access_file", "access.log")
            h = RotatingFileHandler(file_name, 'a', max_bytes, backup_count)
            h.setLevel(logging.DEBUG)
            h.setFormatter(_cplogging.logfmt)
            self.access_log.addHandler(h)

        if crawler_settings.mail_logging.enable:
            h = SMTPHandler(
                crawler_settings.mail_logging.mailhost,
                crawler_settings.mail_logging.from_,
                crawler_settings.mail_logging.to,
                crawler_settings.mail_logging.credentials,
                crawler_settings.mail_logging.subject)
            h.setLevel(logging.CRITICAL)
            self.error_log.addHandler(h)

    def __call__(self, environ, start_response):
        """
        Called as part of the WSGI stack to log the incoming request
        and its response using the common log format. If an error bubbles up
        to this middleware, we log it as such.
        """
        try:
            response = self.app(environ, start_response)
            self.access(environ, response)
            return response
        except Exception:
            self.error(traceback=True, severity=logging.CRITICAL)
            return HttpResponseServerError(_cperror.format_exc())

    def access(self, environ, response):
        """
        Special method that logs a request following the common
        log format. This is mostly taken from CherryPy and adapted
        to the WSGI's style of passing information.
        """
        atoms = {'h': environ.get('REMOTE_ADDR', ''),
                 'l': '-',
                 'u': "-",
                 't': self.time(),
                 'r': "%s %s %s" % (environ['REQUEST_METHOD'],
                                    environ['REQUEST_URI'],
                                    environ['SERVER_PROTOCOL']),
                 's': response.status_code,
                 'b': str(len(response.content)),
                 'f': environ.get('HTTP_REFERER', ''),
                 'a': environ.get('HTTP_USER_AGENT', ''),
                 }
        for k, v in atoms.items():
            if not isinstance(v, str):
                v = str(v)
            # Fortunately, repr(str) escapes unprintable chars, \n, \t, etc
            # and backslash for us. All we have to do is strip the quotes.
            # v = repr(v)[1:-1]
            # Escape double-quote.
            atoms[k] = v.replace('"', '\\"')

        try:
            self.access_log.log(
                logging.INFO, self.access_log_format.format(**atoms))
        except Exception:
            self.error(traceback=True)
