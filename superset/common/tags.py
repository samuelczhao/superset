# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from typing import Any

from sqlalchemy import case, cast, MetaData, String
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql import and_, ColumnElement, func, join, literal, select

from superset.exceptions import SupersetException
from superset.extensions import db
from superset.tags.models import ObjectType, TagType
from superset.utils.decorators import transaction


class ReservedTagNameError(SupersetException):  # noqa: N818
    """
    An implicit tag name is already taken by a tag of an unexpected type.
    """


def tag_name(prefix: str, column: ColumnElement[Any]) -> ColumnElement[str]:
    """
    Build an implicit tag name such as ``editor:1`` from a column.

    The column is cast to a string so that the expression compiles to a
    concatenation (``||`` or ``CONCAT``) on every supported dialect rather than
    to a numeric addition.
    """

    return literal(prefix) + cast(column, String)


def existing_tags(tag: Any, names: set[str]) -> dict[str, TagType]:
    """
    Read the type of the tags that already hold one of ``names``.
    """

    return dict(
        db.session.execute(
            select(tag.c.name, tag.c.type).where(tag.c.name.in_(names))
        ).all()
    )


def reconcile_tags(
    tag: Any,
    existing: dict[str, TagType],
    type_: TagType,
    repair_from: tuple[TagType, ...],
) -> None:
    """
    Bring existing tags holding a reserved name to ``type_``, or refuse to.

    Only the types listed in ``repair_from`` are repaired, since those are the
    ones a previous backfill misclassified. Any other type is an explicit
    collision: reusing the tag would attach implicit associations to a tag the
    application doesn't manage as implicit, and mutating it would destroy user
    data.
    """

    if colliding := sorted(
        f"{name} ({existing_type})"
        for name, existing_type in existing.items()
        if existing_type != type_ and existing_type not in repair_from
    ):
        raise ReservedTagNameError(
            f"Tags with a reserved name exist with an unexpected type, expected "
            f"{type_}: {', '.join(colliding)}. Rename or remove them and re-run."
        )

    if mistyped := sorted(
        name for name, existing_type in existing.items() if existing_type != type_
    ):
        db.session.execute(
            tag.update().where(tag.c.name.in_(mistyped)).values(type=type_)
        )


def create_tags(
    tag: Any,
    names: set[str],
    type_: TagType,
    repair_from: tuple[TagType, ...] = (),
) -> None:
    """
    Create the implicit tags that don't exist yet.

    Tag names are unique, so an existing tag is reconciled in place (see
    :func:`reconcile_tags`) instead of being inserted again. Each insert runs in
    a savepoint so that a tag created concurrently doesn't abort the surrounding
    transaction; the loser of such a race reconciles the row that won instead of
    ignoring it, so a concurrent tag of an unexpected type still raises.

    Associations, unlike tag rows, are not protected against concurrent writes:
    this is an administrative command, and objects created or favorited while it
    runs are picked up by the next run.
    """

    if not names:
        return

    existing = existing_tags(tag, names)
    reconcile_tags(tag, existing, type_, repair_from)

    for name in sorted(names - existing.keys()):
        try:
            with db.session.begin_nested():
                db.session.execute(tag.insert().values(name=name, type=type_))
        except IntegrityError:  # created concurrently
            reconcile_tags(tag, existing_tags(tag, {name}), type_, repair_from)


def add_types_to_charts(
    metadata: MetaData, tag: Any, tagged_object: Any, columns: list[str]
) -> None:
    slices = metadata.tables["slices"]

    charts = (
        select(
            tag.c.id.label("tag_id"),
            slices.c.id.label("object_id"),
            literal(ObjectType.chart.name).label("object_type"),
        )
        .select_from(
            join(
                join(slices, tag, tag.c.name == "type:chart"),
                tagged_object,
                and_(
                    tagged_object.c.tag_id == tag.c.id,
                    tagged_object.c.object_id == slices.c.id,
                    tagged_object.c.object_type == "chart",
                ),
                isouter=True,
                full=False,
            )
        )
        .where(tagged_object.c.tag_id.is_(None))
    )
    query = tagged_object.insert().from_select(columns, charts)
    db.session.execute(query)


def add_types_to_dashboards(
    metadata: MetaData, tag: Any, tagged_object: Any, columns: list[str]
) -> None:
    dashboard_table = metadata.tables["dashboards"]

    dashboards = (
        select(
            tag.c.id.label("tag_id"),
            dashboard_table.c.id.label("object_id"),
            literal(ObjectType.dashboard.name).label("object_type"),
        )
        .select_from(
            join(
                join(dashboard_table, tag, tag.c.name == "type:dashboard"),
                tagged_object,
                and_(
                    tagged_object.c.tag_id == tag.c.id,
                    tagged_object.c.object_id == dashboard_table.c.id,
                    tagged_object.c.object_type == "dashboard",
                ),
                isouter=True,
                full=False,
            )
        )
        .where(tagged_object.c.tag_id.is_(None))
    )
    query = tagged_object.insert().from_select(columns, dashboards)
    db.session.execute(query)


