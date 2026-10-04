"""
The web frontend: FastAPI serving server-rendered pages, with HTMX
swapping parts of a page in place. All scheduling logic lives in
scheduler/; routes here read a form, call one scheduler function, and
render a template.
"""
