# -*- coding: utf-8 -*-
#
# This file is part of Bika LIMS
#
# Copyright 2011-2017 by it's authors.
# Some rights reserved. See LICENSE.txt, AUTHORS.txt.

import csv
import json
import transaction
from DateTime import DateTime
from bika.lims import logger
from bika.lims.browser import ulocalized_time
from bika.lims.content.analysisrequest import schema as ar_schema
from bika.lims.content.arimport import convert_date_string 
from bika.lims.content.arimport import get_row_container 
from bika.lims.content.arimport import get_row_profile_services 
from bika.lims.content.arimport import get_row_services 
from bika.lims.content.arimport import lookup_sampler_uid 
from bika.lims.content.sample import schema as sample_schema
from bika.lims.idserver import renameAfterCreation
from bika.lims.utils import tmpID, getUsers
from bika.lims.vocabularies import CatalogVocabulary
from collective.taskqueue.interfaces import ITaskQueue
from Products.CMFPlone.utils import _createObjectByType
from Products.Archetypes.event import ObjectInitializedEvent
from Products.Archetypes.utils import addStatusMessage
from Products.CMFCore.utils import getToolByName
from zope import event
from zope.component import queryUtility
from zope.i18nmessageid import MessageFactory

_p = MessageFactory(u"plone")

