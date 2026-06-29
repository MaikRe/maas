# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from datetime import datetime
from typing import Self
import uuid

from pydantic import Field

from maasapiserver.v3.api.public.models.responses.base import (
    BaseHal,
    BaseHref,
    HalResponse,
    PaginatedResponse,
)
from maascommon.enums.switch import SwitchLogCategory, SwitchProvisioningStatus
from maasservicelayer.models.switches import (
    Switch,
    SwitchLog,
    SwitchWithTargetImage,
)


class SwitchResponse(HalResponse[BaseHal]):
    kind: str = Field(default="Switch")
    id: int
    switch_uuid: uuid.UUID
    status: SwitchProvisioningStatus
    target_image_id: int | None = None
    target_image: str | None = None

    @classmethod
    def from_model(
        cls,
        switch: SwitchWithTargetImage,
        self_base_hyperlink: str,
    ) -> Self:
        return cls(
            id=switch.id,
            switch_uuid=switch.switch_uuid,
            status=switch.status,
            target_image_id=switch.target_image_id,
            target_image=switch.target_image,
            hal_links=BaseHal(  # pyright: ignore [reportCallIssue]
                self=BaseHref(
                    href=f"{self_base_hyperlink.rstrip('/')}/{switch.id}"
                )
            ),
        )

    @classmethod
    def from_switch_model(
        cls,
        switch: Switch,
        target_image: str | None,
        self_base_hyperlink: str,
    ) -> Self:
        return cls(
            id=switch.id,
            switch_uuid=switch.switch_uuid,
            status=switch.status,
            target_image_id=switch.target_image_id,
            target_image=target_image,
            hal_links=BaseHal(  # pyright: ignore [reportCallIssue]
                self=BaseHref(
                    href=f"{self_base_hyperlink.rstrip('/')}/{switch.id}"
                )
            ),
        )


class SwitchesListResponse(PaginatedResponse[SwitchResponse]):
    kind: str = Field(default="SwitchesList")


class SwitchLogResponse(HalResponse[BaseHal]):
    kind: str = Field(default="SwitchLog")
    id: int
    switch_uuid: uuid.UUID
    log_category: SwitchLogCategory
    exit_code: int
    output: str
    created_at: datetime

    @classmethod
    def from_model(
        cls,
        log: SwitchLog,
        switch_uuid: uuid.UUID,
        self_base_hyperlink: str,
    ) -> Self:
        return cls(
            id=log.id,
            switch_uuid=switch_uuid,
            log_category=log.log_category,
            exit_code=log.exit_code,
            output=log.output,
            created_at=log.created,
            hal_links=BaseHal(  # pyright: ignore [reportCallIssue]
                self=BaseHref(
                    href=f"{self_base_hyperlink.rstrip('/')}/{log.id}"
                )
            ),
        )


class SwitchLogsListResponse(PaginatedResponse[SwitchLogResponse]):
    kind: str = Field(default="SwitchLogsList")
