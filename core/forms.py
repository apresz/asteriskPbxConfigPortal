from django import forms
from django.db.models import Q
from django.forms import BaseInlineFormSet

from .extension_management import sync_extension_relationships
from .models import (
    CallQueue,
    DID,
    Extension,
    Location,
    PagingGroup,
    Phone,
    PhoneLineAppearance,
    PhoneSpeedDial,
    RingGroup,
    normalize_mac_address,
)


class LocationForm(forms.ModelForm):
    SENSITIVE_FIELDS = (
        "deployment_ssh_host",
        "deployment_ssh_port",
        "deployment_ssh_username",
        "deployment_ssh_private_key",
        "deployment_ssh_known_hosts",
        "smtp_username",
        "smtp_password",
        "ami_username",
        "ami_secret",
        "agent_secret",
    )
    CREATE_REQUIRED_SENSITIVE_FIELDS = (
        "deployment_ssh_host",
        "deployment_ssh_username",
        "deployment_ssh_private_key",
        "ami_username",
        "ami_secret",
        "agent_secret",
    )
    PRESERVED_SECRET_FIELDS = (
        "deployment_ssh_private_key",
        "smtp_password",
        "ami_secret",
        "agent_secret",
    )
    FIELDSETS = (
        ("Identity", ("name", "slug", "description", "timezone", "is_active")),
        ("Network", ("lan_subnet", "pbx_lan_ip", "pbx_warp_ip")),
        (
            "Deployment SSH",
            (
                "deployment_ssh_host",
                "deployment_ssh_port",
                "deployment_ssh_username",
                "deployment_ssh_private_key",
                "deployment_ssh_known_hosts",
            ),
        ),
        (
            "SIP / RTP / IAX",
            ("sip_bind_ip", "sip_port", "rtp_port_start", "rtp_port_end", "iax_bind_ip", "iax_port"),
        ),
        ("Emergency", ("default_did", "emergency_caller_id", "emergency_trunk")),
        ("Recording", ("recording_retention_days",)),
        (
            "SMTP",
            (
                "smtp_host",
                "smtp_port",
                "smtp_from_email",
                "smtp_use_tls",
                "smtp_use_ssl",
                "smtp_username",
                "smtp_password",
            ),
        ),
        ("AMI", ("ami_host", "ami_port", "ami_username", "ami_secret")),
        ("Agent", ("agent_secret",)),
        ("Deployment Status", ("deployment_status",)),
    )

    class Meta:
        model = Location
        fields = (
            "name",
            "slug",
            "description",
            "timezone",
            "lan_subnet",
            "pbx_lan_ip",
            "pbx_warp_ip",
            "deployment_ssh_host",
            "deployment_ssh_port",
            "deployment_ssh_username",
            "deployment_ssh_private_key",
            "deployment_ssh_known_hosts",
            "sip_bind_ip",
            "sip_port",
            "rtp_port_start",
            "rtp_port_end",
            "iax_bind_ip",
            "iax_port",
            "default_did",
            "emergency_caller_id",
            "emergency_trunk",
            "recording_retention_days",
            "smtp_host",
            "smtp_port",
            "smtp_from_email",
            "smtp_use_tls",
            "smtp_use_ssl",
            "smtp_username",
            "smtp_password",
            "ami_host",
            "ami_port",
            "ami_username",
            "ami_secret",
            "agent_secret",
            "is_active",
            "deployment_status",
        )
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "deployment_ssh_private_key": forms.PasswordInput(render_value=False),
            "deployment_ssh_known_hosts": forms.Textarea(attrs={"rows": 3}),
            "smtp_password": forms.PasswordInput(render_value=False),
            "ami_secret": forms.PasswordInput(render_value=False),
            "agent_secret": forms.PasswordInput(render_value=False),
        }

    def __init__(self, *args, include_sensitive_fields=False, **kwargs):
        self.include_sensitive_fields = include_sensitive_fields
        self._initial_secret_values = {}
        super().__init__(*args, **kwargs)
        self.restricted_field_labels = []

        if self.instance and self.instance.pk:
            self._initial_secret_values = {
                field_name: getattr(self.instance, field_name)
                for field_name in self.PRESERVED_SECRET_FIELDS
            }

        if not include_sensitive_fields:
            self.restricted_field_labels = [
                Location._meta.get_field(field_name).verbose_name.title()
                for field_name in self.SENSITIVE_FIELDS
            ]
            for field_name in self.SENSITIVE_FIELDS:
                self.fields.pop(field_name, None)
        else:
            for field_name in self.CREATE_REQUIRED_SENSITIVE_FIELDS:
                if field_name in self.fields:
                    self.fields[field_name].required = True
            if self.instance and self.instance.pk:
                for field_name in self.PRESERVED_SECRET_FIELDS:
                    if field_name in self.fields:
                        self.fields[field_name].required = False

        for field_name, field in self.fields.items():
            if field.widget.__class__ in (forms.CheckboxInput,):
                field.widget.attrs.setdefault("class", "checkbox-input")
            else:
                field.widget.attrs.setdefault("class", "form-control")
            if field_name.endswith("_port"):
                field.widget.attrs.setdefault("min", "1")
                field.widget.attrs.setdefault("max", "65535")
            if field_name in {"rtp_port_start", "rtp_port_end"}:
                field.widget.attrs.setdefault("min", "1")
                field.widget.attrs.setdefault("max", "65535")
            if field_name == "recording_retention_days":
                field.widget.attrs.setdefault("min", "1")

    @property
    def fieldsets(self):
        return [
            {
                "legend": legend,
                "fields": [self[field_name] for field_name in field_names if field_name in self.fields],
            }
            for legend, field_names in self.FIELDSETS
            if any(field_name in self.fields for field_name in field_names)
        ]

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.instance and self.instance.pk and self.include_sensitive_fields:
            for field_name, initial_value in self._initial_secret_values.items():
                if field_name in self.fields and not self.cleaned_data.get(field_name):
                    setattr(instance, field_name, initial_value)
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class ExtensionForm(forms.ModelForm):
    RELATIONSHIP_FIELDS = ("direct_dids", "ring_groups", "queues", "paging_groups")
    PRESERVED_SECRET_FIELDS = ("sip_password", "voicemail_pin")
    FIELDSETS = (
        ("Identity", ("location", "number", "display_name", "email", "is_active")),
        ("SIP Credentials", ("sip_username", "sip_password")),
        ("DID / Caller ID", ("direct_dids", "caller_id_name", "caller_id_number")),
        ("Voicemail", ("voicemail_enabled", "voicemail_pin")),
        ("Recording", ("recording_policy",)),
        ("Emergency", ("emergency_calling_enabled",)),
        ("Memberships", ("ring_groups", "queues", "paging_groups")),
    )

    direct_dids = forms.ModelMultipleChoiceField(
        queryset=DID.objects.none(),
        required=False,
        label="DIDs",
    )
    ring_groups = forms.ModelMultipleChoiceField(
        queryset=RingGroup.objects.none(),
        required=False,
        label="Ring groups",
    )
    queues = forms.ModelMultipleChoiceField(
        queryset=CallQueue.objects.none(),
        required=False,
        label="Queues",
    )
    paging_groups = forms.ModelMultipleChoiceField(
        queryset=PagingGroup.objects.none(),
        required=False,
        label="Paging groups",
    )

    class Meta:
        model = Extension
        fields = (
            "location",
            "number",
            "display_name",
            "email",
            "sip_username",
            "sip_password",
            "direct_dids",
            "voicemail_enabled",
            "voicemail_pin",
            "caller_id_name",
            "caller_id_number",
            "recording_policy",
            "emergency_calling_enabled",
            "is_active",
            "ring_groups",
            "queues",
            "paging_groups",
        )
        widgets = {
            "sip_password": forms.PasswordInput(render_value=False),
            "voicemail_pin": forms.PasswordInput(render_value=False),
        }

    def __init__(self, *args, can_disable_911=False, **kwargs):
        self.can_disable_911 = can_disable_911
        self.denied_911_disable = False
        self._initial_secret_values = {}
        super().__init__(*args, **kwargs)

        if self.instance and self.instance.pk:
            self._initial_secret_values = {
                field_name: getattr(self.instance, field_name)
                for field_name in self.PRESERVED_SECRET_FIELDS
            }

        location = self._selected_location()
        self._configure_relationship_fields(location)
        self._configure_initial_relationships()
        self._configure_widgets()

    @property
    def fieldsets(self):
        return [
            {
                "legend": legend,
                "fields": [self[field_name] for field_name in field_names if field_name in self.fields],
            }
            for legend, field_names in self.FIELDSETS
            if any(field_name in self.fields for field_name in field_names)
        ]

    def clean_number(self):
        number = self.cleaned_data["number"]
        duplicate_query = Extension.objects.filter(number=number)
        if self.instance and self.instance.pk:
            duplicate_query = duplicate_query.exclude(pk=self.instance.pk)
        if duplicate_query.exists():
            raise forms.ValidationError("Extension number already exists.")
        return number

    def clean(self):
        cleaned_data = super().clean()
        location = cleaned_data.get("location")
        if location:
            self._validate_relationship_locations(cleaned_data, location)

        emergency_calling_enabled = cleaned_data.get("emergency_calling_enabled")
        original_enabled = True
        if self.instance and self.instance.pk:
            original_enabled = self.instance.emergency_calling_enabled
        if original_enabled and emergency_calling_enabled is False and not self.can_disable_911:
            self.denied_911_disable = True
            self.add_error(
                "emergency_calling_enabled",
                "Only admins can disable 911 calling for an extension.",
            )
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.instance and self.instance.pk:
            for field_name, initial_value in self._initial_secret_values.items():
                if field_name in self.fields and not self.cleaned_data.get(field_name):
                    setattr(instance, field_name, initial_value)
        if not instance.sip_username:
            instance.sip_username = instance.number

        if commit:
            instance.save()
            self.save_m2m()
            sync_extension_relationships(
                instance,
                direct_dids=self.cleaned_data["direct_dids"],
                ring_groups=self.cleaned_data["ring_groups"],
                queues=self.cleaned_data["queues"],
                paging_groups=self.cleaned_data["paging_groups"],
            )
        return instance

    def _selected_location(self):
        if self.is_bound:
            location_id = self.data.get(self.add_prefix("location"))
            if location_id:
                try:
                    return Location.objects.get(pk=location_id)
                except (Location.DoesNotExist, ValueError):
                    return None
        if self.instance and self.instance.pk:
            return self.instance.location
        return None

    def _configure_relationship_fields(self, location):
        if location is None:
            direct_dids = DID.objects.filter(direct_extension__isnull=True)
            if self.instance and self.instance.pk:
                direct_dids = DID.objects.filter(Q(direct_extension__isnull=True) | Q(direct_extension=self.instance))
            self.fields["direct_dids"].queryset = direct_dids.order_by("location__name", "number")
            self.fields["ring_groups"].queryset = RingGroup.objects.order_by("location__name", "name")
            self.fields["queues"].queryset = CallQueue.objects.order_by("location__name", "name")
            self.fields["paging_groups"].queryset = PagingGroup.objects.order_by("location__name", "page_code")
            return

        direct_dids = DID.objects.filter(location=location).order_by("number")
        if self.instance and self.instance.pk:
            direct_dids = direct_dids.filter(
                Q(direct_extension__isnull=True) | Q(direct_extension=self.instance)
            )
        else:
            direct_dids = direct_dids.filter(direct_extension__isnull=True)

        self.fields["direct_dids"].queryset = direct_dids
        self.fields["ring_groups"].queryset = RingGroup.objects.filter(location=location).order_by("name")
        self.fields["queues"].queryset = CallQueue.objects.filter(location=location).order_by("name")
        self.fields["paging_groups"].queryset = PagingGroup.objects.filter(location=location).order_by("page_code")

    def _configure_initial_relationships(self):
        if not (self.instance and self.instance.pk):
            return
        self.fields["direct_dids"].initial = self.instance.direct_dids.all()
        self.fields["ring_groups"].initial = RingGroup.objects.filter(members__extension=self.instance)
        self.fields["queues"].initial = CallQueue.objects.filter(members__extension=self.instance)
        self.fields["paging_groups"].initial = PagingGroup.objects.filter(members__extension=self.instance)

    def _configure_widgets(self):
        for field_name, field in self.fields.items():
            if field.widget.__class__ in (forms.CheckboxInput,):
                field.widget.attrs.setdefault("class", "checkbox-input")
            else:
                field.widget.attrs.setdefault("class", "form-control")
            if field_name in self.RELATIONSHIP_FIELDS:
                field.widget.attrs.setdefault("size", "5")
            if field_name == "number":
                field.widget.attrs.setdefault("maxlength", "4")
                field.widget.attrs.setdefault("inputmode", "numeric")
            if field_name == "voicemail_pin":
                field.widget.attrs.setdefault("inputmode", "numeric")

    def _validate_relationship_locations(self, cleaned_data, location):
        relationship_labels = {
            "direct_dids": "DIDs",
            "ring_groups": "Ring groups",
            "queues": "Queues",
            "paging_groups": "Paging groups",
        }
        for field_name, label in relationship_labels.items():
            records = cleaned_data.get(field_name) or []
            if any(record.location_id != location.id for record in records):
                self.add_error(field_name, f"{label} must belong to the selected location.")


