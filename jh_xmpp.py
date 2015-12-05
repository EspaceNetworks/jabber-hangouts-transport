import time
import logging
import threading
from multiprocessing import Queue, Lock
import queue
import urllib.request
import base64

import config
import xmpp
import xmpp.client
import xmpp.protocol
from xmpp.browser import Browser
from xmpp.protocol import Presence, Message, Error, Iq, NodeProcessed, JID
from xmpp.protocol import NS_REGISTER, NS_PRESENCE, NS_VERSION, NS_COMMANDS, NS_DISCO_INFO, NS_CHATSTATES, NS_ROSTERX, NS_VCARD, NS_AVATAR
from xmpp.simplexml import Node
import jh_hangups

NODE_ROSTER = 'roster'
NODE_VCARDUPDATE='vcard-temp:x:update x'

xmpp_queue = Queue()
xmpp_lock = Lock()
userfile = None

_log = logging.getLogger(__name__)

class Transport:
    online = 1
    userlist = {}
    discoresults = {}

    def __init__(self, jabber, userfile):
        self.jabber = jabber
        self.userfile = userfile
        self.disco = None

    def xmpp_connect(self):
        connected = self.jabber.connect((config.mainServer, config.port))
        if config.dumpProtocol:
            _log.info("connected: %r", connected)
        while not connected:
            time.sleep(5)
            connected = self.jabber.connect((config.mainServer, config.port))
            if config.dumpProtocol:
                _log.info("connected: %r", connected)
        self.register_handlers()
        if config.dumpProtocol:
            _log.info("trying auth")
        connected = self.jabber.auth(config.saslUsername, config.secret)
        if config.dumpProtocol:
            _log.info("auth return: %r", connected)
        return connected

    def register_handlers(self):
        self.jabber.RegisterHandler('presence', self.xmpp_presence)
        self.jabber.RegisterHandler('message', self.xmpp_message)
        self.jabber.RegisterHandler('iq', self.xmpp_iq_discoinfo_results, typ='result', ns=NS_DISCO_INFO)
        self.jabber.RegisterHandler('iq', self.xmpp_iq_register_get, typ='get', ns=NS_REGISTER)
        self.jabber.RegisterHandler('iq', self.xmpp_iq_register_set, typ='set', ns=NS_REGISTER)
        self.jabber.RegisterHandler('iq',self.xmpp_iq_vcard, typ = 'get', ns=NS_VCARD)

        self.disco = Browser()
        self.disco.PlugIn(self.jabber)
        self.disco.setDiscoHandler(self.xmpp_base_disco, node='', jid=config.jid)

    # Disco Handlers
    def xmpp_base_disco(self, con, event, ev_type):
        print("DICOOOO")
        fromjid = event.getFrom().__str__()
        fromstripped = event.getFrom().getStripped()
        to = event.getTo()
        node = event.getQuerynode()
        # Type is either 'info' or 'items'
        if to == config.jid:
            if node is None:
                if ev_type == 'info':
                    return dict(
                        ids=[dict(category='gateway', type='hangouts', name=config.discoName)],
                        features=[NS_VERSION, NS_COMMANDS, NS_PRESENCE, NS_REGISTER, NS_CHATSTATES])
                if ev_type == 'items':
                    alist = [
                        {'node': NODE_ROSTER, 'name': config.discoName + ' Roster', 'jid': config.jid}
                    ]
                    return alist
            elif node == NODE_ROSTER:
                if ev_type == 'info':
                    return {'ids': [], 'features': []}
                if ev_type == 'items':
                    alist = []
                    if fromstripped in self.userlist:
                        for user in self.userlist[fromstripped]['user_list']:
                            alist.append({'jid':'%s@%s' %(user, config.jid),
                                          'name': self.userlist[fromstripped]['user_list'][user]['full_name']})
                    return alist
            else:
                self.jabber.send(Error(event, xmpp.protocol.ERRS['ERR_ITEM_NOT_FOUND']))
                raise NodeProcessed
        elif to.getDomain() == config.jid:
            if fromstripped in self.userlist:
                gaia_id = event.getTo().getNode()
                if type == 'info':
                    if gaia_id in self.userlist[fromstripped]['user_list']:
                        features = [NS_VCARD,NS_VERSION,NS_CHATSTATES]
                        return {'ids':[{'category': 'client',
                                        'type':'hangouts',
                                        'name': self.userlist[fromstripped]['user_list'][gaia_id]['full_name']}],
                                'features':features}
                    else:
                        self.jabber.send(Error(event, xmpp.protocol.ERRS['ERR_NOT_ACCEPTABLE']))
                if type == 'items':
                    if gaia_id in self.userlist[fromstripped]['user_list']:
                        return []
            else:
                self.jabber.send(Error(event, xmpp.protocol.ERRS['ERR_NOT_ACCEPTABLE']))
        else:
            self.jabber.send(Error(event, xmpp.protocol.ERRS['MALFORMED_JID']))
            raise NodeProcessed

    # XMPP Handlers
    def xmpp_presence(self, con, event):
        hobj = None
        fromjid = event.getFrom()
        fromstripped = fromjid.getStripped()
        if fromstripped in userfile:
            if event.getTo().getDomain() == config.jid:
                if event.getType() == 'subscribed':
                    if fromstripped in self.userlist:
                        hobj = self.userlist[fromstripped]
                        if event.getTo() == config.jid:
                            conf = userfile[fromstripped]
                            conf['subscribed'] = True
                            userfile[fromstripped] = conf
                            userfile.sync()

                            # For each new user check if rosterx is adversited then do the rosterx message, else send a
                            # truckload of subscribes.
                            # Part 1, parse the features out of the disco result
                            features = []
                            if event.getFrom().getStripped() in self.discoresults:
                                discoresult = self.discoresults[event.getFrom().getStripped().encode('utf8')]
                                if discoresult.getTag('query').getTag('feature'):
                                    features.append(discoresult.getTag('query').getAttr('var'))
                            # Part 2, make the rosterX message
                            if NS_ROSTERX in features:
                                m = Message(to=fromjid, frm=config.jid, subject='Yahoo Roster Items',
                                            body='Items from Yahoo Roster')
                                p = m.setTag('x', namespace=NS_ROSTERX)
                                for user in hobj['user_list']:
                                    p.addChild(
                                        name='item',
                                        attrs={'jid': '%s@%s' % (user, config.jid),
                                               'name': hobj['user_list']['full_name'],
                                               'action': 'add'})
                                self.jabber.send(m)
                                if config.dumpProtocol:
                                    print(m)
                            else:
                                for user in hobj['user_list']:
                                    self.jabber.send(Presence(frm='%s@%s' % (user, config.jid),
                                                              to=fromjid,
                                                              typ='subscribe'))
                            m = Presence(to=fromjid, frm=config.jid)
                            self.jabber.send(m)
                    else:
                        self.jabber.send(Error(event, xmpp.protocol.ERRS['ERR_NOT_ACCEPTABLE']))

                elif event.getType() == 'subscribe':
                    if fromstripped in self.userlist:
                        hobj = self.userlist[fromstripped]
                        if event.getTo() == config.jid:
                            conf = userfile[fromstripped]
                            conf['usubscribed'] = True
                            userfile[fromstripped] = conf
                            userfile.sync()
                            m = Presence(to=fromjid, frm=config.jid, typ='subscribed')
                            self.jabber.send(m)
                        else:
                            # add new user case.
                            if event.getStatus() is not None:
                                if config.dumpProtocol:
                                    print(event.getStatus().encode('utf-8'))
                                status = event.getStatus()
                            else:
                                status = ''
                            self.jabber.send(Presence(frm=event.getTo(), to=event.getFrom(), typ='subscribed'))
                    else:
                        self.jabber.send(Error(event, xmpp.protocol.ERRS['ERR_NOT_ACCEPTABLE']))
                elif event.getType() == 'unsubscribed':
                    # should do something more elegant here
                    pass
                elif event.getType() is None or event.getType() == 'available' or event.getType() == 'invisible':
                    if fromstripped in self.userlist:
                        self.xmpp_presence_do_update(event, fromstripped)
                    else:
                        try:
                            conf = userfile[fromstripped]
                        except:
                            self.jabber.send(Message(to=fromstripped,
                                                     subject='Transport Configuration Error',
                                                     body='The transport has found that your configuration could not be loaded. Please re-register with the transport'))
                            del userfile[fromstripped]
                            userfile.sync()
                            return
                        jh_hangups.hangups_manager.spawn_thread(fromstripped, xmpp_queue)
                        hobj = {'user_list': {}}
                        self.userlist[fromstripped] = hobj
                        self.jabber.send(Presence(frm=event.getTo(), to=event.getFrom()))
                elif event.getType() == 'unavailable':
                    # Match resources and remove the newly unavailable one
                    if fromstripped in self.userlist:
                        jh_hangups.hangups_manager.send_message(fromstripped, {'what': 'disconnect'})
                        hobj = self.userlist[fromstripped]
                        del self.userlist[fromstripped]
                        del hobj
                    else:
                        self.jabber.send(Presence(to=fromjid, frm=config.jid, typ='unavailable'))
        else:
            # Need to add auto-unsubscribe on probe events here.
            if event.getType() == 'probe':
                self.jabber.send(Presence(to=event.getFrom(), frm=event.getTo(), typ='unsubscribe'))
                self.jabber.send(Presence(to=event.getFrom(), frm=event.getTo(), typ='unsubscribed'))
            elif event.getType() == 'unsubscribed':
                pass
            elif event.getType() == 'unsubscribe':
                self.jabber.send(Presence(frm=event.getTo(), to=event.getFrom(), typ='unsubscribed'))
            else:
                self.jabber.send(Error(event, xmpp.protocol.ERRS['ERR_REGISTRATION_REQUIRED']))

    def xmpp_presence_do_update(self, event, fromstripped):
        jh_hangups.hangups_manager.send_message(fromstripped, {'what': 'set_presence', 'type': event.getType(), 'show': event.getShow()})

    def xmpp_message(self, con, event):
        ev_type = event.getType()
        from_jid = event.getFrom()
        to_jid = event.getTo()
        fromstripped = from_jid.getStripped()
        print("Sending from ", from_jid, " to ", to_jid, ": ", event.getBody())
        if event.getTo().getNode() is not None:
            if fromstripped in self.userlist:
                hobject = self.userlist[fromstripped]
                if event.getTo().getDomain() == config.jid:
                    if event.getBody() is None:
                        state='paused'
                        if event.getTag('composing', namespace=NS_CHATSTATES):
                            state='started'
                        jh_hangups.hangups_manager.send_message(fromstripped, {'what': 'typing_notification',
                                                                    'type': 'one_to_one',
                                                                    'gaia_id': event.getTo().getNode(),
                                                                    'state': state})
                        return

                    resource = 'messenger'
                    if resource == 'messenger':
                        if event.getType() is None or event.getType() == 'normal':
                            print("Send!")
                        elif event.getType() == 'chat':
                            jh_hangups.hangups_manager.send_message(fromstripped, {'what': 'chat_message',
                                                                        'type': 'one_to_one',
                                                                        'gaia_id': event.getTo().getNode(),
                                                                        'message': event.getBody()})
                        else:
                            self.jabber.send(Error(event, xmpp.protocol.ERRS['ERR_BAD_REQUEST']))
            else:
                if config.dumpProtocol: print('no item error')
                self.jabber.send(Error(event, xmpp.protocol.ERRS['ERR_REGISTRATION_REQUIRED']))
        else:
            self.jabber.send(Error(event, xmpp.protocol.ERRS['ERR_BAD_REQUEST']))

    def xmpp_iq_discoinfo_results(self, con, event):
        self.discoresults[event.getFrom().getStripped().encode('utf8')] = event
        raise NodeProcessed

    def xmpp_iq_vcard(self, con, event):
        fromjid = event.getFrom()
        fromstripped = fromjid.getStripped()
        if fromstripped in userfile:
            if event.getTo() == config.jid:
                m = Iq(to=event.getFrom(), frm=event.getTo(), typ='result')
                m.setID(event.getID())
                v = m.addChild(name='vCard', namespace=NS_VCARD)
                v.setTagData(tag='FN', val='Hangouts Transport')
                v.setTagData(tag='NICKNAME', val='Hangouts Transport')
                self.jabber.send(m)
            elif event.getTo().getDomain() == config.jid:
                nick = "Hangout User"
                gaia_id = event.getTo().getNode()
                if fromstripped in self.userlist:
                    if gaia_id in self.userlist[fromstripped]['user_list']:
                        nick = self.userlist[fromstripped]['user_list'][gaia_id]['full_name']

                m = Iq(to=event.getFrom(), frm=event.getTo(), typ='result')
                m.setID(event.getID())
                v = m.addChild(name='vCard', namespace=NS_VCARD)
                v.setTagData(tag='FN', val=nick)
                v.setTagData(tag='NICKNAME', val=nick)
                if fromstripped in self.userlist:
                    if self.userlist[fromstripped]['user_list'][gaia_id]['photo_url'] != '':
                        p = v.addChild(name='PHOTO')
                        p.setTagData(tag='TYPE', val='image/jpeg')
                        photo = download_url(self.userlist[fromstripped]['user_list'][gaia_id]['photo_url'])
                        p.setTagData(tag='BINVAL',
                                     val=base64.b64encode(photo).decode())
                    if len(self.userlist[fromstripped]['user_list'][gaia_id]['phones']) > 0:
                        p = v.addChild(name='TEL')
                        p.addChild(name='HOME')
                        p.addChild(name='VOICE')
                        p.addChild(name='NUMBER', payload=self.userlist[fromstripped]['user_list'][gaia_id]['phones'][0])
                    if len(self.userlist[fromstripped]['user_list'][gaia_id]['emails']) > 0:
                        p = v.addChild(name='EMAIL')
                        p.addChild(name='INTERNET')
                        p.addChild(name='USERID', payload=self.userlist[fromstripped]['user_list'][gaia_id]['emails'][0])
                self.jabber.send(m)
            else:
                self.jabber.send(Error(event, xmpp.protocol.ERRS['ERR_ITEM_NOT_FOUND']))
                raise NodeProcessed
        else:
            self.jabber.send(Error(event, xmpp.protocol.ERRS['ERR_ITEM_NOT_FOUND']))
        raise NodeProcessed

    def xmpp_iq_register_get(self, con, event):
        if event.getTo() == config.jid:
            url = ["http://example.com/?oauth=sdfsdfsdfsdfsdf"]
            auth_token = []
            fromjid = event.getFrom().getStripped()
            query_payload = [Node('instructions',
                                 payload='Please open this URL in a webbrowser and copy the result code here:')]
            if fromjid in self.userfile:
                try:
                    url = userfile[fromjid]['url']
                    auth_token = userfile[fromjid]['auth_token']
                except:
                    pass
                query_payload += [
                    Node('url', payload=url),
                    Node('password', payload=auth_token),
                    Node('registered')]
            else:
                query_payload += [
                    Node('url', payload=url),
                    Node('password')]
            m = event.buildReply('result')
            m.setQueryNS(NS_REGISTER)
            m.setQueryPayload(query_payload)
            self.jabber.send(m)
            # Add disco#info check to client requesting for rosterx support
            i = Iq(to=event.getFrom(), frm=config.jid, typ='get', queryNS=NS_DISCO_INFO)
            self.jabber.send(i)
        else:
            self.jabber.send(Error(event, xmpp.protocol.ERRS['ERR_BAD_REQUEST']))
        raise NodeProcessed

    def xmpp_iq_register_set(self, con, event):
        if event.getTo() == config.jid:
            remove = False
            url = False
            auth_token = False
            fromjid = event.getFrom().getStripped()
            query = event.getTag('query')
            if query.getTag('url'):
                url = query.getTagData('url')
            if query.getTag('password'):
                auth_token = query.getTagData('password')
            if query.getTag('remove'):
                remove = True
            if not remove and url and auth_token:
                if fromjid in self.userfile:
                    conf = self.userfile[fromjid]
                else:
                    conf = {}
                conf['url'] = url
                conf['auth_token'] = auth_token
                print('Conf: ', conf)
                self.userfile[fromjid] = conf
                self.userfile.sync()
                m = event.buildReply('result')
                self.jabber.send(m)
                self.userlist[fromjid] = {}
                # TODO: Connect Hangouts here
            elif remove and not url and not auth_token:
                if fromjid in self.userlist:
                    del self.userlist[fromjid]
                if fromjid in self.userfile:
                    del self.userfile[fromjid]
                    self.userfile.sync()
                    m = event.buildReply('result')
                    self.jabber.send(m)
                    m = Presence(to=event.getFrom(), frm=config.jid, typ='unsubscribe')
                    self.jabber.send(m)
                    m = Presence(to=event.getFrom(), frm=config.jid, typ='unsubscribed')
                    self.jabber.send(m)
                else:
                    self.jabber.send(Error(event, xmpp.protocol.ERRS['ERR_BAD_REQUEST']))
            else:
                self.jabber.send(Error(event, xmpp.protocol.ERRS['ERR_BAD_REQUEST']))
        else:
            self.jabber.send(Error(event, xmpp.protocol.ERRS['ERR_BAD_REQUEST']))
        raise NodeProcessed

    def xmpp_disconnect(self):
        for jid in list(self.userlist.keys()):
            self.jabber.send(Presence(frm=config.jid, to=jid, typ="unavailable"))
            jh_hangups.hangups_manager.send_message(jid, {'what': 'disconnect'})
            jh_hangups.hangups_manager.remove_thread(jid)
            for user in self.userlist[jid]['user_list']:
                self.jabber.send(Presence(frm='%s@%s'%(user, config.jid),
                                          to=jid,
                                          typ="unavailable"))
            hobj = self.userlist[jid]
            del self.userlist[jid]
            del hobj
        time.sleep(5)
        if not self.jabber.reconnectAndReauth():
            time.sleep(5)
            self.xmpp_connect()

    def send_presence(self, fromjid, jid, typ=None, show=None):
        self.jabber.send(Presence(frm=jid, to=fromjid, typ=typ, show=show))

    def send_presence_from_status(self, fromjid, jid, status='online'):
        if status == 'away':
            self.send_presence(fromjid, jid, show='xa')
        elif status == 'online':
            self.send_presence(fromjid, jid)
        elif status == 'offline':
            self.send_presence(fromjid, jid, typ='unavailable')

    def handle_message(self, message):
        print("Handling message from hangouts: ", message)

        fromjid = message['jid']
        if not fromjid in self.userlist:
            return

        if message['what'] == 'user_list':
            hobj = self.userlist[fromjid]
            hobj['user_list'] = message['user_list']
            for user_id in message['user_list']:
                user = message['user_list'][user_id]
                p = Presence(frm='%s@%s'%(user['gaia_id'], config.jid),
                                          to=fromjid,
                                          typ='subscribe',
                                          status='Hangouts contact')
                p.addChild(node=Node(NODE_VCARDUPDATE, payload=[Node('nickname', payload=user['full_name'])]))
                self.jabber.send(p)
                self.send_presence_from_status(fromjid, '%s@%s'%(user['gaia_id'], config.jid), user['status'])
        elif message['what'] == 'presence':
            self.send_presence_from_status(fromjid, '%s@%s'%(message['gaia_id'], config.jid), message['status'])
        elif message['what'] == 'chat_message':
            if message['type'] == 'one_to_one':
                m = Message(typ='chat',
                            frm='%s@%s' % (message['gaia_id'], config.jid),
                            to=JID(fromjid),
                            body=message['message'])
                m.setTag('active', namespace=NS_CHATSTATES)
                self.jabber.send(m)
        elif message['what'] == 'typing_notification':
            if message['type'] == 'one_to_one':
                m = Message(typ='chat',
                            frm='%s@%s' % (message['gaia_id'], config.jid),
                            to=JID(fromjid))
                if message['state'] == 'started':
                    m.setTag('composing', namespace=NS_CHATSTATES)
                else:
                    m.setTag('paused', namespace=NS_CHATSTATES)
                self.jabber.send(m)
        else:
            jh_hangups.hangups_manager.send_message(message['jid'], {'what': 'test'})

class XMPPQueueThread(threading.Thread):
    def __init__(self, transport):
        super().__init__()
        self.transport = transport

    def run(self):
        while self.transport.online == 1:
            try:
                message = xmpp_queue.get(True, 0.01)
            except queue.Empty:
                continue

            xmpp_lock.acquire()
            try:
                self.transport.handle_message(message)
            finally:
                xmpp_lock.release()
        print("Queue thread stopped")

def download_url(url):
    if not url.startswith('http'):
        url = 'http:' + url
    response = urllib.request.urlopen(url)
    data = response.read()
    return data
