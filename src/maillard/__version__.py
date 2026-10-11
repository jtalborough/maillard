"""Maillard version information.

Version format: YYYY.MM.DD[+gitsha]
Set at build time via CI; defaults to 'dev' for local development.
"""

import os

VERSION: str = os.environ.get("APP_VERSION", "dev")
GIT_SHA: str = os.environ.get("GIT_SHA", "unknown")
BUILD_DATE: str = os.environ.get("BUILD_DATE", "")
