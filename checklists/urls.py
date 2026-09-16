from django.urls import path

from . import views


urlpatterns = [
    path("", views.home, name="home"),
    path("checklists/", views.dashboard, name="dashboard"),
    path("checklists/open/", views.open_checklist, name="open-checklist"),
    path("reports/", views.reports, name="reports"),
    path("reports/export.csv", views.report_csv, name="report-csv"),
    path("reports/print/", views.report_print, name="report-print"),
    path("reports/checklists/<int:instance_id>/", views.report_detail, name="report-detail"),
    path("checklists/<int:definition_id>/", views.checklist_detail, name="checklist-detail"),
    path("items/<int:item_id>/state/", views.update_item_state, name="update-item-state"),
]
