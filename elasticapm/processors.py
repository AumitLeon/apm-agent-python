#  BSD 3-Clause License
#
#  Copyright (c) 2012, the Sentry Team, see AUTHORS for more details
#  Copyright (c) 2019, Elasticsearch BV
#  All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#
#  * Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
#
#  * Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
#  * Neither the name of the copyright holder nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
#  DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
#  FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
#  DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
#  SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
#  CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
#  OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE


import re
import warnings
from collections import defaultdict

from elasticapm.conf.constants import ERROR, MASK, SPAN, TRANSACTION
from elasticapm.utils import compat, varmap
from elasticapm.utils.encoding import force_text
from elasticapm.utils.stacks import get_lines_from_file

SANITIZE_VALUE_PATTERNS = [re.compile(r"^[- \d]{16,19}$")]  # credit card numbers, with or without spacers


class Processors:
    def __init__(self, headers):
        self.base = ["authorization", "password", "secret", "passwd", "token", "api_key", "access_token", "sessionid"]
        self._sanitize_field_names = frozenset(headers + self.base)

    def get_processors(self):
        # these should be based on the processors passed.
        return [
            self.remove_http_request_body,
            self.remove_stacktrace_locals,
            self.remove_http_request_body,
            self.sanitize_http_headers,
            self.sanitize_http_request_body,
            self.sanitize_http_request_cookies,
            self.sanitize_http_request_querystring,
            self.sanitize_http_wsgi_env,
            self.sanitize_stacktrace_locals,
        ]

    def for_events(self, *events):
        """
        :param events: list of event types

        Only calls wrapped function if given event_type is in list of events
        """
        events = set(events)

        def wrap(func):
            func.event_types = events
            return func

        return wrap

    @for_events(ERROR, TRANSACTION)
    def remove_http_request_body(self, client, event):
        """
        Removes request.body from context

        :param client: an ElasticAPM client
        :param event: a transaction or error event
        :return: The modified event
        """
        if "context" in event and "request" in event["context"]:
            event["context"]["request"].pop("body", None)
        return event

    @for_events(ERROR, SPAN)
    def remove_stacktrace_locals(self, client, event):
        """
        Removes local variables from any frames.

        :param client: an ElasticAPM client
        :param event: a transaction or error event
        :return: The modified event
        """
        func = lambda frame: frame.pop("vars", None)
        return self._process_stack_frames(event, func)

    @for_events(ERROR, SPAN)
    def sanitize_stacktrace_locals(self, client, event):
        """
        Sanitizes local variables in all frames

        :param client: an ElasticAPM client
        :param event: a transaction or error event
        :return: The modified event
        """

        def func(frame):
            if "vars" in frame:
                frame["vars"] = varmap(self._sanitize, frame["vars"])

        return self._process_stack_frames(event, func)

    @for_events(ERROR, TRANSACTION)
    def sanitize_http_request_cookies(self, client, event):
        """
        Sanitizes http request cookies

        :param client: an ElasticAPM client
        :param event: a transaction or error event
        :return: The modified event
        """

        # sanitize request.cookies dict
        try:
            cookies = event["context"]["request"]["cookies"]
            event["context"]["request"]["cookies"] = varmap(self._sanitize, cookies)
        except (KeyError, TypeError):
            pass

        # sanitize request.header.cookie string
        try:
            cookie_string = event["context"]["request"]["headers"]["cookie"]
            event["context"]["request"]["headers"]["cookie"] = self._sanitize_string(cookie_string, "; ", "=")
        except (KeyError, TypeError):
            pass
        return event

    @for_events(ERROR, TRANSACTION)
    def sanitize_http_response_cookies(self, client, event):
        """
        Sanitizes the set-cookie header of the response
        :param client: an ElasticAPM client
        :param event: a transaction or error event
        :return: The modified event
        """
        try:
            cookie_string = event["context"]["response"]["headers"]["set-cookie"]
            event["context"]["response"]["headers"]["set-cookie"] = self._sanitize_string(cookie_string, ";", "=")
        except (KeyError, TypeError):
            pass
        return event

    @for_events(ERROR, TRANSACTION)
    def sanitize_http_headers(self, client, event):
        """
        Sanitizes http request/response headers

        :param client: an ElasticAPM client
        :param event: a transaction or error event
        :return: The modified event
        """
        # request headers
        try:
            headers = event["context"]["request"]["headers"]
            event["context"]["request"]["headers"] = varmap(self._sanitize, headers)
        except (KeyError, TypeError):
            pass

        # response headers
        try:
            headers = event["context"]["response"]["headers"]
            event["context"]["response"]["headers"] = varmap(self._sanitize, headers)
        except (KeyError, TypeError):
            pass

        return event

    @for_events(ERROR, TRANSACTION)
    def sanitize_http_wsgi_env(self, client, event):
        """
        Sanitizes WSGI environment variables

        :param client: an ElasticAPM client
        :param event: a transaction or error event
        :return: The modified event
        """
        try:
            env = event["context"]["request"]["env"]
            event["context"]["request"]["env"] = varmap(self._sanitize, env)
        except (KeyError, TypeError):
            pass
        return event

    @for_events(ERROR, TRANSACTION)
    def sanitize_http_request_querystring(self, client, event):
        """
        Sanitizes http request query string

        :param client: an ElasticAPM client
        :param event: a transaction or error event
        :return: The modified event
        """
        try:
            query_string = force_text(event["context"]["request"]["url"]["search"], errors="replace")
        except (KeyError, TypeError):
            return event
        if "=" in query_string:
            sanitized_query_string = self._sanitize_string(query_string, "&", "=")
            full_url = event["context"]["request"]["url"]["full"]
            event["context"]["request"]["url"]["search"] = sanitized_query_string
            event["context"]["request"]["url"]["full"] = full_url.replace(query_string, sanitized_query_string)
        return event

    @for_events(ERROR, TRANSACTION)
    def sanitize_http_request_body(self, client, event):
        """
        Sanitizes http request body. This only works if the request body
        is a query-encoded string. Other types (e.g. JSON) are not handled by
        this sanitizer.

        :param client: an ElasticAPM client
        :param event: a transaction or error event
        :return: The modified event
        """
        try:
            body = force_text(event["context"]["request"]["body"], errors="replace")
        except (KeyError, TypeError):
            return event
        if "=" in body:
            sanitized_query_string = self._sanitize_string(body, "&", "=")
            event["context"]["request"]["body"] = sanitized_query_string
        return event

    @for_events(ERROR, SPAN)
    def add_context_lines_to_frames(self, client, event):
        # divide frames up into source files before reading from disk. This should help
        # with utilizing the disk cache better
        #
        # TODO: further optimize by only opening each file once and reading all needed source
        # TODO: blocks at once.
        per_file = defaultdict(list)
        self._process_stack_frames(
            event,
            lambda frame: per_file[frame["context_metadata"][0]].append(frame) if "context_metadata" in frame else None,
        )
        for filename, frames in compat.iteritems(per_file):
            for frame in frames:
                # context_metadata key has been set in elasticapm.utils.stacks.get_frame_info for
                # all frames for which we should gather source code context lines
                fname, lineno, context_lines, loader, module_name = frame.pop("context_metadata")
                pre_context, context_line, post_context = get_lines_from_file(
                    fname, lineno, context_lines, loader, module_name
                )
                if context_line:
                    frame["pre_context"] = pre_context
                    frame["context_line"] = context_line
                    frame["post_context"] = post_context
        return event

    @for_events(ERROR, SPAN)
    def mark_in_app_frames(self, client, event):
        warnings.warn(
            "The mark_in_app_frames processor is deprecated and can be removed from your PROCESSORS setting",
            DeprecationWarning,
        )
        return event

    def _sanitize(self, key, value):
        if value is None:
            return

        if isinstance(value, compat.string_types) and any(pattern.match(value) for pattern in SANITIZE_VALUE_PATTERNS):
            return MASK

        if isinstance(value, dict):
            # varmap will call _sanitize on each k:v pair of the dict, so we don't
            # have to do anything with dicts here
            return value

        if not key:  # key can be a NoneType
            return value

        key = key.lower()
        for field in self._sanitize_field_names:
            if field in key:
                # store mask as a fixed length for security
                return MASK
        return value

    def _sanitize_string(self, unsanitized, itemsep, kvsep):
        """
        sanitizes a string that contains multiple key/value items
        :param unsanitized: the unsanitized string
        :param itemsep: string that separates items
        :param kvsep: string that separates key from value
        :return: a sanitized string
        """
        sanitized = []
        kvs = unsanitized.split(itemsep)
        for kv in kvs:
            kv = kv.split(kvsep)
            if len(kv) == 2:
                sanitized.append((kv[0], self._sanitize(kv[0], kv[1])))
            else:
                sanitized.append(kv)
        return itemsep.join(kvsep.join(kv) for kv in sanitized)

    def _process_stack_frames(self, event, func):
        if "stacktrace" in event:
            for frame in event["stacktrace"]:
                func(frame)
        # an error can have two stacktraces, one in "exception", one in "log"
        if "exception" in event and "stacktrace" in event["exception"]:
            for frame in event["exception"]["stacktrace"]:
                func(frame)
            # check for chained exceptions
            cause = event["exception"].get("cause", None)
            while cause:
                if "stacktrace" in cause[0]:
                    for frame in cause[0]["stacktrace"]:
                        func(frame)
                cause = cause[0].get("cause", None)
        if "log" in event and "stacktrace" in event["log"]:
            for frame in event["log"]["stacktrace"]:
                func(frame)
        return event
