from datetime import date, time, timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import (
    AssignmentSectionMembership,
    ChecklistDefinition,
    ChecklistInstance,
    ChecklistItem,
    ChecklistSection,
    Program,
    ProgramMembership,
    ProgramRole,
    ReportCadence,
    ScheduledReportDelivery,
    SectionTaskMembership,
    Shift,
    StaffCategory,
    StaffMember,
    TaskDefinition,
    TaskState,
)
from .reporting import ReportPeriod, build_report, period_from_params, scheduled_periods
from .services import change_item_state, resolve_checklist


class ReportingFixtureMixin:
    operational_date = date(2026, 9, 15)

    def setUp(self):
        User = get_user_model()
        self.program_a = Program.objects.create(name="Program Alpha", slug="alpha")
        self.program_b = Program.objects.create(name="Program Beta", slug="beta")
        self.manager_a = User.objects.create_user(
            "manager-a", email="manager-a@example.com", password="password"
        )
        self.manager_b = User.objects.create_user(
            "manager-b", email="manager-b@example.com", password="password"
        )
        ProgramMembership.objects.create(
            user=self.manager_a,
            program=self.program_a,
            role=ProgramRole.MANAGER,
            receive_scheduled_reports=True,
        )
        ProgramMembership.objects.create(
            user=self.manager_b,
            program=self.program_b,
            role=ProgramRole.MANAGER,
            receive_scheduled_reports=True,
        )
        self.operator = User.objects.create_user("operator", password="password")
        ProgramMembership.objects.create(
            user=self.operator,
            program=self.program_a,
            role=ProgramRole.OPERATIONAL,
        )
        self.staff_a = StaffMember.objects.create(
            program=self.program_a, first_name="Alfred", last_name="Sampare"
        )
        self.staff_a2 = StaffMember.objects.create(
            program=self.program_a, first_name="Chelsea", last_name="Brown"
        )
        self.staff_b = StaffMember.objects.create(
            program=self.program_b, first_name="Gary", last_name="Hill"
        )
        self.shift = Shift.objects.create(
            name="Morning", start_time=time(7), end_time=time(15), sort_order=10
        )
        self.category_a = StaffCategory.objects.create(
            program=self.program_a, name="Support", slug="support"
        )
        self.category_b = StaffCategory.objects.create(
            program=self.program_b, name="Support", slug="support"
        )
        self.definition_a, self.regular_a, self.optional_a = self._definition(
            self.category_a, "Alpha"
        )
        self.definition_b, self.regular_b, self.optional_b = self._definition(
            self.category_b, "Beta"
        )

    def _definition(self, category, prefix):
        definition = ChecklistDefinition.objects.create(
            name=f"{prefix} checklist", category=category, shift=self.shift
        )
        section = ChecklistSection.objects.create(name=f"{prefix} section", sort_order=10)
        AssignmentSectionMembership.objects.create(
            assignment=definition, section=section, sort_order=10
        )
        regular = TaskDefinition.objects.create(label=f"{prefix} regular")
        optional = TaskDefinition.objects.create(label=f"{prefix} optional", allow_na=True)
        SectionTaskMembership.objects.create(section=section, task=regular, sort_order=10)
        SectionTaskMembership.objects.create(section=section, task=optional, sort_order=20)
        regular.fixture_section = section
        optional.fixture_section = section
        return definition, regular, optional

    def report_a(self, **filters):
        return build_report(
            program=self.program_a,
            period=ReportPeriod(
                "daily", self.operational_date, self.operational_date, "fixture"
            ),
            filters=filters,
        )

    def contribute(self, item, staff, state):
        return change_item_state(
            item_id=item.pk,
            actor=self.operator,
            staff_member=staff,
            new_state=state,
        )


