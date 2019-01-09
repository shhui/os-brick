#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import re
import time

from oslo_concurrency import lockutils
from oslo_concurrency import processutils as putils
from oslo_log import log as logging

from os_brick import exception
from os_brick.i18n import _
from os_brick.initiator.connectors import base
from os_brick import utils


DEVICE_SCAN_ATTEMPTS_DEFAULT = 5

LOG = logging.getLogger(__name__)

synchronized = lockutils.synchronized_with_prefix('os-brick-')


class NVMeConnector(base.BaseLinuxConnector):

    """Connector class to attach/detach NVMe over fabric volumes."""

    def __init__(self, root_helper, driver=None,
                 device_scan_attempts=DEVICE_SCAN_ATTEMPTS_DEFAULT,
                 *args, **kwargs):
        super(NVMeConnector, self).__init__(
            root_helper,
            driver=driver,
            device_scan_attempts=device_scan_attempts,
            *args, **kwargs)

    def _get_system_uuid(self):
        # RSD requires system_uuid to let Cinder RSD Driver identify
        # Nova node for later RSD volume attachment.
        try:
            out, err = self._execute("dmidecode",
                                     root_helper=self._root_helper,
                                     run_as_root=True)
            if err:
                LOG.warning("dmidecode execute error: %s", err)
                return ""
            for line in out.split("\n"):
                line = line.strip()
                if line.startswith("UUID:"):
                    uuid = line.split(" ")[1]
                    LOG.debug("got system uuid: %s", uuid)
                    return uuid
            LOG.warning("Cannot get system uuid from %s", out)
            return ""
        except putils.ProcessExecutionError as e:
            LOG.warning("Unable to locate dmidecode. For Cinder RSD Backend, "
                        "please make sure it is installed: %s", e)
            return ""

    @staticmethod
    def get_connector_properties(root_helper, *args, **kwargs):
        """The NVMe connector properties."""
        nvme = NVMeConnector(root_helper=root_helper,
                             execute=kwargs.get('execute'))
        uuid = nvme._get_system_uuid()
        if uuid:
            return {"system uuid": uuid}
        else:
            return {}

    def get_search_path(self):
        return '/dev'

    def get_volume_paths(self, connection_properties):
        path = connection_properties['device_path']
        LOG.debug("Path of volume to be extended is %(path)s", {'path': path})
        return [path]

    def _get_nvme_devices(self):
        nvme_devices = []
        # match nvme devices like /dev/nvme10n10
        pattern = r'/dev/nvme[0-9]+n[0-9]+'
        cmd = ['nvme', 'list']
        for retry in range(1, self.device_scan_attempts + 1):
            try:
                (out, err) = self._execute(*cmd,
                                           root_helper=self._root_helper,
                                           run_as_root=True)
                for line in out.split('\n'):
                    result = re.match(pattern, line)
                    if result:
                        nvme_devices.append(result.group(0))
                LOG.debug("_get_nvme_devices returned %(nvme_devices)s",
                          {'nvme_devices': nvme_devices})
                return nvme_devices

            except putils.ProcessExecutionError:
                LOG.warning(
                    "Failed to list available NVMe connected controllers, "
                    "retrying.")
                time.sleep(retry ** 2)
        else:
            msg = _("Failed to retrieve available connected NVMe controllers "
                    "when running nvme list.")
            raise exception.CommandExecutionFailed(message=msg)

    @utils.retry(exceptions=exception.VolumePathsNotFound)
    def _get_device_path(self, current_nvme_devices):
        all_nvme_devices = self._get_nvme_devices()
        LOG.debug("all_nvme_devices are %(all_nvme_devices)s",
                  {'all_nvme_devices': all_nvme_devices})
        path = set(all_nvme_devices) - set(current_nvme_devices)
        if not path:
            raise exception.VolumePathsNotFound()
        return list(path)

    @utils.retry(exceptions=putils.ProcessExecutionError)
    def _try_connect_nvme(self, cmd):
        self._execute(*cmd, root_helper=self._root_helper,
                      run_as_root=True)

    @utils.trace
    @synchronized('connect_volume')
    def connect_volume(self, connection_properties):
        """Discover and attach the volume.

        :param connection_properties: The dictionary that describes all
                                      of the target volume attributes.
               connection_properties must include:
               nqn - NVMe subsystem name to the volume to be connected
               target_port - NVMe target port that hosts the nqn sybsystem
               target_portal - NVMe target ip that hosts the nqn sybsystem
        :type connection_properties: dict
        :returns: dict
        """

        current_nvme_devices = self._get_nvme_devices()

        device_info = {'type': 'block'}
        conn_nqn = connection_properties['nqn']
        target_portal = connection_properties['target_portal']
        port = connection_properties['target_port']
        nvme_transport_type = connection_properties['transport_type']
        host_nqn = connection_properties.get('host_nqn')
        cmd = [
            'nvme', 'connect',
            '-t', nvme_transport_type,
            '-n', conn_nqn,
            '-a', target_portal,
            '-s', port]
        if host_nqn:
            cmd.extend(['-q', host_nqn])

        self._try_connect_nvme(cmd)

        path = self._get_device_path(current_nvme_devices)
        device_info['path'] = path[0]
        LOG.debug("NVMe device to be connected to is %(path)s",
                  {'path': path[0]})
        return device_info

    @utils.trace
    @synchronized('disconnect_volume')
    def disconnect_volume(self, connection_properties, device_info,
                          force=False, ignore_errors=False):
        """Detach and flush the volume.

        :param connection_properties: The dictionary that describes all
                                      of the target volume attributes.
               connection_properties must include:
               device_path - path to the volume to be connected
        :type connection_properties: dict

        :param device_info: historical difference, but same as connection_props
        :type device_info: dict

        """

        conn_nqn = connection_properties['nqn']
        if device_info and device_info.get('path'):
            device_path = device_info.get('path')
        else:
            device_path = connection_properties['device_path'] or ''
        current_nvme_devices = self._get_nvme_devices()
        if device_path not in current_nvme_devices:
            LOG.warning("Trying to disconnect device %(device_path)s that "
                        "is not connected.", {'device_path': device_path})
            return

        LOG.debug(
            "Trying to disconnect from NVMe nqn "
            "%(conn_nqn)s with device_path %(device_path)s",
            {'conn_nqn': conn_nqn, 'device_path': device_path})
        cmd = [
            'nvme',
            'disconnect',
            '-d',
            device_path]
        try:
            self._execute(
                *cmd,
                root_helper=self._root_helper,
                run_as_root=True)

        except putils.ProcessExecutionError:
            LOG.error(
                "Failed to disconnect from NVMe nqn "
                "%(conn_nqn)s with device_path %(device_path)s",
                {'conn_nqn': conn_nqn, 'device_path': device_path})
            if not ignore_errors:
                raise

    @utils.trace
    @synchronized('extend_volume')
    def extend_volume(self, connection_properties):
        """Update the local kernel's size information.

        Try and update the local kernel's size information
        for an LVM volume.
        """
        volume_paths = self.get_volume_paths(connection_properties)
        if volume_paths:
            return self._linuxscsi.extend_volume(volume_paths)
        else:
            LOG.warning("Couldn't find any volume paths on the host to "
                        "extend volume for %(props)s",
                        {'props': connection_properties})
            raise exception.VolumePathsNotFound()
