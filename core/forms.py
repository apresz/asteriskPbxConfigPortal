from django import forms

from .models import Location


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
        "smtp_username",
        "smtp_password",
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
