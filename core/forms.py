from django import forms
from django.db import transaction
from django.db.models import Q

from .extension_management import (
    record_911_disable_success,
    sync_extension_assignments,
    validate_911_disable_allowed,
    validate_local_assignments,
)
from .models import DID, CallQueue, Extension, PagingGroup, RingGroup


class ExtensionForm(forms.ModelForm):
    dids = forms.ModelMultipleChoiceField(
        label="Assigned DIDs",
        queryset=DID.objects.none(),
        required=False,
        widget=forms.SelectMultiple(attrs={"size": "5"}),
    )
    ring_groups = forms.ModelMultipleChoiceField(
        queryset=RingGroup.objects.none(),
        required=False,
        widget=forms.SelectMultiple(attrs={"size": "5"}),
    )
    queues = forms.ModelMultipleChoiceField(
        queryset=CallQueue.objects.none(),
        required=False,
        widget=forms.SelectMultiple(attrs={"size": "5"}),
    )
    paging_groups = forms.ModelMultipleChoiceField(
        queryset=PagingGroup.objects.none(),
        required=False,
        widget=forms.SelectMultiple(attrs={"size": "5"}),
    )

    class Meta:
        model = Extension
        fields = [
            "location",
            "number",
            "display_name",
            "email",
            "sip_username",
            "sip_password",
            "voicemail_enabled",
            "voicemail_pin",
            "caller_id_name",
            "caller_id_number",
            "recording_policy",
            "emergency_calling_enabled",
            "is_active",
            "dids",
            "ring_groups",
            "queues",
            "paging_groups",
        ]
        labels = {
            "email": "Voicemail email",
            "sip_password": "SIP password",
            "emergency_calling_enabled": "911 calling enabled",
        }

    def __init__(self, *args, actor, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor = actor
        self._before_911_enabled = (
            bool(self.instance.emergency_calling_enabled) if self.instance.pk else True
        )
        self._style_fields()
        self._set_assignment_querysets()
        self._set_assignment_initials()

    def clean(self):
        cleaned_data = super().clean()
        location = cleaned_data.get("location")
        assignment_errors = validate_local_assignments(
            location_id=location.pk if location else None,
            dids=cleaned_data.get("dids") or [],
            ring_groups=cleaned_data.get("ring_groups") or [],
            queues=cleaned_data.get("queues") or [],
            paging_groups=cleaned_data.get("paging_groups") or [],
            extension=self.instance if self.instance.pk else None,
        )
        for field, message in assignment_errors.items():
            self.add_error(field, message)

        after_911_enabled = cleaned_data.get("emergency_calling_enabled", False)
        try:
            validate_911_disable_allowed(
                actor=self.actor,
                target_number=cleaned_data.get("number") or getattr(self.instance, "number", ""),
                before_enabled=self._before_911_enabled,
                after_enabled=after_911_enabled,
            )
        except forms.ValidationError as exc:
            self.add_error("emergency_calling_enabled", exc)
        return cleaned_data

    def save(self, commit=True):
        extension = super().save(commit=False)
        if not commit:
            return extension

        with transaction.atomic():
            extension.save()
            sync_extension_assignments(
                extension,
                dids=self.cleaned_data["dids"],
                ring_groups=self.cleaned_data["ring_groups"],
                queues=self.cleaned_data["queues"],
                paging_groups=self.cleaned_data["paging_groups"],
            )
            record_911_disable_success(
                actor=self.actor,
                target_number=extension.number,
                before_enabled=self._before_911_enabled,
                after_enabled=extension.emergency_calling_enabled,
            )
        return extension

    def _style_fields(self) -> None:
        for field in self.fields.values():
            css_class = "form-control"
            if isinstance(field.widget, forms.CheckboxInput):
                css_class = "form-check"
            existing_classes = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = f"{existing_classes} {css_class}".strip()

    def _set_assignment_querysets(self) -> None:
        dids = DID.objects.select_related("location").order_by("number")
        if self.instance.pk:
            dids = dids.filter(Q(direct_extension__isnull=True) | Q(direct_extension=self.instance))
        else:
            dids = dids.filter(direct_extension__isnull=True)

        self.fields["dids"].queryset = dids
        self.fields["ring_groups"].queryset = RingGroup.objects.select_related("location").order_by(
            "location__name",
            "name",
        )
        self.fields["queues"].queryset = CallQueue.objects.select_related("location").order_by(
            "location__name",
            "name",
        )
        self.fields["paging_groups"].queryset = PagingGroup.objects.select_related("location").order_by(
            "location__name",
            "name",
        )

    def _set_assignment_initials(self) -> None:
        if not self.instance.pk:
            return
        self.fields["dids"].initial = self.instance.direct_dids.all()
        self.fields["ring_groups"].initial = RingGroup.objects.filter(
            members__extension=self.instance
        )
        self.fields["queues"].initial = CallQueue.objects.filter(members__extension=self.instance)
        self.fields["paging_groups"].initial = PagingGroup.objects.filter(
            members__extension=self.instance
        )


class ExtensionCsvImportForm(forms.Form):
    csv_file = forms.FileField(label="CSV file")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["csv_file"].widget.attrs["class"] = "form-control"
