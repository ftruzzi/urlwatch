# -*- coding: utf-8 -*-
#
# This file is part of urlwatch (https://thp.io/2008/urlwatch/).
# Copyright (c) 2008-2018 Thomas Perl <m@thp.io>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
# 3. The name of the author may not be used to endorse or promote products
#    derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES
# OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
# IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT
# NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF
# THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


import email.utils
import hashlib
import logging
import os
import re
import subprocess
import asyncio
import requests
import urlwatch
from requests.packages.urllib3.exceptions import InsecureRequestWarning

from .util import TrackSubClasses

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

logger = logging.getLogger(__name__)


class ShellError(Exception):
    """Exception for shell commands with non-zero exit code"""

    def __init__(self, result):
        Exception.__init__(self)
        self.result = result

    def __str__(self):
        return '%s: Exit status %d' % (self.__class__.__name__, self.result)


class NotModifiedError(Exception):
    """Exception raised on HTTP 304 responses"""
    ...


class JobBase(object, metaclass=TrackSubClasses):
    __subclasses__ = {}

    __required__ = ()
    __optional__ = ()

    def __init__(self, **kwargs):
        # Set optional keys to None
        for k in self.__optional__:
            if k not in kwargs:
                setattr(self, k, None)

        # Fail if any required keys are not provided
        for k in self.__required__:
            if k not in kwargs:
                raise ValueError('Required field %s missing: %r' % (k, kwargs))

        for k, v in list(kwargs.items()):
            setattr(self, k, v)

    @classmethod
    def job_documentation(cls):
        result = []
        for sc in TrackSubClasses.sorted_by_kind(cls):
            result.extend((
                '  * %s - %s' % (sc.__kind__, sc.__doc__),
                '    Required keys: %s' % (', '.join(sc.__required__),),
                '    Optional keys: %s' % (', '.join(sc.__optional__),),
                '',
            ))
        return '\n'.join(result)

    def get_location(self):
        raise NotImplementedError()

    def pretty_name(self):
        raise NotImplementedError()

    def serialize(self):
        d = {'kind': self.__kind__}
        d.update(self.to_dict())
        return d

    @classmethod
    def unserialize(cls, data):
        if 'kind' not in data:
            # Try to auto-detect the kind of job based on the available keys
            kinds = [subclass.__kind__ for subclass in list(cls.__subclasses__.values())
                     if all(required in data for required in subclass.__required__) and not any(
                     key not in subclass.__required__ and key not in subclass.__optional__ for key in data)]

            if len(kinds) == 1:
                kind = kinds[0]
            elif len(kinds) == 0:
                raise ValueError('Kind is not specified, and no job matches: %r' % (data,))
            else:
                raise ValueError('Multiple kinds of jobs match %r: %r' % (data, kinds))
        else:
            kind = data['kind']

        return cls.__subclasses__[kind].from_dict(data)

    def to_dict(self):
        return {k: getattr(self, k) for keys in (self.__required__, self.__optional__) for k in keys
                if getattr(self, k) is not None}

    @classmethod
    def from_dict(cls, data):
        return cls(**{k: v for k, v in list(data.items()) if k in cls.__required__ or k in cls.__optional__})

    def __repr__(self):
        return '<%s %s>' % (self.__kind__, ' '.join('%s=%r' % (k, v) for k, v in list(self.to_dict().items())))

    def get_guid(self):
        location = self.get_location()
        sha_hash = hashlib.new('sha1')
        sha_hash.update(location.encode('utf-8'))
        return sha_hash.hexdigest()

    def retrieve(self, job_state):
        raise NotImplementedError()


class Job(JobBase):
    __required__ = ()
    __optional__ = ('name', 'filter', 'max_tries', 'diff_tool')

    # determine if hyperlink "a" tag is used in HtmlReporter
    LOCATION_IS_URL = False

    def pretty_name(self):
        return self.name if self.name else self.get_location()


