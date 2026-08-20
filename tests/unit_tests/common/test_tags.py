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

import pytest
from pytest_mock import MockerFixture
from sqlalchemy import column, Integer, table
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm.session import Session

from superset.common.tags import add_favorites, add_owners, add_types, tag_name
from superset.tags.models import ObjectType, Tag, TaggedObject, TagType


def _setup(session: Session) -> None:
    """
    Create the metadata tables plus a user that owns and favorited a chart.
    """

    from flask_appbuilder import Model

    from superset.models.core import FavStar  # noqa: F401
    from superset.models.dashboard import Dashboard  # noqa: F401
    from superset.models.slice import Slice  # noqa: F401
    from superset.models.sql_lab import SavedQuery  # noqa: F401

    metadata = Model.metadata  # pylint: disable=no-member
    metadata.create_all(session.get_bind())

    session.execute(
        metadata.tables["ab_user"].insert(),
        [
            {
                "id": 1,
                "first_name": "alice",
                "last_name": "doe",
                "username": "alice",
                "password": "x",
                "active": True,
                "email": "alice@example.com",
            }
        ],
    )
    session.execute(
        metadata.tables["slices"].insert(),
        [{"id": 10, "slice_name": "chart", "created_by_fk": 1}],
    )
    session.execute(
        metadata.tables["dashboards"].insert(),
        [{"id": 20, "dashboard_title": "dash", "created_by_fk": 1}],
    )
    session.execute(
        metadata.tables["favstar"].insert(),
        [{"id": 1, "user_id": 1, "class_name": "slice", "obj_id": 10}],
    )
    session.commit()


def _tags(session: Session, name: str) -> list[Tag]:
    session.expire_all()
    return session.query(Tag).filter(Tag.name == name).all()


def _associations(session: Session, name: str) -> set[tuple[ObjectType, int]]:
    session.expire_all()
    return {
        (tagged_object.object_type, tagged_object.object_id)
        for tagged_object in session.query(TaggedObject)
        .join(Tag)
        .filter(Tag.name == name)
        .all()
    }


@pytest.mark.parametrize(
    "dialect,expected",
    [
        (postgresql.dialect(), "%(param_1)s || CAST(favstar.user_id AS VARCHAR)"),
        (mysql.dialect(), "concat(%s, CAST(favstar.user_id AS CHAR))"),
        (sqlite.dialect(), "? || CAST(favstar.user_id AS VARCHAR)"),
    ],
)
def test_tag_name_concatenates_portably(dialect: Dialect, expected: str) -> None:
    """
    Implicit tag names compile to a concatenation, not to a numeric addition.
    """

    favstar = table("favstar", column("user_id", Integer))
    compiled = str(
        tag_name("favorited_by:", favstar.c.user_id).compile(dialect=dialect)
    )

    assert compiled == expected


def test_add_types(session: Session) -> None:
    """
    Type tags are created and associated with the objects of that type.
    """

    from flask_appbuilder import Model

    _setup(session)

    add_types(Model.metadata)  # pylint: disable=no-member

    tags = _tags(session, "type:chart")
    assert len(tags) == 1
    assert tags[0].type == TagType.type
    assert _associations(session, "type:chart") == {(ObjectType.chart, 10)}
    assert _associations(session, "type:dashboard") == {(ObjectType.dashboard, 20)}
    assert _associations(session, "type:query") == set()


def test_add_owners(session: Session) -> None:
    """
    Editor tags are created and associated with the objects the user created.

    The ``editor:<user_id>`` name has to be built as a string concatenation; a
    string-plus-integer expression matches no rows (or fails outright).
    """

    from flask_appbuilder import Model

    _setup(session)

    add_owners(Model.metadata)  # pylint: disable=no-member

    tags = _tags(session, "editor:1")
    assert len(tags) == 1
    assert tags[0].type == TagType.editor
    assert _associations(session, "editor:1") == {
        (ObjectType.chart, 10),
        (ObjectType.dashboard, 20),
    }


def test_add_favorites(session: Session) -> None:
    """
    Favorite tags are created with ``TagType.favorited_by`` and associated.
    """

    from flask_appbuilder import Model

    _setup(session)

    add_favorites(Model.metadata)  # pylint: disable=no-member

    tags = _tags(session, "favorited_by:1")
    assert len(tags) == 1
    assert tags[0].type == TagType.favorited_by
    assert _associations(session, "favorited_by:1") == {(ObjectType.chart, 10)}


def test_add_favorites_repairs_misclassified_tag(session: Session) -> None:
    """
    Only favorite tags stored as ``TagType.type`` are repaired.
    """

    from flask_appbuilder import Model

    _setup(session)
    session.add(Tag(name="favorited_by:1", type=TagType.type))
    session.add(Tag(name="type:chart", type=TagType.type))
    session.add(Tag(name="my tag", type=TagType.custom))
    session.commit()

    add_favorites(Model.metadata)  # pylint: disable=no-member

    tags = _tags(session, "favorited_by:1")
    assert len(tags) == 1
    assert tags[0].type == TagType.favorited_by
    assert _associations(session, "favorited_by:1") == {(ObjectType.chart, 10)}

    # unrelated tags are untouched
    assert _tags(session, "type:chart")[0].type == TagType.type
    assert _tags(session, "my tag")[0].type == TagType.custom


def test_backfills_commit_independently(
    session: Session, mocker: MockerFixture
) -> None:
    """
    A failing backfill doesn't discard the tags written by the earlier ones.
    """

    from flask_appbuilder import Model

    _setup(session)
    metadata = Model.metadata  # pylint: disable=no-member

    add_types(metadata)
    mocker.patch(
        "superset.common.tags.create_tags",
        side_effect=RuntimeError("boom"),
    )

    with pytest.raises(RuntimeError):
        add_favorites(metadata)

    assert len(_tags(session, "type:chart")) == 1
    assert _associations(session, "type:chart") == {(ObjectType.chart, 10)}


def test_sync_tags_is_idempotent(session: Session) -> None:
    """
    Re-running the backfill creates no duplicate tags or associations.
    """

    from flask_appbuilder import Model

    _setup(session)
    metadata = Model.metadata  # pylint: disable=no-member

    for _ in range(2):
        add_types(metadata)
        add_owners(metadata)
        add_favorites(metadata)

    session.expire_all()
    assert session.query(Tag).count() == len(ObjectType.__members__) + 2
    assert _associations(session, "type:chart") == {(ObjectType.chart, 10)}
    assert _associations(session, "editor:1") == {
        (ObjectType.chart, 10),
        (ObjectType.dashboard, 20),
    }
    assert _associations(session, "favorited_by:1") == {(ObjectType.chart, 10)}
    assert session.query(TaggedObject).count() == 5