def save_sample_data(self):
    """Save values from the file's header row into the DataGrid columns
    after doing some very basic validation
    """
    bsc = getToolByName(self, 'bika_setup_catalog')
    keywords = self.bika_setup_catalog.uniqueValuesFor('getKeyword')
    profiles = []
    for p in bsc(portal_type='AnalysisProfile'):
        p = p.getObject()
        profiles.append(p.Title())
        profiles.append(p.getProfileKey())

    sample_data = self.get_sample_values()
    if not sample_data:
        return False

    # columns that we expect, but do not find, are listed here.
    # we report on them only once, after looping through sample rows.
    missing = set()

    # This contains all sample header rows that were not handled
    # by this code
    unexpected = set()

    # Save other errors here instead of sticking them directly into
    # the field, so that they show up after MISSING and before EXPECTED
    errors = []

    # This will be the new sample-data field value, when we are done.
    grid_rows = []

    row_nr = 0
    for row in sample_data['samples']:
        row = dict(row)
        row_nr += 1

        # sid is just for referring the user back to row X in their
        # in put spreadsheet
        try:
            gridrow = {'sid': row['Samples']}
        except KeyError, e:
            raise RuntimeError('AR Import: CultivationBatch not in input file')
        del (row['Samples'])

        try:
            gridrow = {'ClientSampleID': row['ClientSampleID']}
        except KeyError, e:
            raise RuntimeError('AR Import: ClientSampleID not in input file')
        del (row['ClientSampleID'])

        try:
            gridrow['CultivationBatch'] = row['CultivationBatch']
        except KeyError, e:
            raise RuntimeError('AR Import: CultivationBatch not in input file')
        del (row['CultivationBatch'])


        try:
            gridrow['ClientStateLicenseID'] = row['ClientStateLicenseID']
        except KeyError, e:
            raise RuntimeError(
                    'AR Import: ClientStateLicenseID not in input file')
        title = row['ClientStateLicenseID']
        if len(title) == 0:
            errors.append("Row %s: ClientStateLicenseID is required" % row_nr)
        if title:
            for license in self.aq_parent.getLicenses():
                license_types = bsc(
                                    portal_type='ClientType',
                                    UID=license['LicenseType'])
                if len(license_types) == 1:
                    license_type = license_types[0].Title
                    if license_type == title:
                        longstring ='{},{LicenseID},{LicenseNumber},{Authority}'
                        id_value = longstring.format(license_type, **license)
                        gridrow['ClientStateLicenseID'] = id_value
        del (row['ClientStateLicenseID'])

        gridrow['ClientReference'] = row['ClientReference']
        del (row['ClientReference'])

        gridrow['Lot'] = row['Lot']
        del (row['Lot'])

        gridrow['Strain'] = row['Strain']
        title = row['Strain']
        if len(title) == 0:
            errors.append("Row %s: Strain is required" % row_nr)
        if title:
            obj = self.lookup(('Strain',),
                              Title=title)
            if obj:
                gridrow['Strain'] = obj[0].UID
        del (row['Strain'])

        #Validation only
        samplingDate = row['SamplingDate']
        if len(samplingDate) == 0:
            errors.append("Row %s: SamplingDate is required" % row_nr)
        try:
            dummy = DateTime(samplingDate)
        except:
            errors.append("Row %s: SamplingDate format is incorrect" % row_nr)

        #Validation only
        if 'Sampler' not in row.keys():
            row['Sampler'] = ''
        else:
            if row['Sampler'] is None and len(row['Sampler']) == 0:
                row['Sampler'] = ''

        #Validation only
        if len(row['Priority']) == 0:
            errors.append("Row %s: Priority is required" % row_nr)

        # We'll use this later to verify the number against selections
        if 'Total number of Analyses or Profiles' in row:
            nr_an = row['Total number of Analyses or Profiles']
            del (row['Total number of Analyses or Profiles'])
        else:
            nr_an = 0
        try:
            nr_an = int(nr_an)
        except ValueError:
            nr_an = 0

        # ContainerType - not part of sample or AR schema
        if 'ContainerType' in row:
            title = row['ContainerType']
            if title:
                obj = self.lookup(('ContainerType',),
                                  Title=row['ContainerType'])
                if obj:
                    gridrow['ContainerType'] = obj[0].UID
            del (row['ContainerType'])

        # match against sample schema
        for k, v in row.items():
            if k in ['Analyses', 'Profiles']:
                continue
            if k in sample_schema:
                del (row[k])
                if v:
                    try:
                        value = self.munge_field_value(
                            sample_schema, row_nr, k, v)
                        gridrow[k] = value
                    except ValueError as e:
                        errors.append(e.message)

        # match against ar schema
        for k, v in row.items():
            if k in ['AnalysisProfile', 'Analyses', 'Profiles']:
                continue
            if k in ar_schema:
                del (row[k])
                if v:
                    try:
                        value = self.munge_field_value(
                            ar_schema, row_nr, k, v)
                        gridrow[k] = value
                    except ValueError as e:
                        errors.append(e.message)

        gridrow['Profiles'] = []
        for k, v in row.items():
            if v in profiles:
                del (row[k])
                gridrow['Profiles'].append(v)

        #if len(gridrow['Profiles']) != nr_an:
        #    errors.append(
        #        "Row %s: Number of analyses does not match provided value" %
        #        row_nr)

        grid_rows.append(gridrow)

    self.setSampleData(grid_rows)

    if missing:
        self.error("SAMPLES: Missing expected fields: %s" %
                   ','.join(missing))

    for thing in errors:
        self.error(thing)

    if unexpected:
        self.error("Unexpected header fields: %s" %
                   ','.join(unexpected))

def workflow_before_validate(self):
    """This function transposes values from the provided file into the
    ARImport object's fields, and checks for invalid values.

    If errors are found:
        - Validation transition is aborted.
        - Errors are stored on object and displayed to user.

    """
    # Re-set the errors on this ARImport each time validation is attempted.
    # When errors are detected they are immediately appended to this field.
    if not self.getClient():
        #Modified from ProcessForm only
        return

    self.setErrors([])

    def item_empty(gridrow, key):
        if not gridrow.get(key, False):
            return True
        return len(gridrow[key]) == 0

    row_nr = 0
    for gridrow in self.getSampleData():
        row_nr += 1
        for key in (
                'SamplingDate', 'Priority', 'Strain', 'ClientStateLicenseID'):
            if item_empty(gridrow, key):
                self.error("Row %s: %s is required" % (row_nr, key))
        samplingDate = gridrow['SamplingDate']
        try:
            new = DateTime(samplingDate)
            ulocalized_time(new, long_format=True, time_only=False, context=self)
        except:
            self.error(
                "Row %s: SamplingDate format must be 2017-06-21" % row_nr)
        sampler = gridrow['Sampler']
        if not sampler:
            gridrow['Sampler'] = ''

    self.validate_headers()
    self.validate_samples()

    if self.getErrors() and self.getErrors() != ():
        addStatusMessage(self.REQUEST, _p('Validation errors.'), 'error')
        transaction.commit()
        self.REQUEST.response.write(
            '<script>document.location.href="%s/edit"</script>' % (
                self.absolute_url()))
    self.REQUEST.response.write(
        '<script>document.location.href="%s/view"</script>' % (
            self.absolute_url()))

