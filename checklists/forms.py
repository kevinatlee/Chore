import uuid

from django import forms

from .models import StaffMember


class FeedbackForm(forms.Form):
    staff = forms.ModelChoiceField(
        label="Your Name",
        queryset=StaffMember.objects.none(),
        empty_label="Select your name",
    )
    feedback = forms.CharField(
        label="Feedback",
        widget=forms.Textarea(attrs={"rows": 8}),
        strip=False,
    )
    submission_token = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, program, **kwargs):
        super().__init__(*args, **kwargs)
        self.program = program
        self.fields["staff"].queryset = StaffMember.objects.filter(
            program=program, is_active=True
        ).order_by("first_name", "last_name", "id")
        if not self.is_bound:
            self.initial.setdefault("submission_token", uuid.uuid4())

    def clean_feedback(self):
        feedback = self.cleaned_data["feedback"]
        if not feedback.strip():
            raise forms.ValidationError("Enter feedback before submitting.")
        return feedback

