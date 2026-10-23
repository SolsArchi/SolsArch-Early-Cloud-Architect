#!/usr/bin/env python3
"""The one way this tool makes an outbound web request.

Every request the tool makes goes through OPENER, which follows a redirect only when:
  * the new address is https (a page cannot move the reader to plain http or to file:, ftp: and the like), and
  * it stays on the same host whenever the request carries a credential, so a token meant for one vendor's API is never forwarded to
    another site that the first one redirected to.
Standard library only.
"""
import urllib.error
import urllib.parse
import urllib.request

LOOPBACK = ("localhost", "127.0.0.1", "::1")


def checked(url, allow_loopback_http=False):
    """The address, or ValueError. Only https is opened; plain http is allowed solely for a model server running on this machine."""
    parts = urllib.parse.urlparse(str(url))
    if parts.scheme == "https" and parts.hostname:
        return url
    if allow_loopback_http and parts.scheme == "http" and parts.hostname in LOOPBACK:
        return url
    raise ValueError("only https addresses are opened (got %r)" % (parts.scheme or "no scheme"))


CREDENTIAL_HEADERS = ("authorization", "proxy-authorization", "x-api-key", "cookie")


class SafeRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlparse(newurl)
        if target.scheme != "https":
            raise urllib.error.URLError("refusing a redirect to %s: only https is followed" % (newurl.split("?")[0][:80]))
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and target.hostname != urllib.parse.urlparse(req.full_url).hostname:
            for name in list(new.headers) + list(new.unredirected_hdrs):
                if name.lower() in CREDENTIAL_HEADERS:
                    new.headers.pop(name, None)
                    new.unredirected_hdrs.pop(name, None)
        return new


OPENER = urllib.request.build_opener(SafeRedirects)


urllib.request.install_opener(OPENER)  # plain urllib.request.urlopen(...) calls now follow the same rules