def save_header_data(self):
    """Save values from the file's header row into their schema fields.
    """
    client = self.aq_parent

    headers = self.get_header_values()
    if not headers:
        return False

    if client:
        self.setClient(client)

    for h, f in [
        ('File name', 'Filename'),
        #('No of Samples', 'NrSamples'),
        ('Client name', 'ClientName'),
        ('Client ID', 'ClientID'),
        #('Client Order Number', 'ClientOrderNumber'),
        #('Client Reference', 'ClientReference')
    ]:
        v = headers.get(h, None)
        if v:
            field = self.schema[f]
            field.set(self, v)
        del (headers[h])
    # Primary Contact
    v = headers.get('Contact', None)
    contacts = [x for x in client.objectValues('Contact')]
    contact = [c for c in contacts if c.Title() == v]
    if contact:
        self.schema['Contact'].set(self, contact)
    else:
        self.error("Specified contact '%s' does not exist; using '%s'"%
                   (v, contacts[0].Title()))
        self.schema['Contact'].set(self, contacts[0])
    del (headers['Contact'])

    # CCContacts
    field_value = {
        'CCNamesReport': '',
        'CCEmailsReport': '',
        'CCNamesInvoice': '',
        'CCEmailsInvoice': ''
    }
    for h, f in [
        # csv header name      DataGrid Column ID
        ('CC Names - Report', 'CCNamesReport'),
        ('CC Emails - Report', 'CCEmailsReport'),
        ('CC Names - Invoice', 'CCNamesInvoice'),
        ('CC Emails - Invoice', 'CCEmailsInvoice'),
    ]:
        if h in headers:
            values = [x.strip() for x in headers.get(h, '').split(",")]
            field_value[f] = values if values else ''
            del (headers[h])
    self.schema['CCContacts'].set(self, [field_value])

    if headers:
        unexpected = ','.join(headers.keys())
        self.error("Unexpected header fields: %s" % unexpected)


