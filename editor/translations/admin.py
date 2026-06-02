from django import forms
from django.contrib import admin
from django.db import models as db_models
from django.utils.html import format_html

from .models import Language, SourceFile, TranslationEntry


@admin.register(Language)
class LanguageAdmin(admin.ModelAdmin):
    list_display = ("name", "lang_code", "mod_name", "memory_path")
    search_fields = ("name", "lang_code")


@admin.register(SourceFile)
class SourceFileAdmin(admin.ModelAdmin):
    list_display = ("name", "language", "entry_count")
    list_filter = ("language",)
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


class TranslationChangelistForm(forms.ModelForm):
    """Form used for inline editing in the changelist view."""
    class Meta:
        model = TranslationEntry
        fields = ["translation", "status"]
        widgets = {
            "translation": forms.Textarea(attrs={
                "rows": 3,
                "style": "width:100%; min-width:200px; font-size:12px; resize:vertical;",
            }),
        }


@admin.register(TranslationEntry)
class TranslationEntryAdmin(admin.ModelAdmin):
    list_display = (
        "source_file",
        "source_preview",
        "translation",      # editable inline in changelist
        "status",
        "updated_at",
    )
    list_display_links = ("source_file",)
    list_filter = ("source_file", StatusFilter, "language")
    search_fields = ("source", "translation", "scope")
    list_per_page = 50

    # Inline editing in the changelist
    list_editable = ("translation", "status")

    # Readonly in the detail form
    readonly_fields = (
        "language", "source_file", "scope",
        "source_display",           # custom read-only rendering of source
        "created_at", "updated_at",
    )

    # Side-by-side source (left, read-only) and translation (right, editable)
    fieldsets = (
        (None, {
            "fields": ("language", "source_file", "scope"),
        }),
        ("Translation", {
            "fields": (("source_display", "translation"),),
        }),
        ("Metadata", {
            "fields": ("status", "created_at", "updated_at"),
        }),
    )

    # Tall textarea for translation in the detail form
    formfield_overrides = {
        db_models.TextField: {
            "widget": forms.Textarea(attrs={
                "rows": 10,
                "style": "width:100%; font-size:13px; resize:vertical;",
            })
        },
    }

    def get_changelist_form(self, request, **kwargs):
        return TranslationChangelistForm

    def source_display(self, obj):
        """Read-only rendering of the source text in the detail form."""
        return format_html(
            '<div class="te-source-readonly">{}</div>',
            obj.source,
        )
    source_display.short_description = "Source (original)"

    def source_preview(self, obj):
        text = obj.source
        if len(text) > 100:
            text = text[:100] + "…"
        return text
    source_preview.short_description = "Source"

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("source_file", "language")
