import hashlib

from django.db import models


def _source_hash(source: str) -> str:
    """Full MD5 hex digest (32 chars) of the source text — used as part of the unique key."""
    return hashlib.md5(source.encode("utf-8")).hexdigest()


class Language(models.Model):
    name = models.CharField(max_length=100)   # "Spanish"
    lang_code = models.CharField(max_length=10)  # "es"
    mod_name = models.CharField(max_length=100)  # "SpanishLanguage"

    class Meta:
        verbose_name = "Language"
        verbose_name_plural = "Languages"

    def __str__(self):
        return f"{self.name} ({self.lang_code})"


class SourceFile(models.Model):
    name = models.CharField(max_length=200, unique=True)  # "ActivatedAbilities.example.xml"
    xml_content = models.TextField(blank=True, default="")  # raw XML of the source file

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class TranslationEntry(models.Model):
    STATUS_AUTO = "auto"
    STATUS_REVIEWED = "reviewed"
    STATUS_FAILED = "failed"
    STATUS_PENDING = "pending"

    STATUS_CHOICES = [
        (STATUS_AUTO, "Auto (LLM)"),
        (STATUS_REVIEWED, "Reviewed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_PENDING, "Pending"),
    ]

    language = models.ForeignKey(Language, on_delete=models.CASCADE, related_name="entries")
    source_file = models.ForeignKey(SourceFile, on_delete=models.CASCADE, related_name="entries")
    scope = models.CharField(max_length=2000)
    source = models.TextField()
    source_hash = models.CharField(max_length=32, blank=True)  # full MD5 of source — part of unique key
    translation = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_AUTO)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("language", "source_file", "scope", "source_hash")
        ordering = ["source_file__name", "scope"]
        verbose_name = "Translation Entry"
        verbose_name_plural = "Translation Entries"

    def save(self, *args, **kwargs):
        if not self.source_hash and self.source:
            self.source_hash = _source_hash(self.source)
        super().save(*args, **kwargs)

    def __str__(self):
        src = self.source[:60] + "…" if len(self.source) > 60 else self.source
        return src
