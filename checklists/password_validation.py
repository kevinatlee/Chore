from django.contrib.auth.password_validation import (
    CommonPasswordValidator,
    MinimumLengthValidator,
    NumericPasswordValidator,
    UserAttributeSimilarityValidator,
)


def is_shared_operational_account(user):
    if user is None or not getattr(user, "pk", None):
        return False
    from .models import ProgramRole

    if user.is_staff or user.is_superuser:
        return False
    if user.program_memberships.filter(
        role=ProgramRole.MANAGER, is_active=True
    ).exists():
        return False
    return user.program_memberships.filter(
        role=ProgramRole.OPERATIONAL,
        is_active=True,
        program__is_active=True,
    ).exists()


class _OperationalAccountExceptionMixin:
    def validate(self, password, user=None):
        if is_shared_operational_account(user):
            return
        return super().validate(password, user)

    def get_help_text(self):
        return f"For individual accounts, {super().get_help_text().removeprefix('Your ')}"


class OperationalAccountSimilarityValidator(
    _OperationalAccountExceptionMixin, UserAttributeSimilarityValidator
):
    pass


class OperationalAccountMinimumLengthValidator(
    _OperationalAccountExceptionMixin, MinimumLengthValidator
):
    pass


class OperationalAccountCommonPasswordValidator(
    _OperationalAccountExceptionMixin, CommonPasswordValidator
):
    pass


class OperationalAccountNumericPasswordValidator(
    _OperationalAccountExceptionMixin, NumericPasswordValidator
):
    pass
