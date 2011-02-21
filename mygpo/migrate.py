from datetime import datetime
from couchdbkit import Server, Document

from mygpo.core.models import Podcast, PodcastGroup, Episode, SubscriberData
from mygpo.users.models import Rating, EpisodeAction, User, Device, SubscriptionAction
from mygpo.log import log
from mygpo import utils
from mygpo.decorators import repeat_on_conflict

"""
This module contains methods for converting objects from the old
ORM-based backend to the CouchDB-based backend
"""


def save_podcast_signal(sender, instance=False, **kwargs):
    """
    Signal-handler for creating/updating a CouchDB-based podcast when
    an ORM-based podcast has been saved
    """
    if not instance:
        return

    try:
        newp = Podcast.for_oldid(instance.id)
        if newp:
            update_podcast(oldp=instance, newp=newp)
        else:
            create_podcast(instance)

    except Exception, e:
        log('error while updating CouchDB-Podcast: %s' % repr(e))


def delete_podcast_signal(sender, instance=False, **kwargs):
    """
    Signal-handler for deleting a CouchDB-based podcast when an ORM-based
    podcast is deleted
    """
    if not instance:
        return

    try:
        newp = Podcast.for_oldid(instance.id)
        if newp:
            newp.delete()

    except Exception, e:
        log('error while deleting CouchDB-Podcast: %s' % repr(e))


def save_episode_signal(sender, instance=False, **kwargs):
    """
    Signal-handler for creating/updating a CouchDB-based episode when
    an ORM-based episode has been saved
    """
    if not instance:
        return

    try:
        newe = Episode.for_oldid(instance.id)
        newp = Podcast.get(newe.podcast)

        if newe:
            update_episode(instance, newe, newp)
        else:
            create_episode(instance)

    except Exception, e:
        log('error while updating CouchDB Episode: %s' % repr(e))



@repeat_on_conflict(['oldp'])
def update_podcast(oldp, newp):
    """
    Updates newp based on oldp and returns True if an update was necessary
    """
    updated = False

    # Update related podcasts
    from mygpo.data.models import RelatedPodcast
    if newp._id:
        rel_podcast = set([r.rel_podcast for r in RelatedPodcast.objects.filter(ref_podcast=oldp)])
        rel = list(podcasts_to_ids(rel_podcast))
        if newp.related_podcasts != rel:
            newp.related_podcasts = rel
            updated = True

    # Update Group-assignment
    if oldp.group:
        group = get_group(oldp.group)
        if not newp in list(group.podcasts):
            newp = group.add_podcast(newp)
            updated = True

    # Update subscriber-data
    from mygpo.data.models import HistoricPodcastData
    sub = HistoricPodcastData.objects.filter(podcast=oldp).order_by('date')
    if sub.count() and len(newp.subscribers) != sub.count():
        transf = lambda s: SubscriberData(
            timestamp = datetime(s.date.year, s.date.month, s.date.day),
            subscriber_count = s.subscriber_count)
        check = lambda s: s.date.weekday() == 6

        newp.subscribers = newp.subscribers + map(transf, filter(check, sub))
        newp.subscribers = utils.set_cmp(newp.subscribers, lambda x: x.timestamp)
        newp.subscribers = list(sorted(set(newp.subscribers), key=lambda s: s.timestamp))
        updated = True

    PROPERTIES = ('language', 'content_types', 'title',
        'description', 'link', 'last_update', 'logo_url',
        'author', 'group_member_name')

    for p in PROPERTIES:
        if getattr(newp, p, None) != getattr(oldp, p, None):
            setattr(newp, p, getattr(oldp, p, None))
            updated = True

    if not oldp.url in newp.urls:
        newp.urls.append(oldp.url)
        updated = True

    if updated:
        newp.save()

    return updated


def create_podcast(oldp, sparse=False):
    """
    Creates a (CouchDB) Podcast document from a (ORM) Podcast object
    """
    p = Podcast()
    p.oldid = oldp.id
    p.save()
    if not sparse:
        update_podcast(oldp=oldp, newp=p)

    return p


