# This file is part of fedora_messaging.
# Copyright (C) 2018 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

from __future__ import absolute_import, unicode_literals

import logging

import pika
from twisted.internet import defer, protocol, error
# twisted.logger is available with Twisted 15+
from twisted.python import log

from ..exceptions import PublishReturned, ConnectionException
from .protocol import FedoraMessagingProtocol


class FedoraMessagingFactory(protocol.ReconnectingClientFactory):
    """Reconnecting factory for the Fedora Messaging protocol."""

    name = 'FedoraMessaging:Factory'
    protocol = FedoraMessagingProtocol

    def __init__(self, parameters, bindings):
        """Initialize the protocol.

        Args:
            parameters (pika.ConnectionParameters): The connection parameters.
            bindings (dict): which bindings to setup on connect.
        """
        self.bindings = bindings
        self._parameters = parameters
        self._message_callback = None
        self.client = None
        self._client_ready = defer.Deferred()

    def startedConnecting(self, connector):
        log.msg('Connecting to the Fedora Messaging broker',
                system=self.name, logLevel=logging.DEBUG)

    def buildProtocol(self, addr):
        self.resetDelay()
        log.msg('Connected to the Fedora Messaging broker', system=self.name)
        self.client = self.protocol(self._parameters)
        self.client.factory = self
        self.client.ready.addCallback(
            lambda _: self._on_client_ready()
        )
        return self.client

    @defer.inlineCallbacks
    def _on_client_ready(self):
        # Setup read (on connect and reconnect).
        if self._message_callback is not None:
            yield self.client.setupRead(self._message_callback)
            yield self.client.resumeProducing()
        # Run ready callbacks.
        self._client_ready.callback(None)

    def clientConnectionLost(self, connector, reason):
        if not isinstance(reason.value, error.ConnectionDone):
            log.msg('Lost connection. Reason: {}'.format(reason.value),
                    system=self.name, logLevel=logging.WARNING)
        if self._client_ready.called:
            # Renew the ready deferred, it will callback when the
            # next connection is ready.
            self._client_ready = defer.Deferred()
        protocol.ReconnectingClientFactory.clientConnectionLost(
            self, connector, reason)

    def clientConnectionFailed(self, connector, reason):
        log.msg('Connection failed. Reason: {}'.format(reason.value),
                system=self.name, logLevel=logging.WARNING)
        protocol.ReconnectingClientFactory.clientConnectionFailed(
            self, connector, reason)

    def stopTrying(self):
        protocol.ReconnectingClientFactory.stopTrying(self)
        if not self._client_ready.called:
            self._client_ready.errback(pika.exceptions.AMQPConnectionError(
                "Could not connect, reconnection cancelled."
            ))

    @defer.inlineCallbacks
    def stopFactory(self):
        if self.client:
            yield self.client.stopProducing()
        protocol.ReconnectingClientFactory.stopFactory(self)

    @defer.inlineCallbacks
    def consume(self, message_callback):
        """Pass incoming messages to the provided callback.

        Args:
            message_callback (callable): The callable to pass the message to
                when one arrives.
        """
        log.msg('Messages reading setup',
                system=self.name, logLevel=logging.DEBUG)
        new_setup = self._message_callback is None
        self._message_callback = message_callback
        if self._client_ready.called and new_setup:
            # If consume() is called after the client is ready (and we did
            # not setup before), do it now.
            yield self.client.setupRead(self._message_callback)
            yield self.client.resumeProducing()

    @defer.inlineCallbacks
    def publish(self, message, exchange=None):
        """
        Publish a :class:`fedora_messaging.message.Message` to an `exchange`_
        on the message broker.

        Args:
            message (message.Message): The message to publish.
            exchange (str): The name of the AMQP exchange to publish to; defaults
to :ref:`conf-publish-exchange`

        Raises:
            PublishReturned: If the published message is rejected by the broker.
            ConnectionException: If a connection error occurs while publishing.

        .. _exchange: https://www.rabbitmq.com/tutorials/amqp-concepts.html#exchanges
        """
        yield self._client_ready
        try:
            yield self.client.publish(message, exchange)
        except (
                pika.exceptions.ConnectionClosed, pika.exceptions.ChannelClosed
                ) as e:
            log.msg('Connection lost while publishing, retrying.',
                    system=self.name, logLevel=logging.WARNING)
            yield self.publish(message, exchange)
        except (
                pika.exceptions.NackError, pika.exceptions.UnroutableError
                ) as e:
            log.msg('Message was rejected by the broker ({})'.format(e),
                    system=self.name, logLevel=logging.WARNING)
            raise PublishReturned(reason=e)
        except pika.exceptions.AMQPError as e:
            self.stopTrying()
            yield self.client.close()
            raise ConnectionException(reason=e)
