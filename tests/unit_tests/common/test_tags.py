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

from sqlalchemy.orm.session import Session

from superset.common.tags import add_favorites
from superset.tags.models import Tag, TagType


def _setup(session: Session) -> None:
    from flask_appbuilder import Model

    from superset.models.core import FavStar  # noqa: F401

    Model.metadata.create_all(session.get_bind())  # pylint: disable=no-member

    session.execute(
        Model.metadata.tables["ab_user"].insert(),  # pylint: disable=no-member
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
    session.add(FavStar(user_id=1, class_name="slice", obj_id=10))
    session.flush()


def _favorite_tags(session: Session) -> list[Tag]:
    return session.query(Tag).filter(Tag.name == "favorited_by:1").all()


def test_add_favorites_creates_favorited_by_tag(session: Session) -> None:
    """
    The backfill must classify ``favorited_by:`` tags as ``TagType.favorited_by``.
    """
    from flask_appbuilder import Model

    _setup(session)

    add_favorites(Model.metadata)  # pylint: disable=no-member

    tags = _favorite_tags(session)
    assert len(tags) == 1
    assert tags[0].type == TagType.favorited_by


def test_add_favorites_repairs_misclassified_tag(session: Session) -> None:
    """
    Tags backfilled with the wrong type are repaired instead of duplicated.
    """
    from flask_appbuilder import Model

    _setup(session)
    session.add(Tag(name="favorited_by:1", type=TagType.type))
    session.add(Tag(name="type:chart", type=TagType.type))
    session.add(Tag(name="my tag", type=TagType.custom))
    session.flush()

    add_favorites(Model.metadata)  # pylint: disable=no-member

    tags = _favorite_tags(session)
    assert len(tags) == 1
    assert tags[0].type == TagType.favorited_by

    # unrelated tags are untouched
    assert (
        session.query(Tag).filter(Tag.name == "type:chart").one().type == TagType.type
    )
    assert session.query(Tag).filter(Tag.name == "my tag").one().type == TagType.custom


def test_add_favorites_is_idempotent(session: Session) -> None:
    """
    Re-running the backfill creates no duplicate tags.
    """
    from flask_appbuilder import Model

    _setup(session)

    add_favorites(Model.metadata)  # pylint: disable=no-member
    add_favorites(Model.metadata)  # pylint: disable=no-member

    tags = _favorite_tags(session)
    assert len(tags) == 1
    assert tags[0].type == TagType.favorited_by
