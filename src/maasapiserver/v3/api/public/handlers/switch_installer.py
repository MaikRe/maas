# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

from typing import Annotated
import uuid

from fastapi import Depends, Header, Request, Response
from fastapi.responses import PlainTextResponse
import structlog

from maasapiserver.common.api.base import Handler, handler
from maasapiserver.common.api.models.responses.errors import (
    BadRequestBodyResponse,
    NotFoundBodyResponse,
    UnauthorizedBodyResponse,
    ValidationErrorBodyResponse,
)
from maasapiserver.common.utils.http import extract_absolute_uri
from maasapiserver.v3.api import services
from maasapiserver.v3.api.public.models.requests.query import PaginationParams
from maasapiserver.v3.api.public.models.responses.switches import (
    SwitchLogResponse,
    SwitchLogsListResponse,
)
from maasapiserver.v3.auth.base import check_permissions
from maasapiserver.v3.constants import V3_API_PREFIX
from maascommon.enums.switch import SwitchLogCategory, SwitchProvisioningStatus
from maascommon.openfga.base import MAASResourceEntitlement
from maasservicelayer.exceptions.catalog import (
    BaseExceptionDetail,
    NotFoundException,
    ValidationException,
)
from maasservicelayer.models.fields import MacAddress
from maasservicelayer.services import ServiceCollectionV3
from maasservicelayer.services.switches import RACK_HTTP_PORT

logger = structlog.get_logger()

_LOG_SIZE_LIMIT_BYTES = 50 * 1024 * 1024  # 50 MB


