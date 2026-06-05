from django import forms
from django.db.models import Q
from django.forms import BaseInlineFormSet

from .extension_management import sync_extension_relationships
from .audio_prompts import AudioPromptValidationError, validate_audio_prompt_upload
from .rtp_config import RTP_PORT_MAX, RTP_PORT_MIN, RTPRangeError, validate_rtp_port_range
from .models import (
    AudioPrompt,
    CallQueue,
    DID,
    Extension,
    FeatureCode,
    InboundDestination,
    IVR,
    IVRMenuOption,
    Location,
    OutboundRoute,
    OutboundRouteTrunk,
    PagingGroup,
    PagingGroupMember,
    Phone,
    PhoneLineAppearance,
    PhoneSpeedDial,
    Provider,
    QueueMember,
    RingGroup,
    RingGroupMember,
    Trunk,
    normalize_mac_address,
)


def _configure_standard_widgets(fields):
    for field_name, field in fields.items():
        if field.widget.__class__ in (forms.CheckboxInput,):
            field.widget.attrs.setdefault("class", "checkbox-input")
        else:
            field.widget.attrs.setdefault("class", "form-control")
        if field_name.endswith("_seconds") or field_name in {"timeout_seconds", "retry_seconds", "penalty", "priority"}:
            field.widget.attrs.setdefault("min", "1")
        if field_name in {"priority", "deployment_ssh_port", "sip_port", "rtp_port_start", "rtp_port_end", "iax_port"}:
            field.widget.attrs.setdefault("min", str(RTP_PORT_MIN))
            field.widget.attrs.setdefault("max", str(RTP_PORT_MAX))

def _selected_location_from_form(form):
    if form.is_bound:
        location_id = form.data.get(form.add_prefix("location"))
        if location_id:
            try:
                return Location.objects.get(pk=location_id)
            except (Location.DoesNotExist, ValueError):
                return None
    if getattr(form.instance, "location_id", None):
        return form.instance.location
    return None


def _destination_queryset(location):
    queryset = InboundDestination.objects.select_related(
        "location",
        "extension",
        "ivr",
        "ring_group",
        "queue",
    ).order_by("location__name", "name")
    if location is not None:
        queryset = queryset.filter(location=location)
    return queryset


def _extension_queryset_for_location(location):
    queryset = Extension.objects.select_related("location").order_by("location__name", "number")
    if location is not None:
        queryset = queryset.filter(location=location)
    return queryset


def _sync_extension_members(parent, selected_extensions, model, parent_field, defaults):
    selected_ids = [extension.id for extension in selected_extensions]
    model.objects.filter(**{parent_field: parent}).exclude(extension_id__in=selected_ids).delete()
    for index, extension in enumerate(selected_extensions, start=1):
        member, created = model.objects.get_or_create(
            **{parent_field: parent},
            extension=extension,
            defaults=defaults(index),
        )
        if not created:
            update_fields = []
            for field_name, value in defaults(index).items():
                if getattr(member, field_name) != value:
                    setattr(member, field_name, value)
                    update_fields.append(field_name)
            if update_fields:
                member.save(update_fields=update_fields)


