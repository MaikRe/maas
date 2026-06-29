# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from pathlib import Path
import uuid

import structlog

from maascommon.enums.switch import SwitchLogCategory, SwitchProvisioningStatus
from maascommon.utils.images import get_bootresource_store_path
from maasservicelayer.builders.switches import SwitchBuilder, SwitchLogBuilder
from maasservicelayer.context import Context
from maasservicelayer.db.filters import QuerySpec
from maasservicelayer.db.repositories.interfaces import InterfaceClauseFactory
from maasservicelayer.db.repositories.switches import (
    SwitchesRepository,
    SwitchLogsRepository,
)
from maasservicelayer.exceptions.catalog import (
    BaseExceptionDetail,
    ConflictException,
    NotFoundException,
)
from maasservicelayer.exceptions.constants import CONFLICT_VIOLATION_TYPE
from maasservicelayer.models.base import ListResult
from maasservicelayer.models.switches import (
    Switch,
    SwitchLog,
    SwitchScript,
    SwitchWithTargetImage,
)
from maasservicelayer.services.base import BaseService
from maasservicelayer.services.bootresourcefiles import (
    BootResourceFilesService,
)
from maasservicelayer.services.bootresources import BootResourceService
from maasservicelayer.services.bootresourcesets import BootResourceSetsService
from maasservicelayer.services.interfaces import InterfacesService
from maasservicelayer.services.staticipaddress import StaticIPAddressService

# The rack nginx HTTP service port.
RACK_HTTP_PORT = 5248

# Map from ONIE Onie-Arch header values to the directory name used for
# the architecture-specific maas-switch-provisioner binary.
_ONIE_ARCH_TO_PROVISIONER_DIR: dict[str, str] = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "ppc64el": "ppc64el",
    "ppc64le": "ppc64el",
}
_DEFAULT_PROVISIONER_DIR = "amd64"

_WRAPPER_SCRIPT_TEMPLATE = """\
#!/bin/sh
set -eu

export MAAS_URL="{maas_url}"
export SWITCH_UUID="{switch_uuid}"
export SWITCH_MAC="{switch_mac}"
export NOS_URL="{nos_url}"

wget -q "{provisioner_url}" -O /tmp/maas-switch-provisioner
chmod +x /tmp/maas-switch-provisioner
exec /tmp/maas-switch-provisioner
"""


logger = structlog.getLogger()


