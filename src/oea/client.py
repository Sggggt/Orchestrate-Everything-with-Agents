"""Connect to an existing host; importing this module never starts a Runtime."""

from ma_runtime.client import PlatformClient
from ma_runtime.errors import RuntimeFault

__all__ = ["PlatformClient", "RuntimeFault"]
