"""
:copyright: (c) 2014 Building Energy Inc
"""
import calendar
import datetime
from dateutil import parser
import re
import string
import operator
import os

from django.core.mail import send_mail
from django.conf import settings
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from django.template import loader
from django.core.cache import cache
from django.core.files.storage import DefaultStorage
from django.db.models import Q
from django.db.models.loading import get_model
from django.core.urlresolvers import reverse_lazy

from celery.task import task, chord

from audit_logs.models import AuditLog
from landing.models import SEEDUser as User
from mcm import cleaners, mapper, reader
from mcm.data.ESPM import espm as espm_schema
from mcm.data.SEED import seed as seed_schema
from mcm.utils import batch
import ngram

from data_importer.models import (
    ImportFile, ImportRecord, STATUS_READY_TO_MERGE, ROW_DELIMITER
)

from green_button import xml_importer

from seed.models import (
    ASSESSED_RAW,
    PORTFOLIO_RAW,
    GREEN_BUTTON_RAW,
    ASSESSED_BS,
    PORTFOLIO_BS,
    GREEN_BUTTON_BS,
    BS_VALUES_LIST,
    Column,
    get_column_mappings,
    find_unmatched_buildings,
    find_canonical_building_values,
    SYSTEM_MATCH,
    POSSIBLE_MATCH,
    initialize_canonical_building,
    set_initial_sources,
    save_snapshot_match,
    save_column_names,
    BuildingSnapshot,
    CanonicalBuilding,
    Compliance,
    Project,
    ProjectBuilding,
)

from seed.decorators import lock_and_track, get_prog_key, increment_cache
from seed.utils.buildings import get_source_type, get_search_query
from seed.utils.mapping import get_mappable_columns

from superperms.orgs.models import Organization

from . import exporter


# Maximum number of possible matches under which we'll allow a system match.
MAX_SEARCH = 5
# Minimum confidence of two buildings being related.
MIN_CONF = .80
# Knows how to clean floats for ESPM data.
ASSESSED_CLEANER = cleaners.Cleaner(seed_schema.schema)
PORTFOLIO_CLEANER = cleaners.Cleaner(espm_schema.schema)
PUNCT_REGEX = re.compile('[{0}]'.format(
    re.escape(string.punctuation)
))


@task
def export_buildings(export_id, export_name, export_type,
                     building_ids, export_model='seed.BuildingSnapshot',
                     selected_fields=None):
    model = get_model(*export_model.split("."))

    selected_buildings = model.objects.filter(pk__in=building_ids)

    def _row_cb(i):
        cache.set("export_buildings__%s" % export_id, i)

    my_exporter = getattr(exporter, "export_%s" % export_type, None)
    if not my_exporter:
        _row_cb(-1)  # this means there was an error
        return

    exported_filename = my_exporter(selected_buildings,
                                    selected_fields,
                                    _row_cb)
    exported_file = open(exported_filename)

    s3_keyname = exporter._make_export_filename(export_id,
                                                export_name,
                                                export_type)
    s3_key = DefaultStorage().bucket.new_key(s3_keyname)
    s3_key.set_contents_from_file(exported_file)

    exported_file.close()
    os.remove(exported_filename)

    _row_cb(selected_buildings.count())  # means we're done!


@task
def invite_to_seed(domain, email_address, token, user_pk, first_name):
    signup_url = reverse_lazy('landing:signup', kwargs={
        'uidb64': urlsafe_base64_encode(force_bytes(user_pk)),
        "token": token
    })
    context = {
        'email': email_address,
        'domain': domain,
        'protocol': 'https',
        'first_name': first_name,
        'signup_url': signup_url
    }

    subject = 'New SEED account'
    email_body = loader.render_to_string(
        'seed/account_create_email.html',
        context
    )
    reset_email = settings.SERVER_EMAIL
    send_mail(subject, email_body, reset_email, [email_address])


