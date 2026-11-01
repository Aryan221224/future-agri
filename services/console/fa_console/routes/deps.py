"""Shared request helpers."""
from fastapi import Request


def state(request: Request):
    return request.app.state
