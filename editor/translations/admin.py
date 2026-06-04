from django import forms
from django.contrib import admin
from django.db import models as db_models
from django.db.models import Q
from django.urls import reverse
from django.utils.html import format_html

import re

from .models import Language, SourceFile, TranslationEntry


@admin.register(Language)
class LanguageAdmin(admin.ModelAdmin):
    list_display = ("name", "lang_code", "mod_name")
    search_fields = ("name", "lang_code")


@admin.register(SourceFile)
class SourceFileAdmin(admin.ModelAdmin):
    list_display = ("name", "entry_count")
    search_fields = ("name",)

    def entry_count(self, obj):
        return obj.entries.count()
    entry_count.short_description = "Entries"


class StatusFilter(admin.SimpleListFilter):
    title = "Status"
    parameter_name = "status"

    def lookups(self, request, model_admin):
        return TranslationEntry.STATUS_CHOICES

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(status=self.value())
        return queryset


@admin.register(TranslationEntry)
class TranslationEntryAdmin(admin.ModelAdmin):
    list_display = (
        "source_file",
        "source_preview",
        "translation_preview",
        "status",
        "updated_at",
    )
    list_display_links = ("source_file", "source_preview")
    list_filter = ("source_file", StatusFilter, "language")
    search_fields = ("source", "translation", "scope")
    list_per_page = 50

    readonly_fields = (
        "language", "source_file", "scope",
        "xml_file_display",
        "source_display",
        "created_at", "updated_at",
    )

    fieldsets = (
        (None, {
            "fields": ("language", "source_file", "scope"),
        }),
        ("XML Context", {
            "fields": ("xml_file_display",),
            "classes": ("wide",),
        }),
        ("Translation", {
            "fields": (("source_display", "translation"),),
            "classes": ("te-translation-fieldset",),
        }),
        ("Metadata", {
            "fields": ("status", "created_at", "updated_at"),
        }),
    )

    formfield_overrides = {
        db_models.TextField: {
            "widget": forms.Textarea(attrs={
                "rows": 10,
                "style": "width:100%; resize:vertical;",
                "spellcheck": "false",
            })
        },
    }

    def xml_file_display(self, obj):
        content = obj.source_file.xml_content if obj.source_file else ""
        if not content:
            return "—"
        return format_html('<pre class="te-xml-content">{}</pre>', content)
    xml_file_display.short_description = "Source XML"

    def source_display(self, obj):
        return format_html('<div class="te-source-readonly">{}</div>', obj.source)
    source_display.short_description = "Source (original)"

    def source_preview(self, obj):
        text = obj.source
        if len(text) > 100:
            text = text[:100] + "…"
        return text
    source_preview.short_description = "Source"

    def translation_preview(self, obj):
        text = obj.translation
        if not text:
            return "—"
        if len(text) > 100:
            text = text[:100] + "…"
        return text
    translation_preview.short_description = "Translation"

    def get_search_results(self, request, queryset, search_term):
        if not search_term:
            return queryset, False
        pattern = re.escape(search_term)
        q = (
            Q(source__regex=pattern)
            | Q(translation__regex=pattern)
            | Q(scope__regex=pattern)
        )
        return queryset.filter(q), False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("source_file", "language")

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}
        qs = self.get_queryset(request)
        ids = list(qs.values_list("pk", flat=True))
        try:
            idx = ids.index(int(object_id))
            app_label = self.model._meta.app_label
            model_name = self.model._meta.model_name
            url_name = f"admin:{app_label}_{model_name}_change"
            extra_context["prev_url"] = reverse(url_name, args=[ids[idx - 1]]) if idx > 0 else None
            extra_context["next_url"] = reverse(url_name, args=[ids[idx + 1]]) if idx < len(ids) - 1 else None
            extra_context["nav_position"] = f"{idx + 1} / {len(ids)}"
        except (ValueError, IndexError):
            pass
        return super().change_view(request, object_id, form_url, extra_context)
