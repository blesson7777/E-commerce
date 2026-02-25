import io
import json
import os
import zipfile
from urllib.error import URLError
from urllib.request import urlopen

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from locations.models import District
from locations.models import Location
from locations.models import State


DEFAULT_SOURCE_URL = 'https://download.geonames.org/export/zip/IN.zip'
ZIP_JSON_FOLDER_TOKEN = '/api/v01/json/'
GEONAMES_DATA_FILE = 'IN.txt'


def clean_text(value, *, title_case=False):
    text = ' '.join(str(value or '').strip().split())
    if title_case:
        text = text.title()
    return text


class Command(BaseCommand):
    help = (
        'Import India states, districts, and postal locations from a full India pincode dataset '
        '(default: Geonames IN.zip, with IndiaPost JSON fallback).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--source-url',
            default=DEFAULT_SOURCE_URL,
            help='Zip URL containing postal dataset. Supports Geonames IN.zip or IndiaPost pin zip.',
        )
        parser.add_argument(
            '--zip-path',
            help='Optional local path to IndiaPost zip file (used instead of --source-url).',
        )
        parser.add_argument(
            '--clear-existing',
            action='store_true',
            help='Delete existing State, District, and Location rows before import.',
        )
        parser.add_argument(
            '--max-files',
            type=int,
            default=0,
            help='Optional limit for number of JSON files to process (for testing).',
        )

    def _load_zip_bytes(self, zip_path, source_url):
        if zip_path:
            if not os.path.exists(zip_path):
                raise CommandError(f'Zip path not found: {zip_path}')
            with open(zip_path, 'rb') as fp:
                return fp.read()
        try:
            with urlopen(source_url, timeout=120) as response:
                return response.read()
        except URLError as exc:
            raise CommandError(f'Unable to download source zip from {source_url}: {exc}') from exc

    def handle(self, *args, **options):
        zip_path = options['zip_path']
        source_url = options['source_url']
        clear_existing = options['clear_existing']
        max_files = options['max_files'] or 0

        if clear_existing:
            self.stdout.write(self.style.WARNING('Clearing existing geographic data...'))
            # Null dependent FKs first to avoid SQLite "too many SQL variables" during cascade updates.
            from accounts.models import CustomerProfile
            from catalog.models import Product

            CustomerProfile.objects.update(location=None, district=None)
            Product.objects.update(location=None)

            db_alias = Location.objects.db
            Location.objects.all()._raw_delete(db_alias)
            District.objects.all()._raw_delete(db_alias)
            State.objects.all()._raw_delete(db_alias)

        before_states = State.objects.count()
        before_districts = District.objects.count()
        before_locations = Location.objects.count()

        self.stdout.write('Loading IndiaPost dataset zip...')
        zip_bytes = self._load_zip_bytes(zip_path, source_url)

        try:
            archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except zipfile.BadZipFile as exc:
            raise CommandError('Downloaded file is not a valid zip archive.') from exc

        state_cache = {s.name.lower(): s for s in State.objects.all()}
        district_cache = {(d.state_id, d.name.lower()): d for d in District.objects.select_related('state')}
        location_seen = set()
        batch = []
        batch_size = 2500

        processed_rows = 0
        skipped_rows = 0

        def upsert_location_row(state_name, district_name, office_name, postal_code):
            nonlocal processed_rows, skipped_rows

            if not state_name or not district_name or not office_name or not postal_code:
                skipped_rows += 1
                return

            state_key = state_name.lower()
            state_obj = state_cache.get(state_key)
            if state_obj is None:
                state_obj, _ = State.objects.get_or_create(
                    name=state_name,
                    defaults={'is_active': True},
                )
                state_cache[state_key] = state_obj

            district_key = (state_obj.id, district_name.lower())
            district_obj = district_cache.get(district_key)
            if district_obj is None:
                district_obj, _ = District.objects.get_or_create(
                    state=state_obj,
                    name=district_name,
                    defaults={'is_active': True},
                )
                district_cache[district_key] = district_obj

            location_key = (district_obj.id, office_name.lower(), postal_code)
            if location_key in location_seen:
                return
            location_seen.add(location_key)

            batch.append(
                Location(
                    district=district_obj,
                    name=office_name,
                    postal_code=postal_code,
                    is_active=True,
                )
            )
            processed_rows += 1

            if len(batch) >= batch_size:
                Location.objects.bulk_create(batch, ignore_conflicts=True, batch_size=batch_size)
                batch.clear()

        archive_names = set(archive.namelist())
        if GEONAMES_DATA_FILE in archive_names:
            self.stdout.write('Detected Geonames IN.zip source. Processing IN.txt rows...')
            with archive.open(GEONAMES_DATA_FILE) as fp:
                for index, line in enumerate(io.TextIOWrapper(fp, encoding='utf-8', errors='ignore'), start=1):
                    parts = line.rstrip('\n').split('\t')
                    if len(parts) < 6:
                        skipped_rows += 1
                        continue
                    # Columns: country, pincode, place, state, state_code, district, ...
                    postal_code = clean_text(parts[1])
                    office_name = clean_text(parts[2], title_case=True)
                    state_name = clean_text(parts[3], title_case=True)
                    district_name = clean_text(parts[5], title_case=True)
                    upsert_location_row(state_name, district_name, office_name, postal_code)

                    if index % 10000 == 0:
                        self.stdout.write(f'Processed rows: {index}')
        else:
            json_names = sorted(
                name for name in archive.namelist()
                if name.endswith('.json') and ZIP_JSON_FOLDER_TOKEN in name
            )
            if not json_names:
                raise CommandError('No supported source found (expected IN.txt or IndiaPost JSON files).')

            if max_files > 0:
                json_names = json_names[:max_files]

            self.stdout.write(f'Detected IndiaPost source. Processing {len(json_names)} JSON files...')

            for index, json_name in enumerate(json_names, start=1):
                try:
                    rows = json.loads(archive.read(json_name).decode('utf-8'))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    skipped_rows += 1
                    continue

                if not isinstance(rows, list):
                    skipped_rows += 1
                    continue

                for row in rows:
                    if not isinstance(row, dict):
                        skipped_rows += 1
                        continue

                    state_name = clean_text(row.get('statename'), title_case=True)
                    district_name = clean_text(row.get('Districtname'), title_case=True)
                    office_name = clean_text(row.get('officename'), title_case=True)
                    postal_code = clean_text(row.get('pincode'))
                    upsert_location_row(state_name, district_name, office_name, postal_code)

                if index % 300 == 0:
                    self.stdout.write(f'Processed files: {index}/{len(json_names)}')

        if batch:
            Location.objects.bulk_create(batch, ignore_conflicts=True, batch_size=batch_size)

        after_states = State.objects.count()
        after_districts = District.objects.count()
        after_locations = Location.objects.count()

        self.stdout.write(self.style.SUCCESS('India geographic import completed.'))
        self.stdout.write(f'States added: {after_states - before_states}')
        self.stdout.write(f'Districts added: {after_districts - before_districts}')
        self.stdout.write(f'Locations added: {after_locations - before_locations}')
        self.stdout.write(f'Rows processed: {processed_rows}')
        self.stdout.write(f'Rows skipped: {skipped_rows}')
