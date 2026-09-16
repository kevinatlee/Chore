from django.urls import path

from . import views


urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("checklists/<int:definition_id>/", views.checklist_detail, name="checklist-detail"),
    path("items/<int:item_id>/state/", views.update_item_state, name="update-item-state"),
]