class SwitchInstallerHandler(Handler):
    """Handles ONIE switch provisioning endpoints.

    **WARNING:** This is an experimental, preview feature. The API and behaviour
    may change in future releases without backward compatibility guarantees.
    Not intended for production use.
    """

    TAGS = ["Switches"]

    @handler(
        path="/switch-installer",
        methods=["GET"],
        tags=TAGS,
        responses={
            200: {
                "content": {"text/plain": {}},
                "description": "Templated provisioning wrapper script",
            },
            400: {"model": BadRequestBodyResponse},
            404: {"model": NotFoundBodyResponse},
        },
        response_model_exclude_none=True,
        dependencies=[],
    )
    async def get_switch_installer(
        self,
        request: Request,
        onie_eth_addr: Annotated[MacAddress, Header()],
        onie_arch: Annotated[str | None, Header()] = None,
        services_collection: ServiceCollectionV3 = Depends(services),  # noqa: B008
    ):
        """Serve the ONIE provisioning wrapper script for a registered switch.

        **Experimental**: this endpoint is part of an experimental feature set
        and may change in future releases.

        The script exports pre-filled environment variables and downloads the
        ``maas-switch-provisioner`` Go binary from the rack controller, then
        executes it. The binary handles all further provisioning steps.

        Responds with 404 if the switch is already DEPLOYING, READY, or FAILED.
        """
        base = extract_absolute_uri(request).rstrip("/")
        # Derive the v3 API URL and the rack URL from the request host.
        maas_url = f"{base}/MAAS/a/v3"
        parsed = request.url
        rack_base_url = f"{parsed.scheme}://{parsed.hostname}:{RACK_HTTP_PORT}"
        logger.info(onie_eth_addr)
        script = await services_collection.switches.generate_wrapper_script(
            mac_address=str(onie_eth_addr),
            maas_url=maas_url,
            rack_base_url=rack_base_url,
            onie_arch=onie_arch,
        )
        if script is None:
            # Return a Response directly (no exception) so that any
            # status-transition DB writes are committed, not rolled back.
            return Response(
                status_code=404,
                content='{"kind":"Error","code":404,"message":"Switch not found or not eligible for provisioning."}',
                media_type="application/json",
            )

        logger.info(
            "switch_installer_served",
            mac_address=str(onie_eth_addr),
        )
        return PlainTextResponse(content=script)

    @handler(
        path="/switches/{switch_uuid}/status",
        methods=["POST"],
        tags=TAGS,
        responses={
            204: {},
            400: {"model": BadRequestBodyResponse},
            404: {"model": NotFoundBodyResponse},
            422: {"model": ValidationErrorBodyResponse},
        },
        status_code=204,
        dependencies=[],
    )
    async def update_switch_status(
        self,
        switch_uuid: uuid.UUID,
        request: Request,
        services_collection: ServiceCollectionV3 = Depends(services),  # noqa: B008
    ) -> Response:
        """Update the provisioning status of a switch (used by the switch itself).

        **Experimental**: this endpoint is part of an experimental feature set
        and may change in future releases.

        Accepts a plain-text body: one of ``DEPLOYING``, ``READY``, or ``FAILED``.
        """
        body = (await request.body()).decode("utf-8", errors="replace").strip()
        try:
            status = SwitchProvisioningStatus(body)
        except ValueError:
            raise ValidationException.build_for_field(  # noqa: B904
                field="status",
                message=f"Invalid status '{body}'. Must be one of: DEPLOYING, READY, FAILED.",
            )

        if status == SwitchProvisioningStatus.NOT_PROVISIONED:
            raise ValidationException.build_for_field(
                field="status",
                message="Cannot set status to NOT_PROVISIONED via this endpoint.",
            )

        try:
            await services_collection.switches.update_provisioning_status(
                switch_uuid, status
            )
        except NotFoundException:
            raise NotFoundException(  # noqa: B904
                details=[
                    BaseExceptionDetail(
                        type="SwitchNotFound",
                        message=f"Switch with uuid '{switch_uuid}' was not found.",
                    )
                ]
            )
        return Response(status_code=204)

    @handler(
        path="/switches/{switch_uuid}/provisioning-script",
        methods=["GET"],
        tags=TAGS,
        responses={
            200: {
                "content": {"text/plain": {}},
                "description": "Raw provisioning script content",
            },
            404: {"model": NotFoundBodyResponse},
        },
        response_model_exclude_none=True,
        dependencies=[],
    )
    async def get_provisioning_script(
        self,
        switch_uuid: uuid.UUID,
        services_collection: ServiceCollectionV3 = Depends(services),  # noqa: B008
    ):
        """Fetch the assigned provisioning script for a switch.

        **Experimental**: this endpoint is part of an experimental feature set
        and may change in future releases.

        Returns the raw script content for execution by the Go provisioner.
        Returns 404 if no script is assigned to the switch.
        """
        try:
            script = (
                await services_collection.switches.get_provisioning_script(
                    switch_uuid
                )
            )
        except NotFoundException:
            raise NotFoundException(  # noqa: B904
                details=[
                    BaseExceptionDetail(
                        type="SwitchNotFound",
                        message=f"Switch with uuid '{switch_uuid}' was not found.",
                    )
                ]
            )
        if script is None:
            raise NotFoundException(
                details=[
                    BaseExceptionDetail(
                        type="ProvisioningScriptNotAssigned",
                        message=f"No provisioning script is assigned to switch '{switch_uuid}'.",
                    )
                ]
            )
        return PlainTextResponse(content=script.content)

    @handler(
        path="/switches/{switch_uuid}/logs",
        methods=["POST"],
        tags=TAGS,
        responses={
            201: {},
            400: {"model": BadRequestBodyResponse},
            404: {"model": NotFoundBodyResponse},
            413: {"description": "Payload Too Large"},
        },
        status_code=201,
        dependencies=[],
    )
    async def upload_switch_log(
        self,
        switch_uuid: uuid.UUID,
        request: Request,
        x_log_category: Annotated[str, Header()],
        x_exit_code: Annotated[str, Header()],
        services_collection: ServiceCollectionV3 = Depends(services),  # noqa: B008
    ) -> Response:
        """Upload provisioning execution logs for a switch.

        **Experimental**: this endpoint is part of an experimental feature set
        and may change in future releases.

        Required headers:
        - ``X-Log-Category``: one of ``WRAPPER``, ``NOS_INSTALLATION``, ``PROVISIONING_SCRIPT``
        - ``X-Exit-Code``: integer process exit code
        """
        try:
            log_category = SwitchLogCategory(x_log_category)
        except ValueError:
            raise ValidationException.build_for_field(  # noqa: B904
                field="X-Log-Category",
                message=f"Invalid log category '{x_log_category}'. Must be one of: WRAPPER, NOS_INSTALLATION, PROVISIONING_SCRIPT.",
            )

        try:
            exit_code = int(x_exit_code)
        except ValueError:
            raise ValidationException.build_for_field(  # noqa: B904
                field="X-Exit-Code",
                message=f"Invalid exit code '{x_exit_code}'. Must be an integer.",
            )

        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > _LOG_SIZE_LIMIT_BYTES:
            return Response(status_code=413)

        body = await request.body()
        if len(body) > _LOG_SIZE_LIMIT_BYTES:
            return Response(status_code=413)

        output = body.decode("utf-8", errors="replace")

        try:
            await services_collection.switches.create_provisioning_log(
                switch_uuid=switch_uuid,
                log_category=log_category,
                exit_code=exit_code,
                output=output,
            )
        except NotFoundException:
            raise NotFoundException(  # noqa: B904
                details=[
                    BaseExceptionDetail(
                        type="SwitchNotFound",
                        message=f"Switch with uuid '{switch_uuid}' was not found.",
                    )
                ]
            )
        return Response(status_code=201)

    @handler(
        path="/switches/{switch_uuid}/logs",
        methods=["GET"],
        tags=TAGS,
        responses={
            200: {"model": SwitchLogsListResponse},
            401: {"model": UnauthorizedBodyResponse},
            404: {"model": NotFoundBodyResponse},
        },
        response_model_exclude_none=True,
        status_code=200,
        dependencies=[
            Depends(
                check_permissions(
                    MAASResourceEntitlement.CAN_VIEW_GLOBAL_ENTITIES
                )
            )
        ],
    )
    async def list_switch_logs(
        self,
        switch_uuid: uuid.UUID,
        pagination_params: PaginationParams = Depends(),  # noqa: B008
        category: str | None = None,
        services_collection: ServiceCollectionV3 = Depends(services),  # noqa: B008
    ) -> SwitchLogsListResponse:
        """List provisioning logs for a switch, optionally filtered by category.

        **Experimental**: this endpoint is part of an experimental feature set
        and may change in future releases.

        Optional query parameter: ``category`` — one of ``WRAPPER``,
        ``NOS_INSTALLATION``, ``PROVISIONING_SCRIPT``, or omit for all.
        """
        if category is not None:
            try:
                SwitchLogCategory(category)
            except ValueError:
                raise ValidationException.build_for_field(  # noqa: B904
                    field="category",
                    message=f"Invalid category '{category}'. Must be one of: WRAPPER, NOS_INSTALLATION, PROVISIONING_SCRIPT.",
                )

        try:
            logs = await services_collection.switches.list_provisioning_logs(
                switch_uuid=switch_uuid,
                page=pagination_params.page,
                size=pagination_params.size,
                log_category=category,
            )
        except NotFoundException:
            raise NotFoundException(  # noqa: B904
                details=[
                    BaseExceptionDetail(
                        type="SwitchNotFound",
                        message=f"Switch with uuid '{switch_uuid}' was not found.",
                    )
                ]
            )

        base_href = f"{V3_API_PREFIX}/switches/{switch_uuid}/logs"
        return SwitchLogsListResponse(
            items=[
                SwitchLogResponse.from_model(
                    log=log,
                    switch_uuid=switch_uuid,
                    self_base_hyperlink=base_href,
                )
                for log in logs.items
            ],
            total=logs.total,
            next=(
                f"{base_href}?{pagination_params.to_next_href_format()}"
                if logs.has_next(
                    pagination_params.page, pagination_params.size
                )
                else None
            ),
        )