class PhoneForm(forms.ModelForm):
    FIELDSETS = (
        ("Identity", ("location", "mac_address", "model", "label", "is_active")),
    )

    mac_address = forms.CharField(max_length=32, label="MAC address")

    class Meta:
        model = Phone
        fields = ("location", "mac_address", "model", "label", "is_active")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            if field.widget.__class__ in (forms.CheckboxInput,):
                field.widget.attrs.setdefault("class", "checkbox-input")
            else:
                field.widget.attrs.setdefault("class", "form-control")
        self.fields["mac_address"].widget.attrs.setdefault("placeholder", "SEP001122334455")
        self.fields["model"].choices = [
            choice
            for choice in Phone.PhoneModel.choices
            if choice[0] in {
                Phone.PhoneModel.CISCO_9971,
                Phone.PhoneModel.CISCO_9951,
                Phone.PhoneModel.CISCO_8961,
            }
        ]

    @property
    def fieldsets(self):
        return [
            {
                "legend": legend,
                "fields": [self[field_name] for field_name in field_names if field_name in self.fields],
            }
            for legend, field_names in self.FIELDSETS
        ]

    def clean_mac_address(self):
        return normalize_mac_address(self.cleaned_data["mac_address"])


class PhoneLineAppearanceForm(forms.ModelForm):
    class Meta:
        model = PhoneLineAppearance
        fields = ("line_index", "extension", "label")

    def __init__(self, *args, location=None, **kwargs):
        self.location = location
        super().__init__(*args, **kwargs)
        if location is not None:
            self.fields["extension"].queryset = Extension.objects.filter(location=location).order_by("number")
        else:
            self.fields["extension"].queryset = Extension.objects.select_related("location").order_by(
                "location__name",
                "number",
            )
        for field_name, field in self.fields.items():
            field.widget.attrs.setdefault("class", "form-control")
            if field_name in {"line_index"}:
                field.widget.attrs["min"] = "1"

    def clean(self):
        cleaned_data = super().clean()
        extension = cleaned_data.get("extension")
        if extension and self.location and extension.location_id != self.location.id:
            self.add_error("extension", "Line appearance extensions must belong to the selected phone location.")
        return cleaned_data