#TODO (AK): Ensure this gets tested in PR #61
@task
def add_buildings(project_slug, project_dict, user_pk):
    """adds buildings to a project. if a user has selected all buildings,
       then the the search parameters within project_dict are used to determine
       the total set
       of buildings.
       also creates a Compliance inst. if sastifying params are present

       :param str project_slug: a project's slug used to get the project
       :param dict project_dict: contains search params, and browser state
       infomation
       :user_pk int or str: the user's pk or id

    """
    project = Project.objects.get(slug=project_slug)
    user = User.objects.get(pk=user_pk)
    project.last_modified_by = user
    project.save()

    selected_buildings = project_dict.get('selected_buildings', [])

    cache.set(
        project.adding_buildings_status_percentage_cache_key,
        {'percentage_done': 0, 'numerator': 0, 'denominator': 0}
    )
    i = 0
    denominator = 1
    if not project_dict.get('select_all_checkbox', False):
        for sfid in selected_buildings:
            i += 1
            denominator = len(selected_buildings)
            try:
                cache.set(
                    project.adding_buildings_status_percentage_cache_key,
                    {
                        'percentage_done': (
                            float(i) / len(selected_buildings) * 100
                        ),
                        'numerator': i, 'denominator': denominator
                    }
                )
            except ZeroDivisionError:
                pass
            ab = BuildingSnapshot.objects.get(pk=sfid)
            ProjectBuilding.objects.get_or_create(
                project=project, building_snapshot=ab
            )
    else:
        query_buildings = get_search_query(user, project_dict)
        denominator = query_buildings.count() - len(selected_buildings)
        cache.set(
            project.adding_buildings_status_percentage_cache_key,
            {'percentage_done': 10, 'numerator': i, 'denominator': denominator}
        )
        i = 0
        for b in query_buildings:
            # todo: only get back query_buildings pks as a list, and create
            # using the pk,
            #       not the python object
            i += 1
            ProjectBuilding.objects.get_or_create(
                project=project, building_snapshot=b
            )
            cache.set(
                project.adding_buildings_status_percentage_cache_key,
                {
                    'percentage_done': float(i) / denominator * 100,
                    'numerator': i, 'denominator': denominator
                }
            )
        for building in selected_buildings:
            i += 1
            project.building_snapshots.remove(
                BuildingSnapshot.objects.get(pk=building)
            )
            cache.set(
                project.adding_buildings_status_percentage_cache_key,
                {
                    'percentage_done': (
                        float(denominator - len(selected_buildings) + i) /
                        denominator * 100
                    ),
                    'numerator': denominator - len(selected_buildings) + i,
                    'denominator': denominator
                }
            )

    cache.set(
        project.adding_buildings_status_percentage_cache_key,
        {'percentage_done': 100, 'numerator': i, 'denominator': denominator}
    )

    deadline_date = project_dict.get('deadline_date')
    if isinstance(deadline_date, (int, float)):
        deadline_date = datetime.datetime.fromtimestamp(deadline_date / 1000)
    elif isinstance(deadline_date, basestring):
        deadline_date = parser.parse(deadline_date)
    else:
        deadline_date = None
    end_date = project_dict.get('end_date')
    if isinstance(end_date, (int, float)):
        end_date = datetime.datetime.fromtimestamp(end_date / 1000)
    elif isinstance(end_date, basestring):
        end_date = parser.parse(end_date)
    else:
        end_date = None
    if end_date:
        last_day_of_month = calendar.monthrange(
            end_date.year, end_date.month
        )[1]
        end_date = datetime.datetime(
            end_date.year, end_date.month, last_day_of_month
        )

    if project_dict.get('compliance_type'):
        compliance = Compliance.objects.create(
            compliance_type=project_dict.get('compliance_type'),
            end_date=end_date,
            deadline_date=deadline_date,
            project=project
        )
        compliance.save()


