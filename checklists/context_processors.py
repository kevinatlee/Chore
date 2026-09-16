from .reporting import programs_for_reporting


def reporting_access(request):
    if not request.user.is_authenticated:
        return {"can_view_reports": False}
    return {"can_view_reports": programs_for_reporting(request.user).exists()}