class SwitchesService(BaseService[Switch, SwitchesRepository, SwitchBuilder]):
    def __init__(
        self,
        context: Context,
        switches_repository: SwitchesRepository,
        switch_logs_repository: SwitchLogsRepository,
        staticipaddress_service: StaticIPAddressService,
        interfaces_service: InterfacesService,
        boot_resources_service: BootResourceService,
        boot_resource_sets_service: BootResourceSetsService,
        boot_resource_files_service: BootResourceFilesService,
    ):
        super().__init__(context, switches_repository)
        self.switch_logs_repository = switch_logs_repository
        self.staticipaddress_service = staticipaddress_service
        self.interfaces_service = interfaces_service
        self.boot_resources_service = boot_resources_service
        self.boot_resource_sets_service = boot_resource_sets_service
        self.boot_resource_files_service = boot_resource_files_service

    async def get_one_with_target_image(
        self, id: int
    ) -> SwitchWithTargetImage | None:
        return await self.repository.get_one_with_target_image(id)

    async def list_with_target_image(
        self, page: int, size: int
    ) -> ListResult[SwitchWithTargetImage]:
        return await self.repository.list_with_target_image(page, size)

    async def get_by_uuid(self, switch_uuid: uuid.UUID) -> Switch | None:
        return await self.repository.get_by_uuid(switch_uuid)

    async def update_provisioning_status(
        self, switch_uuid: uuid.UUID, status: SwitchProvisioningStatus
    ) -> Switch:
        switch = await self.get_by_uuid(switch_uuid)
        if not switch:
            raise NotFoundException()
        builder = SwitchBuilder(status=status)
        return await self.update_by_id(switch.id, builder)

    async def get_provisioning_script(
        self, switch_uuid: uuid.UUID
    ) -> SwitchScript | None:
        switch = await self.get_by_uuid(switch_uuid)
        if not switch:
            raise NotFoundException()
        return await self.repository.get_script_for_switch(switch.id)

    async def create_provisioning_log(
        self,
        switch_uuid: uuid.UUID,
        log_category: SwitchLogCategory,
        exit_code: int,
        output: str,
    ) -> SwitchLog:
        switch = await self.get_by_uuid(switch_uuid)
        if not switch:
            raise NotFoundException()
        builder = SwitchLogBuilder(
            switch_id=switch.id,
            log_category=log_category,
            exit_code=exit_code,
            output=output,
        )
        return await self.switch_logs_repository.create(builder)

    async def list_provisioning_logs(
        self,
        switch_uuid: uuid.UUID,
        page: int,
        size: int,
        log_category: str | None = None,
    ) -> ListResult[SwitchLog]:
        switch = await self.get_by_uuid(switch_uuid)
        if not switch:
            raise NotFoundException()
        return await self.switch_logs_repository.list_for_switch(
            switch.id, page, size, log_category
        )

    async def create_new_switch_and_interface(
        self,
        builder: SwitchBuilder,
        mac_address: str,
    ) -> Switch:
        builder.switch_uuid = uuid.uuid4()
        builder.status = SwitchProvisioningStatus.NOT_PROVISIONED
        switch = await self.create(builder)
        await self.interfaces_service.create_switch_interface(
            switch_id=switch.id, mac=mac_address
        )
        return switch

    async def create_switch_and_link_interface(
        self,
        builder: SwitchBuilder,
        interface_id: int,
    ) -> Switch:
        builder.switch_uuid = uuid.uuid4()
        builder.status = SwitchProvisioningStatus.NOT_PROVISIONED
        switch = await self.create(builder)
        await self.interfaces_service.link_interface_to_switch(
            interface_id=interface_id, switch_id=switch.id
        )
        return switch

    async def get_switch_by_mac_address(
        self, mac_address: str
    ) -> Switch | None:
        interface = await self.interfaces_service.get_one(
            query=QuerySpec(
                where=InterfaceClauseFactory.with_mac_address(mac_address)
            )
        )
        if not interface or not interface.switch_id:
            return None
        return await self.get_by_id(id=interface.switch_id)

    async def check_installer_for_switch(self, mac_address: str) -> int | None:
        switch = await self.get_switch_by_mac_address(mac_address)
        if not switch:
            raise NotFoundException()
        return switch.target_image_id if switch.target_image_id else None

    async def _get_installer_boot_file(self, boot_resource_id: int):
        """Return the single boot resource file for a NOS installer image.

        NOS installer images are uploaded directly (not synced via simplestreams),
        so we use the latest resource set regardless of sync-record completeness.
        The file is written to disk synchronously during upload, so the latest
        set is always fully available.
        """
        boot_resource = await self.boot_resources_service.get_by_id(
            id=boot_resource_id
        )
        if not boot_resource:
            raise NotFoundException()

        resource_set = (
            await self.boot_resource_sets_service.get_latest_for_boot_resource(
                boot_resource.id
            )
        )
        if not resource_set:
            raise NotFoundException()

        files = (
            await self.boot_resource_files_service.get_files_in_resource_set(
                resource_set.id
            )
        )
        if not files:
            raise NotFoundException()

        if len(files) != 1:
            raise ConflictException(
                details=[
                    BaseExceptionDetail(
                        type=CONFLICT_VIOLATION_TYPE,
                        message=f"NOS installer images are expected to be self-extracting binaries with one (and only one) file, and it currently has {len(files)} files.",
                    )
                ]
            )
        return files[0]

    async def get_installer_file_for_switch(
        self, mac_address: str
    ) -> tuple[Path, str, int] | None:
        boot_resource_id = await self.check_installer_for_switch(mac_address)
        if not boot_resource_id:
            return None

        boot_file = await self._get_installer_boot_file(boot_resource_id)
        file_path = get_bootresource_store_path() / boot_file.filename_on_disk

        if not file_path.exists():
            raise NotFoundException()

        return (file_path, boot_file.filename, boot_file.size)

    async def get_installer_filename_on_disk(
        self, mac_address: str
    ) -> str | None:
        """Return the filename_on_disk for the NOS installer, used to build
        the rack-side image URL at /images/<filename_on_disk>."""
        boot_resource_id = await self.check_installer_for_switch(mac_address)
        if not boot_resource_id:
            return None
        boot_file = await self._get_installer_boot_file(boot_resource_id)
        return boot_file.filename_on_disk

    async def generate_wrapper_script(
        self,
        mac_address: str,
        maas_url: str,
        rack_base_url: str,
        onie_arch: str | None = None,
    ) -> str | None:
        """Generate the ONIE provisioning wrapper script for a switch.

        The script exports environment variables and downloads + executes the
        maas-switch-provisioner Go binary from the rack.

        Args:
            mac_address: Management MAC of the switch.
            maas_url: Region v3 API base URL, e.g. ``http://maas.local/MAAS/a/v3``.
            rack_base_url: Rack HTTP base URL, e.g. ``http://maas.local:5248``.
            onie_arch: Value of the ``Onie-Arch`` request header, used to
                select the architecture-specific binary.

        Returns:
            Rendered shell script as a string, or None if the switch is not
            found or is not eligible for provisioning. Callers must return a
            404 response directly (without raising an exception) so that any
            status-transition side-effects are committed to the database.
        """
        switch = await self.get_switch_by_mac_address(mac_address)
        if not switch:
            return None
        logger.info(f"switch status: {switch.status}")

        if switch.status in (
            SwitchProvisioningStatus.DEPLOYING,
            SwitchProvisioningStatus.READY,
            SwitchProvisioningStatus.FAILED,
        ):
            if switch.status in (
                SwitchProvisioningStatus.DEPLOYING,
                SwitchProvisioningStatus.READY,
            ):
                await self.update_provisioning_status(
                    switch.switch_uuid, SwitchProvisioningStatus.FAILED
                )
            return None

        filename_on_disk = await self.get_installer_filename_on_disk(
            mac_address
        )
        nos_url = (
            f"{rack_base_url.rstrip('/')}/images/{filename_on_disk}"
            if filename_on_disk
            else ""
        )
        provisioner_url = (
            f"{rack_base_url.rstrip('/')}/switch-provisioner/"
            f"{_ONIE_ARCH_TO_PROVISIONER_DIR.get(onie_arch or '', _DEFAULT_PROVISIONER_DIR)}"
            f"/maas-switch-provisioner"
        )

        logger.info(f"nos file nane: {filename_on_disk}")
        logger.info(f"nos url: {nos_url}")
        logger.info(f"provisioner go lang: {provisioner_url}")
        return _WRAPPER_SCRIPT_TEMPLATE.format(
            maas_url=maas_url.rstrip("/"),
            switch_uuid=str(switch.switch_uuid),
            switch_mac=mac_address,
            nos_url=nos_url,
            provisioner_url=provisioner_url,
        )

    async def pre_delete_hook(self, resource_to_be_deleted: Switch) -> None:
        interfaces = await self.interfaces_service.get_many(
            query=QuerySpec(
                where=InterfaceClauseFactory.with_switch_id(
                    resource_to_be_deleted.id
                )
            )
        )
        if not interfaces:
            return

        interface_ids = [iface.id for iface in interfaces]

        orphaned_ips = await self.staticipaddress_service.get_ips_for_interfaces_without_other_links(
            interface_ids
        )

        await self.interfaces_service.unlink_interfaces_from_ips(
            interface_ids=interface_ids
        )

        await self.interfaces_service.delete_many_by_id(interface_ids)

        for ip in orphaned_ips:
            await self.staticipaddress_service.delete_by_id(ip.id)