def add_types_to_saved_queries(
    metadata: MetaData, tag: Any, tagged_object: Any, columns: list[str]
) -> None:
    saved_query = metadata.tables["saved_query"]

    saved_queries = (
        select(
            tag.c.id.label("tag_id"),
            saved_query.c.id.label("object_id"),
            literal(ObjectType.query.name).label("object_type"),
        )
        .select_from(
            join(
                join(saved_query, tag, tag.c.name == "type:query"),
                tagged_object,
                and_(
                    tagged_object.c.tag_id == tag.c.id,
                    tagged_object.c.object_id == saved_query.c.id,
                    tagged_object.c.object_type == "query",
                ),
                isouter=True,
                full=False,
            )
        )
        .where(tagged_object.c.tag_id.is_(None))
    )
    query = tagged_object.insert().from_select(columns, saved_queries)
    db.session.execute(query)


def add_types_to_datasets(
    metadata: MetaData, tag: Any, tagged_object: Any, columns: list[str]
) -> None:
    tables = metadata.tables["tables"]

    datasets = (
        select(
            tag.c.id.label("tag_id"),
            tables.c.id.label("object_id"),
            literal(ObjectType.dataset.name).label("object_type"),
        )
        .select_from(
            join(
                join(tables, tag, tag.c.name == "type:dataset"),
                tagged_object,
                and_(
                    tagged_object.c.tag_id == tag.c.id,
                    tagged_object.c.object_id == tables.c.id,
                    tagged_object.c.object_type == "dataset",
                ),
                isouter=True,
                full=False,
            )
        )
        .where(tagged_object.c.tag_id.is_(None))
    )
    query = tagged_object.insert().from_select(columns, datasets)
    db.session.execute(query)


@transaction()
def add_types(metadata: MetaData) -> None:
    """
    Tag every object according to its type:

      INSERT INTO tagged_object (tag_id, object_id, object_type)
      SELECT
        tag.id AS tag_id,
        slices.id AS object_id,
        'chart' AS object_type
      FROM slices
      JOIN tag
        ON tag.name = 'type:chart'
      LEFT OUTER JOIN tagged_object
        ON tagged_object.tag_id = tag.id
        AND tagged_object.object_id = slices.id
        AND tagged_object.object_type = 'chart'
      WHERE tagged_object.tag_id IS NULL;

      INSERT INTO tagged_object (tag_id, object_id, object_type)
      SELECT
        tag.id AS tag_id,
        dashboards.id AS object_id,
        'dashboard' AS object_type
      FROM dashboards
      JOIN tag
      ON tag.name = 'type:dashboard'
      LEFT OUTER JOIN tagged_object
        ON tagged_object.tag_id = tag.id
        AND tagged_object.object_id = dashboards.id
        AND tagged_object.object_type = 'dashboard'
      WHERE tagged_object.tag_id IS NULL;

      INSERT INTO tagged_object (tag_id, object_id, object_type)
      SELECT
        tag.id AS tag_id,
        saved_query.id AS object_id,
        'query' AS object_type
      FROM saved_query
      JOIN tag
      ON tag.name = 'type:query';
      LEFT OUTER JOIN tagged_object
        ON tagged_object.tag_id = tag.id
        AND tagged_object.object_id = saved_query.id
        AND tagged_object.object_type = 'query'
      WHERE tagged_object.tag_id IS NULL;

      INSERT INTO tagged_object (tag_id, object_id, object_type)
      SELECT
        tag.id AS tag_id,
        tables.id AS object_id,
        'dataset' AS object_type
      FROM tables
      JOIN tag
        ON tag.name = 'type:dataset'
      LEFT OUTER JOIN tagged_object
        ON tagged_object.tag_id = tag.id
        AND tagged_object.object_id = tables.id
        AND tagged_object.object_type = 'dataset'
      WHERE tagged_object.tag_id IS NULL;

    """

    tag = metadata.tables["tag"]
    tagged_object = metadata.tables["tagged_object"]
    columns = ["tag_id", "object_id", "object_type"]

    # add a tag for each object type
    create_tags(
        tag, {f"type:{type_}" for type_ in ObjectType.__members__}, TagType.type
    )

    add_types_to_charts(metadata, tag, tagged_object, columns)
    add_types_to_dashboards(metadata, tag, tagged_object, columns)
    add_types_to_saved_queries(metadata, tag, tagged_object, columns)
    add_types_to_datasets(metadata, tag, tagged_object, columns)