class LocationForm(forms.ModelForm):
    SENSITIVE_FIELDS = (
        "deployment_ssh_host",
        "deployment_ssh_port",
        "deployment_ssh_username",
        "deployment_ssh_private_key",
        "deployment_ssh_known_hosts",
        "deployment_staging_path",
        "deployment_asterisk_path",
        "deployment_tftp_path",
        "deployment_reload_command",
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
                "deployment_staging_path",
                "deployment_asterisk_path",
                "deployment_tftp_path",
                "deployment_reload_command",
            ),
        ),
        (
            "SIP / RTP / IAX",
            ("sip_bind_ip", "sip_port", "rtp_port_start", "rtp_port_end", "iax_bind_ip", "iax_port"),
        ),
        ("Emergency", ("default_did", "emergency_caller_id", "emergency_trunk")),
        ("Inbound Routing", ("default_inbound_destination",)),
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
            "deployment_staging_path",
            "deployment_asterisk_path",
            "deployment_tftp_path",
            "deployment_reload_command",
            "sip_bind_ip",
            "sip_port",
            "rtp_port_start",
            "rtp_port_end",
            "iax_bind_ip",
            "iax_port",
            "default_did",
            "emergency_caller_id",
            "emergency_trunk",
            "default_inbound_destination",
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
                field.widget.attrs.setdefault("min", str(RTP_PORT_MIN))
                field.widget.attrs.setdefault("max", str(RTP_PORT_MAX))
            if field_name in {"rtp_port_start", "rtp_port_end"}:
                field.widget.attrs.setdefault("min", str(RTP_PORT_MIN))
                field.widget.attrs.setdefault("max", str(RTP_PORT_MAX))
            if field_name == "recording_retention_days":
                field.widget.attrs.setdefault("min", "1")

        if "default_inbound_destination" in self.fields:
            destinations = InboundDestination.objects.select_related(
                "location",
                "extension",
                "ivr",
                "ring_group",
                "queue",
            ).order_by("location__name", "name")
            if self.instance and self.instance.pk:
                destinations = destinations.filter(location=self.instance)
            self.fields["default_inbound_destination"].queryset = destinations

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

    def clean(self):
        cleaned_data = super().clean()
        if "rtp_port_start" in self.fields and "rtp_port_end" in self.fields:
            try:
                validate_rtp_port_range(cleaned_data.get("rtp_port_start"), cleaned_data.get("rtp_port_end"))
            except RTPRangeError as exc:
                for field_name, message in exc.field_errors.items():
                    if field_name in self.fields and field_name not in self.errors:
                        self.add_error(field_name, message)
        return cleaned_data

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


class ProviderForm(forms.ModelForm):
    FIELDSETS = (
        ("Identity", ("name", "slug", "provider_type", "is_active")),
        ("Notes", ("notes",)),
    )

    class Meta:
        model = Provider
        fields = ("name", "slug", "provider_type", "notes", "is_active")
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["provider_type"].choices = [
            choice
            for choice in Provider.ProviderType.choices
            if choice[0] in {Provider.ProviderType.SIP, Provider.ProviderType.IAX2}
        ]
        _configure_standard_widgets(self.fields)

    @property
    def fieldsets(self):
        return [
            {
                "legend": legend,
                "fields": [self[field_name] for field_name in field_names if field_name in self.fields],
            }
            for legend, field_names in self.FIELDSETS
        ]