def workflow_script_import(self):
    """Create objects from valid ARImport
    """

    bsc = getToolByName(self, 'bika_setup_catalog')
    workflow = getToolByName(self, 'portal_workflow')
    client = self.aq_parent
    batch = self.schema['Batch'].get(self)
    contact = self.getContact()

    title = _p('Submitting AR Import')
    description = _p('Creating and initialising objects')

    profiles = [x.getObject() for x in bsc(portal_type='AnalysisProfile')]

    gridrows = self.schema['SampleData'].get(self)
    task_queue = queryUtility(ITaskQueue, name='arimport-create')
    if task_queue is not None:
        path = [i for i in client.getPhysicalPath()]
        path.append('ar_import_async')
        path = '/'.join(path)

        params = {
                'gridrows': json.dumps(gridrows),
                'client_uid': client.UID(),
                'batch_uid': batch.UID(),
                'client_order_num': self.getClientOrderNumber(),
                'contact_uid': contact.UID(),
                }
        logger.info('Queue Task: path=%s' % path)
        logger.debug('Que Task: path=%s, params=%s' % (
                        path, params))
        task_id = task_queue.add(path,
                method='POST',
                params=params)
        # document has been written to, and redirect() fails here
        self.REQUEST.response.write(
            '<script>document.location.href="%s"</script>' % (
                client.absolute_url()))
        return
    row_cnt = 0
    for therow in gridrows:
        row = therow.copy()
        row_cnt += 1
        # Create Sample
        sample = _createObjectByType('Sample', client, tmpID())
        sample.unmarkCreationFlag()
        # First convert all row values into something the field can take
        sample.edit(**row)
        sample._renameAfterCreation()
        event.notify(ObjectInitializedEvent(sample))
        sample.at_post_create_script()
        swe = self.bika_setup.getSamplingWorkflowEnabled()
        if swe:
            workflow.doActionFor(sample, 'sampling_workflow')
        else:
            workflow.doActionFor(sample, 'no_sampling_workflow')
        part = _createObjectByType('SamplePartition', sample, 'part-1')
        part.unmarkCreationFlag()
        renameAfterCreation(part)
        if swe:
            workflow.doActionFor(part, 'sampling_workflow')
        else:
            workflow.doActionFor(part, 'no_sampling_workflow')
        # Container is special... it could be a containertype.
        container = get_row_container(row)
        if container:
            if container.portal_type == 'ContainerType':
                containers = container.getContainers()
            # XXX And so we must calculate the best container for this partition
            part.edit(Container=containers[0])

        # Profiles are titles, profile keys, or UIDS: convert them to UIDs.
        newprofiles = []
        for title in row['Profiles']:
            objects = [x for x in profiles
                       if title in (x.getProfileKey(), x.UID(), x.Title())]
            for obj in objects:
                newprofiles.append(obj.UID())
        row['Profiles'] = newprofiles

        # BBB in bika.lims < 3.1.9, only one profile is permitted
        # on an AR.  The services are all added, but only first selected
        # profile name is stored.
        row['Profile'] = newprofiles[0] if newprofiles else None

        # Same for analyses
        (analyses, errors) = get_row_services(row)
        if errors:
            for err in errors:
                self.error(err)
        newanalyses = set(analyses)
        (analyses, errors) = get_row_profile_services(row)
        if errors:
            for err in errors:
                self.error(err)
        newanalyses.update(analyses)
        row['Analyses'] = []
        # get batch
        if batch:
            row['Batch'] = batch
        # Add AR fields from schema into this row's data
        row['ClientReference'] = row['ClientReference']
        row['ClientOrderNumber'] = self.getClientOrderNumber()
        row['Contact'] = self.getContact()
        row['DateSampled'] = convert_date_string(row['DateSampled'])
        if row['Sampler']:
            row['Sampler'] = lookup_sampler_uid(row['Sampler'])

        # Create AR
        ar = _createObjectByType("AnalysisRequest", client, tmpID())
        ar.setSample(sample)
        ar.unmarkCreationFlag()
        ar.edit(**row)
        ar._renameAfterCreation()
        ar.setAnalyses(list(newanalyses))
        for analysis in ar.getAnalyses(full_objects=True):
            analysis.setSamplePartition(part)
        ar.at_post_create_script()
        if swe:
            workflow.doActionFor(ar, 'sampling_workflow')
        else:
            workflow.doActionFor(ar, 'no_sampling_workflow')
    # document has been written to, and redirect() fails here
    self.REQUEST.response.write(
        '<script>document.location.href="%s"</script>' % (
            self.aq_parent.absolute_url()))

def get_sample_values(self):
    """Read the rows specifying Samples and return a dictionary with
    related data.

    keys are:
        headers - row with "Samples" in column 0.  These headers are
           used as dictionary keys in the rows below.
        prices - Row with "Analysis Price" in column 0.
        total_analyses - Row with "Total analyses" in colmn 0
        price_totals - Row with "Total price excl Tax" in column 0
        samples - All other sample rows.

    """
    res = {'samples': []}
    lines = self.getOriginalFile().data.splitlines()
    reader = csv.reader(lines)
    next_rows_are_sample_rows = False
    for row in reader:
        if not any(row):
            continue

        if next_rows_are_sample_rows:
            vals = [x.strip() for x in row]
            if not any(vals):
                continue
            res['samples'].append(zip(res['headers'], vals))
        elif row[0].strip().lower() == 'samples':
            res['headers'] = [x.strip() for x in row]
            next_rows_are_sample_rows = True
    return res

