import unittest

from pydantic import ValidationError

from app.schemas.formats_pydantic import (
    AxisPlanAddition,
    AxisReasoningOutput,
    PlanOutput,
    QueryRun,
    TaskSpec,
)
from app.rendering.render_todo import render_todo
from app.prompts.system_prompts import planner_system_prompt


def task(**updates) -> TaskSpec:
    values = {
        "id": "t1",
        "title": "Gather evidence",
        "agent": "web_search",
        "query": "Gather sourced evidence.",
        "expects": "outputs/t1_evidence.md",
    }
    values.update(updates)
    return TaskSpec(**values)


class AxisPlanningTests(unittest.TestCase):
    def test_checkpoint_requires_focus(self):
        with self.assertRaises(ValidationError):
            task(axis_checkpoint=True)

    def test_non_checkpoint_discards_focus(self):
        spec = task(axis_focus="unused")
        self.assertIsNone(spec.axis_focus)

    def test_axis_output_is_one_detailed_string(self):
        output = AxisReasoningOutput(reasoning="Investigate cash conversion.")
        self.assertEqual(output.model_dump(), {
            "reasoning": "Investigate cash conversion.",
        })

    def test_addition_requires_at_least_one_task(self):
        with self.assertRaises(ValidationError):
            AxisPlanAddition(tasks=[])

    def test_axis_fields_are_not_rendered_in_user_todo(self):
        checkpoint = task(
            axis_checkpoint=True,
            axis_focus="Decision to revisit: risk-adjusted comparison.",
        )
        run = QueryRun(
            user_query="Compare two companies.",
            goal="Compare two companies.",
            workspace="/tmp/axis-test",
            started_at="2026-06-15T00:00:00+00:00",
            plan=PlanOutput(tasks=[checkpoint]),
            workspace_id="test",
            user_id="00000000-0000-0000-0000-000000000000",
            status="running",
        )

        rendered = render_todo(run)

        self.assertNotIn("axis_checkpoint", rendered)
        self.assertNotIn("axis_focus", rendered)
        self.assertNotIn("risk-adjusted comparison", rendered)

        serialized = checkpoint.model_dump()
        self.assertNotIn("axis_checkpoint", serialized)
        self.assertNotIn("axis_focus", serialized)
        self.assertIn("axis_checkpoint", TaskSpec.model_json_schema()["properties"])

    def test_planner_prompt_uses_injected_doc_inventory(self):
        self.assertIn("Available workspace documents context", planner_system_prompt)
        self.assertIn("There is no document lookup tool", planner_system_prompt)
        self.assertIn("top_level_summary", planner_system_prompt)
        self.assertNotIn("fetch_doc_ids", planner_system_prompt)


if __name__ == "__main__":
    unittest.main()
