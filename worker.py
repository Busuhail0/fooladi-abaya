"""Cloudflare Python Worker entrypoint; no local persistent filesystem."""
from flask import Response, abort
from jinja2 import DictLoader
from workers import wsgi

from app import create_app
from bundled_assets import TEMPLATES, STATIC

app=create_app(cloud=True)
app.jinja_loader=DictLoader(TEMPLATES)

def static_asset(filename):
    asset=STATIC.get(filename)
    if not asset:abort(404)
    body,mime=asset
    return Response(body,content_type=mime)

app.view_functions['static']=static_asset
Default=wsgi.entrypoint(app)
