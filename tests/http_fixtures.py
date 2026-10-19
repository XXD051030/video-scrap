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
