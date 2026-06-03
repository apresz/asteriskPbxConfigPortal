from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.contrib import messages
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from .access import permission_required, user_has_permission
from .extension_csv import export_extensions_csv, extension_csv_template, import_extensions_csv
from .extension_management import sync_extension_assignments
from .forms import ExtensionCsvImportForm, ExtensionForm
from .models import Extension, PortalPermission
from .navigation import PORTAL_AREAS, visible_portal_areas


def health(request):
    return JsonResponse({"status": "ok"})


@permission_required(PortalPermission.VIEW)
def home(request):
    context = {"areas": visible_portal_areas(request.user)}
    return render(request, _template(request, "core/home.html", "core/partials/home_content.html"), context)


@login_required
def portal_area(request, slug: str):
    area = PORTAL_AREAS.get(slug)
    if area is None:
        raise Http404("Unknown portal area")
    if not user_has_permission(request.user, area["permission"]):
        raise PermissionDenied

    context = {"area": area, "slug": slug, "areas": visible_portal_areas(request.user)}
    return render(request, _template(request, "core/area.html", "core/partials/area_content.html"), context)


@permission_required(PortalPermission.VIEW)
def extension_list(request):
    extensions = (
        Extension.objects.select_related("location")
        .prefetch_related(
            "direct_dids",
            "ring_group_memberships__ring_group",
            "queue_memberships__queue",
            "paging_group_memberships__paging_group",
        )
        .all()
    )
    context = {
        "area": PORTAL_AREAS["extensions"],
        "slug": "extensions",
        "extensions": extensions,
        "can_edit": user_has_permission(request.user, PortalPermission.EDIT_CONFIG),
    }
    return render(
        request,
        _template(request, "core/extensions/list.html", "core/partials/extensions/list_content.html"),
        context,
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def extension_create(request):
    if request.method == "POST":
        form = ExtensionForm(request.POST, actor=request.user)
        if form.is_valid():
            extension = form.save()
            messages.success(request, f"Extension {extension.number} created.")
            return redirect("extensions")
    else:
        form = ExtensionForm(actor=request.user)
    return _render_extension_form(request, form=form, title="Create extension", submit_label="Create")


@permission_required(PortalPermission.EDIT_CONFIG)
def extension_edit(request, pk: int):
    extension = get_object_or_404(Extension, pk=pk)
    if request.method == "POST":
        form = ExtensionForm(request.POST, actor=request.user, instance=extension)
        if form.is_valid():
            extension = form.save()
            messages.success(request, f"Extension {extension.number} updated.")
            return redirect("extensions")
    else:
        form = ExtensionForm(actor=request.user, instance=extension)
    return _render_extension_form(
        request,
        form=form,
        title=f"Edit extension {extension.number}",
        submit_label="Save",
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def extension_delete(request, pk: int):
    extension = get_object_or_404(Extension, pk=pk)
    if request.method == "POST":
        number = extension.number
        try:
            with transaction.atomic():
                sync_extension_assignments(extension)
                extension.delete()
            messages.success(request, f"Extension {number} deleted.")
            return redirect("extensions")
        except ProtectedError:
            messages.error(
                request,
                f"Extension {number} is still referenced by phones or routing rules.",
            )
    context = {"extension": extension}
    return render(
        request,
        _template(
            request,
            "core/extensions/confirm_delete.html",
            "core/partials/extensions/confirm_delete_content.html",
        ),
        context,
    )


@permission_required(PortalPermission.VIEW)
def extension_csv_template_view(request):
    return _csv_response(extension_csv_template(), filename="extension-template.csv")


@permission_required(PortalPermission.VIEW)
def extension_export(request):
    return _csv_response(export_extensions_csv(), filename="extensions.csv")


@permission_required(PortalPermission.EDIT_CONFIG)
def extension_import(request):
    import_errors = []
    if request.method == "POST":
        form = ExtensionCsvImportForm(request.POST, request.FILES)
        if form.is_valid():
            result = import_extensions_csv(form.cleaned_data["csv_file"], actor=request.user)
            if result.errors:
                import_errors = result.errors
                messages.error(request, "CSV import was rejected.")
            else:
                messages.success(request, f"Imported {result.imported_count} extension rows.")
                return redirect("extensions")
    else:
        form = ExtensionCsvImportForm()
    context = {"form": form, "import_errors": import_errors}
    return render(
        request,
        _template(
            request,
            "core/extensions/import.html",
            "core/partials/extensions/import_content.html",
        ),
        context,
    )


def _template(request, full_template: str, partial_template: str) -> str:
    if request.headers.get("HX-Request") == "true":
        return partial_template
    return full_template


def _render_extension_form(request, *, form: ExtensionForm, title: str, submit_label: str):
    context = {"form": form, "title": title, "submit_label": submit_label}
    return render(
        request,
        _template(request, "core/extensions/form.html", "core/partials/extensions/form_content.html"),
        context,
    )


def _csv_response(content: str, *, filename: str) -> HttpResponse:
    response = HttpResponse(content, content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
