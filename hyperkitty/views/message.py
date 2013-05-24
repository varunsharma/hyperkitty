#-*- coding: utf-8 -*-
# Copyright (C) 1998-2012 by the Free Software Foundation, Inc.
#
# This file is part of HyperKitty.
#
# HyperKitty is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# HyperKitty is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along with
# HyperKitty.  If not, see <http://www.gnu.org/licenses/>.
#
# Author: Aamir Khan <syst3m.w0rm@gmail.com>
# Author: Aurelien Bompard <abompard@fedoraproject.org>
#

import urllib
import datetime
from collections import namedtuple

import django.utils.simplejson as json
from django.http import HttpResponse, Http404
from django.shortcuts import redirect, render
from django.core.urlresolvers import reverse
from django.core.exceptions import SuspiciousOperation
from django.template import RequestContext, loader
from django.contrib.auth.decorators import login_required

from hyperkitty.lib import get_store, get_months, post_to_list, PostingFailed
from hyperkitty.lib.voting import set_message_votes
from hyperkitty.models import Rating
from forms import ReplyForm, PostForm


def index(request, mlist_fqdn, message_id_hash):
    '''
    Displays a single message identified by its message_id_hash (derived from
    message_id)
    '''
    store = get_store(request)
    message = store.get_message_by_hash_from_list(mlist_fqdn, message_id_hash)
    if message is None:
        raise Http404
    message.sender_email = message.sender_email.strip()
    set_message_votes(message, request.user)
    mlist = store.get_list(mlist_fqdn)

    context = {
        'mlist' : mlist,
        'message': message,
        'message_id_hash' : message_id_hash,
        'months_list': get_months(store, mlist.name),
        'reply_form': ReplyForm(),
    }
    return render(request, "message.html", context)


def attachment(request, mlist_fqdn, message_id_hash, counter, filename):
    """
    Sends the numbered attachment for download. The filename is not used for
    lookup, but validated nonetheless for security reasons.
    """
    store = get_store(request)
    message = store.get_message_by_hash_from_list(mlist_fqdn, message_id_hash)
    if message is None:
        raise Http404
    attachment = store.get_attachment_by_counter(
            mlist_fqdn, message.message_id, int(counter))
    if attachment is None or attachment.name != filename:
        raise Http404
    # http://djangosnippets.org/snippets/1710/
    response = HttpResponse(attachment.content)
    response['Content-Type'] = attachment.content_type
    response['Content-Length'] = attachment.size
    if attachment.encoding is not None:
        response['Content-Encoding'] = attachment.encoding
    # Follow RFC2231, browser support is sufficient nowadays (2012-09)
    response['Content-Disposition'] = 'attachment; filename*=UTF-8\'\'%s' \
            % urllib.quote(attachment.name.encode('utf-8'))
    return response


def vote(request, mlist_fqdn, message_id_hash):
    """ Add a rating to a given message identified by messageid. """
    if request.method != 'POST':
        raise SuspiciousOperation

    if not request.user.is_authenticated():
        return HttpResponse('You must be logged in to vote',
                            content_type="text/plain", status=403)

    store = get_store(request)
    message = store.get_message_by_hash_from_list(mlist_fqdn, message_id_hash)
    if message is None:
        raise Http404

    value = int(request.POST['vote'])
    if value not in [-1, 0, 1]:
        raise SuspiciousOperation

    # Checks if the user has already voted for a this message.
    try:
        v = Rating.objects.get(user=request.user, messageid=message_id_hash,
                               list_address=mlist_fqdn)
        if v.vote == value:
            return HttpResponse("You've already cast this vote",
                                content_type="text/plain", status=403)
    except Rating.DoesNotExist:
        if value != 0:
            v = Rating(list_address=mlist_fqdn, messageid=message_id_hash,
                       vote=value)
            v.user = request.user
        else:
            return HttpResponse("There is no vote to cancel",
                                content_type="text/plain", status=500)

    if value == 0:
        v.delete()
    else:
        v.vote = value
        v.save()

    # Extract all the votes for this message to refresh it
    set_message_votes(message, request.user)
    FakeMList = namedtuple("MailingList", ["name"])
    t = loader.get_template('messages/like_form.html')
    html = t.render(RequestContext(request, {
            "object": message,
            "message_id_hash": message_id_hash,
            "mlist": FakeMList(name=mlist_fqdn)}
            ))

    result = { "like": message.likes, "dislike": message.dislikes,
               "html": html, }
    return HttpResponse(json.dumps(result),
                        mimetype='application/javascript')


@login_required
def reply(request, mlist_fqdn, message_id_hash):
    """ Sends a reply to the list.
    TODO: unit tests
    """
    if request.method != 'POST':
        raise SuspiciousOperation
    form = ReplyForm(request.POST)
    if not form.is_valid():
        return HttpResponse(form.errors.as_text(),
                            content_type="text/plain", status=400)
    store = get_store(request)
    mlist = store.get_list(mlist_fqdn)
    if form.cleaned_data["newthread"]:
        subject = form.cleaned_data["subject"]
        headers = {}
    else:
        message = store.get_message_by_hash_from_list(mlist.name, message_id_hash)
        subject = message.subject
        if not message.subject.lower().startswith("re:"):
            subject = "Re: %s" % subject
        headers = {"In-Reply-To": "<%s>" % message.message_id,
                   "References": "<%s>" % message.message_id, }
    try:
        post_to_list(request, mlist, subject, form.cleaned_data["message"], headers)
    except PostingFailed, e:
        return HttpResponse(str(e), content_type="text/plain", status=500)

    reply = {
        "sender_name": "%s %s" % (request.user.first_name,
                                  request.user.last_name),
        "sender_email": request.user.email,
        "content": form.cleaned_data["message"],
        "level": message.thread_depth, # no need to increment, level = thread_depth - 1
    }
    t = loader.get_template('ajax/temp_message.html')
    html = t.render(RequestContext(request, { 'email': reply }))
    result = {"result": "The reply has been sent successfully.",
              "message_html": html}
    return HttpResponse(json.dumps(result),
                        mimetype="application/javascript")


@login_required
def new_message(request, mlist_fqdn):
    """ Sends a new thread-starting message to the list.
    TODO: unit tests
    """
    store = get_store(request)
    mlist = store.get_list(mlist_fqdn)
    failure = None
    if request.method == 'POST':
        form = PostForm(request.POST)
        if form.is_valid():
            today = datetime.date.today()
            redirect_url = reverse(
                    'archives_with_month', kwargs={
                        "mlist_fqdn": mlist_fqdn,
                        'year': today.year,
                        'month': today.month})
            redirect_url += "?msg=sent-ok"
            try:
                post_to_list(request, mlist, form.cleaned_data['subject'],
                             form.cleaned_data["message"])
            except PostingFailed, e:
                failure = str(e)
            else:
                return redirect(redirect_url)
    else:
        form = PostForm()
    context = {
        "mlist": mlist,
        "post_form": form,
        "failure": failure,
        'months_list': get_months(store, mlist.name),
    }
    return render(request, "message_new.html", context)