def add_owners_to_charts(
    metadata: MetaData, tag: Any, tagged_object: Any, columns: list[str]
) -> None:
    slices = metadata.tables["slices"]

    charts = (
        select(
            tag.c.id.label("tag_id"),
            slices.c.id.label("object_id"),
            literal(ObjectType.chart.name).label("object_type"),
        )
        .select_from(
            join(
                join(
                    slices,
                    tag,
                    and_(
                        tag.c.type == TagType.editor,
                        tag.c.name == tag_name("editor:", slices.c.created_by_fk),
                    ),
                ),
                tagged_object,
                and_(
                    tagged_object.c.tag_id == tag.c.id,
                    tagged_object.c.object_id == slices.c.id,
                    tagged_object.c.object_type == "chart",
                ),
                isouter=True,
                full=False,
            )
        )
        .where(tagged_object.c.tag_id.is_(None))
    )
    query = tagged_object.insert().from_select(columns, charts)
    db.session.execute(query)


def add_owners_to_dashboards(
    metadata: MetaData, tag: Any, tagged_object: Any, columns: list[str]
) -> None:
    dashboard_table = metadata.tables["dashboards"]

    dashboards = (
        select(
            tag.c.id.label("tag_id"),
            dashboard_table.c.id.label("object_id"),
            literal(ObjectType.dashboard.name).label("object_type"),
        )
        .select_from(
            join(
                join(
                    dashboard_table,
                    tag,
                    and_(
                        tag.c.type == TagType.editor,
                        tag.c.name
                        == tag_name("editor:", dashboard_table.c.created_by_fk),
                    ),
                ),
                tagged_object,
                and_(
                    tagged_object.c.tag_id == tag.c.id,
                    tagged_object.c.object_id == dashboard_table.c.id,
                    tagged_object.c.object_type == "dashboard",
                ),
                isouter=True,
                full=False,
            )
        )
        .where(tagged_object.c.tag_id.is_(None))
    )
    query = tagged_object.insert().from_select(columns, dashboards)
    db.session.execute(query)


def add_owners_to_saved_queries(
    metadata: MetaData, tag: Any, tagged_object: Any, columns: list[str]
) -> None:
    saved_query = metadata.tables["saved_query"]

    saved_queries = (
        select(
            tag.c.id.label("tag_id"),
            saved_query.c.id.label("object_id"),
            literal(ObjectType.query.name).label("object_type"),
        )
        .select_from(
            join(
                join(
                    saved_query,
                    tag,
                    and_(
                        tag.c.type == TagType.editor,
                        tag.c.name == tag_name("editor:", saved_query.c.created_by_fk),
                    ),
                ),
                tagged_object,
                and_(
                    tagged_object.c.tag_id == tag.c.id,
                    tagged_object.c.object_id == saved_query.c.id,
                    tagged_object.c.object_type == "query",
                ),
                isouter=True,
                full=False,
            )
        )
        .where(tagged_object.c.tag_id.is_(None))
    )
    query = tagged_object.insert().from_select(columns, saved_queries)
    db.session.execute(query)


def add_owners_to_datasets(
    metadata: MetaData, tag: Any, tagged_object: Any, columns: list[str]
) -> None:
    tables = metadata.tables["tables"]

    datasets = (
        select(
            tag.c.id.label("tag_id"),
            tables.c.id.label("object_id"),
            literal(ObjectType.dataset.name).label("object_type"),
        )
        .select_from(
            join(
                join(
                    tables,
                    tag,
                    and_(
                        tag.c.type == TagType.editor,
                        tag.c.name == tag_name("editor:", tables.c.created_by_fk),
                    ),
                ),
                tagged_object,
                and_(
                    tagged_object.c.tag_id == tag.c.id,
                    tagged_object.c.object_id == tables.c.id,
                    tagged_object.c.object_type == "dataset",
                ),
                isouter=True,
                full=False,
            )
        )
        .where(tagged_object.c.tag_id.is_(None))
    )
    query = tagged_object.insert().from_select(columns, datasets)
    db.session.execute(query)