class TrunkForm(forms.ModelForm):
    FIELDSETS = (
        ("Identity", ("location", "provider", "name", "trunk_type", "is_active")),
        ("Credentials", ("host", "username", "password")),
        ("Emergency", ("is_emergency_capable",)),
    )

    class Meta:
        model = Trunk
        fields = (
            "location",
            "provider",
            "name",
            "trunk_type",
            "host",
            "username",
            "password",
            "is_emergency_capable",
            "is_active",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["provider"].queryset = Provider.objects.filter(
            provider_type__in=[Provider.ProviderType.SIP, Provider.ProviderType.IAX2]
        ).order_by("name")
        _configure_standard_widgets(self.fields)

    @property
    def fieldsets(self):
        return [
            {
                "legend": legend,
                "fields": [self[field_name] for field_name in field_names if field_name in self.fields],
            }
            for legend, field_names in self.FIELDSETS
        ]


class OutboundRouteForm(forms.ModelForm):
    FIELDSETS = (
        ("Pattern", ("location", "name", "dial_pattern", "priority", "is_active")),
        ("Caller ID", ("caller_id_source", "caller_id_number", "is_emergency_route")),
        ("Recording", ("recording_policy",)),
    )

    class Meta:
        model = OutboundRoute
        fields = (
            "location",
            "name",
            "dial_pattern",
            "priority",
            "caller_id_source",
            "caller_id_number",
            "is_emergency_route",
            "recording_policy",
            "is_active",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["dial_pattern"].widget.attrs.setdefault("placeholder", "NXXNXXXXXX")
        self.fields["caller_id_number"].label = "Custom caller ID"
        _configure_standard_widgets(self.fields)

    @property
    def fieldsets(self):
        return [
            {
                "legend": legend,
                "fields": [self[field_name] for field_name in field_names if field_name in self.fields],
            }
            for legend, field_names in self.FIELDSETS
        ]


class OutboundRouteTrunkForm(forms.ModelForm):
    class Meta:
        model = OutboundRouteTrunk
        fields = ("priority", "trunk")

    def __init__(self, *args, location=None, **kwargs):
        super().__init__(*args, **kwargs)
        if location is not None:
            self.fields["trunk"].queryset = Trunk.objects.filter(location=location, is_active=True).order_by("name")
        else:
            self.fields["trunk"].queryset = Trunk.objects.select_related("location").filter(is_active=True).order_by(
                "location__name",
                "name",
            )
        _configure_standard_widgets(self.fields)


class BaseOutboundRouteTrunkFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        self._validate_unique_values("priority", "Trunk priorities must be unique per route.")
        self._validate_unique_values("trunk", "Trunks can only appear once per route.")
        if self.instance and self.instance.is_emergency_route:
            for form in self.forms:
                if not getattr(form, "cleaned_data", None) or form.cleaned_data.get("DELETE"):
                    continue
                trunk = form.cleaned_data.get("trunk")
                if trunk and not trunk.is_emergency_capable:
                    form.add_error("trunk", "Emergency routes can only use emergency-capable trunks.")

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


class InboundDestinationForm(forms.ModelForm):
    FIELDSETS = (
        ("Identity", ("location", "name", "destination_type")),
        ("Target", ("extension", "ivr", "ring_group", "queue")),
    )
    TARGET_FIELDS = {
        InboundDestination.DestinationType.EXTENSION: "extension",
        InboundDestination.DestinationType.IVR: "ivr",
        InboundDestination.DestinationType.RING_GROUP: "ring_group",
        InboundDestination.DestinationType.QUEUE: "queue",
    }

    class Meta:
        model = InboundDestination
        fields = ("location", "name", "destination_type", "extension", "ivr", "ring_group", "queue")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        location = _selected_location_from_form(self)
        self.fields["extension"].queryset = _extension_queryset_for_location(location)
        self.fields["ivr"].queryset = IVR.objects.select_related("location").order_by("location__name", "name")
        self.fields["ring_group"].queryset = RingGroup.objects.select_related("location").order_by("location__name", "name")
        self.fields["queue"].queryset = CallQueue.objects.select_related("location").order_by("location__name", "name")
        if location is not None:
            self.fields["ivr"].queryset = self.fields["ivr"].queryset.filter(location=location)
            self.fields["ring_group"].queryset = self.fields["ring_group"].queryset.filter(location=location)
            self.fields["queue"].queryset = self.fields["queue"].queryset.filter(location=location)
        for field_name in self.TARGET_FIELDS.values():
            self.fields[field_name].required = False
        _configure_standard_widgets(self.fields)

    @property
    def fieldsets(self):
        return [
            {
                "legend": legend,
                "fields": [self[field_name] for field_name in field_names if field_name in self.fields],
            }
            for legend, field_names in self.FIELDSETS
        ]

    def clean(self):
        cleaned_data = super().clean()
        location = cleaned_data.get("location")
        destination_type = cleaned_data.get("destination_type")
        target_field = self.TARGET_FIELDS.get(destination_type)
        if target_field is None:
            return cleaned_data

        for field_name in self.TARGET_FIELDS.values():
            if field_name != target_field:
                cleaned_data[field_name] = None

        target = cleaned_data.get(target_field)
        if target is None:
            self.add_error(target_field, "Choose the target for this destination type.")
            return cleaned_data
        if location and target.location_id != location.id:
            self.add_error(target_field, "Target must belong to the selected location.")
        return cleaned_data


class DIDForm(forms.ModelForm):
    FIELDSETS = (
        ("Identity", ("location", "number", "label", "is_active")),
        ("Carrier", ("provider", "trunk")),
        ("Routing", ("direct_extension", "default_destination")),
    )

    class Meta:
        model = DID
        fields = (
            "location",
            "number",
            "label",
            "provider",
            "trunk",
            "direct_extension",
            "default_destination",
            "is_active",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        location = _selected_location_from_form(self)
        self.fields["direct_extension"].queryset = _extension_queryset_for_location(location)
        self.fields["default_destination"].queryset = _destination_queryset(location)
        self.fields["provider"].queryset = Provider.objects.order_by("name")
        self.fields["trunk"].queryset = Trunk.objects.select_related("location").order_by("location__name", "name")
        if location is not None:
            self.fields["trunk"].queryset = self.fields["trunk"].queryset.filter(location=location)
        self.fields["direct_extension"].required = False
        self.fields["default_destination"].required = False
        self.fields["provider"].required = False
        self.fields["trunk"].required = False
        self.fields["number"].widget.attrs.setdefault("placeholder", "+15551203000")
        _configure_standard_widgets(self.fields)

    @property
    def fieldsets(self):
        return [
            {
                "legend": legend,
                "fields": [self[field_name] for field_name in field_names if field_name in self.fields],
            }
            for legend, field_names in self.FIELDSETS
        ]

    def clean_number(self):
        number = self.cleaned_data["number"]
        duplicate_query = DID.objects.filter(number=number)
        if self.instance and self.instance.pk:
            duplicate_query = duplicate_query.exclude(pk=self.instance.pk)
        if duplicate_query.exists():
            raise forms.ValidationError("DID number already exists.")
        return number


class IVRForm(forms.ModelForm):
    DESTINATION_FIELDS = (
        "business_hours_destination",
        "after_hours_destination",
        "timeout_destination",
        "invalid_destination",
    )
    FIELDSETS = (
        ("Identity", ("location", "name", "prompt", "prompt_upload", "prompt_name", "is_active")),
        (
            "Routing",
            (
                "business_hours_destination",
                "after_hours_destination",
                "timeout_seconds",
                "timeout_destination",
                "invalid_destination",
            ),
        ),
    )

    class Meta:
        model = IVR
        fields = (
            "location",
            "name",
            "prompt",
            "prompt_name",
            "business_hours_destination",
            "after_hours_destination",
            "timeout_seconds",
            "timeout_destination",
            "invalid_destination",
            "is_active",
        )

    prompt_upload = forms.FileField(
        label="Upload prompt",
        required=False,
        help_text="Accepted formats: WAV, MP3, or M4A.",
        widget=forms.ClearableFileInput(attrs={"accept": ".wav,.mp3,.m4a,audio/wav,audio/mpeg,audio/mp4"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        location = _selected_location_from_form(self)
        self.fields["prompt"].queryset = AudioPrompt.objects.select_related("location").order_by("location__name", "name")
        self.fields["prompt"].required = False
        self.fields["prompt_name"].required = False
        self.fields["prompt_name"].label = "Prompt path"
        self.fields["prompt_name"].help_text = "Optional legacy Asterisk prompt path. Upload or select a prompt when possible."
        if location:
            self.fields["prompt"].queryset = self.fields["prompt"].queryset.filter(location=location)
        for field_name in self.DESTINATION_FIELDS:
            self.fields[field_name].queryset = _destination_queryset(location)
            self.fields[field_name].required = False
        _configure_standard_widgets(self.fields)

    def clean_prompt_upload(self):
        upload = self.cleaned_data.get("prompt_upload")
        if not upload:
            return upload
        if not self.cleaned_data.get("location"):
            raise forms.ValidationError("Choose a location before uploading a prompt.")
        try:
            validate_audio_prompt_upload(upload)
        except AudioPromptValidationError as exc:
            raise forms.ValidationError(str(exc)) from exc
        return upload

    def clean(self):
        cleaned_data = super().clean()
        prompt = cleaned_data.get("prompt")
        location = cleaned_data.get("location")
        if prompt and location and prompt.location_id != location.id:
            self.add_error("prompt", "Prompt must belong to the selected location.")
        return cleaned_data

    @property
    def fieldsets(self):
        return [
            {
                "legend": legend,
                "fields": [self[field_name] for field_name in field_names if field_name in self.fields],
            }
            for legend, field_names in self.FIELDSETS
        ]


class IVRMenuOptionForm(forms.ModelForm):
    class Meta:
        model = IVRMenuOption
        fields = ("digit", "label", "destination")

    def __init__(self, *args, location=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["destination"].queryset = _destination_queryset(location)
        self.fields["digit"].widget.attrs.setdefault("maxlength", "1")
        self.fields["digit"].widget.attrs.setdefault("inputmode", "numeric")
        _configure_standard_widgets(self.fields)


class BaseIVRMenuOptionFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        digits = set()
        for form in self.forms:
            if not getattr(form, "cleaned_data", None) or form.cleaned_data.get("DELETE"):
                continue
            digit = form.cleaned_data.get("digit")
            if digit in {None, ""}:
                continue
            if digit in digits:
                raise forms.ValidationError("IVR menu digits must be unique.")
            digits.add(digit)


class RingGroupForm(forms.ModelForm):
    FIELDSETS = (
        ("Identity", ("location", "name", "strategy", "timeout_seconds", "is_active")),
        ("Static Members", ("members",)),
    )
    members = forms.ModelMultipleChoiceField(queryset=Extension.objects.none(), required=False)

    class Meta:
        model = RingGroup
        fields = ("location", "name", "strategy", "timeout_seconds", "is_active", "members")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        location = _selected_location_from_form(self)
        self.fields["members"].queryset = _extension_queryset_for_location(location)
        if self.instance and self.instance.pk:
            self.fields["members"].initial = Extension.objects.filter(ring_group_memberships__ring_group=self.instance)
        _configure_standard_widgets(self.fields)
        self.fields["members"].widget.attrs.setdefault("size", "6")

    @property
    def fieldsets(self):
        return [
            {
                "legend": legend,
                "fields": [self[field_name] for field_name in field_names if field_name in self.fields],
            }
            for legend, field_names in self.FIELDSETS
        ]

    def clean(self):
        cleaned_data = super().clean()
        self._validate_member_locations(cleaned_data)
        return cleaned_data

    def save(self, commit=True):
        ring_group = super().save(commit=commit)
        if commit:
            _sync_extension_members(
                ring_group,
                self.cleaned_data["members"],
                RingGroupMember,
                "ring_group",
                lambda index: {"priority": index},
            )
        return ring_group

    def _validate_member_locations(self, cleaned_data):
        location = cleaned_data.get("location")
        members = cleaned_data.get("members") or []
        if location and any(member.location_id != location.id for member in members):
            self.add_error("members", "Ring group members must belong to the selected location.")


class CallQueueForm(forms.ModelForm):
    FIELDSETS = (
        (
            "Identity",
            ("location", "name", "strategy", "timeout_seconds", "retry_seconds", "music_on_hold", "is_active"),
        ),
        ("Routing", ("overflow_destination",)),
        ("Recording", ("recording_policy",)),
        ("Static Members", ("members",)),
    )
    members = forms.ModelMultipleChoiceField(queryset=Extension.objects.none(), required=False)

    class Meta:
        model = CallQueue
        fields = (
            "location",
            "name",
            "strategy",
            "timeout_seconds",
            "retry_seconds",
            "music_on_hold",
            "overflow_destination",
            "recording_policy",
            "is_active",
            "members",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        location = _selected_location_from_form(self)
        self.fields["members"].queryset = _extension_queryset_for_location(location)
        self.fields["overflow_destination"].queryset = _destination_queryset(location)
        self.fields["overflow_destination"].required = False
        if self.instance and self.instance.pk:
            self.fields["members"].initial = Extension.objects.filter(queue_memberships__queue=self.instance)
        _configure_standard_widgets(self.fields)
        self.fields["members"].widget.attrs.setdefault("size", "6")

    @property
    def fieldsets(self):
        return [
            {
                "legend": legend,
                "fields": [self[field_name] for field_name in field_names if field_name in self.fields],
            }
            for legend, field_names in self.FIELDSETS
        ]

    def clean(self):
        cleaned_data = super().clean()
        location = cleaned_data.get("location")
        members = cleaned_data.get("members") or []
        overflow_destination = cleaned_data.get("overflow_destination")
        if location and any(member.location_id != location.id for member in members):
            self.add_error("members", "Queue members must belong to the selected location.")
        if location and overflow_destination and overflow_destination.location_id != location.id:
            self.add_error("overflow_destination", "Overflow destination must belong to the selected location.")
        return cleaned_data

    def save(self, commit=True):
        queue = super().save(commit=commit)
        if commit:
            _sync_extension_members(
                queue,
                self.cleaned_data["members"],
                QueueMember,
                "queue",
                lambda index: {"penalty": 0},
            )
        return queue


class PagingGroupForm(forms.ModelForm):
    FIELDSETS = (
        ("Identity", ("location", "name", "page_code", "is_active")),
        ("Static Members", ("members",)),
    )
    members = forms.ModelMultipleChoiceField(queryset=Extension.objects.none(), required=False)

    class Meta:
        model = PagingGroup
        fields = ("location", "name", "page_code", "is_active", "members")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        location = _selected_location_from_form(self)
        self.fields["members"].queryset = _extension_queryset_for_location(location)
        if self.instance and self.instance.pk:
            self.fields["members"].initial = Extension.objects.filter(paging_group_memberships__paging_group=self.instance)
        self.fields["page_code"].widget.attrs.setdefault("maxlength", "4")
        self.fields["page_code"].widget.attrs.setdefault("inputmode", "numeric")
        _configure_standard_widgets(self.fields)
        self.fields["members"].widget.attrs.setdefault("size", "6")

    @property
    def fieldsets(self):
        return [
            {
                "legend": legend,
                "fields": [self[field_name] for field_name in field_names if field_name in self.fields],
            }
            for legend, field_names in self.FIELDSETS
        ]

    def clean(self):
        cleaned_data = super().clean()
        location = cleaned_data.get("location")
        members = cleaned_data.get("members") or []
        if location and any(member.location_id != location.id for member in members):
            self.add_error("members", "Paging group members must belong to the selected location.")
        return cleaned_data

    def save(self, commit=True):
        paging_group = super().save(commit=commit)
        if commit:
            _sync_extension_members(
                paging_group,
                self.cleaned_data["members"],
                PagingGroupMember,
                "paging_group",
                lambda index: {},
            )
        return paging_group


class FeatureCodeForm(forms.ModelForm):
    FIELDSETS = (
        ("Identity", ("location", "code", "name", "feature_type", "is_active")),
        ("Routing", ("destination",)),
        ("Notes", ("notes",)),
    )

    class Meta:
        model = FeatureCode
        fields = ("location", "code", "name", "feature_type", "destination", "notes", "is_active")
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        location = _selected_location_from_form(self)
        self.fields["destination"].queryset = _destination_queryset(location)
        self.fields["destination"].required = False
        self.fields["code"].widget.attrs.setdefault("placeholder", "*98")
        _configure_standard_widgets(self.fields)

    @property
    def fieldsets(self):
        return [
            {
                "legend": legend,
                "fields": [self[field_name] for field_name in field_names if field_name in self.fields],
            }
            for legend, field_names in self.FIELDSETS
        ]

    def clean(self):
        cleaned_data = super().clean()
        location = cleaned_data.get("location")
        destination = cleaned_data.get("destination")
        if location and destination and destination.location_id != location.id:
            self.add_error("destination", "Destination must belong to the selected location.")
        return cleaned_data


IVRMenuOptionFormSet = forms.inlineformset_factory(
    IVR,
    IVRMenuOption,
    form=IVRMenuOptionForm,
    formset=BaseIVRMenuOptionFormSet,
    fields=("digit", "label", "destination"),
    extra=4,
    can_delete=True,
)


class PhoneForm(forms.ModelForm):
    FIELDSETS = (
        ("Identity", ("location", "mac_address", "model", "firmware_load_name", "label", "is_active")),
    )

    mac_address = forms.CharField(max_length=32, label="MAC address")

    class Meta:
        model = Phone
        fields = ("location", "mac_address", "model", "firmware_load_name", "label", "is_active")

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

OutboundRouteTrunkFormSet = forms.inlineformset_factory(
    OutboundRoute,
    OutboundRouteTrunk,
    form=OutboundRouteTrunkForm,
    formset=BaseOutboundRouteTrunkFormSet,
    fields=("priority", "trunk"),
    extra=3,
    can_delete=True,
)