class ReportCalculationTests(ReportingFixtureMixin, TestCase):
    def ordered_definitions(self):
        self.definition_a.sort_order = 40
        self.definition_a.save(update_fields=("sort_order",))
        front_desk = StaffCategory.objects.create(
            program=self.program_a, name="Front Desk", slug="front-desk"
        )
        front_definition, _, _ = self._definition(front_desk, "Front Desk")
        front_definition.sort_order = 10
        front_definition.save(update_fields=("sort_order",))
        awake_night = StaffCategory.objects.create(
            program=self.program_a, name="Awake Night", slug="awake-night"
        )
        awake_definition, _, _ = self._definition(awake_night, "Awake Night")
        awake_definition.sort_order = 60
        awake_definition.save(update_fields=("sort_order",))
        return front_definition, self.definition_a, awake_definition

    def test_report_rows_follow_shift_assignment_order_not_position_name(self):
        definitions = self.ordered_definitions()
        for definition in definitions:
            resolve_checklist(definition, self.operational_date)

        rows = self.report_a()["rows"]

        self.assertEqual(
            [row["category"] for row in rows],
            ["Front Desk", "Support", "Awake Night"],
        )

    def test_multi_day_report_orders_by_date_then_shift_assignment(self):
        definitions = self.ordered_definitions()
        second_date = self.operational_date + timedelta(days=1)
        for operational_date in (self.operational_date, second_date):
            for definition in definitions:
                resolve_checklist(definition, operational_date)

        rows = build_report(
            program=self.program_a,
            period=ReportPeriod(
                "custom", self.operational_date, second_date, "two days"
            ),
        )["rows"]

        self.assertEqual(
            [(row["operational_date"], row["category"]) for row in rows],
            [
                (self.operational_date, "Front Desk"),
                (self.operational_date, "Support"),
                (self.operational_date, "Awake Night"),
                (second_date, "Front Desk"),
                (second_date, "Support"),
                (second_date, "Awake Night"),
            ],
        )

    def test_completed_partial_na_and_multiple_contributors(self):
        instance = resolve_checklist(self.definition_a, self.operational_date)
        regular = instance.items.get(source_task=self.regular_a)
        optional = instance.items.get(source_task=self.optional_a)
        self.contribute(regular, self.staff_a, TaskState.COMPLETED)
        self.contribute(optional, self.staff_a2, TaskState.NOT_APPLICABLE)
        row = self.report_a()["rows"][0]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["applicable_count"], 1)
        self.assertEqual(row["completed_count"], 1)
        self.assertEqual(row["na_count"], 1)
        self.assertEqual(row["completion_percentage"], 100.0)
        self.assertCountEqual(row["contributors"], ["Alfred Sampare", "Chelsea Brown"])
        optional_task = next(task for task in row["tasks"] if task["label"] == "Alpha optional")
        self.assertEqual(optional_task["state"], TaskState.NOT_APPLICABLE)
        self.assertEqual(optional_task["contributor"], "Chelsea Brown")
        self.assertEqual(
            optional_task["contributions"],
            [
                {
                    "staff": "Chelsea Brown",
                    "staff_id": self.staff_a2.pk,
                    "previous_state": TaskState.PENDING,
                    "new_state": TaskState.NOT_APPLICABLE,
                    "activity_text": "",
                    "created_at": optional_task["contributions"][0]["created_at"],
                }
            ],
        )

    def test_unfinished_checklist_lists_every_contributor(self):
        instance = resolve_checklist(self.definition_a, self.operational_date)
        regular = instance.items.get(source_task=self.regular_a)
        optional = instance.items.get(source_task=self.optional_a)
        self.contribute(regular, self.staff_a, TaskState.COMPLETED)
        self.contribute(optional, self.staff_a2, TaskState.COMPLETED)
        self.contribute(optional, self.staff_a2, TaskState.PENDING)
        row = self.report_a()["rows"][0]
        self.assertEqual(row["status"], "incomplete")
        self.assertCountEqual(row["contributors"], ["Alfred Sampare", "Chelsea Brown"])

    def test_no_contributors_and_late_entry_updates_live_report(self):
        instance = resolve_checklist(self.definition_a, self.operational_date)
        self.assertEqual(self.report_a()["rows"][0]["contributors"], [])
        self.contribute(
            instance.items.get(source_task=self.regular_a),
            self.staff_a,
            TaskState.COMPLETED,
        )
        self.assertEqual(self.report_a()["totals"]["completed"], 1)

    def test_report_filter_uses_operational_staff_identity(self):
        instance = resolve_checklist(self.definition_a, self.operational_date)
        self.contribute(
            instance.items.get(source_task=self.regular_a),
            self.staff_a,
            TaskState.COMPLETED,
        )
        report = self.report_a(staff=str(self.staff_a.pk))
        self.assertEqual(list(report["staff_choices"]), [self.staff_a])
        self.assertEqual(len(report["rows"]), 1)
        self.assertEqual(len(report["rows"][0]["filtered_tasks"]), 1)

    def test_category_shift_status_task_state_and_section_filters_are_scoped(self):
        instance = resolve_checklist(self.definition_a, self.operational_date)
        self.contribute(
            instance.items.get(source_task=self.regular_a),
            self.staff_a,
            TaskState.COMPLETED,
        )
        foreign_shift = Shift.objects.create(
            name="Evening", start_time=time(15), end_time=time(23), sort_order=20
        )
        self._definition_for_shift(self.category_b, foreign_shift, "Beta evening")

        self.assertEqual(len(self.report_a(category=str(self.category_a.pk))["rows"]), 1)
        self.assertEqual(self.report_a(category=str(self.category_b.pk))["rows"], [])
        self.assertEqual(len(self.report_a(shift=str(self.shift.pk))["rows"]), 1)
        self.assertEqual(self.report_a(shift=str(foreign_shift.pk))["rows"], [])
        self.assertEqual(len(self.report_a(status="incomplete")["rows"]), 1)
        self.assertEqual(self.report_a(status="completed")["rows"], [])

        completed = self.report_a(task_state=TaskState.COMPLETED)["rows"][0]
        pending = self.report_a(task_state=TaskState.PENDING)["rows"][0]
        section = self.report_a(section="Alpha section")["rows"][0]
        self.assertEqual([task["label"] for task in completed["filtered_tasks"]], ["Alpha regular"])
        self.assertEqual([task["label"] for task in pending["filtered_tasks"]], ["Alpha optional"])
        self.assertEqual(len(section["filtered_tasks"]), 2)
        self.assertEqual(self.report_a(section="Beta section")["rows"], [])

    def _definition_for_shift(self, category, shift, prefix):
        definition = ChecklistDefinition.objects.create(
            name=f"{prefix} checklist", category=category, shift=shift
        )
        section = ChecklistSection.objects.create(name=f"{prefix} section", sort_order=10)
        AssignmentSectionMembership.objects.create(
            assignment=definition, section=section, sort_order=10
        )
        task = TaskDefinition.objects.create(label=f"{prefix} regular")
        SectionTaskMembership.objects.create(section=section, task=task, sort_order=10)
        return definition

    def test_large_report_does_not_exceed_sqlite_parameter_limit(self):
        instance = resolve_checklist(self.definition_a, self.operational_date)
        sources = TaskDefinition.objects.bulk_create(
            [
                TaskDefinition(label=f"Scale task {index}")
                for index in range(1000)
            ]
        )
        ChecklistItem.objects.bulk_create(
            [
                ChecklistItem(
                    instance=instance,
                    source_task=source,
                    section_name_snapshot="Alpha section",
                    section_order_snapshot=10,
                    task_label_snapshot=source.label,
                    task_order_snapshot=100 + index,
                )
                for index, source in enumerate(sources)
            ]
        )

        report = self.report_a()

        self.assertEqual(report["totals"]["checklists"], 1)
        self.assertEqual(report["totals"]["pending"], 1002)