@task
def remove_buildings(project_slug, project_dict, user_pk):
    """adds buildings to a project. if a user has selected all buildings,
       then the the search parameters within project_dict are used to determine
       the total set of buildings.

       :param str project_slug: a project's slug used to get the project
       :param dict project_dict: contains search params, and browser state
           infomation
       :user_pk int or str: the user's pk or id
    """
    project = Project.objects.get(slug=project_slug)
    user = User.objects.get(pk=user_pk)
    project.last_modified_by = user
    project.save()

    selected_buildings = project_dict.get('selected_buildings', [])

    cache.set(
        project.removing_buildings_status_percentage_cache_key,
        {'percentage_done': 0, 'numerator': 0, 'denominator': 0}
    )
    i = 0
    denominator = 1
    if not project_dict.get('select_all_checkbox', False):
        for sfid in selected_buildings:
            i += 1
            denominator = len(selected_buildings)
            cache.set(
                project.removing_buildings_status_percentage_cache_key,
                {
                    'percentage_done': (
                        float(i) / max(len(selected_buildings), 1) * 100
                    ),
                    'numerator': i,
                    'denominator': denominator
                }
            )
            ab = BuildingSnapshot.objects.get(pk=sfid)
            ProjectBuilding.objects.get(
                project=project, building_snapshot=ab
            ).delete()
    else:
        query_buildings = get_search_query(user, project_dict)
        denominator = query_buildings.count() - len(selected_buildings)
        cache.set(
            project.adding_buildings_status_percentage_cache_key,
            {
                'percentage_done': 10,
                'numerator': i,
                'denominator': denominator
            }
        )
        for b in query_buildings:
            ProjectBuilding.objects.get(
                project=project, building_snapshot=b
            ).delete()
        cache.set(
            project.adding_buildings_status_percentage_cache_key,
            {
                'percentage_done': 50,
                'numerator': denominator - len(selected_buildings),
                'denominator': denominator
            }
        )
        for building in selected_buildings:
            i += 1
            ab = BuildingSnapshot.objects.get(source_facility_id=building)
            ProjectBuilding.objects.create(
                project=project, building_snapshot=ab
            )
            cache.set(
                project.adding_buildings_status_percentage_cache_key,
                {
                    'percentage_done': (
                        float(denominator - len(selected_buildings) + i) /
                        denominator * 100
                    ),
                    'numerator': denominator - len(selected_buildings) + i,
                    'denominator': denominator
                }
            )

    cache.set(
        project.removing_buildings_status_percentage_cache_key,
        {'percentage_done': 100, 'numerator': i, 'denominator': denominator}
    )


#
## New MCM tasks for importing ESPM data.
###

def add_cache_increment_parameter(tasks):
    """This adds the cache increment value to the signature to each subtask."""
    denom = len(tasks) or 1
    increment = 1.0 / denom * 100
    # This is kind of terrible. Once we know how much progress each task
    # yeilds, we must pass that value into the Signature for the sub tassks.
    for _task in tasks:
        _task.args = _task.args + (increment,)

    return tasks


@task
def finish_import_record(import_record_pk):
    """Set all statuses to Done, etc."""
    states = ('done', 'active', 'queued')
    actions = ('merge_analysis', 'premerge_analysis')
    # Really all these status attributes are tedious.
    import_record = ImportRecord.objects.get(pk=import_record_pk)
    for action in actions:
        for state in states:
            value = False
            if state == 'done':
                value = True
            setattr(import_record, '{0}_{1}'.format(action, state), value)

    import_record.finish_time = datetime.datetime.utcnow()
    import_record.status = STATUS_READY_TO_MERGE
    import_record.save()


@task
def finish_mapping(results, file_pk):
    import_file = ImportFile.objects.get(pk=file_pk)
    import_file.mapping_done = True
    import_file.save()
    finish_import_record(import_file.import_record.pk)
    prog_key = get_prog_key('map_data', file_pk)
    cache.set(prog_key, 100)


def _translate_unit_to_type(unit):
    if unit is None or unit == 'String':
        return 'str'

    return unit.lower()


def _build_cleaner(org):
    """Return a cleaner instance that knows about a mapping's unit types.

    Basically, this just tells us how to try and cast types during cleaning
    based on the Column definition in the database.

    :param org: superperms.orgs.Organization instance.
    :returns: dict of dicts. {'types': {'col_name': 'type'},}
    """
    units = {'types': {}}
    for column in Column.objects.filter(
        mapped_mappings__super_organization=org
    ).select_related('unit'):
        column_type = 'str'
        if column.unit:
            column_type = _translate_unit_to_type(
                column.unit.get_unit_type_display()
            )
        units['types'][column.column_name] = column_type

    # TODO(gavin): make this completely data-driven.
    # Update with our predefined types for our BuildingSnapshot
    # column types.
    units['types'].update(seed_schema.schema['types'])

    return cleaners.Cleaner(units)


