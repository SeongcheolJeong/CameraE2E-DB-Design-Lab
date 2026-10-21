"""Read-only registry reuse scoped to a single domain operation.

This object must not be stored on a service or returned to a client. Immutable
records and verified blobs are checked on their first use in this operation;
the next operation starts fresh, including mutable job completion state.
"""

from __future__ import annotations

import copy


class OperationRegistry:
    def __init__(self, registry):
        self.base = registry
        self._records = {}
        self._jobs = {}

    def __getattr__(self, name):
        if name.startswith(("put", "store", "submit", "publish", "finish", "claim",
                            "cancel", "recover", "transaction", "index")):
            raise ValueError("Evidence read operation cannot mutate the registry")
        return getattr(self.base, name)

    def get(self, kind, identity):
        key = kind, identity
        if key not in self._records:
            self._records[key] = self.base.get(kind, identity)
        return copy.deepcopy(self._records[key])

    def get_run(self, identity):
        if identity not in self._jobs:
            self._jobs[identity] = self.base.get_run(identity)
        return copy.deepcopy(self._jobs[identity])

    def blob_path(self, checksum):
        # A Path is not an immutable byte snapshot. Reusing a previously checked
        # path could read changed bytes under an old checksum in this operation.
        return self.base.blob_path(checksum)


def operation_service(service):
    local = copy.copy(service)
    local.registry = OperationRegistry(service.registry)
    return local
