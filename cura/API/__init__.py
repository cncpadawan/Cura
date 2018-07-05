# Copyright (c) 2018 Ultimaker B.V.
# Cura is released under the terms of the LGPLv3 or higher.
from UM.PluginRegistry import PluginRegistry
from cura.API.Backups import Backups


class CuraAPI:
    """
    The official Cura API that plugins can use to interact with Cura.
    Python does not technically prevent talking to other classes as well,
    but this API provides a version-safe interface with proper deprecation warnings etc.
    Usage of any other methods than the ones provided in this API can cause plugins to be unstable.
    """

    # For now we use the same API version to be consistent.
    VERSION = PluginRegistry.APIVersion

    # Backups API.
    backups = Backups()
