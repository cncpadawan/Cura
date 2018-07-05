# Copyright (c) 2018 Ultimaker B.V.
# Cura is released under the terms of the LGPLv3 or higher.

from typing import Optional, Dict, Any
import json
import os
import shutil
import zipfile
import tempfile

from PyQt5.QtCore import pyqtSlot, QObject, pyqtSignal, QUrl

from UM.Application import Application
from UM.Logger import Logger
from UM.Resources import Resources
from UM.Version import Version


class CuraPackageManager(QObject):
    Version = 1

    def __init__(self, parent = None):
        super().__init__(parent)

        self._application = Application.getInstance()
        self._container_registry = self._application.getContainerRegistry()
        self._plugin_registry = self._application.getPluginRegistry()

        #JSON files that keep track of all installed packages.
        self._user_package_management_file_path = None
        self._bundled_package_management_file_path = None
        for search_path in Resources.getSearchPaths():
            candidate_bundled_path = os.path.join(search_path, "bundled_packages.json")
            if os.path.exists(candidate_bundled_path):
                self._bundled_package_management_file_path = candidate_bundled_path
        for search_path in (Resources.getDataStoragePath(), Resources.getConfigStoragePath()):
            candidate_user_path = os.path.join(search_path, "packages.json")
            if os.path.exists(candidate_user_path):
                self._user_package_management_file_path = candidate_user_path
        if self._user_package_management_file_path is None: #Doesn't exist yet.
            self._user_package_management_file_path = os.path.join(Resources.getDataStoragePath(), "packages.json")

        self._bundled_package_dict = {}     # A dict of all bundled packages
        self._installed_package_dict = {}   # A dict of all installed packages
        self._to_remove_package_set = set() # A set of packages that need to be removed at the next start
        self._to_install_package_dict = {}  # A dict of packages that need to be installed at the next start

    installedPackagesChanged = pyqtSignal() # Emitted whenever the installed packages collection have been changed.

    def initialize(self):
        self._loadManagementData()
        self._removeAllScheduledPackages()
        self._installAllScheduledPackages()

    # (for initialize) Loads the package management file if exists
    def _loadManagementData(self) -> None:
        # The bundles package management file should always be there
        if not os.path.exists(self._bundled_package_management_file_path):
            Logger.log("w", "Bundled package management file could not be found!")
            return
        # Load the bundled packages:
        with open(self._bundled_package_management_file_path, "r", encoding = "utf-8") as f:
            self._bundled_package_dict = json.load(f, encoding = "utf-8")
            Logger.log("i", "Loaded bundled packages data from %s", self._bundled_package_management_file_path)

        # Load the user package management file
        if not os.path.exists(self._user_package_management_file_path):
            Logger.log("i", "User package management file %s doesn't exist, do nothing", self._user_package_management_file_path)
            return

        # Need to use the file lock here to prevent concurrent I/O from other processes/threads
        container_registry = self._application.getContainerRegistry()
        with container_registry.lockFile():

            # Load the user packages:
            with open(self._user_package_management_file_path, "r", encoding="utf-8") as f:
                management_dict = json.load(f, encoding="utf-8")
                self._installed_package_dict = management_dict.get("installed", {})
                self._to_remove_package_set = set(management_dict.get("to_remove", []))
                self._to_install_package_dict = management_dict.get("to_install", {})
                Logger.log("i", "Loaded user packages management file from %s", self._user_package_management_file_path)

    def _saveManagementData(self) -> None:
        # Need to use the file lock here to prevent concurrent I/O from other processes/threads
        container_registry = self._application.getContainerRegistry()
        with container_registry.lockFile():
            with open(self._user_package_management_file_path, "w", encoding = "utf-8") as f:
                data_dict = {"version": CuraPackageManager.Version,
                             "installed": self._installed_package_dict,
                             "to_remove": list(self._to_remove_package_set),
                             "to_install": self._to_install_package_dict}
                json.dump(data_dict, f, sort_keys = True, indent = 4)
                Logger.log("i", "Package management file %s was saved", self._user_package_management_file_path)

    # (for initialize) Removes all packages that have been scheduled to be removed.
    def _removeAllScheduledPackages(self) -> None:
        remove_failures = set()
        for package_id in self._to_remove_package_set:
            try:
                self._purgePackage(package_id)
                del self._installed_package_dict[package_id]
            except:
                remove_failures.add(package_id)
                Logger.log("e", "There was an error uninstalling the package {package}".format(package = package_id))
        self._to_remove_package_set = remove_failures
        self._saveManagementData()

    # (for initialize) Installs all packages that have been scheduled to be installed.
    def _installAllScheduledPackages(self) -> None:
        while self._to_install_package_dict:
            package_id, package_info = list(self._to_install_package_dict.items())[0]
            self._installPackage(package_info)
            del self._to_install_package_dict[package_id]
            self._saveManagementData()

    def getBundledPackageInfo(self, package_id: str) -> Optional[dict]:
        package_info = None
        if package_id in self._bundled_package_dict:
            package_info = self._bundled_package_dict[package_id]["package_info"]
        return package_info

    # Checks the given package is installed. If so, return a dictionary that contains the package's information.
    def getInstalledPackageInfo(self, package_id: str) -> Optional[dict]:
        if package_id in self._to_remove_package_set:
            return None

        if package_id in self._to_install_package_dict:
            package_info = self._to_install_package_dict[package_id]["package_info"]
            return package_info

        if package_id in self._installed_package_dict:
            package_info = self._installed_package_dict[package_id]["package_info"]
            return package_info

        if package_id in self._bundled_package_dict:
            package_info = self._bundled_package_dict[package_id]["package_info"]
            return package_info

        return None

    def getAllInstalledPackageIDs(self) -> set:
        # Add bundled, installed, and to-install packages to the set of installed package IDs
        all_installed_ids = set()

        if self._bundled_package_dict.keys():
            all_installed_ids = all_installed_ids.union(set(self._bundled_package_dict.keys()))
        if self._installed_package_dict.keys():
            all_installed_ids = all_installed_ids.union(set(self._installed_package_dict.keys()))
        all_installed_ids = all_installed_ids.difference(self._to_remove_package_set)
        # If it's going to be installed and to be removed, then the package is being updated and it should be listed.
        if self._to_install_package_dict.keys():
            all_installed_ids = all_installed_ids.union(set(self._to_install_package_dict.keys()))

        return all_installed_ids

    def getAllInstalledPackagesInfo(self) -> dict:

        all_installed_ids = self.getAllInstalledPackageIDs()

        # map of <package_type> -> <package_id> -> <package_info>
        installed_packages_dict = {}
        for package_id in all_installed_ids:
            # Skip required plugins as they should not be tampered with
            if package_id in Application.getInstance().getRequiredPlugins():
                continue

            package_info = None
            # Add bundled plugins
            if package_id in self._bundled_package_dict:
                package_info = self._bundled_package_dict[package_id]["package_info"]
                package_info["is_installed"] = True

            # Add installed plugins
            if package_id in self._installed_package_dict:
                package_info = self._installed_package_dict[package_id]["package_info"]
                package_info["is_installed"] = True

            # Add to install plugins
            if package_id in self._to_install_package_dict:
                package_info = self._to_install_package_dict[package_id]["package_info"]
                package_info["is_installed"] = False

            if package_info is None:
                continue

            # We also need to get information from the plugin registry such as if a plugin is active
            package_info["is_active"] = self._plugin_registry.isActivePlugin(package_id)

            # If the package ID is in bundled, label it as such
            package_info["is_bundled"] = package_info["package_id"] in self._bundled_package_dict.keys() and not self.isUserInstalledPackage(package_info["package_id"])

            # If there is not a section in the dict for this type, add it
            if package_info["package_type"] not in installed_packages_dict:
                installed_packages_dict[package_info["package_type"]] = []
                
            # Finally, add the data
            installed_packages_dict[package_info["package_type"]].append(package_info)

        return installed_packages_dict

    # Checks if the given package is installed (at all).
    def isPackageInstalled(self, package_id: str) -> bool:
        return self.getInstalledPackageInfo(package_id) is not None

    # This is called by drag-and-dropping curapackage files.
    @pyqtSlot(QUrl)
    def installPackageViaDragAndDrop(self, file_url: str) -> None:
        filename = QUrl(file_url).toLocalFile()
        return self.installPackage(filename)

    # Schedules the given package file to be installed upon the next start.
    @pyqtSlot(str)
    def installPackage(self, filename: str) -> None:
        has_changes = False
        try:
            # Get package information
            package_info = self.getPackageInfo(filename)
            if not package_info:
                return
            package_id = package_info["package_id"]

            # Check if it is installed
            installed_package_info = self.getInstalledPackageInfo(package_info["package_id"])
            to_install_package = installed_package_info is None  # Install if the package has not been installed
            if installed_package_info is not None:
                # Compare versions and only schedule the installation if the given package is newer
                new_version = package_info["package_version"]
                installed_version = installed_package_info["package_version"]
                if Version(new_version) > Version(installed_version):
                    Logger.log("i", "Package [%s] version [%s] is newer than the installed version [%s], update it.",
                               package_id, new_version, installed_version)
                    to_install_package = True

            if to_install_package:
                # Need to use the lock file to prevent concurrent I/O issues.
                with self._container_registry.lockFile():
                    Logger.log("i", "Package [%s] version [%s] is scheduled to be installed.",
                               package_id, package_info["package_version"])
                    # Copy the file to cache dir so we don't need to rely on the original file to be present
                    package_cache_dir = os.path.join(os.path.abspath(Resources.getCacheStoragePath()), "cura_packages")
                    if not os.path.exists(package_cache_dir):
                        os.makedirs(package_cache_dir, exist_ok=True)

                    target_file_path = os.path.join(package_cache_dir, package_id + ".curapackage")
                    shutil.copy2(filename, target_file_path)

                    self._to_install_package_dict[package_id] = {"package_info": package_info,
                                                                 "filename": target_file_path}
                    has_changes = True
        except:
            Logger.logException("c", "Failed to install package file '%s'", filename)
        finally:
            self._saveManagementData()
            if has_changes:
                self.installedPackagesChanged.emit()

    # Schedules the given package to be removed upon the next start.
    # \param package_id id of the package
    # \param force_add is used when updating. In that case you actually want to uninstall & install
    @pyqtSlot(str)
    def removePackage(self, package_id: str, force_add: bool = False) -> None:
        # Check the delayed installation and removal lists first
        if not self.isPackageInstalled(package_id):
            Logger.log("i", "Attempt to remove package [%s] that is not installed, do nothing.", package_id)
            return

        # Extra safety check
        if package_id not in self._installed_package_dict and package_id in self._bundled_package_dict:
            Logger.log("i", "Not uninstalling [%s] because it is a bundled package.")
            return

        if package_id not in self._to_install_package_dict or force_add:
            # Schedule for a delayed removal:
            self._to_remove_package_set.add(package_id)
        else:
            if package_id in self._to_install_package_dict:
                # Remove from the delayed installation list if present
                del self._to_install_package_dict[package_id]

        self._saveManagementData()
        self.installedPackagesChanged.emit()

    ##  Is the package an user installed package?
    def isUserInstalledPackage(self, package_id: str):
        return package_id in self._installed_package_dict

    ##  Removes everything associated with the given package ID.
    #   \return It may produce an exception if the user has no permissions to delete this files (on Windows).
    #   This exception must be catched.
    def _purgePackage(self, package_id: str) -> None:
        # Iterate through all directories in the data storage directory and look for sub-directories that belong to
        # the package we need to remove, that is the sub-dirs with the package_id as names, and remove all those dirs.
        data_storage_dir = os.path.abspath(Resources.getDataStoragePath())

        for root, dir_names, _ in os.walk(data_storage_dir):
            for dir_name in dir_names:
                package_dir = os.path.join(root, dir_name, package_id)
                if os.path.exists(package_dir):
                    Logger.log("i", "Removing '%s' for package [%s]", package_dir, package_id)
                    shutil.rmtree(package_dir)
            break

    # Installs all files associated with the given package.
    def _installPackage(self, installation_package_data: dict):
        package_info = installation_package_data["package_info"]
        filename = installation_package_data["filename"]

        package_id = package_info["package_id"]
        Logger.log("i", "Installing package [%s] from file [%s]", package_id, filename)

        # Load the cached package file and extract all contents to a temporary directory
        if not os.path.exists(filename):
            Logger.log("w", "Package [%s] file '%s' is missing, cannot install this package", package_id, filename)
            return
        try:
            with zipfile.ZipFile(filename, "r") as archive:
                temp_dir = tempfile.TemporaryDirectory()
                archive.extractall(temp_dir.name)
        except Exception:
            Logger.logException("e", "Failed to install package from file [%s]", filename)
            return

        from cura.CuraApplication import CuraApplication
        installation_dirs_dict = {
            "materials": Resources.getStoragePath(CuraApplication.ResourceTypes.MaterialInstanceContainer),
            "qualities": Resources.getStoragePath(CuraApplication.ResourceTypes.QualityInstanceContainer),
            "plugins": os.path.abspath(Resources.getStoragePath(Resources.Plugins)),
        }

        # Remove it first and then install
        try:
            self._purgePackage(package_id)
        except:
            Logger.log("e", "There was an error deleting the package {package} during updating.".format(package = package_id))
            return

        # Copy the folders there
        for sub_dir_name, installation_root_dir in installation_dirs_dict.items():
            src_dir_path = os.path.join(temp_dir.name, "files", sub_dir_name)
            dst_dir_path = os.path.join(installation_root_dir, package_id)

            if not os.path.exists(src_dir_path):
                continue
            self.__installPackageFiles(package_id, src_dir_path, dst_dir_path)

        # Remove the file
        try:
            os.remove(filename)
        except Exception:
            Logger.log("w", "Tried to delete file [%s], but it failed", filename)

        # Move the info to the installed list of packages only when it succeeds
        self._installed_package_dict[package_id] = self._to_install_package_dict[package_id]

    def __installPackageFiles(self, package_id: str, src_dir: str, dst_dir: str) -> None:
        Logger.log("i", "Moving package {package_id} from {src_dir} to {dst_dir}".format(package_id=package_id, src_dir=src_dir, dst_dir=dst_dir))
        shutil.move(src_dir, dst_dir)

    # Gets package information from the given file.
    def getPackageInfo(self, filename: str) -> Dict[str, Any]:
        if not os.path.exists(filename):
            Logger.log("w", "Package file '%s' is missing, cannot get info from this package", filename)
            return {}

        with zipfile.ZipFile(filename) as archive:
            try:
                # All information is in package.json
                with archive.open("package.json") as f:
                    package_info_dict = json.loads(f.read().decode("utf-8"))
                    return package_info_dict
            except Exception as e:
                Logger.logException("w", "Could not get package information from file '%s': %s" % (filename, e))
                return {}

    # Gets the license file content if present in the given package file.
    # Returns None if there is no license file found.
    def getPackageLicense(self, filename: str) -> Optional[str]:
        if not os.path.exists(filename):
            Logger.log("w", "Package file '%s' is missing, cannot get license from this package", filename)
            return None

        license_string = None
        with zipfile.ZipFile(filename) as archive:
            # Go through all the files and use the first successful read as the result
            for file_info in archive.infolist():
                if file_info.filename.endswith("LICENSE"):
                    Logger.log("d", "Found potential license file '%s'", file_info.filename)
                    try:
                        with archive.open(file_info.filename, "r") as f:
                            data = f.read()
                        license_string = data.decode("utf-8")
                        break
                    except:
                        Logger.logException("e", "Failed to load potential license file '%s' as text file.",
                                            file_info.filename)
                        license_string = None
        return license_string
