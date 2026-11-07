"""Shared key derivation, so every purpose that needs a secret derived
from SESSION_SECRET (session-cookie signing, encrypting stored database
passwords, encrypting the stored AI API key, ...) gets its own
independently-derived subkey via HKDF with a distinct `purpose` label,
instead of every purpose reusing the exact same sha256(SESSION_SECRET)
bytes. Domain separation like this is standard key-derivation practice
(NIST SP 800-108 / RFC 5869's whole reason for the "info" parameter): a
weakness or leak specific to one consumer's derived key shouldn't hand
over the others for free.

This does NOT remove SESSION_SECRET itself as a single root of trust --
doing that for real needs an external KMS/Vault-backed key source, which
is a bigger change than this. What it does fix is every purpose silently
sharing one literal derived value.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import settings


def derive_key(purpose: str, length: int = 32) -> bytes:
    if not settings.SESSION_SECRET:
        raise RuntimeError("SESSION_SECRET is not configured")
    hkdf = HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=f"pgmon:{purpose}".encode("utf-8"))
    return hkdf.derive(settings.SESSION_SECRET.encode("utf-8"))


def derive_fernet_key(purpose: str) -> bytes:
    """32 HKDF-derived bytes, base64-urlsafe-encoded as Fernet requires."""
    return base64.urlsafe_b64encode(derive_key(purpose))


def legacy_fernet_key() -> bytes:
    """The pre-domain-separation derivation (plain sha256(SESSION_SECRET),
    reused verbatim for every purpose) -- kept only so a Fernet token
    written before this module existed still decrypts, instead of every
    already-registered database's stored password (or a stored AI key)
    silently breaking the moment this ships. Never used for new
    encryptions; only as a fallback when the current-purpose key fails to
    decrypt a value stored under the old scheme."""
    if not settings.SESSION_SECRET:
        raise RuntimeError("SESSION_SECRET is not configured")
    return base64.urlsafe_b64encode(hashlib.sha256(settings.SESSION_SECRET.encode("utf-8")).digest())