def apply_extra_data(model, key, value):
    """Function sent to MCM to apply mapped columns into extra_data."""
    model.extra_data[key] = value


def apply_data_func(mappable_columns):
    """Returns a function that captures mappable_types in a closure
       and will add a key to extra data if not in mappable_types else
    """

    def result_fn(model, key, value):
        if key in mappable_columns:
            setattr(model, key, value)
        else:
            apply_extra_data(model, key, value)

    return result_fn


@task
def map_row_chunk(
    chunk, file_pk, source_type, prog_key, increment, *args, **kwargs
):
    """Does the work of matching a mapping to a source type and saving

    :param chunk: list of dict of str. One row's worth of parse data.
    :param file_pk: int, the PK for an ImportFile obj.
    :param source_type: int, represented by either ASSESSED_RAW, or
        PORTFOLIO_RAW.
    :param cleaner: (optional), the cleaner class you want to send
    to mapper.map_row. (e.g. turn numbers into floats.).
    :param raw_ids: (optional kwarg), the list of ids in chunk order.

    """
    import_file = ImportFile.objects.get(pk=file_pk)
    save_type = PORTFOLIO_BS
    if source_type == ASSESSED_RAW:
        save_type = ASSESSED_BS

    concats = []

    org = Organization.objects.get(
        pk=import_file.import_record.super_organization.pk
    )

    mapping, concats = get_column_mappings(org)
    map_cleaner = _build_cleaner(org)

    # For those column mapping which are not db columns, we
    # need to let MCM know that we apply our mapping function to those.
    apply_columns = []

    mappable_columns = get_mappable_columns()
    for item in mapping:
        if mapping[item] not in mappable_columns:
            apply_columns.append(item)

    apply_func = apply_data_func(mappable_columns)

    for row in chunk:
        model = mapper.map_row(
            row,
            mapping,
            BuildingSnapshot,
            cleaner=map_cleaner,
            concat=concats,
            apply_columns=apply_columns,
            apply_func=apply_func,
            *args,
            **kwargs
        )

        model.import_file = import_file
        model.source_type = save_type
        model.clean()
        model.super_organization = import_file.import_record.super_organization
        model.save()
    if model:
        # Make sure that we've saved all of the extra_data column names
        save_column_names(model, mapping=mapping)

    increment_cache(prog_key, increment)


@task
@lock_and_track
def _map_data(file_pk, *args, **kwargs):
    """Get all of the raw data and process it using appropriate mapping.
    @lock_and_track returns a progress_key

    :param file_pk: int, the id of the import_file we're working with.

    """
    import_file = ImportFile.objects.get(pk=file_pk)
    # Don't perform this task if it's already been completed.
    if import_file.mapping_done:
        prog_key = get_prog_key('map_data', file_pk)
        cache.set(prog_key, 100)
        return {'status': 'warning', 'message': 'mapping already complete'}

    # If we haven't finished saving, we shouldn't proceed with mapping
    # Re-queue this task.
    if not import_file.raw_save_done:
        map_data.apply_async(args=[file_pk], countdown=60, expires=120)
        return {'status': 'error', 'message': 'waiting for raw data save.'}

    source_type_dict = {
        'Portfolio Raw': PORTFOLIO_RAW,
        'Assessed Raw': ASSESSED_RAW,
        'Green Button Raw': GREEN_BUTTON_RAW,
    }
    source_type = source_type_dict.get(import_file.source_type, ASSESSED_RAW)

    qs = BuildingSnapshot.objects.filter(
        import_file=import_file,
        source_type=source_type,
    ).iterator()

    prog_key = get_prog_key('map_data', file_pk)
    tasks = []
    for chunk in batch(qs, 100):
        serialized_data = [obj.extra_data for obj in chunk]
        tasks.append(map_row_chunk.subtask(
            (serialized_data, file_pk, source_type, prog_key)
        ))

    tasks = add_cache_increment_parameter(tasks)
    if tasks:
        chord(tasks, interval=15)(finish_mapping.subtask([file_pk]))
    else:
        finish_mapping.task(file_pk)

    return {'status': 'success'}


