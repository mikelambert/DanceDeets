import datetime

from google.appengine.ext import db
from mapreduce import control
from mapreduce import model
from mapreduce import operation as op

from events import cities
from events import tags
from events import users
import locations

EVENT_RANKING = 'EVENT_RANKING'
USER_RANKING = 'USER_RANKING'

# location is a city in cities.py
# time_period is one of ALL_TIME, LAST_MONTH, LAST_WEEK
# dance_style is one of tags.CHOREO_EVENT, tags.FREESTYLE_EVENT, None

LAST_WEEK = 'LAST_WEEK'
LAST_MONTH = 'LAST_MONTH'
ALL_TIME = 'ALL_TIME'

TIME_PERIODS = [
    ALL_TIME,
    LAST_MONTH,
    LAST_WEEK,
]

FREESTYLE_STYLE = tags.FREESTYLE_EVENT
CHOREO_STYLE = tags.CHOREO_EVENT
ANY_STYLE = 'ANY_STYLE'

STYLES = [
    ANY_STYLE,
    FREESTYLE_STYLE,
    CHOREO_STYLE,
]

CHOREO_FAN = users.CHOREO_FAN
CHOREO_DANCER = users.CHOREO_DANCER
FREESTYLE_FAN = users.FREESTYLE_FAN
FREESTYLE_DANCER = users.FREESTYLE_DANCER
DANCE_FAN = 'DANCE_FAN'
DANCE_DANCER = 'DANCE_DANCER'

PEOPLES = [
    DANCE_FAN,
    DANCE_DANCER,
    CHOREO_FAN,
    CHOREO_DANCER,
    FREESTYLE_FAN,
    FREESTYLE_DANCER,
]

FANS = [
    CHOREO_FAN,
    FREESTYLE_FAN,
    DANCE_FAN,
]

DANCERS = [
    CHOREO_DANCER,
    FREESTYLE_DANCER,
    DANCE_DANCER,
]

string_translations = {
    ALL_TIME: 'all time',
    LAST_MONTH: 'last month',
    LAST_WEEK: 'last week',
    ANY_STYLE: 'all dancing',
    FREESTYLE_STYLE: 'freestyle dancing',
    CHOREO_STYLE: 'choreo dancing',
    DANCE_FAN: 'all dance fans',
    DANCE_DANCER: 'all dancers',
    CHOREO_FAN: 'choreo fans',
    CHOREO_DANCER: 'choreo dancers',
    FREESTYLE_FAN: 'freeestyle fans',
    FREESTYLE_DANCER: 'freestyle dancers',
}

def get_time_periods(timestamp):
    if timestamp > datetime.datetime.now() - datetime.timedelta(days=7):
        yield LAST_WEEK
    if timestamp > datetime.datetime.now() - datetime.timedelta(days=31):
        yield LAST_MONTH
    yield ALL_TIME

def get_user_dance_styles(user):
    if user.freestyle == users.FREESTYLE_FAN:
        yield FREESTYLE_FAN
    elif user.freestyle == users.FREESTYLE_DANCER:
        yield FREESTYLE_FAN
        yield FREESTYLE_DANCER

    if user.choreo == users.CHOREO_FAN:
        yield CHOREO_FAN
    elif user.choreo == users.CHOREO_DANCER:
        yield CHOREO_FAN
        yield CHOREO_DANCER

    if user.choreo != users.CHOREO_APATHY or user.freestyle != users.FREESTYLE_APATHY:
        yield DANCE_FAN
        if user.choreo == users.CHOREO_DANCER or user.freestyle == users.FREESTYLE_DANCER:
            yield DANCE_DANCER

def get_event_dance_styles(dbevent):
    if tags.FREESTYLE_EVENT in dbevent.search_tags:
        yield FREESTYLE_STYLE
    if tags.CHOREO_EVENT in dbevent.search_tags:
        yield CHOREO_STYLE
    yield ANY_STYLE

def count_event(dbevent):
    #TODO(lambert): store largest_city in the event
    if not dbevent.start_time: # deleted event, don't count
        return
    region = cities.get_largest_nearby_city_name(dbevent.address)
    for time_period in get_time_periods(dbevent.creation_time or dbevent.start_time):
        for dance_style in get_event_dance_styles(dbevent):
            yield op.counters.Increment("%s/%s/%s" % (region, time_period, dance_style))

def count_user(user):
    #TODO(lambert): store largest_city in the user
    user_city = cities.get_largest_nearby_city_name(user.location)
    for time_period in get_time_periods(user.creation_time):
        for dance_style in get_user_dance_styles(user):
            yield op.counters.Increment("%s/%s/%s" % (user_city, time_period, dance_style))

def begin_ranking_calculations():
    control.start_map(
        name='Compute Event Rankings',
        reader_spec='mapreduce.input_readers.DatastoreInputReader',
        handler_spec='logic.rankings.count_event',
        reader_parameters={'entity_kind': 'events.eventdata.DBEvent'},
        _app=EVENT_RANKING,
    )
    control.start_map(
        name='Compute User Rankings',
        reader_spec='mapreduce.input_readers.DatastoreInputReader',
        handler_spec='logic.rankings.count_user',
        reader_parameters={'entity_kind': 'events.users.User'},
        _app=USER_RANKING,
    )

def get_event_rankings():
    mapreduce_states = model.MapreduceState.gql('WHERE result_status = :result_status AND app_id = :app_id ORDER BY start_time DESC', result_status='success', app_id=EVENT_RANKING).fetch(1)
    if not mapreduce_states:
        return None
    final_counter_map = mapreduce_states[0].counters_map.counters
    cities = {}
    for k, counter in final_counter_map.iteritems():
        if k.count('/') != 2:
            continue
        city, time_period, dance_style = k.split('/')
        cities.setdefault(city, {}).setdefault(time_period, {})[dance_style] = counter
    return cities

def get_user_rankings():
    mapreduce_states = model.MapreduceState.gql('WHERE result_status = :result_status AND app_id = :app_id ORDER BY start_time DESC', result_status='success', app_id=USER_RANKING).fetch(1)
    if not mapreduce_states:
        return None
    final_counter_map = mapreduce_states[0].counters_map.counters
    cities = {}
    for k, counter in final_counter_map.iteritems():
        if k.count('/') != 2:
            continue
        city, time_period, dance_style = k.split('/')
        cities.setdefault(city, {}).setdefault(time_period, {})[dance_style] = counter
    return cities

def compute_sum(all_rankings, toplevel, time_period):
    total_count = 0
    for style in toplevel:
        for city, times_styles in all_rankings.iteritems():
            count = times_styles.get(time_period, {}).get(style, 0)
            total_count += count
    return total_count

def compute_template_rankings(all_rankings, toplevel, time_period, use_url=True):
    style_rankings = []
    for style in toplevel:
        city_ranking = []
        for city, times_styles in all_rankings.iteritems():
            if city == 'Unknown':
                continue
            count = times_styles.get(time_period, {}).get(style, 0)
            if count:
                freestyle = (style != tags.CHOREO_EVENT) and users.FREESTYLE_DANCER or users.FREESTYLE_APATHY
                choreo = (style != tags.FREESTYLE_EVENT) and users.CHOREO_DANCER or users.CHOREO_APATHY
                if use_url:
                    url = '/?user_location=%s&distance=100&distance_units=km&freestyle=%s&choreo=%s' % (city, freestyle, choreo)
                else:
                    url = None
                city_ranking.append(dict(city=city, count=count, url=url))
        city_ranking = sorted(city_ranking, key=lambda x: -x['count'])
        style_rankings.append(dict(style=style, ranking=city_ranking))
    return style_rankings