@transaction()
def add_owners(metadata: MetaData) -> None:
    """
    Tag every object according to its editor:

      INSERT INTO tagged_object (tag_id, object_id, object_type)
      SELECT
        tag.id AS tag_id,
        slices.id AS object_id,
        'chart' AS object_type
      FROM slices
      JOIN tag
      ON tag.name = CONCAT('editor:', slices.created_by_fk)
      LEFT OUTER JOIN tagged_object
        ON tagged_object.tag_id = tag.id
        AND tagged_object.object_id = slices.id
        AND tagged_object.object_type = 'chart'
      WHERE tagged_object.tag_id IS NULL;

      SELECT
        tag.id AS tag_id,
        dashboards.id AS object_id,
        'dashboard' AS object_type
      FROM dashboards
      JOIN tag
      ON tag.name = CONCAT('editor:', dashboards.created_by_fk)
      LEFT OUTER JOIN tagged_object
        ON tagged_object.tag_id = tag.id
        AND tagged_object.object_id = dashboards.id
        AND tagged_object.object_type = 'dashboard'
      WHERE tagged_object.tag_id IS NULL;

      SELECT
        tag.id AS tag_id,
        saved_query.id AS object_id,
        'query' AS object_type
      FROM saved_query
      JOIN tag
      ON tag.name = CONCAT('editor:', saved_query.created_by_fk)
      LEFT OUTER JOIN tagged_object
        ON tagged_object.tag_id = tag.id
        AND tagged_object.object_id = saved_query.id
        AND tagged_object.object_type = 'query'
      WHERE tagged_object.tag_id IS NULL;

      SELECT
        tag.id AS tag_id,
        tables.id AS object_id,
        'dataset' AS object_type
      FROM tables
      JOIN tag
      ON tag.name = CONCAT('editor:', tables.created_by_fk)
      LEFT OUTER JOIN tagged_object
        ON tagged_object.tag_id = tag.id
        AND tagged_object.object_id = tables.id
        AND tagged_object.object_type = 'dataset'
      WHERE tagged_object.tag_id IS NULL;

    """

    tag = metadata.tables["tag"]
    tagged_object = metadata.tables["tagged_object"]
    users = metadata.tables["ab_user"]
    columns = ["tag_id", "object_id", "object_type"]

    # create an implicit tag for each user
    ids = select(users.c.id)
    names = {f"editor:{id_}" for (id_,) in db.session.execute(ids)}
    create_tags(tag, names, TagType.editor)

    add_owners_to_charts(metadata, tag, tagged_object, columns)
    add_owners_to_dashboards(metadata, tag, tagged_object, columns)
    add_owners_to_saved_queries(metadata, tag, tagged_object, columns)
    add_owners_to_datasets(metadata, tag, tagged_object, columns)


@transaction()
def add_favorites(metadata: MetaData) -> None:
    """
    Tag every object that was favorited:

      INSERT INTO tagged_object (tag_id, object_id, object_type)
      SELECT
        tag.id AS tag_id,
        favstar.obj_id AS object_id,
        CASE
          WHEN LOWER(favstar.class_name) = 'slice' THEN 'chart'
          ELSE LOWER(favstar.class_name)
        END AS object_type
      FROM favstar
      JOIN tag
      ON tag.name = CONCAT('favorited_by:', favstar.user_id)
      LEFT OUTER JOIN tagged_object
        ON tagged_object.tag_id = tag.id
        AND tagged_object.object_id = favstar.obj_id
        AND tagged_object.object_type = CASE
          WHEN LOWER(favstar.class_name) = 'slice' THEN 'chart'
          ELSE LOWER(favstar.class_name)
        END
      WHERE tagged_object.tag_id IS NULL;

    """

    tag = metadata.tables["tag"]
    tagged_object = metadata.tables["tagged_object"]
    users = metadata.tables["ab_user"]
    favstar = metadata.tables["favstar"]
    columns = ["tag_id", "object_id", "object_type"]

    # create an implicit tag for each user, repairing tags that a previous
    # backfill misclassified as ``TagType.type``
    ids = select(users.c.id)
    names = {f"favorited_by:{id_}" for (id_,) in db.session.execute(ids)}
    create_tags(tag, names, TagType.favorited_by, repair_from=(TagType.type,))

    # ``favstar.class_name`` holds the model name, which for charts ("slice")
    # differs from the object type stored on ``tagged_object``
    class_name = func.lower(favstar.c.class_name)
    object_type = case(
        (class_name == "slice", literal(ObjectType.chart.name)),
        else_=class_name,
    )

    favstars = (
        select(
            tag.c.id.label("tag_id"),
            favstar.c.obj_id.label("object_id"),
            object_type.label("object_type"),
        )
        .select_from(
            join(
                join(
                    favstar,
                    tag,
                    and_(
                        tag.c.type == TagType.favorited_by,
                        tag.c.name == tag_name("favorited_by:", favstar.c.user_id),
                    ),
                ),
                tagged_object,
                and_(
                    tagged_object.c.tag_id == tag.c.id,
                    tagged_object.c.object_id == favstar.c.obj_id,
                    tagged_object.c.object_type == object_type,
                ),
                isouter=True,
                full=False,
            )
        )
        .where(tagged_object.c.tag_id.is_(None))
    )
    query = tagged_object.insert().from_select(columns, favstars)
    db.session.execute(query)