class PeriodBoundaryTests(TestCase):
    def test_all_web_period_boundaries(self):
        daily = period_from_params({"period": "daily", "date": "2026-09-15"})
        weekly = period_from_params({"period": "weekly", "date": "2026-09-16"})
        monthly = period_from_params({"period": "monthly", "date": "2024-02-17"})
        annual = period_from_params({"period": "annual", "date": "2024-06-12"})
        rolling = period_from_params({"period": "rolling365", "date": "2024-03-01"})
        self.assertEqual((daily.start, daily.end), (date(2026, 9, 15), date(2026, 9, 15)))
        self.assertEqual((weekly.start, weekly.end), (date(2026, 9, 14), date(2026, 9, 20)))
        self.assertEqual((monthly.start, monthly.end), (date(2024, 2, 1), date(2024, 2, 29)))
        self.assertEqual((annual.start, annual.end), (date(2024, 1, 1), date(2024, 12, 31)))
        self.assertEqual((rolling.start, rolling.end), (date(2023, 3, 3), date(2024, 3, 1)))
        self.assertEqual(monthly.selected_date, date(2024, 2, 17))
        self.assertEqual(annual.selected_date, date(2024, 6, 12))

        legacy_month = period_from_params({"period": "monthly", "month": "2025-04"})
        legacy_year = period_from_params({"period": "annual", "year": "2025"})
        self.assertEqual((legacy_month.start, legacy_month.end), (date(2025, 4, 1), date(2025, 4, 30)))
        self.assertEqual((legacy_year.start, legacy_year.end), (date(2025, 1, 1), date(2025, 12, 31)))

    def test_scheduled_periods_use_previous_complete_periods(self):
        monday = dict(scheduled_periods(date(2026, 9, 21)))
        self.assertEqual((monday["weekly"].start, monday["weekly"].end), (date(2026, 9, 14), date(2026, 9, 20)))
        january = dict(scheduled_periods(date(2027, 1, 1)))
        self.assertEqual((january["annual"].start, january["annual"].end), (date(2026, 1, 1), date(2026, 12, 31)))


