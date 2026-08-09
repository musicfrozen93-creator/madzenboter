"""REST API layer — the only way the website talks to the analysis backend.

Transport only: validate the request, resolve a provider, run the pipeline,
serialize the result. No technical analysis lives here.
"""

from api.app import create_app

__all__ = ['create_app']
