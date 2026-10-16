from .reporting import programs_for_reporting
from .services import programs_for_feedback, programs_for_operations


def reporting_access(request):
    if not request.user.is_authenticated:
        return {
            "can_view_reports": False,
            "can_operate": False,
            "can_submit_feedback": False,
        }
    return {
        "can_view_reports": programs_for_reporting(request.user).exists(),
        "can_operate": programs_for_operations(request.user).exists(),
        "can_submit_feedback": programs_for_feedback(request.user).exists(),
    }
