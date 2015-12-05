import asyncio
import sys
import threading

from hangups.auth import GoogleAuthError
import hangups.hangouts_pb2 as hangouts_pb2
import hangups

hangups_manager = None

def presence_to_status(presence):
    status = 'offline'
    if presence.reachable:
        status = 'away'
        if presence.available:
            status = 'online'
    return status

class HangupsManager:
    hangouts_threads = {}

    def spawn_thread(self, jid, xmpp_queue):
        thread = HangupsThread(jid, xmpp_queue)
        self.hangouts_threads[jid] = thread
        thread.start()

    def get_thread(self, jid):
        if not jid in self.hangouts_threads:
            return None
        return self.hangouts_threads[jid]

    def remove_thread(self, jid):
        if jid in self.hangouts_threads:
            del self.hangouts_threads[jid]

    def send_message(self, jid, message):
        thread = self.get_thread(jid)
        if thread is not None:
            thread.call_soon_thread_safe(message)

class HangupsThread(threading.Thread):
    def __init__(self, jid, xmpp_queue):
        super().__init__()

        self.jid = jid
        self.xmpp_queue = xmpp_queue

        try:
            self.cookies = hangups.auth.get_auth_stdin('refresh_token.txt')
        except hangups.GoogleAuthError as e:
            sys.exit('Login failed ({})'.format(e))

        self.conv_list = None
        self.user_list = None
        self.state = None
        self.show = None

    def run(self):
        policy = asyncio.get_event_loop_policy()
        self.loop = policy.new_event_loop()
        policy.set_event_loop(self.loop)

        self.client = hangups.Client(self.cookies)
        self.client.on_connect.add_observer(self.on_connect)

        self.set_state('disconnected')
        self.loop.run_until_complete(self.client.connect())
        print("Hangup thread stopped")

    def call_soon_thread_safe(self, message):
        self.loop.call_soon_threadsafe(asyncio.async, self.on_message(message))

    def send_message_to_xmpp(self, message):
        message['jid'] = self.jid
        self.xmpp_queue.put(message)

    def set_state(self, state):
        print("Setting state: ", state)
        self.state = state

    def set_presence(self, type, show):
        print("Setting presence: ", type, show)
        self.type = type
        self.show = show

    @asyncio.coroutine
    def chat_message(self, message):
        if message['message'] is None:
            return
        if message['type'] == 'one_to_one':
            conv = self.conv_list.get_one_to_one_with_user(message['gaia_id'])
            if conv:
                segments = hangups.ChatMessageSegment.from_str(message['message'])
                yield from conv.send_message(segments)

    @asyncio.coroutine
    def typing_notification(self, message):
        if message['type'] == 'one_to_one':
            conv = self.conv_list.get_one_to_one_with_user(message['gaia_id'])
            if conv:
                type = hangouts_pb2.TYPING_TYPE_PAUSED
                if message['state'] == 'started':
                    type = hangouts_pb2.TYPING_TYPE_STARTED
                yield from conv.set_typing(type)

    @asyncio.coroutine
    def on_message(self, message):
        print("Message to process in a corouting: ", message)
        if message['what'] == 'disconnect':
            self.set_state('disconnected')
            yield from self.client.disconnect()
            self.loop.stop()
        elif message['what'] == 'connect':
            self.set_state('connected')
        elif message['what'] == 'set_presence':
            self.set_presence(message['type'], message['show'])
        elif message['what'] == 'chat_message':
            yield from self.chat_message(message)
        elif message['what'] == 'typing_notification':
            yield from self.typing_notification(message)

    @asyncio.coroutine
    def on_connect(self):
        """Handle connecting for the first time."""

        # Get the list of users and conversations
        self.user_list, self.conv_list = (
            yield from hangups.build_user_conversation_list(self.client)
        )

        self.user_list.on_presence.add_observer(self.on_presence)
        self.conv_list.on_event.add_observer(self.on_event)
        self.conv_list.on_typing.add_observer(self.on_typing)

        # Query presence information for user list
        presence_request = hangouts_pb2.QueryPresenceRequest(
            request_header=self.client.get_request_header(),
            participant_id=[
                hangouts_pb2.ParticipantId(gaia_id=user_id.gaia_id,
                                           chat_id=user_id.chat_id) for user_id in self.user_list._user_dict.keys()],
            field_mask=[
                hangouts_pb2.FIELD_MASK_REACHABLE,
                hangouts_pb2.FIELD_MASK_AVAILABLE,
                hangouts_pb2.FIELD_MASK_DEVICE])
        presence_response = yield from self.client.query_presence(presence_request)
        for presence_result in presence_response.presence_result:
            self.user_list.set_presence_from_presence_result(presence_result)

        # Send user list to XMPP
        user_list_dict = {}
        for user in self.user_list.get_all():

            user_list_dict[user.id_.gaia_id] = {
                'chat_id': user.id_.chat_id,
                'gaia_id': user.id_.gaia_id,
                'first_name': user.first_name,
                'full_name': user.full_name,
                'is_self': user.is_self,
                'emails': user.emails._values,
                'phones': user.phones._values,
                'photo_url': user.photo_url,
                'status': presence_to_status(user.presence),
                'status_message': user.get_mood_message(),
            }
        self.send_message_to_xmpp({'what': 'user_list', 'user_list': user_list_dict})

    @asyncio.coroutine
    def on_presence(self, user, presence):
        self.send_message_to_xmpp({'what': 'presence',
                                   'gaia_id': user.id_.gaia_id,
                                   'status': presence_to_status(presence),
                                   'status_message': user.get_mood_message()})

    def on_event(self, conv_event):
        """Open conversation tab for new messages when they arrive."""
        conv = self.conv_list.get(conv_event.conversation_id)
        user = conv.get_user(conv_event.user_id)
        if isinstance(conv_event, hangups.ChatMessageEvent):
            if conv._conversation.type == hangouts_pb2.CONVERSATION_TYPE_ONE_TO_ONE:
                if not user.is_self:
                    self.send_message_to_xmpp({'what': 'chat_message',
                                               'type': 'one_to_one',
                                               'gaia_id': user.id_.gaia_id,
                                               'message': conv_event.text})

    def on_typing(self, typing_message):
        """Open conversation tab for new messages when they arrive."""
        conv = self.conv_list.get(typing_message.conv_id)
        user = conv.get_user(typing_message.user_id)
        if conv is not None and user is not None:
            typing_states = {
                hangouts_pb2.TYPING_TYPE_UNKNOWN: 'unknown',
                hangouts_pb2.TYPING_TYPE_STARTED: 'started',
                hangouts_pb2.TYPING_TYPE_PAUSED:  'paused',
                hangouts_pb2.TYPING_TYPE_STOPPED: 'stopped',
            }
            if conv._conversation.type == hangouts_pb2.CONVERSATION_TYPE_ONE_TO_ONE:
                if not user.is_self:
                    self.send_message_to_xmpp({'what': 'typing_notification',
                                               'type': 'one_to_one',
                                               'gaia_id': user.id_.gaia_id,
                                               'state': typing_states[typing_message.status]})
