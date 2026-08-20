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

from unittest.mock import MagicMock, patch


def _run_chart_export(
    include_tags: bool = True, feature_enabled: bool = True
) -> tuple[list[str], MagicMock]:
    from superset.commands.chart.export import ExportChartsCommand

    chart = MagicMock()
    chart.id = 1
    chart.slice_name = "Test Chart"
    chart.table = None

    export_tags = MagicMock()
    export_tags.run.return_value = iter([("tags.yaml", lambda: "")])

    with (
        patch(
            "superset.commands.chart.export.ChartDAO.find_by_ids",
            return_value=[chart],
        ),
        patch(
            "superset.commands.chart.export.ExportTagsCommand",
            return_value=export_tags,
        ),
        patch(
            "superset.commands.chart.export.feature_flag_manager.is_feature_enabled",
            return_value=feature_enabled,
        ),
    ):
        files = [
            file_name
            for file_name, _ in ExportChartsCommand(
                [chart.id], include_tags=include_tags
            ).run()
        ]

    return files, export_tags


def test_export_charts_includes_tags_by_default() -> None:
    files, _ = _run_chart_export()

    assert "tags.yaml" in files


def test_export_charts_can_exclude_tags() -> None:
    files, _ = _run_chart_export(include_tags=False)

    assert "tags.yaml" not in files


def test_export_charts_tag_setting_is_isolated_per_instance() -> None:
    from superset.commands.chart.export import ExportChartsCommand

    assert not hasattr(ExportChartsCommand, "_include_tags")
    assert not hasattr(ExportChartsCommand, "disable_tag_export")
    assert not hasattr(ExportChartsCommand, "enable_tag_export")

    files, _ = _run_chart_export(include_tags=False)
    assert "tags.yaml" not in files

    files, _ = _run_chart_export()
    assert "tags.yaml" in files


def test_export_charts_respects_disabled_tagging_system() -> None:
    for include_tags in (True, False):
        files, export_tags = _run_chart_export(
            include_tags=include_tags, feature_enabled=False
        )

        assert "tags.yaml" not in files
        export_tags.run.assert_not_called()
