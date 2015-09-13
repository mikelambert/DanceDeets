import logging

from google.appengine.ext import deferred
from mapreduce import context

import base_servlet
import fb_api
from util import fb_mapreduce
from util import timings
from users import users
from . import eventdata
from . import event_updates

def add_event_tuple_if_updating(events_to_update, fbl, db_event, only_if_updated):
    fb_event = fbl.fetched_data(fb_api.LookupEvent, db_event.fb_event_id, only_if_updated=only_if_updated)
    # This happens when an event moves from TIME_FUTURE into TIME_PAST
    if event_updates.need_forced_update(db_event):
        fb_event = fbl.fetched_data(fb_api.LookupEvent, db_event.fb_event_id)
    # If we have an event in need of updating, record that
    if fb_event:
        events_to_update.append((db_event, fb_event))

def load_fb_events_using_backup_tokens(event_ids, only_if_updated, update_geodata):
    db_events = eventdata.DBEvent.get_by_ids(event_ids)
    events_to_update = []
    for db_event in db_events:
        processed = False
        logging.info("Looking for event id %s with user ids %s", db_event.fb_event_id, db_event.visible_to_fb_uids)
        for user in users.User.get_by_ids(db_event.visible_to_fb_uids):
            fbl = fb_api.FBLookup(user.fb_uid, user.fb_access_token)
            real_fb_event = fbl.get(fb_api.LookupEvent, db_event.fb_event_id)
            if real_fb_event['empty'] != fb_api.EMPTY_CAUSE_INSUFFICIENT_PERMISSIONS:
                add_event_tuple_if_updating(events_to_update, fbl, db_event, only_if_updated)
                processed = True
        # If we didn't process, it means none of our access_tokens are valid.
        if not processed:
            # Now mark our event as lacking in valid access_tokens, so that our pipeline can pick it up and look for a new one
            db_event.visible_to_fb_uids = []
            db_event.put()
            # Let's update the DBEvent as necessary (note, this uses the last-updated FBLookup)
            add_event_tuple_if_updating(events_to_update, fbl, db_event, only_if_updated)
    event_updates.update_and_save_events(events_to_update, update_geodata=update_geodata)


@timings.timed
def yield_load_fb_event(fbl, db_events):
    logging.info("loading db events %s", [db_event.fb_event_id for db_event in db_events])
    fbl.request_multi(fb_api.LookupEvent, [x.fb_event_id for x in db_events])
    #fbl.request_multi(fb_api.LookupEventPageComments, [x.fb_event_id for x in db_events])
    fbl.batch_fetch()
    events_to_update = []
    ctx = context.get()
    if ctx:
        params = ctx.mapreduce_spec.mapper.params
        update_geodata = params['update_geodata']
        only_if_updated = params['only_if_updated']
    else:
        update_geodata = True
        only_if_updated = True
    empty_fb_event_ids = []
    for db_event in db_events:
        try:
            real_fb_event = fbl.fetched_data(fb_api.LookupEvent, db_event.fb_event_id)
            if real_fb_event['empty'] != fb_api.EMPTY_CAUSE_INSUFFICIENT_PERMISSIONS:
                empty_fb_event_ids.append(db_event.fb_event_id)
            else:
                add_event_tuple_if_updating(events_to_update, fbl, db_event, only_if_updated)
        except fb_api.NoFetchedDataException, e:
            logging.info("No data fetched for event id %s: %s", db_event.fb_event_id, e)
    # Now trigger off a background reloading of empty fb_events
    deferred.defer(load_fb_events_using_backup_tokens, empty_fb_event_ids, only_if_updated=only_if_updated, update_geodata=update_geodata)
    # And then re-save all the events in here
    event_updates.update_and_save_events(events_to_update, update_geodata=update_geodata)
map_load_fb_event = fb_mapreduce.mr_wrap(yield_load_fb_event)
load_fb_event = fb_mapreduce.nomr_wrap(yield_load_fb_event)


@timings.timed
def yield_load_fb_event_attending(fbl, db_events):
    fbl.get_multi(fb_api.LookupEventAttending, [x.fb_event_id for x in db_events])
map_load_fb_event_attending = fb_mapreduce.mr_wrap(yield_load_fb_event_attending)
load_fb_event_attending = fb_mapreduce.nomr_wrap(yield_load_fb_event_attending)

def mr_load_fb_events(fbl, time_period=None, update_geodata=True, only_if_updated=True, queue='slow-queue'):
    if time_period:
        filters = [('search_time_period', '=', time_period)]
        name = 'Load %s Events' % time_period
    else:
        filters = []
        name = 'Load All Events'
    fb_mapreduce.start_map(
        fbl=fbl,
        name=name,
        handler_spec='events.event_reloading_tasks.map_load_fb_event',
        entity_kind='events.eventdata.DBEvent',
        handle_batch_size=20,
        filters=filters,
        extra_mapper_params={'update_geodata': update_geodata, 'only_if_updated': only_if_updated},
        queue=queue,
    )

class LoadEventHandler(base_servlet.BaseTaskFacebookRequestHandler):
    def get(self):
        event_ids = [x for x in self.request.get('event_ids').split(',') if x]
        db_events = [x for x in eventdata.DBEvent.get_by_ids(event_ids) if x]
        load_fb_event(self.fbl, db_events)
    post=get

class LoadEventAttendingHandler(base_servlet.BaseTaskFacebookRequestHandler):
    def get(self):
        event_ids = [x for x in self.request.get('event_ids').split(',') if x]
        db_events = [x for x in eventdata.DBEvent.get_by_ids(event_ids) if x]
        load_fb_event_attending(self.fbl, db_events)
    post=get

class ReloadEventsHandler(base_servlet.BaseTaskFacebookRequestHandler):
    def get(self):
        update_geodata = self.request.get('update_geodata') != '0'
        only_if_updated = self.request.get('only_if_updated') != '0'
        time_period = self.request.get('time_period', None)
        mr_load_fb_events(self.fbl, time_period=time_period, update_geodata=update_geodata, only_if_updated=only_if_updated)
    post=get