class PhoneSpeedDialForm(forms.ModelForm):
    class Meta:
        model = PhoneSpeedDial
        fields = ("position", "label", "destination")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            field.widget.attrs.setdefault("class", "form-control")
            if field_name == "position":
                field.widget.attrs["min"] = "1"


class BasePhoneLineAppearanceFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        self._validate_unique_values("line_index", "Line numbers must be unique per phone.")
        self._validate_unique_values("extension", "Extensions can only appear once per phone.")

    def _validate_unique_values(self, field_name, message):
        values = set()
        for form in self.forms:
            if not getattr(form, "cleaned_data", None) or form.cleaned_data.get("DELETE"):
                continue
            value = form.cleaned_data.get(field_name)
            if value in {None, ""}:
                continue
            if value in values:
                raise forms.ValidationError(message)
            values.add(value)


class BasePhoneSpeedDialFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        positions = set()
        for form in self.forms:
            if not getattr(form, "cleaned_data", None) or form.cleaned_data.get("DELETE"):
                continue
            position = form.cleaned_data.get("position")
            if position in {None, ""}:
                continue
            if position in positions:
                raise forms.ValidationError("Speed-dial positions must be unique per phone.")
            positions.add(position)


PhoneLineAppearanceFormSet = forms.inlineformset_factory(
    Phone,
    PhoneLineAppearance,
    form=PhoneLineAppearanceForm,
    formset=BasePhoneLineAppearanceFormSet,
    fields=("line_index", "extension", "label"),
    extra=2,
    can_delete=True,
)

PhoneSpeedDialFormSet = forms.inlineformset_factory(
    Phone,
    PhoneSpeedDial,
    form=PhoneSpeedDialForm,
    formset=BasePhoneSpeedDialFormSet,
    fields=("position", "label", "destination"),
    extra=3,
    can_delete=True,
)