@task
@lock_and_track
def map_data(file_pk, *args, **kwargs):
    """Small wrapper to ensure we isolate our mapping process from requests."""
    _map_data.delay(file_pk, *args, **kwargs)
    return {'status': 'succuss'}


@task
def _save_raw_data_chunk(chunk, file_pk, prog_key, increment, *args, **kwargs):
    """Save the raw data to the database."""
    import_file = ImportFile.objects.get(pk=file_pk)
    # Save our "column headers" and sample rows for F/E.
    source_type = get_source_type(import_file)
    for c in chunk:
        raw_bs = BuildingSnapshot()
        raw_bs.import_file = import_file
        raw_bs.extra_data = c
        raw_bs.source_type = source_type

        # We require a save to get our PK
        # We save here to set our initial source PKs.
        raw_bs.save()
        super_org = import_file.import_record.super_organization
        raw_bs.super_organization = super_org

        set_initial_sources(raw_bs)
        raw_bs.save()

    # Indicate progress
    increment_cache(prog_key, increment)


@task
def finish_raw_save(results, file_pk):
    import_file = ImportFile.objects.get(pk=file_pk)
    import_file.raw_save_done = True
    import_file.save()
    prog_key = get_prog_key('save_raw_data', file_pk)
    cache.set(prog_key, 100)


def cache_first_rows(import_file, parser):
    """Cache headers, and rows 2-6 for validation/viewing.

    :param import_file: ImportFile inst.
    :param parser: unicode-csv.Reader instance.

    Unfortunately, this is duplicated logic from data_importer,
    but since data_importer makes many faulty assumptions we need to do
    it differently.

    """
    parser.seek_to_beginning()
    rows = parser.next()

    validation_rows = []
    for i in range(5):
        row = rows.next()
        if row:
            validation_rows.append(row)

    import_file.cached_second_to_fifth_row = "\n".join(
        [
            ROW_DELIMITER.join(map(lambda x: str(x), r.values()))
            for r in validation_rows
        ]
    )
    first_row = rows.next().keys()
    if first_row:
        first_row = ROW_DELIMITER.join(first_row)
    import_file.cached_first_row = first_row or ''

    import_file.save()
    # Reset our file pointer for mapping.
    parser.seek_to_beginning()


@task
@lock_and_track
def _save_raw_green_button_data(file_pk, *args, **kwargs):
    """
    Pulls identifying information out of the xml data, find_or_creates
    a building_snapshot for the data, parses and stores the timeseries
    meter data and associates it with the building snapshot.
    """

    import_file = ImportFile.objects.get(pk=file_pk)

    import_file.raw_save_done = True
    import_file.save()

    res = xml_importer.import_xml(import_file)

    prog_key = get_prog_key('save_raw_data', file_pk)
    cache.set(prog_key, 100)

    if res:
        return {'status': 'success'}

    return {
        'status': 'error',
        'message': 'data failed to import'
    }


@task
@lock_and_track
def _save_raw_data(file_pk, *args, **kwargs):
    """Chunk up the CSV and save data into the DB raw."""
    import_file = ImportFile.objects.get(pk=file_pk)

    if import_file.raw_save_done:
        return {'status': 'warning', 'message': 'raw data already saved'}

    if import_file.source_type == "Green Button Raw":
        return _save_raw_green_button_data(file_pk, *args, **kwargs)

    parser = reader.MCMParser(import_file.local_file)
    cache_first_rows(import_file, parser)
    rows = parser.next()
    import_file.num_rows = 0

    prog_key = get_prog_key('save_raw_data', file_pk)

    tasks = []
    for chunk in batch(rows, 100):
        import_file.num_rows += len(chunk)
        tasks.append(_save_raw_data_chunk.subtask((chunk, file_pk, prog_key)))

    tasks = add_cache_increment_parameter(tasks)
    import_file.num_columns = parser.num_columns()
    import_file.save()

    if tasks:
        chord(tasks, interval=15)(finish_raw_save.subtask([file_pk]))
    else:
        finish_raw_save.task(file_pk)

    return {'status': 'success'}


