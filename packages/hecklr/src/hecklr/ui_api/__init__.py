"""The local UI's server (``hecklr ui``): a read-only results API plus labels and approvals.

``create_app(project, user=...)`` returns the FastAPI application; see ADR 0016a.
"""

from hecklr.ui_api.app import STATIC_DIR, create_app

__all__ = ["STATIC_DIR", "create_app"]