class AsyncJob(Job):
    """Async job that uses asyncio and requires an event loop"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.loop = None

    def setup(self, loop):
        """Set the event loop and run other stuff on the main thread before multithreaded job processing"""
        self.loop = loop

    def retrieve(self, job_state):
        """
        Check that the event loop is set and running.
        Subclasses should call this method (or implement similar checking) before
        scheduling coroutines on the event loop to prevent deadlock.
        """
        if not self.loop:
            raise RuntimeError('Event loop not set up')
        if not self.loop.is_running():
            raise RuntimeError("The event loop must be running when `retrieve` is called on an AsyncJob")

    def cleanup(self):
        """Called after multithreaded job processing. The event loop should have stopped by this point."""
        if not self.loop:
            raise RuntimeError('`setup` must be called before `cleanup`')
        if self.loop.is_running():
            raise RuntimeError('The event loop should have stopped before `cleanup` is called')


class ShellJob(Job):
    """Run a shell command and get its standard output"""

    __kind__ = 'shell'

    __required__ = ('command',)
    __optional__ = ()

    def get_location(self):
        return self.command

    def retrieve(self, job_state):
        process = subprocess.Popen(self.command, stdout=subprocess.PIPE, shell=True)
        stdout_data, stderr_data = process.communicate()
        result = process.wait()
        if result != 0:
            raise ShellError(result)

        return stdout_data.decode('utf-8')


class UrlJob(Job):
    """Retrieve an URL from a web server"""

    __kind__ = 'url'

    __required__ = ('url',)
    __optional__ = ('cookies', 'data', 'method', 'ssl_no_verify', 'ignore_cached', 'http_proxy', 'https_proxy',
                    'headers', 'ignore_connection_errors')

    LOCATION_IS_URL = True
    CHARSET_RE = re.compile('text/(html|plain); charset=([^;]*)')

    def get_location(self):
        return self.url

    def retrieve(self, job_state):
        headers = {
            'User-agent': urlwatch.__user_agent__,
        }

        proxies = {
            'http': os.getenv('HTTP_PROXY'),
            'https': os.getenv('HTTPS_PROXY'),
        }

        if job_state.etag is not None:
            headers['If-None-Match'] = job_state.etag

        if job_state.timestamp is not None:
            headers['If-Modified-Since'] = email.utils.formatdate(job_state.timestamp)

        if self.ignore_cached:
            headers['If-None-Match'] = None
            headers['If-Modified-Since'] = email.utils.formatdate(0)
            headers['Cache-Control'] = 'max-age=172800'
            headers['Expires'] = email.utils.formatdate()

        if self.method is None:
            self.method = "GET"
        if self.data is not None:
            self.method = "POST"
            headers['Content-type'] = 'application/x-www-form-urlencoded'
            logger.info('Sending POST request to %s', self.url)

        if self.http_proxy is not None:
            proxies['http'] = self.http_proxy
        if self.https_proxy is not None:
            proxies['https'] = self.https_proxy

        file_scheme = 'file://'
        if self.url.startswith(file_scheme):
            logger.info('Using local filesystem (%s URI scheme)', file_scheme)
            return open(self.url[len(file_scheme):], 'rt').read()

        if self.headers:
            self.add_custom_headers(headers)

        response = requests.request(url=self.url,
                                    data=self.data,
                                    headers=headers,
                                    method=self.method,
                                    verify=(not self.ssl_no_verify),
                                    cookies=self.cookies,
                                    proxies=proxies)

        response.raise_for_status()
        if response.status_code == requests.codes.not_modified:
            raise NotModifiedError()

        # Save ETag from response into job_state, which will be saved in cache
        job_state.etag = response.headers.get('ETag')

        # If we can't find the encoding in the headers, requests gets all
        # old-RFC-y and assumes ISO-8859-1 instead of UTF-8. Use the old
        # urlwatch behavior and try UTF-8 decoding first.
        content_type = response.headers.get('Content-type', '')
        content_type_match = self.CHARSET_RE.match(content_type)
        if not content_type_match:
            try:
                try:
                    try:
                        return response.content.decode('utf-8')
                    except UnicodeDecodeError:
                        return response.content.decode('latin1')
                except UnicodeDecodeError:
                    return response.content.decode('utf-8', 'ignore')
            except LookupError:
                # If this is an invalid encoding, decode as ascii (Debian bug 731931)
                return response.content.decode('ascii', 'ignore')

        return response.text

    def add_custom_headers(self, headers):
        """
        Adds custom request headers from the job list (URLs) to the pre-filled dictionary `headers`.
        Pre-filled values of conflicting header keys (case-insensitive) are overwritten by custom value.
        """
        headers_to_remove = [x for x in headers if x.lower() in [y.lower() for y in self.headers]]
        for header in headers_to_remove:
            headers.pop(header, None)
        headers.update(self.headers)


class BrowserJob(AsyncJob):
    """Retrieve an URL, emulating a real web browser"""

    __kind__ = 'browser'

    __required__ = ('navigate',)

    LOCATION_IS_URL = True
    browser = None

    def setup(self, loop):
        super().setup(loop)
        # Launch the browser if not already. All BrowserJob instances share the same browser instance
        if not BrowserJob.browser:
            import pyppeteer
            BrowserJob.browser = self.loop.run_until_complete(pyppeteer.launch())

    def get_location(self):
        return self.navigate

    @classmethod
    @asyncio.coroutine
    def _render(cls, url):
        context = yield from cls.browser.createIncognitoBrowserContext()
        page = yield from context.newPage()
        yield from page.goto(url)
        content = yield from page.content()
        yield from context.close()
        return content

    def retrieve(self, job_state):
        super().retrieve(job_state)
        return asyncio.run_coroutine_threadsafe(BrowserJob._render(self.navigate), self.loop).result()

    def cleanup(self):
        super().cleanup()
        if BrowserJob.browser:
            self.loop.run_until_complete(BrowserJob.browser.close())
            BrowserJob.browser = None