@task
@lock_and_track
def save_raw_data(file_pk, *args, **kwargs):
    _save_raw_data.delay(file_pk, *args, **kwargs)
    return {'status': 'success'}


def _stringify(values):
    """Take iterable of str and NoneTypes and reduce to space sep. str."""
    return ' '.join(
        [PUNCT_REGEX.sub('', value.lower()) for value in values if value]
    )


def handle_results(results, b_idx, can_rev_idx, unmatched_list, user_pk):
    """Seek IDs and save our snapshot match.

    :param results: list of tuples. [('match', 0.99999),...]
    :param b_idx: int, the index of the current building in the unmatched_list.
    :param can_rev_idx: dict, reverse index from match -> canonical PK.
    :param user_pk: user ID, used for AuditLog logging
    :unmatched_list: list of dicts, the result of a values_list query for
        unmatched BSes.

    """
    match_string, confidence = results[0]  # We always care about closest match
    match_type = SYSTEM_MATCH
    # If we passed the minimum threshold, we're here, but we need to
    # distinguish probable matches from good matches.
    if confidence < getattr(settings, 'MATCH_MED_THRESHOLD', 0.7):
        match_type = POSSIBLE_MATCH

    can_snap_pk = can_rev_idx[match_string]
    building_pk = unmatched_list[b_idx][0]  # First element is PK

    bs = save_snapshot_match(
        can_snap_pk, building_pk, confidence=confidence, match_type=match_type
    )
    canon = bs.canonical_building
    AuditLog.objects.create(
        user_id=user_pk,
        content_object=canon,
        action_note='System matched building.',
        action='save_system_match',
        organization=bs.super_organization,
    )


@task
@lock_and_track
def match_buildings(file_pk, user_pk):
    """kicks off system matching, returns progress key"""
    import_file = ImportFile.objects.get(pk=file_pk)
    if import_file.matching_done:
        prog_key = get_prog_key('match_buildings', file_pk)
        cache.set(prog_key, 100)
        return {'status': 'warning', 'message': 'matching already complete'}

    if not import_file.mapping_done:
        # Re-add to the queue, hopefully our mapping will be done by then.
        match_buildings.apply_async(
            args=[file_pk, user_pk], countdown=10, expires=20
        )
        return {
            'status': 'error',
            'message': 'waiting for mapping to complete'
        }

    _match_buildings.delay(file_pk, user_pk)

    return {'status': 'success'}


def get_canonical_snapshots(org_id):
    """Return all of the BuildingSnapshots that are canonical for an org."""
    snapshots = BuildingSnapshot.objects.filter(
        canonicalbuilding__active=True, super_organization_id=org_id
    )

    return snapshots


def get_canonical_id_matches(org_id, pm_id, tax_id, custom_id):
    """Returns canonical snapshots that match at least one id."""
    params = []
    can_snapshots = get_canonical_snapshots(org_id)
    if pm_id:
        params.append(Q(pm_property_id=pm_id))
        params.append(Q(tax_lot_id=pm_id))
        params.append(Q(custom_id_1=pm_id))
    if tax_id:
        params.append(Q(pm_property_id=tax_id))
        params.append(Q(tax_lot_id=tax_id))
        params.append(Q(custom_id_1=tax_id))
    if custom_id:
        params.append(Q(pm_property_id=custom_id))
        params.append(Q(tax_lot_id=custom_id))
        params.append(Q(custom_id_1=custom_id))

    if not params:
        # Return an empty QuerySet if we don't have any params.
        return can_snapshots.none()

    canonical_matches = can_snapshots.filter(
        reduce(operator.or_, params)
    )

    return canonical_matches


def handle_id_matches(unmatched_bs, import_file, user_pk):
    """"Deals with exact maches in the IDs of buildings."""
    id_matches = get_canonical_id_matches(
        unmatched_bs.super_organization_id,
        unmatched_bs.pm_property_id,
        unmatched_bs.tax_lot_id,
        unmatched_bs.custom_id_1
    )
    if not id_matches.exists():
        return

    # merge save as system match with high confidence.
    for can_snap in id_matches:
        # Merge all matches together; updating "unmatched" pointer
        # as we go.
        unmatched_bs = save_snapshot_match(
            can_snap.pk,
            unmatched_bs.pk,
            confidence=0.9,  # TODO(gavin) represent conf better.
            match_type=SYSTEM_MATCH,
            user=import_file.import_record.owner
        )
        canon = unmatched_bs.canonical_building
        canon.canonical_snapshot = unmatched_bs
        canon.save()
        AuditLog.objects.create(
            user_id=user_pk,
            content_object=canon,
            action_note='System matched building ID.',
            action='save_system_match',
            organization=unmatched_bs.super_organization,
        )

    # Returns the most recent child of all merging.
    return unmatched_bs


