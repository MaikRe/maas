#  Copyright 2026 Canonical Ltd.  This software is licensed under the
#  GNU Affero General Public License version 3 (see the file LICENSE).

import uuid
from operator import eq

from sqlalchemy import func, Select, select, Table

from maasservicelayer.db.filters import Clause, ClauseFactory
from maasservicelayer.db.repositories.base import BaseRepository
from maasservicelayer.db.tables import (
    BootResourceTable,
    SwitchLogsTable,
    SwitchScriptAssignmentTable,
    SwitchScriptsTable,
    SwitchTable,
)
from maasservicelayer.models.base import ListResult
from maasservicelayer.models.switches import (
    Switch,
    SwitchLog,
    SwitchScript,
    SwitchWithTargetImage,
)


class SwitchClauseFactory(ClauseFactory):
    @classmethod
    def with_id(cls, id: int) -> Clause:
        return Clause(condition=eq(SwitchTable.c.id, id))

    @classmethod
    def with_ids(cls, ids: list[int]) -> Clause:
        return Clause(condition=SwitchTable.c.id.in_(ids))

    @classmethod
    def with_uuid(cls, switch_uuid: uuid.UUID) -> Clause:
        return Clause(condition=eq(SwitchTable.c.switch_uuid, switch_uuid))


class SwitchLogClauseFactory(ClauseFactory):
    @classmethod
    def with_switch_id(cls, switch_id: int) -> Clause:
        return Clause(condition=eq(SwitchLogsTable.c.switch_id, switch_id))

    @classmethod
    def with_log_category(cls, log_category: str) -> Clause:
        return Clause(
            condition=eq(SwitchLogsTable.c.log_category, log_category)
        )


class SwitchesRepository(BaseRepository[Switch]):
    def get_repository_table(self) -> Table:
        return SwitchTable

    def get_model_factory(self) -> type[Switch]:
        return Switch

    @property
    def select_all_join_boot_resource(self) -> Select:
        return select(
            SwitchTable,
            BootResourceTable.c.name.label("target_image"),
        ).select_from(
            SwitchTable.outerjoin(
                BootResourceTable,
                SwitchTable.c.target_image_id == BootResourceTable.c.id,
            )
        )

    async def get_one_with_target_image(
        self, id: int
    ) -> SwitchWithTargetImage | None:
        stmt = self.select_all_join_boot_resource.where(SwitchTable.c.id == id)
        row = (await self.execute_stmt(stmt)).one_or_none()
        return (
            SwitchWithTargetImage(**row._asdict()) if row is not None else None
        )

    async def list_with_target_image(
        self, page: int, size: int
    ) -> ListResult[SwitchWithTargetImage]:
        total_stmt = select(func.count()).select_from(SwitchTable)
        total = (await self.execute_stmt(total_stmt)).scalar_one()

        stmt = self.select_all_join_boot_resource.offset(
            (page - 1) * size
        ).limit(size)
        result = (await self.execute_stmt(stmt)).all()
        return ListResult(
            items=[SwitchWithTargetImage(**row._asdict()) for row in result],
            total=total,
        )

    async def get_by_uuid(self, switch_uuid: uuid.UUID) -> Switch | None:
        stmt = select(SwitchTable).where(
            SwitchTable.c.switch_uuid == switch_uuid
        )
        row = (await self.execute_stmt(stmt)).one_or_none()
        return Switch(**row._asdict()) if row is not None else None

    async def get_script_for_switch(self, switch_id: int) -> SwitchScript | None:
        stmt = (
            select(SwitchScriptsTable)
            .select_from(
                SwitchScriptAssignmentTable.join(
                    SwitchScriptsTable,
                    SwitchScriptAssignmentTable.c.script_id
                    == SwitchScriptsTable.c.id,
                )
            )
            .where(SwitchScriptAssignmentTable.c.switch_id == switch_id)
        )
        row = (await self.execute_stmt(stmt)).one_or_none()
        return SwitchScript(**row._asdict()) if row is not None else None


class SwitchScriptsRepository(BaseRepository[SwitchScript]):
    def get_repository_table(self) -> Table:
        return SwitchScriptsTable

    def get_model_factory(self) -> type[SwitchScript]:
        return SwitchScript


class SwitchLogsRepository(BaseRepository[SwitchLog]):
    def get_repository_table(self) -> Table:
        return SwitchLogsTable

    def get_model_factory(self) -> type[SwitchLog]:
        return SwitchLog

    async def list_for_switch(
        self,
        switch_id: int,
        page: int,
        size: int,
        log_category: str | None = None,
    ) -> ListResult[SwitchLog]:
        base_where = SwitchLogsTable.c.switch_id == switch_id
        if log_category:
            base_where = base_where & (
                SwitchLogsTable.c.log_category == log_category
            )

        total_stmt = select(func.count()).select_from(SwitchLogsTable).where(
            base_where
        )
        total = (await self.execute_stmt(total_stmt)).scalar_one()

        stmt = (
            select(SwitchLogsTable)
            .where(base_where)
            .order_by(SwitchLogsTable.c.created.asc())
            .offset((page - 1) * size)
            .limit(size)
        )
        result = (await self.execute_stmt(stmt)).all()
        return ListResult(
            items=[SwitchLog(**row._asdict()) for row in result],
            total=total,
        )

