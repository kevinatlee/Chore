from datetime import time

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


ROSTER = [
    ("Alfred", "Sampare"),
    ("Andrew", "Bevan"),
    ("Chelsea", "Brown"),
    ("Chrystal", "Auckland"),
    ("Daniel", "Buck"),
    ("Destiny", "Patelas"),
    ("Emmalee", "Pimentel"),
    ("Gary", "Hill"),
    ("Gurbinder", "Singh"),
    ("Jacob", "Larose"),
    ("Jaime", "Houlden"),
    ("Jaskirat", "Singh"),
    ("Jeremiah", "Gerow"),
    ("Jolene", "Williams"),
    ("Josh", "Hansen"),
    ("Kianna", "Coburn"),
    ("Konark", "Rawat"),
    ("Lucy", "Cabral"),
    ("Melodie", "Daigle"),
    ("Nadine", "Morgan"),
    ("Nayandeep", "Singh"),
    ("Nicole", "Morven"),
    ("Pranshukh", "Nayyar"),
    ("Ri", "Sharma"),
    ("Ron", "Bevan"),
    ("Roxanne", "Wagner"),
    ("Ruth", "McMillian"),
    ("Sandra", "Glover"),
    ("Sonya", "Kushnerek"),
    ("Vita", "Hunter"),
]