def _finish_matching(import_file, progress_key):
    import_file.matching_done = True
    import_file.mapping_completion = 100
    import_file.save()
    cache.set(progress_key, 100)


@task
@lock_and_track
def _match_buildings(file_pk, user_pk):
    """ngram search against all of the canonical_building snapshots for org."""
    min_threshold = settings.MATCH_MIN_THRESHOLD
    import_file = ImportFile.objects.get(pk=file_pk)
    prog_key = get_prog_key('match_buildings', file_pk)
    org = Organization.objects.filter(
        users=import_file.import_record.owner
    )[0]

    unmatched_buildings = find_unmatched_buildings(import_file)

    newly_matched_building_pks = []
    for unmatched in unmatched_buildings:
        match = handle_id_matches(unmatched, import_file, user_pk)
        if match:
            newly_matched_building_pks.extend([match.pk, unmatched.pk])

    # Remove any buildings we just did exact ID matches with.
    unmatched_buildings = unmatched_buildings.exclude(
        pk__in=newly_matched_building_pks
    ).values_list(*BS_VALUES_LIST)

    # If we don't find any unmatched buildings, there's nothing left to do.
    if not unmatched_buildings:
        _finish_matching(import_file, prog_key)
        return

    # Here we want all the values not related to the BS id for doing comps.
    unmatched_ngrams = [
        _stringify(list(values)[1:]) for values in unmatched_buildings
    ]

    canonical_buildings = find_canonical_building_values(org)
    if not canonical_buildings:
        # There are no canonical_buildings for this organization, all unmatched
        # buildings will then become canonicalized.
        hydrated_unmatched_buildings = BuildingSnapshot.objects.filter(
            pk__in=[item[0] for item in unmatched_buildings]
        )
        num_unmatched = len(unmatched_ngrams) or 1
        increment = 1.0 / num_unmatched * 100
        for (i, unmatched) in enumerate(hydrated_unmatched_buildings):
            initialize_canonical_building(unmatched, user_pk)
            if i % 100 == 0:
                increment_cache(prog_key, increment * 100)

        _finish_matching(import_file, prog_key)
        return

    # This allows us to retrieve the PK for a given NGram after a match.
    can_rev_idx = {
        _stringify(value[1:]): value[0] for value in canonical_buildings
    }
    n = ngram.NGram(
        [_stringify(values[1:]) for values in canonical_buildings]
    )

    # For progress tracking

    num_unmatched = len(unmatched_ngrams) or 1
    increment = 1.0 / num_unmatched * 100

    # PKs when we have a match.
    import_file.mapping_completion = 0
    import_file.save()
    for i, building in enumerate(unmatched_ngrams):
        results = n.search(building, min_threshold)
        if results:
            handle_results(
                results, i, can_rev_idx, unmatched_buildings, user_pk
            )
        else:
            hydrated_building = BuildingSnapshot.objects.get(
                pk=unmatched_buildings[i][0]
            )
            initialize_canonical_building(hydrated_building, user_pk)

        if i % 100 == 0:
            increment_cache(prog_key, increment * 100)
            import_file.mapping_completion += int(increment * 100)
            import_file.save()

    _finish_matching(import_file, prog_key)
    return {'status': 'success'}