def get_group(oldg):
    group = PodcastGroup.for_oldid(oldg.id)
    if not group:
        group = create_podcastgroup(oldg)

    return group


def create_podcastgroup(oldg):
    """
    Creates a (CouchDB) PodcastGroup document from a
    (ORM) PodcastGroup object
    """
    g = PodcastGroup()
    g.oldid = oldg.id
    update_podcastgroup(oldg, g)
    g.save()
    return g



@repeat_on_conflict(['newg'])
def update_podcastgroup(oldg, newg):

    if newg.title != oldg.title:
        newg.title = oldg.title
        newg.save()
        return True

    return False


def get_blacklist(blacklist):
    """
    Returns a list of Ids of all blacklisted podcasts
    """
    blacklisted = [b.podcast for b in blacklist]
    blacklist_ids = []
    for p in blacklisted:
        newp = Podcast.for_oldid(p.id)
        if not newp:
            newp = create_podcast(p)

        blacklist_ids.append(newp._id)
    return blacklist_ids


def get_ratings(ratings):
    """
    Returns a list of Rating-objects, based on the relational Ratings
    """
    conv = lambda r: Rating(rating=r.rating, timestamp=r.timestamp)
    return map(conv, ratings)


def podcasts_to_ids(podcasts):
    for p in podcasts:
        podcast = Podcast.for_oldid(p.id)
        if not podcast:
            podcast = create_podcast(p, sparse=True)
        yield podcast.get_id()


def get_or_migrate_podcast(oldp):
    return Podcast.for_oldid(oldp.id) or create_podcast(oldp)


def create_episode_action(action):
    a = EpisodeAction()
    a.action = action.action
    a.timestamp = action.timestamp
    a.device_oldid = action.device.id if action.device else None
    a.started = action.started
    a.playmark = action.playmark
    return a

def create_episode(olde, sparse=False):
    podcast = get_or_migrate_podcast(olde.podcast)
    e = Episode()
    e.oldid = olde.id
    e.urls.append(olde.url)
    e.podcast = podcast.get_id()

    if not sparse:
        update_episode(olde, e, podcast)

    e.save()

    return e


def get_or_migrate_episode(olde):
    return Episode.for_oldid(olde.id) or create_episode(olde)


def update_episode(olde, newe, podcast):
    updated = False

    if not olde.url in newe.urls:
        newe.urls.append(olde.url)
        updated = False

    PROPERTIES = ('title', 'description', 'link',
        'author', 'duration', 'filesize', 'language',
        'last_update', 'outdated')

    for p in PROPERTIES:
        if getattr(newe, p, None) != getattr(olde, p, None):
            setattr(newe, p, getattr(olde, p, None))
            updated = True


    if newe.released != olde.timestamp:
        newe.released = olde.timestamp
        updated = True

    if olde.mimetype and not olde.mimetype in newe.mimetypes:
        newe.mimetypes.append(olde.mimetype)
        updated = True

    @repeat_on_conflict(['newe'])
    def save(newe):
        newe.save()

    if updated:
        save(newe=newe)

    return updated


def get_or_migrate_user(user):
    u = User.for_oldid(user.id)
    if u:
        return u

    u = User()
    u.oldid = user.id
    u.username = user.username
    u.save()
    return u


def get_or_migrate_device(device, user=None):
    d = Device.for_user_uid(device.user, device.uid)
    if d:
        return d

    d = Device()
    d.oldid = device.id
    d.uid = device.uid
    d.name = device.name
    d.type = device.type
    d.deleted = device.deleted
    u = user or get_or_migrate_user(device.user)
    u.devices.append(d)
    u.save()
    return d


def migrate_subscription_action(old_action):
    action = SubscriptionAction()
    action.timestamp = old_action.timestamp
    action.action = 'subscribe' if old_action.action == 1 else 'unsubscribe'
    action.device = get_or_migrate_device(old_action.device).id
    return action