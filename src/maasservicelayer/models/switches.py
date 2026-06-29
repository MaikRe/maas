# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import uuid
from typing import Self

from maascommon.enums.switch import SwitchLogCategory, SwitchProvisioningStatus
from maasservicelayer.models.base import (
    generate_builder,
    MaasTimestampedBaseModel,
)


@generate_builder()
class Switch(MaasTimestampedBaseModel):
    """Model representing a network switch."""

    target_image_id: int | None = None
    switch_uuid: uuid.UUID
    status: SwitchProvisioningStatus = SwitchProvisioningStatus.NOT_PROVISIONED


class SwitchWithTargetImage(Switch):
    """Model representing a network switch, with its target image name."""

    target_image: str | None = None

    @classmethod
    def from_switch(cls, switch: Switch, target_image: str | None) -> Self:
        return cls(
            id=switch.id,
            target_image_id=switch.target_image_id,
            switch_uuid=switch.switch_uuid,
            status=switch.status,
            created=switch.created,
            updated=switch.updated,
            target_image=target_image,
        )


@generate_builder()
class SwitchScript(MaasTimestampedBaseModel):
    """Model representing a provisioning script for a switch."""

    name: str
    description: str = ""
    content: str


@generate_builder()
class SwitchLog(MaasTimestampedBaseModel):
    """Model representing a provisioning log entry for a switch."""

    switch_id: int
    log_category: SwitchLogCategory
    exit_code: int
    output: str