@task
@lock_and_track
def _remap_data(import_file_pk):
    """The delecate parts of deleting and remapping data for a file.

    :param import_file_pk: int, the ImportFile primary key.
    :param mapping_cache_key: str, the cache key for this file's mapping prog.

    """
    # Reset mapping progress cache as well.
    import_file = ImportFile.objects.get(pk=import_file_pk)
    # Delete buildings already mapped for this file.
    BuildingSnapshot.objects.filter(
        import_file=import_file,
        source_type__in=(ASSESSED_BS, PORTFOLIO_BS, GREEN_BUTTON_BS)
    ).exclude(
        children__isnull=False
    ).delete()

    import_file.mapping_done = False
    import_file.mapping_completion = None
    import_file.save()

    map_data(import_file_pk)


@task
def remap_data(import_file_pk):
    """"Delete mapped buildings for current import file, re-map them."""
    import_file = ImportFile.objects.get(pk=import_file_pk)
    # Check to ensure that the building has not already been merged.
    mapping_cache_key = get_prog_key('map_data', import_file.pk)
    if import_file.matching_done or import_file.matching_completion:
        cache.set(mapping_cache_key, 100)
        return {
            'status': 'warning', 'message': 'Mapped buildings already merged'
        }

    _remap_data.delay(import_file_pk)

    # Make sure that our mapping cache progress is reset.
    cache.set(mapping_cache_key, 0)
    # Here we also return the mapping_prog_key so that the front end can
    # follow the progress.
    return {'status': 'success', 'progress_key': mapping_cache_key}


@task
@lock_and_track
def delete_organization_buildings(org_pk, *args, **kwargs):
    """Deletes all BuildingSnapshot instances within an organization

    :param org_pk: int, str, the organization pk
    :returns: Dict. with keys ``status`` and ``progress_key``
    """
    _delete_organization_buildings.delay(org_pk, *args, **kwargs)
    return {'status': 'success'}


@task
@lock_and_track
def _delete_organization_buildings(org_pk, chunk_size=100, *args, **kwargs):
    """Deletes all BuildingSnapshot instances within an organization

    :param org_pk: int, str, the organization pk
    """
    qs = BuildingSnapshot.objects.filter(super_organization=org_pk)
    ids = qs.values_list('id', flat=True)
    deleting_cache_key = get_prog_key(
        'delete_organization_buildings',
        org_pk
    )
    if not ids:
        cache.set(deleting_cache_key, 100)
        return

    # delete the canonical buildings
    can_ids = CanonicalBuilding.objects.filter(
        canonical_snapshot__super_organization=org_pk
    ).values_list('id', flat=True)
    _delete_canonical_buildings.delay(can_ids)

    step = float(chunk_size) / len(ids)
    cache.set(deleting_cache_key, 0)
    tasks = []
    for del_ids in batch(ids, chunk_size):
        # we could also use .s instead of .subtask and not wrap the *args
        tasks.append(
            _delete_organization_buildings_chunk.subtask(
                (del_ids, deleting_cache_key, step, org_pk)
            )
        )
    chord(tasks, interval=15)(finish_delete.subtask([org_pk]))


@task
def _delete_organization_buildings_chunk(del_ids, prog_key, increment,
                                         org_pk, *args, **kwargs):
    """deletes a list of ``del_ids`` and increments the cache"""
    qs = BuildingSnapshot.objects.filter(super_organization=org_pk)
    qs.filter(pk__in=del_ids).delete()
    increment_cache(prog_key, increment * 100)


@task
def finish_delete(results, org_pk):
    prog_key = get_prog_key('delete_organization_buildings', org_pk)
    cache.set(prog_key, 100)


@task
def _delete_canonical_buildings(ids, chunk_size=300):
    """deletes CanonicalBuildings

    :param ids: list of ids to delete from CanonicalBuilding
    :param chunk_size: number of CanonicalBuilding instances to delete per
    iteration
    """
    for del_ids in batch(ids, chunk_size):
        CanonicalBuilding.objects.filter(pk__in=del_ids).delete()


@task
def log_deleted_buildings(ids, user_pk, chunk_size=300):
    """
    AudigLog logs a delete entry for the canonical building or each
    BuildingSnapshot in ``ids``
    """
    for del_ids in batch(ids, chunk_size):
        for b in BuildingSnapshot.objects.filter(pk__in=del_ids):
            AuditLog.objects.create(
                user_id=user_pk,
                content_object=b.canonical_building,
                organization=b.super_organization,
                action='delete_building',
                action_note='Deleted building.'
            )