class ReportSecurityAndExportTests(ReportingFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        alpha = resolve_checklist(self.definition_a, self.operational_date)
        self.contribute(
            alpha.items.get(source_task=self.regular_a),
            self.staff_a,
            TaskState.COMPLETED,
        )
        resolve_checklist(self.definition_b, self.operational_date)

    def test_manager_cannot_cross_program_by_program_or_instance_id(self):
        self.client.force_login(self.manager_a)
        self.assertEqual(
            self.client.get(reverse("reports"), {"program": self.program_b.pk}).status_code,
            403,
        )
        beta = ChecklistInstance.objects.get(program=self.program_b)
        self.assertEqual(
            self.client.get(
                reverse("report-detail", args=(beta.pk,)),
                {"date": self.operational_date},
            ).status_code,
            403,
        )

        category_tamper = self.client.get(
            reverse("reports"),
            {"date": self.operational_date, "category": self.category_b.pk},
        )
        self.assertEqual(category_tamper.status_code, 200)
        self.assertEqual(category_tamper.context["rows"], [])
        self.assertNotContains(category_tamper, "Beta regular")

        foreign_shift = Shift.objects.create(
            name="Evening", start_time=time(15), end_time=time(23), sort_order=20
        )
        foreign_definition = ChecklistDefinition.objects.create(
            name="Beta evening", category=self.category_b, shift=foreign_shift
        )
        foreign_section = ChecklistSection.objects.create(name="Foreign section", sort_order=10)
        AssignmentSectionMembership.objects.create(
            assignment=foreign_definition, section=foreign_section, sort_order=10
        )
        foreign_task = TaskDefinition.objects.create(label="Foreign task")
        SectionTaskMembership.objects.create(
            section=foreign_section, task=foreign_task, sort_order=10
        )
        shift_tamper = self.client.get(
            reverse("reports"),
            {"date": self.operational_date, "shift": foreign_shift.pk},
        )
        self.assertEqual(shift_tamper.status_code, 200)
        self.assertEqual(shift_tamper.context["rows"], [])
        self.assertNotContains(shift_tamper, "Foreign task")

    def test_operational_account_is_denied_and_admin_needs_no_membership(self):
        beta = ChecklistInstance.objects.get(program=self.program_b)
        self.client.force_login(self.operator)
        for url in (
            reverse("reports"),
            reverse("report-csv"),
            reverse("report-print"),
            reverse("report-detail", args=(beta.pk,)),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

        admin = get_user_model().objects.create_superuser(
            "site-admin", "site-admin@example.com", "password"
        )
        self.assertFalse(ProgramMembership.objects.filter(user=admin).exists())
        self.client.force_login(admin)
        response = self.client.get(
            reverse("reports"),
            {"program": self.program_a.pk, "date": self.operational_date},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 1)
        self.assertEqual(response.context["program"], self.program_a)

    def test_csv_and_print_are_scoped_and_use_staff_names(self):
        self.client.force_login(self.manager_a)
        params = {"date": self.operational_date, "staff": self.staff_a.pk}
        csv_response = self.client.get(reverse("report-csv"), params)
        content = csv_response.content.decode()
        self.assertIn("Alpha regular", content)
        self.assertIn("Alfred Sampare", content)
        self.assertNotIn("Beta regular", content)
        print_response = self.client.get(reverse("report-print"), params)
        self.assertEqual(print_response.status_code, 200)
        self.assertContains(print_response, "Alfred Sampare")

    def test_task_display_is_consistent_in_report_print_and_csv(self):
        alpha = ChecklistInstance.objects.get(program=self.program_a)
        item = alpha.items.get(source_task=self.regular_a)
        stored_snapshot = "Disinfect Phone/Door Handles; Check WISH/ComVida and DNA"
        item.task_label_snapshot = stored_snapshot
        item.save(update_fields=("task_label_snapshot",))
        self.client.force_login(self.manager_a)
        params = {"date": self.operational_date}

        detail = self.client.get(reverse("report-detail", args=(alpha.pk,)), params)
        printed = self.client.get(reverse("report-print"), params)
        csv_content = self.client.get(reverse("report-csv"), params).content.decode()
        expected = "Disinfect phone / door handles; check WISH / ComVida and DNA"
        self.assertContains(detail, expected)
        self.assertContains(printed, expected)
        self.assertIn(expected, csv_content)
        item.refresh_from_db()
        self.assertEqual(item.task_label_snapshot, stored_snapshot)

    def test_report_filter_structure_and_styles_prevent_overflow(self):
        self.client.force_login(self.manager_a)
        response = self.client.get(reverse("reports"), {"date": self.operational_date})
        self.assertContains(response, 'class="report-filters"')
        self.assertContains(response, 'class="report-filter-row report-filter-row-primary"')
        self.assertContains(response, 'class="report-filter-row report-filter-row-secondary"')
        content = response.content.decode()
        primary_start = content.index("report-filter-row-primary")
        secondary_start = content.index("report-filter-row-secondary")
        primary = content[primary_start:secondary_start]
        secondary = content[secondary_start:content.index("</form>", secondary_start)]
        self.assertLess(primary.index("Period"), primary.index("Date"))
        self.assertLess(primary.index("Date"), primary.index("Completion"))
        self.assertLess(primary.index("Completion"), primary.index("Section"))
        self.assertLess(secondary.index("Position"), secondary.index("Shift"))
        self.assertLess(secondary.index("Shift"), secondary.index("Staff"))
        self.assertLess(secondary.index("Staff"), secondary.index("Apply Filters"))
        self.assertLess(secondary.index("Apply Filters"), secondary.index("Reset Filters"))
        self.assertContains(response, "<h1>Chore Reports</h1>", html=True)
        self.assertContains(response, '<option value="">All shifts</option>', html=True)
        self.assertNotContains(response, "All valid shifts")
        self.assertContains(response, '<option value="">All</option>', html=True)
        self.assertNotContains(response, "All sections")
        self.assertContains(response, ">Apply Filters</button>")
        self.assertContains(response, ">Reset Filters</a>")
        self.assertContains(response, '<th class="report-date">Date</th>', html=True)
        self.assertContains(
            response,
            f'<td class="report-date">{self.operational_date.isoformat()}</td>',
            html=True,
        )
        self.assertContains(response, 'name="section"')
        self.assertContains(response, 'type="date"')
        self.assertContains(response, 'class="date-control"')
        self.assertContains(response, 'class="date-control-display"')
        self.assertContains(response, 'class="date-control-input"')
        self.assertContains(response, 'id="report-date"')
        self.assertContains(response, 'name="date"')
        self.assertContains(
            response, f'value="{self.operational_date.isoformat()}"'
        )
        self.assertContains(response, "date_control.js")
        self.assertContains(response, '<label for="report-date">Date')
        for label in ("Period", "Position", "Shift", "Staff", "Completion", "Section"):
            self.assertContains(response, f"<label>{label}", html=False)
        self.assertNotContains(response, 'name="month"')
        self.assertNotContains(response, 'name="year"')
        self.assertNotContains(response, 'name="task_state"')
        self.assertNotContains(response, "Staff category")
        self.assertNotContains(response, "Staff contribution")
        self.assertNotContains(response, "Checklist state")
        self.assertContains(response, "report-position-shift-options")
        self.assertContains(response, 'position.addEventListener("change"')
        self.assertEqual(
            response.context["shift_options_by_position"][str(self.category_a.pk)],
            [{"id": self.shift.pk, "name": "Morning"}],
        )
        monthly = self.client.get(
            reverse("reports"),
            {"period": "monthly", "date": "2024-02-17"},
        )
        self.assertContains(monthly, 'name="date"')
        self.assertContains(monthly, 'value="2024-02-17"')
        legacy = self.client.get(
            reverse("reports"),
            {"period": "monthly", "month": "2024-02"},
        )
        self.assertContains(legacy, 'name="date"')
        self.assertContains(legacy, 'value="2024-02-01"')
        self.assertNotContains(legacy, "month=2024-02")
        with open("checklists/static/checklists/styles.css", encoding="utf-8") as stylesheet:
            css = stylesheet.read()
        self.assertIn(".report-filter-row-primary { grid-template-columns: repeat(4, minmax(0, 1fr)); }", css)
        self.assertIn(".report-filter-row-secondary { grid-template-columns: repeat(3, minmax(0, 1fr)) minmax(12.5rem, 1fr);", css)
        self.assertIn(".filter-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));", css)
        self.assertIn(".report-filters > *, .report-filter-row > * { min-width: 0; }", css)
        self.assertIn(".report-filter-row-primary, .report-filter-row-secondary { grid-template-columns: 1fr; }", css)
        self.assertIn(".report-table .report-date { min-width: 7.25rem; white-space: nowrap; }", css)
        self.assertIn("max-width: 100%", css)
        self.assertIn(".date-control { width: 100%; max-width: 100%; min-width: 0; }", css)
        self.assertIn("--control-height: 2.75rem;", css)
        self.assertIn(
            "select { height: var(--control-height); min-height: var(--control-height);",
            css,
        )
        self.assertIn("height: var(--control-height);", css)
        self.assertIn("@media (hover: none) and (pointer: coarse)", css)
        self.assertIn(".date-control-display {", css)
        self.assertIn(".date-control-input {", css)
        self.assertIn("opacity: 0; cursor: pointer;", css)
        self.assertNotIn(".date-control-input { display: none", css)
        self.assertNotIn("visibility: hidden", css)
        self.assertNotIn("::-webkit-date-and-time-value", css)
        self.assertNotIn("::-webkit-datetime-edit", css)
        with open("checklists/static/checklists/date_control.js", encoding="utf-8") as script:
            date_control_js = script.read()
        self.assertIn('value.split("-").map(Number)', date_control_js)
        self.assertIn("new Date(year, month - 1, day)", date_control_js)
        self.assertIn('input.addEventListener("input", updateDisplay)', date_control_js)
        self.assertIn('input.addEventListener("change", updateDisplay)', date_control_js)
        for template_path in (
            "templates/checklists/dashboard.html",
            "templates/checklists/report.html",
        ):
            with self.subTest(template_path=template_path), open(
                template_path, encoding="utf-8"
            ) as template:
                self.assertIn(
                    '{% include "checklists/_date_control.html"', template.read()
                )

    def test_report_summary_renders_percentage_and_complete_inline(self):
        selected_date = self.operational_date
        markup = render_to_string(
            "checklists/report.html",
            {
                "program": self.program_a,
                "period": ReportPeriod(
                    "daily", selected_date, selected_date, "fixture", selected_date
                ),
                "totals": {
                    "checklists": 7,
                    "completed": 163,
                    "applicable": 221,
                    "completion_percentage": 73.8,
                },
                "rows": [],
                "filters": {},
                "available_programs": Program.objects.filter(pk=self.program_a.pk),
                "category_choices": [],
                "shift_choices": [],
                "staff_choices": [],
                "section_choices": [],
                "shift_options_by_position": {},
                "query_string": "",
            },
        )

        self.assertIn("<strong>73.8%</strong><span>Complete</span>", markup)
        self.assertEqual(markup.count("73.8%"), 1)
        self.assertNotIn("<span>%</span>", markup)

    def test_full_year_mock_history_remains_reportable_to_manager_and_admin(self):
        historical_date = self.operational_date - timedelta(days=364)
        instance = resolve_checklist(self.definition_a, historical_date)
        instance.is_mock_data = True
        instance.save(update_fields=("is_mock_data",))
        change_item_state(
            item_id=instance.items.get(source_task=self.regular_a).pk,
            actor=None,
            staff_member=self.staff_a,
            new_state=TaskState.COMPLETED,
            system=True,
        )
        params = {"period": "daily", "date": historical_date.isoformat()}

        self.client.force_login(self.manager_a)
        manager_response = self.client.get(reverse("reports"), params)
        self.assertEqual(manager_response.status_code, 200)
        self.assertContains(manager_response, historical_date.isoformat())
        rolling_response = self.client.get(
            reverse("reports"),
            {"period": "rolling365", "date": self.operational_date.isoformat()},
        )
        self.assertEqual(rolling_response.status_code, 200)
        self.assertContains(rolling_response, historical_date.isoformat())

        admin = get_user_model().objects.create_superuser(
            "historical-admin", "historical-admin@example.com", "password"
        )
        self.client.force_login(admin)
        admin_response = self.client.get(
            reverse("reports"), {**params, "program": self.program_a.pk}
        )
        self.assertEqual(admin_response.status_code, 200)
        self.assertContains(admin_response, historical_date.isoformat())


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ScheduledReportTests(ReportingFixtureMixin, TestCase):
    def test_program_routing_multiple_managers_and_idempotence(self):
        second = get_user_model().objects.create_user(
            "manager-a-2", email="manager-a-2@example.com"
        )
        ProgramMembership.objects.create(
            user=second,
            program=self.program_a,
            role=ProgramRole.MANAGER,
            receive_scheduled_reports=True,
        )
        get_user_model().objects.create_superuser(
            "site-admin", "site-admin@example.com", "password"
        )
        resolve_checklist(self.definition_a, self.operational_date)
        resolve_checklist(self.definition_b, self.operational_date)
        call_command("send_scheduled_reports", at="2026-09-16T08:00:00", verbosity=0)
        recipients = [address for message in mail.outbox for address in message.to]
        self.assertCountEqual(
            recipients,
            ["manager-a@example.com", "manager-a-2@example.com", "manager-b@example.com"],
        )
        self.assertNotIn("site-admin@example.com", recipients)
        messages = {message.to[0]: message.body for message in mail.outbox}
        self.assertIn("Program Alpha", messages["manager-a@example.com"])
        self.assertNotIn("Program Beta", messages["manager-a@example.com"])
        self.assertIn("Program Beta", messages["manager-b@example.com"])
        self.assertNotIn("Program Alpha", messages["manager-b@example.com"])
        alpha_snapshot = ScheduledReportDelivery.objects.get(
            program=self.program_a, recipient=self.manager_a
        ).snapshot
        beta_snapshot = ScheduledReportDelivery.objects.get(
            program=self.program_b, recipient=self.manager_b
        ).snapshot
        self.assertIn("Alpha regular", str(alpha_snapshot))
        self.assertNotIn("Beta regular", str(alpha_snapshot))
        self.assertIn("Beta regular", str(beta_snapshot))
        self.assertNotIn("Alpha regular", str(beta_snapshot))
        call_command("send_scheduled_reports", at="2026-09-16T09:00:00", verbosity=0)
        self.assertEqual(len(mail.outbox), 3)

    def test_sent_snapshot_remains_stable_after_late_entry(self):
        instance = resolve_checklist(self.definition_a, self.operational_date)
        call_command("send_scheduled_reports", at="2026-09-16T08:00:00", verbosity=0)
        delivery = ScheduledReportDelivery.objects.get(
            program=self.program_a, cadence=ReportCadence.DAILY
        )
        original = delivery.snapshot
        self.contribute(
            instance.items.get(source_task=self.regular_a),
            self.staff_a,
            TaskState.COMPLETED,
        )
        delivery.refresh_from_db()
        self.assertEqual(delivery.snapshot, original)


class RetentionTests(ReportingFixtureMixin, TestCase):
    def test_boundary_retained_older_operational_data_purged(self):
        boundary = resolve_checklist(self.definition_a, date(2019, 9, 16))
        older = resolve_checklist(self.definition_a, date(2019, 9, 15))
        change_item_state(
            item_id=older.items.get(source_task=self.regular_a).pk,
            actor=None,
            staff_member=self.staff_a,
            new_state=TaskState.COMPLETED,
            system=True,
        )
        for configured_object in (
            self.category_a,
            self.shift,
            self.definition_a,
            self.regular_a.fixture_section,
            self.regular_a,
        ):
            configured_object.is_active = False
            configured_object.save(update_fields=("is_active",))
        call_command("purge_operational_data", as_of="2026-09-16", verbosity=0)
        self.assertTrue(ChecklistInstance.objects.filter(pk=boundary.pk).exists())
        self.assertFalse(ChecklistInstance.objects.filter(pk=older.pk).exists())
        self.assertTrue(StaffMember.objects.filter(pk=self.staff_a.pk).exists())
        self.assertTrue(Program.objects.filter(pk=self.program_a.pk).exists())
        for configured_object in (
            self.category_a,
            self.shift,
            self.definition_a,
            self.regular_a.fixture_section,
            self.regular_a,
        ):
            configured_object.refresh_from_db()
            self.assertFalse(configured_object.is_active)

    def test_inactive_configuration_history_reports_but_cannot_be_reopened(self):
        instance = resolve_checklist(self.definition_a, self.operational_date)
        self.definition_a.is_active = False
        self.definition_a.save(update_fields=("is_active",))

        self.assertEqual(self.report_a()["rows"][0]["id"], instance.pk)
        with self.assertRaises(PermissionDenied):
            resolve_checklist(self.definition_a, date(2026, 9, 16))

    def test_dry_run_preserves_data(self):
        old = resolve_checklist(self.definition_a, date(2018, 1, 1))
        output = StringIO()
        call_command(
            "purge_operational_data",
            as_of="2026-09-16",
            dry_run=True,
            stdout=output,
            verbosity=0,
        )
        self.assertTrue(ChecklistInstance.objects.filter(pk=old.pk).exists())
