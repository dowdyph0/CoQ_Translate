import json, hashlib, os, sys
sys.path.insert(0, '/mnt/c/Users/ph0/Desktop/CoQ_Translate/editor')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'editor_project.settings')
import django; django.setup()

from translations.models import Language, SourceFile, TranslationEntry, _source_hash

lang = Language.objects.get(name='Spanish')
data = json.load(open('/mnt/c/Users/ph0/Desktop/CoQ_Translate/SpanishLanguage/translation_memory.json'))
entries = data.get('entries', data) if isinstance(data, dict) else data

print(f'JSON entries: {len(entries)}')
print(f'DB entries:   {TranslationEntry.objects.filter(language=lang).count()}')

missing = 0
missing_examples = []
for item in entries:
    file_name = item['file']
    scope = item['scope']
    source = item['source']
    sh = _source_hash(source)
    exists = TranslationEntry.objects.filter(
        language=lang,
        source_file__name=file_name,
        scope=scope,
        source_hash=sh,
    ).exists()
    if not exists:
        missing += 1
        if len(missing_examples) < 5:
            missing_examples.append((file_name, scope, source[:60]))

print(f'Missing in DB: {missing}')
print('Examples:')
for f, s, t in missing_examples:
    print(f'  {f} | {s} | {t}')