def correct_data(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    Program = apps.get_model("checklists", "Program")
    ProgramMembership = apps.get_model("checklists", "ProgramMembership")
    StaffMember = apps.get_model("checklists", "StaffMember")
    StaffAssignment = apps.get_model("checklists", "StaffAssignment")
    Shift = apps.get_model("checklists", "Shift")
    ChecklistDefinition = apps.get_model("checklists", "ChecklistDefinition")
    ChecklistInstance = apps.get_model("checklists", "ChecklistInstance")
    ChecklistItem = apps.get_model("checklists", "ChecklistItem")
    ChecklistSection = apps.get_model("checklists", "ChecklistSection")
    TaskDefinition = apps.get_model("checklists", "TaskDefinition")
    StaffContribution = apps.get_model("checklists", "StaffContribution")
    ScheduledReportDelivery = apps.get_model("checklists", "ScheduledReportDelivery")

    program, _ = Program.objects.get_or_create(
        slug="sonder-house", defaults={"name": "Sonder House", "is_active": True}
    )
    for first_name, last_name in ROSTER:
        key = f"sonder-house-{first_name}-{last_name}".lower().replace(" ", "-")
        StaffMember.objects.update_or_create(
            seed_key=key,
            defaults={
                "program": program,
                "first_name": first_name,
                "last_name": last_name,
                "is_active": True,
            },
        )

    test_user = User.objects.filter(username="teststaff").first()
    if test_user:
        test_contribution_ids = StaffContribution.objects.filter(
            recorded_by_id=test_user.pk
        ).values_list("pk", flat=True)
        StaffContribution.objects.filter(pk__in=test_contribution_ids).delete()

    staff_cache = {}
    for contribution in StaffContribution.objects.select_related(
        "recorded_by", "item__instance__program"
    ):
        user = contribution.recorded_by
        contribution_program = contribution.item.instance.program
        first_name = (user.first_name or user.username or "Legacy").strip()
        last_name = (user.last_name or "Attribution").strip()
        cache_key = (contribution_program.pk, user.pk)
        staff = staff_cache.get(cache_key)
        if staff is None:
            staff = StaffMember.objects.filter(
                program=contribution_program,
                first_name=first_name,
                last_name=last_name,
            ).first()
            if staff is None:
                staff = StaffMember.objects.create(
                    program=contribution_program,
                    first_name=first_name,
                    last_name=last_name,
                    is_active=False,
                    seed_key=f"legacy-user-{user.pk}-program-{contribution_program.pk}",
                )
            staff_cache[cache_key] = staff
        contribution.staff_id = staff.pk
        contribution.save(update_fields=("staff",))

    for item in ChecklistItem.objects.all():
        latest = StaffContribution.objects.filter(item_id=item.pk).order_by(
            "created_at", "id"
        ).last()
        if latest:
            item.current_staff_id = latest.staff_id
            item.current_state = latest.new_state
            item.state_changed_at = latest.created_at
        elif item.current_contributor_id:
            item.current_staff_id = None
            item.current_state = "pending"
            item.state_changed_at = None
        item.save(update_fields=("current_staff", "current_state", "state_changed_at"))

    morning = Shift.objects.filter(start_time=time(7), end_time=time(15)).first()
    evening = Shift.objects.filter(start_time=time(15), end_time=time(23)).first()
    night = Shift.objects.filter(start_time=time(23), end_time=time(7)).first()
    life_shift = Shift.objects.filter(start_time=time(8), end_time=time(15)).first()
    if morning:
        morning.name = "Morning"
        morning.seed_key = "shift-morning"
        morning.sort_order = 10
        morning.is_active = True
        morning.save()
    if evening:
        evening.name = "Evening"
        evening.seed_key = "shift-evening"
        evening.sort_order = 20
        evening.is_active = True
        evening.save()
    if night:
        night.name = "Night"
        night.seed_key = "shift-night"
        night.sort_order = 30
        night.is_active = True
        night.save()
    if life_shift and morning:
        ChecklistDefinition.objects.filter(shift=life_shift).update(shift=morning)
        ChecklistInstance.objects.filter(shift=life_shift).update(
            shift=morning,
            shift_name_snapshot="Morning",
            shift_start_snapshot=time(7),
            shift_end_snapshot=time(15),
        )
        life_shift.delete()

    replacements = (
        ("front-desk-day", "front-desk-morning"),
        ("front-desk-overnight", "front-desk-night"),
        ("support-day", "support-morning"),
        ("awake-night-overnight", "awake-night-night"),
        ("life-skills-life-skills-day", "life-skills-morning"),
    )
    for model in (ChecklistDefinition, ChecklistSection, TaskDefinition):
        for instance in model.objects.exclude(seed_key__isnull=True):
            updated = instance.seed_key
            for old, new in replacements:
                updated = updated.replace(old, new)
            if updated != instance.seed_key:
                instance.seed_key = updated
                instance.save(update_fields=("seed_key",))

    ProgramMembership.objects.filter(role="staff").update(role="operational")
    if test_user:
        operational_user = User.objects.filter(username="sonderhouse").first()
        if operational_user and operational_user.pk != test_user.pk:
            ProgramMembership.objects.filter(user=test_user).delete()
            StaffAssignment.objects.filter(user=test_user).delete()
            ScheduledReportDelivery.objects.filter(recipient=test_user).delete()
            test_user.delete()
        else:
            test_user.username = "sonderhouse"
            test_user.first_name = "Sonder House"
            test_user.last_name = "Operations"
            test_user.is_staff = False
            test_user.is_superuser = False
            test_user.is_active = True
            test_user.save()
            ProgramMembership.objects.update_or_create(
                user=test_user,
                program=program,
                defaults={
                    "role": "operational",
                    "is_active": True,
                    "is_test_staff": False,
                    "receive_scheduled_reports": False,
                },
            )
    StaffAssignment.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("checklists", "0003_phase_2_program_reporting")]

    operations = [
        migrations.CreateModel(
            name="StaffMember",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("first_name", models.CharField(max_length=100)),
                ("last_name", models.CharField(max_length=100)),
                ("is_active", models.BooleanField(default=True)),
                ("seed_key", models.SlugField(blank=True, editable=False, max_length=220, null=True, unique=True)),
                ("program", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="staff_members", to="checklists.program")),
            ],
            options={
                "verbose_name": "operational staff member",
                "verbose_name_plural": "operational staff",
                "ordering": ("first_name", "last_name", "id"),
                "constraints": [models.UniqueConstraint(fields=("program", "first_name", "last_name"), name="unique_program_staff_name")],
            },
        ),
        migrations.AddField(
            model_name="checklistinstance",
            name="is_mock_data",
            field=models.BooleanField(default=False),
        ),
        migrations.RenameField(
            model_name="staffcontribution", old_name="staff", new_name="recorded_by"
        ),
        migrations.AlterField(
            model_name="staffcontribution",
            name="recorded_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="recorded_staff_contributions", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="staffcontribution",
            name="staff",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name="staff_contributions", to="checklists.staffmember"),
        ),
        migrations.AddField(
            model_name="checklistitem",
            name="current_staff",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="current_checklist_items", to="checklists.staffmember"),
        ),
        migrations.RunPython(correct_data, migrations.RunPython.noop),
        migrations.RemoveField(model_name="programmembership", name="is_test_staff"),
        migrations.AlterField(
            model_name="programmembership",
            name="role",
            field=models.CharField(choices=[("operational", "Operational access"), ("manager", "Manager")], max_length=16),
        ),
        migrations.RemoveField(model_name="checklistitem", name="current_contributor"),
        migrations.AlterField(
            model_name="staffcontribution",
            name="staff",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="staff_contributions", to="checklists.staffmember"),
        ),
        migrations.DeleteModel(name="StaffAssignment"),
    ]
