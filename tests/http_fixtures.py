"""Realistic buffered HTML responses used by offline scraper tests."""
import requests

def html_response(*, text, url):
    response = requests.Response()
    response.status_code = 200
    response.url = url
    response.encoding = "utf-8"
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    response._content = text.encode("utf-8")
    response._content_consumed = True
    return response


def inspected_hls_failure(downloader, url, message):
    """Set the inspected-input precondition when faking a later backend failure."""
    def fail(*_args, **_kwargs):
        from src.downloader import _HlsFallbackError
        text = "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nclip.ts\n#EXT-X-ENDLIST\n"
        downloader._remember_hls_snapshot(url, url, text)
        raise _HlsFallbackError(message, url)
    return fail
