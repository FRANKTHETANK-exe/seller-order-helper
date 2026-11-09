"""Capability boundary for POSIX-only owner-private Sandbox artifacts.

Windows chmod/stat does not verify access control lists. Fail closed there until
an ACL-aware implementation is independently reviewed and tested.
"""
import os


def supports_private_files():
    return (os.name == "posix" and callable(getattr(os, "getuid", None))
            and callable(getattr(os, "fchmod", None)))
